# -*- coding: utf-8 -*-
"""恢复第二步：把被误标 missing 的 E 盘行放回 pending，再由 fix-status 还原真实状态。

为什么必须分两步：
  `12_maintain.py fix-status` 有一条铁律——**永不把 missing 改成别的状态**
  （因为 missing 可能是真的文件不在了，误改会让它被反复重试）。
  所以误标的行必须先由本脚本显式解除 missing，再让 fix-status 按 file_encoding
  把它们还原成 done / skipped_dup。

安全前提：事故前 missing 恰好只有 F 盘的 7,987 条，E 盘一条都没有。
本脚本只处理 `path LIKE 'E:\\%' AND status='missing'` 的行。

用法：
    .venv\\Scripts\\python.exe tmp\\restore_status.py --apply
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
    con = sqlite3.connect(str(cfg.db_path), timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    n = con.execute(
        "SELECT COUNT(*) FROM files WHERE path LIKE 'E:\\%' AND status='missing'"
    ).fetchone()[0]
    print(f"E 盘被误标 missing 的行：{n:,}")
    if not apply:
        print("（只读报告；加 --apply 执行）")
        con.close()
        return 0
    cur = con.execute(
        "UPDATE files SET status='pending', updated_at=? "
        "WHERE path LIKE 'E:\\%' AND status='missing'", (local_now_iso(),))
    con.commit()
    print(f"已改回 pending：{cur.rowcount:,}")
    con.close()
    print("下一步：12_maintain.py fix-status")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
