"""诊断：分块法与暴力法的差异，是否**仅仅**来自「多个候选距离并列」时的取舍。

若是，则：
  * 两者检出的重复总数相同
  * 差异条目在两边的最小距离相同（只是挑了不同的代表）
  * 逐条 d=0（完全相同）的情况两边必然一致
这些都不影响去重效果（都会把重复项标掉，只是「保留谁」不同）。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.dedup_core import find_duplicates, find_duplicates_naive, hex_to_int, hamming  # noqa: E402


def make_corpus(n_pairs: int, n_rand: int, seed: int) -> list[tuple[int, str]]:
    rnd = random.Random(seed)
    items: list[tuple[int, str]] = []
    fid = 1
    for _ in range(n_pairs):
        b = rnd.getrandbits(64)
        items.append((fid, f"{b:016x}")); fid += 1
        for _k in range(2):
            flip = 0
            for pos in rnd.sample(range(64), rnd.randint(1, 8)):
                flip |= 1 << pos
            items.append((fid, f"{b ^ flip:016x}")); fid += 1
        flip = 0
        for pos in rnd.sample(range(8), 8):
            flip |= 1 << pos
        items.append((fid, f"{b ^ flip:016x}")); fid += 1
    for _ in range(n_rand):
        items.append((fid, f"{rnd.getrandbits(64):016x}")); fid += 1
    rnd.shuffle(items)
    return items


def main() -> int:
    for seed in (1, 2, 3):
        items = make_corpus(250, 1500, seed)
        pn = find_duplicates_naive(items)
        pf, _u, _i = find_duplicates(items)
        dn = {p.dup_hash: p for p in pn}
        df = {p.dup_hash: p for p in pf}

        only_n = set(dn) - set(df)
        only_f = set(df) - set(dn)
        common = set(dn) & set(df)

        # 1) 总数是否相同
        print(f"\n=== seed={seed} ===")
        print(f"  重复总数：暴力 {len(pn)}  分块 {len(pf)}  {'相同' if len(pn)==len(pf) else '不同 ✗'}")
        print(f"  仅暴力 {len(only_n)}  仅分块 {len(only_f)}  共同 {len(common)}")

        # 2) 共同条目里，距离是否一致
        dist_mismatch = [h for h in common if dn[h].distance != df[h].distance]
        print(f"  共同条目的距离不一致：{len(dist_mismatch)}")

        # 3) 对差异条目，核对「最小距离」是否相同
        bad = 0
        for h in list(only_n)[:5] + list(only_f)[:5]:
            v = hex_to_int(h)
            # 在暴力结果里该 hash 若存在，取其距离
            d_naive = dn[h].distance if h in dn else None
            d_fast = df[h].distance if h in df else None
            if d_naive is not None and d_fast is not None and d_naive != d_fast:
                bad += 1
                print(f"    距离不一致 {h}: 暴力 {d_naive} vs 分块 {d_fast}")
        if not bad:
            print("    差异条目的最小距离均一致（仅代表不同）")

        # 4) d=0（完全相同）是否完全一致
        zero_n = {p.dup_hash for p in pn if p.distance == 0}
        zero_f = {p.dup_hash for p in pf if p.distance == 0}
        print(f"  d=0（完全相同）条目：暴力 {len(zero_n)}  分块 {len(zero_f)}  "
              f"{'一致' if zero_n == zero_f else '不一致 ✗'}")

        # 5) 关键结论：被标记为重复的 dup_hash 集合是否相同
        print(f"  被标为重复的 dup_file_id 集合："
              f"{'一致 ✓' if {p.dup_file_id for p in pn} == {p.dup_file_id for p in pf} else '不一致 ✗'}"
              f"（两法数量 {len({p.dup_file_id for p in pn})} vs {len({p.dup_file_id for p in pf})}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
