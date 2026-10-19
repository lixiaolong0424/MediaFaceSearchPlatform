"""修正版设计验证：用 C = radius + 1 块（鸽巢保证零漏检）。

教训：先前用 4 块 × 16 位是错的。
  4 块时，8 个差异位可以均匀落在 4 块里（每块 2 位）→ 没有任何一块全同
  → 该对无法通过「共享某一块」找到 → 漏检（实测漏 137/789）。
正确：块数 C 必须 > radius（这里 radius=8 → C=9）。
  8 个差异位最多污染 8 块，9 块中至少 1 块完全不受影响 → 必然共享 → 零漏检。

代价：块变小 → 桶变大 → 候选变多。本脚本实测这个代价有多大。
"""

from __future__ import annotations

import random
import time
from collections import defaultdict
from typing import Dict, List, Set, Tuple

RADIUS = 8


def chunk_specs(total_bits: int, c: int) -> List[Tuple[int, int]]:
    """把 total_bits 尽量均匀切成 c 块，返回 [(shift, width), ...]。"""
    base, rem = divmod(total_bits, c)
    out = []
    shift = 0
    for i in range(c):
        w = base + (1 if i < rem else 0)
        out.append((shift, w))
        shift += w
    return out


def chunks_of(h: int, specs: List[Tuple[int, int]]) -> List[int]:
    return [(h >> s) & ((1 << w) - 1) for s, w in specs]


def hamming_int(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def brute(hashes: List[int]) -> Dict[int, int]:
    kept: List[int] = []
    result: Dict[int, int] = {}
    for h in hashes:
        found = None
        for k in kept:
            if hamming_int(h, k) <= RADIUS:
                found = k
                break
        if found is None:
            kept.append(h)
        else:
            result[h] = found
    return result


def bucketed(hashes: List[int], c: int) -> Tuple[Dict[int, int], int]:
    specs = chunk_specs(64, c)
    index: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    result: Dict[int, int] = {}
    comps = 0
    for h in hashes:
        cs = chunks_of(h, specs)
        cands: Set[int] = set()
        for ci, cv in enumerate(cs):
            b = index.get((ci, cv))
            if b:
                cands.update(b)
        found = None
        for k in cands:
            comps += 1
            if hamming_int(h, k) <= RADIUS:
                found = k
                break
        if found is None:
            for ci, cv in enumerate(cs):
                index[(ci, cv)].append(h)
        else:
            result[h] = found
    return result, comps


def make_corpus(n_pairs: int, n_rand: int, seed: int = 1) -> List[int]:
    rnd = random.Random(seed)
    hashes: List[int] = []
    for _ in range(n_pairs):
        b = rnd.getrandbits(64)
        hashes.append(b)
        for _ in range(2):
            flip = 0
            for pos in rnd.sample(range(64), rnd.randint(1, RADIUS)):
                flip |= 1 << pos
            hashes.append(b ^ flip)
        # 极端：8 个差异位挤在最小的块里（考验块粒度）
        specs = chunk_specs(64, 9)
        s, w = min(specs, key=lambda t: t[1])
        flip = 0
        for pos in rnd.sample(range(min(w, RADIUS)), min(w, RADIUS)):
            flip |= 1 << (s + pos)
        if flip:
            hashes.append(b ^ flip)
    hashes.extend(rnd.getrandbits(64) for _ in range(n_rand))
    rnd.shuffle(hashes)
    return hashes


def main() -> int:
    print("=== A) 正确性 + 速度：不同块数对比（n≈4600，含极端用例）===")
    hashes = make_corpus(400, 3000, seed=1)
    t0 = time.monotonic()
    r_brute = brute(hashes)
    t_brute = time.monotonic() - t0
    print(f"  暴力基准: 检出 {len(r_brute):>4}  耗时 {t_brute:6.2f}s   n={len(hashes)}")
    print(f"  {'块数C':>5} {'检出':>6} {'漏检':>6} {'多检':>6} {'比较次数':>13} {'耗时':>8} {'加速':>7}")
    for c in (4, 9, 12):
        t0 = time.monotonic()
        r, comps = bucketed(hashes, c)
        dt = time.monotonic() - t0
        miss = len(set(r_brute) - set(r))
        extra = len(set(r) - set(r_brute))
        flag = "✓ 零漏检" if miss == 0 else f"✗ 漏 {miss}"
        print(f"  {c:>5} {len(r):>6} {miss:>6} {extra:>6} {comps:>13,} "
              f"{dt:>7.2f}s {t_brute / max(dt, 1e-6):>6.1f}x  {flag}")

    print("\n=== B) C=9 的规模外推（真实随机哈希为主）===")
    for n in (20_000, 60_000):
        rnd = random.Random(7)
        hs = [rnd.getrandbits(64) for _ in range(n)]
        t0 = time.monotonic()
        r, comps = bucketed(hs, 9)
        dt = time.monotonic() - t0
        rate = n / dt
        print(f"  n={n:>7,}  检出 {len(r):>3}  比较 {comps:>12,}  耗时 {dt:6.2f}s  "
              f"{rate:>9,.0f} 张/秒  外推 41.3 万 ≈ {413000 / rate / 60:>5.1f} 分钟")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
