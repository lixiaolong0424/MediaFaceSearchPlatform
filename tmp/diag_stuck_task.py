# -*- coding: utf-8 -*-
"""诊断「抽取画面 · 已处理 0 / 11,969」：是显示卡住，还是真的没在跑。

判断依据（三条独立证据）：
  1. tasks 表：状态 / done / total / updated_at（心跳是否在动）
  2. 进程：run_task.py 与 03_extract.py 还在不在
  3. 数据：pending 数量有没有在减少（真跑了就一定在动）
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.common import load_config  # noqa: E402


def main() -> int:
    cfg = load_config()
    print(f"时间：{datetime.now():%H:%M:%S}   库：{cfg.db_path}")
    con = sqlite3.connect(str(cfg.db_path), timeout=10)
    con.row_factory = sqlite3.Row

    print("\n== 1) 最近 5 个任务 ==")
    now = datetime.now()
    for r in con.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 5"):
        upd = str(r["updated_at"] or "")
        age = ""
        try:
            age = f"{(now - datetime.fromisoformat(upd)).total_seconds():.0f}s 前"
        except ValueError:
            pass
        print(f"  {r['task_id']}  {r['title']}  state={r['state']}")
        print(f"     stage={r['stage']}  {r['done']}/{r['total']}  "
              f"step {r['stage_ix']}/{r['stage_total']}  pid={r['pid']}")
        print(f"     message={r['message']!r}  error={(r['error'] or '')[:160]!r}")
        print(f"     心跳 updated_at={upd}（{age}）cancel={r['cancel']}")

    print("\n== 2) 相关进程 ==")
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get",
             "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, timeout=20).stdout
        lines = [ln for ln in out.splitlines()
                 if any(k in ln for k in ("run_task", "03_extract", "04_encode",
                                          "06_face", "07_face", "01_manifest", "streamlit"))]
        for ln in lines:
            print("  ", ln.strip()[:160])
        if not lines:
            print("   （没有任何流水线/界面进程）")
    except Exception as exc:  # noqa: BLE001
        print("   查询失败：", exc)

    print("\n== 3) 数据是否在动 ==")
    by = dict(con.execute("SELECT status, COUNT(*) FROM files GROUP BY status"))
    print("   files:", by)
    print("   stage_status(extract):",
          dict(con.execute("SELECT status, COUNT(*) FROM stage_status "
                           "WHERE stage='extract' GROUP BY status")))
    print("   死信:", dict(con.execute(
        "SELECT stage, COUNT(*) FROM dead_letter GROUP BY stage")))
    print("   最近 3 条 stage 更新时间:",
          [r[0] for r in con.execute(
              "SELECT MAX(updated_at) FROM stage_status GROUP BY stage ORDER BY 1 DESC LIMIT 3")])

    print("\n== 4) 抽样：现在还剩多少待抽帧 ==")
    q = ("SELECT COUNT(*) FROM files f LEFT JOIN stage_status ss "
         "ON ss.file_id=f.file_id AND ss.stage='extract' "
         "WHERE f.kind IN ('photo','video') AND f.status IN ('pending','error') "
         "AND (ss.status IS NULL OR ss.status IN ('pending','error'))")
    print("   待抽帧（不含死信）:", con.execute(q).fetchone()[0])
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
