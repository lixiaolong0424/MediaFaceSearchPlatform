# -*- coding: utf-8 -*-
"""回答「为什么预览说 503,064 个文件找不到（100%）」。

对比两件事：
  1) 库里记录的 rel_path 长什么样（它们是相对 E:\\ 的路径）
  2) 用户选的「新位置」里实际有什么（顶层目录 + 抽样）
只读。
"""
from __future__ import annotations

import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.common import load_config  # noqa: E402

NEW_ROOT = Path(r"F:\LARGE\2_LIFE\20250829手机和平板照片备份\20250828照片")


def main() -> int:
    cfg = load_config()
    con = sqlite3.connect(str(cfg.db_path))
    con.row_factory = sqlite3.Row

    print("=" * 74)
    print("① 库里 E 盘记录的顶层结构（rel_path 的第一段）")
    print("=" * 74)
    cnt: Counter[str] = Counter()
    for r in con.execute("SELECT rel_path FROM files WHERE path LIKE 'E:\\%'"):
        seg = str(r["rel_path"] or "").replace("/", "\\").lstrip("\\").split("\\", 1)[0]
        if seg:
            cnt[seg] += 1
    for seg, n in cnt.most_common(12):
        print(f"  {n:>8,}  {seg}")
    print(f"  合计 {sum(cnt.values()):,} 条")

    print("\n" + "=" * 74)
    print("② 抽 5 条：库里的 rel_path vs 新位置里应该出现的地方")
    print("=" * 74)
    for r in con.execute("SELECT rel_path FROM files WHERE path LIKE 'E:\\%' LIMIT 5"):
        rel = str(r["rel_path"])
        cand = NEW_ROOT / rel
        print(f"  rel_path = {rel!r}")
        print(f"    期望存在于 {cand}  →  {'在' if cand.exists() else '**不在**'}")

    print("\n" + "=" * 74)
    print(f"③ 新位置实际有什么：{NEW_ROOT}")
    print("=" * 74)
    if not NEW_ROOT.exists():
        print("  这个目录不存在。")
        return 0
    tops = []
    try:
        with os.scandir(NEW_ROOT) as it:
            for e in it:
                if e.is_dir():
                    tops.append(e.name)
    except OSError as exc:
        print(f"  读不了：{exc}")
        return 0
    for name in sorted(tops)[:12]:
        print(f"  {name}")
    print(f"  顶层目录共 {len(tops)} 个")

    n_files = 0
    for _dp, _dn, fns in os.walk(NEW_ROOT):
        n_files += len(fns)
    print(f"  递归文件数（全部类型）：{n_files:,}")

    # 库里有多少记录真正落在新根下
    pref = str(NEW_ROOT).rstrip("\\") + "\\"
    n_db = con.execute("SELECT COUNT(*) FROM files WHERE path LIKE ?", (pref + "%",)).fetchone()[0]
    print(f"  库里在这个目录下的记录：{n_db:,}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
