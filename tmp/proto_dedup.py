"""设计验证：分块索引的正确性与速度。

要替换的现有实现（02_dedup.py）：

    by_hash: Dict[str, int] = {}
    for r in rows:                       # 413k 张照片
        h = compute_phash(p)
        for oh, ofid in by_hash.items(): # ← 与「之前所有哈希」逐一比较
            if hamming(h, oh) <= 8: ...
        by_hash[h] = fid

问题：唯一哈希占多数时没有 break，比较次数 ≈ n²/2 = 852 亿次，实测
2.14e6 次/秒 → 约 11 小时。

拟采用的替代方案 —— 鸽巢原理分块索引：
    两个 64 位哈希汉明距离 <= 8 ⟹ 把 64 位切成 4 块（每块 16 位）后，
    至少有 1 块完全相同（鸽巢：8 个差异位最多污染 8 块，只有 4 块时有剩余）。
    ⟹ 建立 4 张索引 [块号][16 位值] -> [hash,...]，查询时只看这 4 个桶。

本脚本用暴力实现做基准，验证分块索引既能给出完全相同的结果，又快得多。
"""

from __future__ import annotations

import random
import time
from collections import defaultdict
from typing import Dict, List, Tuple

RADIUS = 8
CHUNKS = 4
BITS_PER_CHUNK = 16
MASK = (1 << BITS_PER_CHUNK) - 1


def chunks_of(h: int) -> List[int]:
    return [(h >> (i * BITS_PER_CHUNK)) & MASK for i in range(CHUNKS)]


def hamming_int(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def brute(hashes: List[int]) -> Dict[int, int]:
    """返回 {dup_hash: kept_hash}（第一次出现的保留）。"""
    kept: Dict[int, int] = {}
    result: Dict[int, int] = {}
    for h in hashes:
        found = None
        for k in kept:
            if hamming_int(h, k) <= RADIUS:
                found = k
                break
        if found is None:
            kept[h] = h
        else:
            result[h] = found
    return result


def bucketed(hashes: List[int]) -> Dict[int, int]:
    """分块索引：只比较与当前哈希共享任一 16 位块的候选。"""
    index: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    kept: set[int] = set()
    result: Dict[int, int] = {}
    for h in hashes:
        cs = chunks_of(h)
        # 收集候选（共享任一块）
        cands: set[int] = set()
        for ci, cv in enumerate(cs):
            bucket = index.get((ci, cv))
            if bucket:
                cands.update(bucket)
        found = None
        for k in cands:
            if hamming_int(h, k) <= RADIUS:
                found = k
                break
        if found is None:
            kept.add(h)
            for ci, cv in enumerate(cs):
                index[(ci, cv)].append(h)
        else:
            result[h] = found
    return result


def make_corpus(n_random: int, n_pairs: int, seed: int = 0) -> Tuple[List[int], List[Tuple[int, int]]]:
    """构造：随机哈希 + 若干「已知近重复对」（含跨块的极端情况）。"""
    rnd = random.Random(seed)
    hashes: List[int] = []
    truth: List[Tuple[int, int]] = []
    bases = [rnd.getrandbits(64) for _ in range(n_pairs)]
    for b in bases:
        hashes.append(b)
        # 变体 1：随机翻 1~8 位
        for _ in range(2):
            flip = 0
            for pos in rnd.sample(range(64), rnd.randint(1, RADIUS)):
                flip |= 1 << pos
            v = b ^ flip
            hashes.append(v)
            truth.append((v, b))
        # 变体 2：只在同一个 16 位块内翻满 8 位（最坏情况，考验分块）
        flip = 0
        for pos in rnd.sample(range(BITS_PER_CHUNK), RADIUS):
            flip |= 1 << pos
        v = b ^ flip
        hashes.append(v)
        truth.append((v, b))
    hashes.extend(rnd.getrandbits(64) for _ in range(n_random))
    rnd.shuffle(hashes)
    return hashes, truth


def main() -> int:
    print("=== A) 正确性：分块索引 vs 暴力，结果必须一致 ===")
    for n_pairs, n_rand in ((200, 800), (400, 3000)):
        hashes, truth = make_corpus(n_rand, n_pairs, seed=1)
        t0 = time.monotonic()
        r_brute = brute(hashes)
        t_brute = time.monotonic() - t0
        t0 = time.monotonic()
        r_bucket = bucketed(hashes)
        t_bucket = time.monotonic() - t0
        same = r_brute == r_bucket
        print(f"  n={len(hashes):>5}  暴力检出 {len(r_brute):>4}  分块检出 {len(r_bucket):>4}  "
              f"一致={'是' if same else '否 ✗'}   暴力 {t_brute:6.2f}s  分块 {t_bucket:6.2f}s"
              f"  {t_brute / max(t_bucket, 1e-6):>6.1f}x")
        if not same:
            only_brute = set(r_brute) - set(r_bucket)
            only_bucket = set(r_bucket) - set(r_brute)
            print(f"    仅暴力检出 {len(only_brute)} 个；仅分块检出 {len(only_bucket)} 个")
            for h in list(only_brute)[:3]:
                print(f"      漏检 {h:016x} -> 暴力认为与 {r_brute[h]:016x} 重复，"
                      f"实际距离 {hamming_int(h, r_brute[h])}")

    print("\n=== B) 纯随机哈希（真实场景：近重复占比低）的规模外推 ===")
    for n in (20_000, 50_000):
        rnd = random.Random(7)
        hashes = [rnd.getrandbits(64) for _ in range(n)]
        t0 = time.monotonic()
        r = bucketed(hashes)
        dt = time.monotonic() - t0
        rate = n / dt
        print(f"  n={n:>7,}  检出 {len(r):>3}  耗时 {dt:6.2f}s  "
              f"→ {rate:>9,.0f} 张/秒   外推 41.3 万张 ≈ {413000 / rate / 60:>5.1f} 分钟")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
