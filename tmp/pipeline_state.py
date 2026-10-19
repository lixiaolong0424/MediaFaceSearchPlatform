"""查看当前索引流水线的真实状态（只读）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"


def show(label: str, sql: str) -> None:
    con = sqlite3.connect(str(DB))
    try:
        rows = con.execute(sql).fetchall()
        print(f"  {label:<14} {rows if rows else '(空)'}")
    except sqlite3.OperationalError as exc:
        print(f"  {label:<14} (表/列不存在：{exc})")
    finally:
        con.close()


def main() -> int:
    print("=== files ===")
    show("by_status", "SELECT status, COUNT(*) FROM files GROUP BY status ORDER BY 2 DESC")
    show("by_kind", "SELECT kind, COUNT(*) FROM files GROUP BY kind")
    show("有 phash", "SELECT COUNT(*) FROM files WHERE phash IS NOT NULL")
    show("time_source", "SELECT time_source, COUNT(*) FROM files GROUP BY time_source")
    print("=== 下游产物 ===")
    show("shots", "SELECT COUNT(*) FROM shots")
    show("faces", "SELECT COUNT(*) FROM face_records")
    print("=== 阶段状态机 ===")
    show("file_encoding", "SELECT model || ':' || status, COUNT(*) FROM file_encoding GROUP BY 1")
    show("stage_status", "SELECT stage || ':' || status, COUNT(*) FROM stage_status GROUP BY 1")
    print("=== libraries ===")
    show("roots", "SELECT root, files_hint FROM (SELECT root, CAST(0 AS INT) files_hint, last_scan FROM libraries)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
