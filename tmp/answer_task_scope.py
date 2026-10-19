# -*- coding: utf-8 -*-
"""回答：当前扫描任务做不做视频？做不做编码/检脸/入库？"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.common import load_config  # noqa: E402


def main() -> int:
    cfg = load_config()
    con = sqlite3.connect(str(cfg.db_path))
    con.row_factory = sqlite3.Row

    t = con.execute("SELECT * FROM tasks WHERE state IN ('queued','running') "
                    "ORDER BY created_at DESC LIMIT 1").fetchone()
    if t is None:
        t = con.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT 1").fetchone()
        print("（当前没有运行中的任务，下面的计划是最近一次的）")
    plan = json.loads(t["plan"] or "{}")
    print(f"任务 {t['task_id']}  {t['title']}  state={t['state']}")
    print(f"  阶段={t['stage']}  进度={t['done']}/{t['total']}  "
          f"第 {t['stage_ix']}/{t['stage_total']} 步")
    print("  计划步骤：")
    for s in plan.get("steps", []):
        print(f"    {s['script']:<20} {s.get('args')}")
    print("  参数：", {k: v for k, v in plan.items() if k != "steps"})

    print("\n待处理（status pending/error）：")
    for r in con.execute("SELECT kind, COUNT(*) n FROM files "
                         "WHERE status IN ('pending','error') GROUP BY kind"):
        print(f"  {r['kind']}: {r['n']:,}")
    print("\n已完成（status done）：")
    for r in con.execute("SELECT kind, COUNT(*) n FROM files WHERE status='done' "
                         "GROUP BY kind"):
        print(f"  {r['kind']}: {r['n']:,}")
    print("\n各阶段已完成：")
    for r in con.execute("SELECT stage, status, COUNT(*) n FROM stage_status "
                         "GROUP BY stage, status ORDER BY stage"):
        print(f"  {r['stage']:<16} {r['status']:<8} {r['n']:,}")
    print("\nfile_encoding：")
    for r in con.execute("SELECT model, status, COUNT(*) n FROM file_encoding "
                         "GROUP BY model, status ORDER BY model, status"):
        print(f"  {r['model']:<10} {r['status']:<12} {r['n']:,}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
