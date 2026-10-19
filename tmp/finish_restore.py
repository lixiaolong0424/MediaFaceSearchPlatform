# -*- coding: utf-8 -*-
"""收尾：核对库状态 + 清掉残留的 F 根登记 + 清掉卡住的 running 任务。

用法：
    .venv\\Scripts\\python.exe tmp\finish_restore.py [--apply]
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.common import load_config, local_now_iso  # noqa: E402


def main() -> int:
    apply = "--apply" in sys.argv
    cfg = load_config()
    print(f"config media_root = {cfg.media_root}")
    con = sqlite3.connect(str(cfg.db_path), timeout=60)
    con.execute("PRAGMA busy_timeout=60000")

    by_status = dict(con.execute(
        "SELECT status, COUNT(*) FROM files GROUP BY status").fetchall())
    print("files by_status:", by_status)
    libs = [r for r in con.execute("SELECT root FROM libraries")]
    print("libraries:", [r[0] for r in libs])
    run = con.execute(
        "SELECT task_id, title FROM tasks WHERE state IN ('queued','running')").fetchall()
    print("未结束的任务:", run)

    n_f = con.execute("SELECT COUNT(*) FROM files WHERE path LIKE 'F:\\%'").fetchone()[0]
    n_e = con.execute("SELECT COUNT(*) FROM files WHERE path LIKE 'E:\\%'").fetchone()[0]
    print(f"路径前缀：E 盘 {n_e:,} 条，F 盘 {n_f:,} 条（F 盘是真·F 盘文件）")

    if not apply:
        print("\n（只读报告；加 --apply 执行清理）")
        con.close()
        return 0

    now = local_now_iso()
    d1 = con.execute("DELETE FROM libraries WHERE root LIKE 'F:\\LARGE\\%'").rowcount
    if not con.execute("SELECT 1 FROM libraries WHERE root='E:\\'").fetchone():
        con.execute("INSERT INTO libraries(root, last_scan, status) VALUES ('E:\\',?,'idle')",
                    (now,))
    d2 = con.execute(
        "UPDATE tasks SET state='failed', finished_at=?, updated_at=?, "
        "message='进程已退出（未正常结束）' WHERE state IN ('queued','running')",
        (now, now)).rowcount
    con.commit()
    print(f"已删除 F 根登记 {d1} 条，已把 {d2} 个卡住的任务标为失败")
    print("libraries:", [r[0] for r in con.execute("SELECT root FROM libraries")])
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
