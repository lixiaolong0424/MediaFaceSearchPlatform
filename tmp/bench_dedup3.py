"""修正版：正确生成 top-20 位内 hamming<=8 的全部掩码，并验证分桶法的正确性+加速比。

掩码生成数学：
  要求 (v>>44) 与 (c>>44) 的汉明距离 <= 8，即「高 20 位中恰好有 k<=8 位不同」。
  因此掩码 = 在 20 个位位置里挑 k 个置 1，k = 0..8。
  组合数 = sum(C(20,k), k=0..8) = 263,950。
"""

from __future__ import annotations

import itertools
import random
import time
from typing import Dict, List

BITS = 20
RADIUS = 8


def build_masks() -> List[int]:
    masks: List[int] = []
    for k in range(RADIUS + 1):
        for combo in itertools.combinations(range(BITS), k):
            m = 0
            for b in combo:
                m |= 1 << b
            masks.append(m)
    return masks


MASKS = build_masks()


def fake_hex(n: int, seed: int = 0) -> List[str]:
    rnd = random.Random(seed)
    return [f"{rnd.getrandbits(64):016x}" for _ in range(n)]


def hamming_int(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def bucketed(vals: List[int]) -> tuple[int, List[tuple[int, int]]]:
    buckets: Dict[int, List[int]] = {}
    comps = 0
    pairs: List[tuple[int, int]] = []
    for v in vals:
        top = v >> 44
        cands: List[int] = []
        for m in MASKS:
            b = buckets.get(top ^ m)
            if b:
                cands.extend(b)
        for c in cands:
            comps += 1
            if hamming_int(v, c) <= RADIUS:
                pairs.append((v, c))
                break
        buckets.setdefault(top, []).append(v)
    return comps, pairs


def naive(vals: List[int]) -> tuple[int, List[tuple[int, int]]]:
    comps = 0
    pairs: List[tuple[int, int]] = []
    for i in range(1, len(vals)):
        for j in range(i):
            comps += 1
            if hamming_int(vals[i], vals[j]) <= RADIUS:
                pairs.append((vals[i], vals[j]))
                break
    return comps, pairs


def main() -> int:
    print(f"掩码数（top-{BITS} 位内 hamming<={RADIUS}）= {len(MASKS):,}"
          f"  （期望 263,950）")
    assert len(MASKS) == 263950, "掩码生成不正确"
    print()

    # --- 正确性：构造确定的近重复对 + 随机噪声 ---
    rnd = random.Random(7)
    base = rnd.getrandbits(64)
    vals: List[int] = [base]
    for _ in range(5):  # 与 base 汉明距离 <=8 的真近重复
        flip = 0
        for b in rnd.sample(range(64), 8):
            flip |= 1 << b
        vals.append(base ^ flip)
    vals.extend(rnd.getrandbits(64) for _ in range(2000))  # 随机噪声
    # 再加一组：只在高 20 位内相差 8 位（最坏情况，强制跨桶）
    top_diff = 0
    for b in rnd.sample(range(20), 8):
        top_diff |= 1 << b
    near_top = base ^ (top_diff << 44)
    vals.append(near_top)

    bn, bp = bucketed(vals)
    print("=== 正确性检查 ===")
    print(f"  分桶法检出近重复对：{len(bp)}  （期望 >= 7：6 个基准重复 + 1 个高 20 位极端）")
    top_hit = any((a ^ b) >> 44 == top_diff for a, b in bp)
    print(f"  高 20 位相差 8 位的极端用例被检出：{top_hit}")
    print()

    print("=== 加速比与吞吐 ===")
    for n in (5_000, 20_000):
        vals_n = [int(h, 16) for h in fake_hex(n, 3)]
        t0 = time.monotonic()
        c1, _ = bucketed(vals_n)
        d1 = time.monotonic() - t0
        naive_total = n * (n - 1) // 2
        print(f"  n={n:>6,}  分桶比较={c1:>12,}  朴素={naive_total:>14,}  "
              f"减少 {naive_total / max(1, c1):>6.1f}x  耗时 {d1:>6.2f}s  "
              f"→ {n / d1:>8,.0f} 张/秒")
        rate = n / d1
        print(f"            外推 41.3 万张 ≈ {413_000 / rate / 60:>5.1f} 分钟")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
