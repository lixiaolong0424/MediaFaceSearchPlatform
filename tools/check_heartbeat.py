# -*- coding: utf-8 -*-
"""诊断「heartbeat timeout」失败 + 并发跑起来的多份流水线。

只读，不改任何东西。
"""
from __future__ import annotations

import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import db as D  # noqa: E402
from src.common import load_config  # noqa: E402


def live_pids() -> dict[int, str]:
    """当前所有 python 进程 → 命令行（用于判断任务进程是否真的还在）。"""
    out: dict[int, str] = {}
    try:
        ps = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get",
             "ProcessId,CommandLine", "/format:csv"],
            capture_output=True, text=True, errors="replace", timeout=30).stdout
    except Exception as exc:  # noqa: BLE001
        print("  查询进程失败：", exc)
        return out
    for line in ps.splitlines()[1:]:
        parts = [x.strip() for x in line.split(",") if x.strip()]
        if len(parts) < 2:
            continue
        pid = parts[-1]
        if not pid.isdigit():
            continue
        out[int(pid)] = ",".join(parts[:-1])
    return out


def main() -> int:
    cfg = load_config()
    print("=== 任务表（最近 8 条）===")
    con = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        rows = con.execute(
            "SELECT task_id,title,state,stage,done,total,created_at,updated_at,"
            "finished_at,pid,error FROM tasks ORDER BY created_at DESC LIMIT 8").fetchall()
    finally:
        con.close()
    now = datetime.now()
    for r in rows:
        age = ""
        try:
            age = f"{(now - datetime.fromisoformat(str(r['updated_at']))).total_seconds():.0f}s 前"
        except Exception:  # noqa: BLE001
            pass
        print(f"  {r['task_id']} {r['title']:<10} state={r['state']:<9} "
              f"{r['stage'] or '-':<8} {r['done']}/{r['total']}  pid={r['pid']}  "
              f"updated={r['updated_at']}（{age}）")
        if r["error"]:
            print(f"      error={r['error']}")

    print("\n=== 活着的 python 进程 ===")
    pids = live_pids()
    for pid, cl in pids.items():
        tag = ""
        for key in ("run_task.py", "01_manifest.py", "03_extract.py",
                    "04_encode.py", "06_face", "07_face", "streamlit"):
            if key in cl:
                tag = key
                break
        print(f"  pid={pid:<7} {tag:<16} {cl[:90]}")

    print("\n=== 交叉比对：任务 vs 进程 ===")
    running = [r for r in rows if r["state"] == "running"]
    print(f"  state=running 的任务数：{len(running)}")
    for r in running:
        pid = int(r["pid"] or 0)
        alive = pid in pids
        print(f"    {r['task_id']} pid={pid} 进程{'活着' if alive else '**已不在**'}")
    failed_recent = [r for r in rows if r["state"] == "failed"
                     and r["error"] == "heartbeat timeout"]
    for r in failed_recent:
        pid = int(r["pid"] or 0)
        alive = pid in pids
        print(f"    [被判超时] {r['task_id']} pid={pid} 进程"
              f"{'**仍然活着** ← 说明是误判！' if alive else '已退出（可能真死）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
