"""打印仍待处理的文件数（pending/error，photo+video）。

供监视脚本判定「03_extract 是否真的完成」——比「进程是否存活」更可靠：
python.exe 的 CommandLine 在 Windows 上有时是 null，靠进程名/命令行判断会误判。
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
            "SELECT COUNT(*) FROM files "
            "WHERE kind IN ('photo','video') AND status IN ('pending','error')"
        ).fetchone()[0]
        con.close()
        print(int(n))
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"-1", file=sys.stdout)
        print(f"# {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
