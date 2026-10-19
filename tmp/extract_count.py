"""给 PowerShell 监视脚本用的单值查询：打印 stage_status 里 extract 的行数。

单独成文件的原因：在 PowerShell 里内联 `python -c "...\\"SELECT...\\"..."` 需要
三层转义，实测会被 PowerShell 吃掉 `*` 等字符而失败。改成调用本脚本最稳。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "library.db"
    try:
        con = sqlite3.connect(str(db))
        n = con.execute(
            "SELECT COUNT(*) FROM stage_status WHERE stage = 'extract'").fetchone()[0]
        total = con.execute(
            "SELECT COUNT(*) FROM files WHERE kind IN ('photo','video')").fetchone()[0]
        con.close()
        print(f"{n} {total}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"-1 -1", file=sys.stdout)
        print(f"# {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
