# -*- coding: utf-8 -*-
"""查两件事：3 个 error 文件是谁；中间文件占用缓存的写入时间。"""
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

    print("== status='error' 的文件 ==")
    for r in con.execute("SELECT file_id, kind, path FROM files WHERE status='error'"):
        print(f"  {r['file_id']}  {r['kind']}  {r['path'][:110]}")

    print("\n== 这些文件的编码错误原因 ==")
    for r in con.execute(
            "SELECT fe.file_id, fe.model, fe.error FROM file_encoding fe "
            "JOIN files f ON f.file_id=fe.file_id WHERE f.status='error'"):
        print(f"  {r['file_id']} {r['model']}: {str(r['error'])[:160]}")

    print("\n== 死信（反复失败）==")
    for r in con.execute(
            "SELECT stage, COUNT(*) n FROM dead_letter GROUP BY stage"):
        print(f"  {r['stage']}: {r['n']}")

    print("\n== nvme 占用缓存 ==")
    row = con.execute("SELECT value, updated_at FROM app_settings WHERE key='nvme_usage'").fetchone()
    if row:
        print("  value:", json.loads(row["value"]), " updated_at:", row["updated_at"])
    else:
        print("  （还没有缓存值）")
    for key in ("nvme_pending_at",):
        row = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        print(f"  {key}:", row["value"] if row else None)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
