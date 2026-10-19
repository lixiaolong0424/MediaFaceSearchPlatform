# -*- coding: utf-8 -*-
"""诊断：路径变更向导把库改坏了之后，到底哪些 row 变成了什么样子。

只读，不改任何东西。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "library.db"
F_ROOT = "F:\\LARGE\\2_LIFE\\20250829手机和平板照片备份\\20250828照片"


def main() -> int:
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row

    print("== 总览 ==")
    for r in con.execute("SELECT status, COUNT(*) n FROM files GROUP BY status ORDER BY n DESC"):
        print(f"  {r['status']:<12} {r['n']:>8,}")

    print("\n== 路径前缀分布（前 6） ==")
    pref = Counter()
    for (p,) in con.execute("SELECT path FROM files"):
        pref[p[:40]] += 1
    for k, v in pref.most_common(6):
        print(f"  {v:>8,}  {k!r}")

    print("\n== rel_path 样例（10 条） ==")
    for r in con.execute("SELECT file_id, path, rel_path, status, kind FROM files LIMIT 10"):
        print(f"  {r['file_id']:>7} {r['status']:<10} {r['kind']:<6} "
              f"rel={r['rel_path']!r}  path={r['path']!r}")

    print("\n== 磁盘存在性抽样（每种状态 200 条） ==")
    for st in ("missing", "pending", "done"):
        rows = con.execute(
            "SELECT path FROM files WHERE status=? LIMIT 200", (st,)).fetchall()
        ok = sum(1 for r in rows if os.path.exists(r["path"]))
        print(f"  {st:<10} 抽样 {len(rows):>4} 条，磁盘上真实存在 {ok} 条")

    print("\n== 以 F 根开头的行 ==")
    n = con.execute("SELECT COUNT(*) FROM files WHERE path LIKE ?",
                    (F_ROOT + "%",)).fetchone()[0]
    print(f"  {n:,}")
    n2 = con.execute("SELECT COUNT(*) FROM files WHERE path LIKE ? AND status='pending'",
                     (F_ROOT + "%",)).fetchone()[0]
    print(f"  其中 pending：{n2:,}")
    for r in con.execute("SELECT path, rel_path FROM files WHERE path LIKE ? AND status='pending' LIMIT 5",
                         (F_ROOT + "%",)):
        print(f"    rel={r['rel_path']!r}  exists={os.path.exists(r['path'])}  path={r['path']!r}")

    print("\n== libraries ==")
    for r in con.execute("SELECT root, last_scan, status FROM libraries"):
        print(f"  {dict(r)}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
