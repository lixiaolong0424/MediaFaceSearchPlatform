"""修复版 pHash 去重内核：用「鸽巢分块索引」替代 O(n²) 全表比对。

背景（实测，2026-10-15）
------------------------
现有 02_dedup.py 的查找是：

    for oh, ofid in by_hash.items():        # 与「之前所有哈希」逐一比
        if hamming(h, oh) <= 8: ...

唯一哈希占多数时没有提前 break，比较次数 ≈ n²/2。
真实照片子集实测：n=4,000 → 686 万次比较 / 3.1s，更新率 2.2e6 次/秒。
外推 41.3 万张 → 852 亿次比较 → **10.8 小时**。

修复原理（鸽巢原理）
--------------------
把 64 位哈希切成 C 块。若两个哈希汉明距离 ≤ R，则差异位最多 R 个，
最多污染 R 个块；因此只要 **C > R**，就必然存在至少一个块**完全相同**。
    R = 8  ⟹  C ≥ 9 块（64/9 → 8 位块宽）
于是只需比较「与当前哈希共享任一块」的候选，而不是全部历史哈希。

块数必须 > R：实测 C=4 会漏检 120/827（8 个差异位可均匀分布在 4 块里），
C=9 与暴力法结果**完全一致**。

实测效果
--------
  n=4,600（含极端用例）：暴力 827 检出 / 2.61s；C=9 检出 827 / 0.20s（12.9x，零漏检）
  真实随机哈希 n=60,000：比较次数从 n²/2=1.8e9 降到 1.16e8（约 15x）
  外推 41.3 万张：比较环节 10.8 小时 → 约 1.4 小时

注意：本模块只加速「查找」。pHash 计算本身约 22 张/秒（纯 I/O 解码），
41.3 万张需约 5.1 小时，是另一项固定成本，不在本模块范围内。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

HASH_BITS = 64


# --------------------------------------------------------------------------- #
# 分块
# --------------------------------------------------------------------------- #


def chunk_specs(total_bits: int = HASH_BITS, chunks: int = 9) -> List[Tuple[int, int]]:
    """把 total_bits 尽量均匀切成 chunks 块，返回 [(shift, width), ...]。"""
    base, rem = divmod(total_bits, chunks)
    out: List[Tuple[int, int]] = []
    shift = 0
    for i in range(chunks):
        w = base + (1 if i < rem else 0)
        out.append((shift, w))
        shift += w
    return out


def chunks_for(hash_size: int, radius: int) -> List[Tuple[int, int]]:
    """按哈希位宽与汉明阈值决定块方案。

    块数必须严格大于 radius（鸽巢原理），否则会漏检。
    """
    bits = hash_size * hash_size  # hashlib 风格：hash_size=8 → 64 位
    return chunk_specs(bits, radius + 1)


def hex_to_int(h: str) -> Optional[int]:
    try:
        return int(h, 16)
    except (TypeError, ValueError):
        return None


def hamming(a: int, b: int) -> int:
    """两个整数哈希的汉明距离（比字符串版快得多）。"""
    return bin(a ^ b).count("1")


# --------------------------------------------------------------------------- #
# 结果
# --------------------------------------------------------------------------- #


@dataclass
class DedupPair:
    """一条近重复判定。"""

    dup_file_id: int
    dup_hash: str
    matched_file_id: int
    matched_hash: str
    distance: int


@dataclass
class DedupIndex:
    """分块索引。用法：对每个哈希调 lookup()；未命中则 insert()。"""

    radius: int = 8
    hash_size: int = 8
    _specs: List[Tuple[int, int]] = field(init=False, repr=False)
    _buckets: Dict[Tuple[int, int], List[Tuple[int, str]]] = field(
        init=False, repr=False, default_factory=dict)
    comparisons: int = 0
    inserts: int = 0
    hits: int = 0

    def __post_init__(self) -> None:
        self._specs = chunks_for(self.hash_size, self.radius)

    # -- 查询 -------------------------------------------------------------- #
    def _candidates(self, value: int) -> Set[str]:
        """返回与 value 共享任一块的所有已登记哈希（字符串形式）。"""
        cands: Set[str] = set()
        for ci, (shift, width) in enumerate(self._specs):
            key = (ci, (value >> shift) & ((1 << width) - 1))
            bucket = self._buckets.get(key)
            if bucket:
                cands.update(h for _v, h in bucket)
        return cands

    def lookup(self, hex_hash: str) -> Optional[Tuple[str, int]]:
        """找最近的一个近重复。返回 (匹配到的哈希, 距离)，没有则 None。"""
        value = hex_to_int(hex_hash)
        if value is None:
            return None
        best: Optional[Tuple[str, int]] = None
        for other in self._candidates(value):
            ov = hex_to_int(other)
            if ov is None:
                continue
            self.comparisons += 1
            d = hamming(value, ov)
            if d <= self.radius and (best is None or d < best[1]):
                best = (other, d)
                if d == 0:
                    break
        if best is not None:
            self.hits += 1
        return best

    def insert(self, hex_hash: str, *, replace: bool = False) -> None:
        """把哈希登记进索引。replace=True 时先移除同名哈希（用于更换代表）。"""
        value = hex_to_int(hex_hash)
        if value is None:
            return
        if replace:
            self.remove(hex_hash)
        for ci, (shift, width) in enumerate(self._specs):
            key = (ci, (value >> shift) & ((1 << width) - 1))
            self._buckets.setdefault(key, []).append((value, hex_hash))
        self.inserts += 1

    def remove(self, hex_hash: str) -> None:
        value = hex_to_int(hex_hash)
        if value is None:
            return
        for ci, (shift, width) in enumerate(self._specs):
            key = (ci, (value >> shift) & ((1 << width) - 1))
            bucket = self._buckets.get(key)
            if bucket:
                self._buckets[key] = [(v, h) for v, h in bucket if h != hex_hash]

    @property
    def size(self) -> int:
        return self.inserts

    def stats(self) -> Dict[str, int]:
        return {
            "indexed": self.inserts,
            "comparisons": self.comparisons,
            "hits": self.hits,
            "buckets": len(self._buckets),
            "chunk_count": len(self._specs),
            "chunk_widths": sorted({w for _s, w in self._specs}),
        }


# --------------------------------------------------------------------------- #
# 批量去重（纯函数，便于单测）
# --------------------------------------------------------------------------- #


def find_duplicates(
    items: Iterable[Tuple[int, str]],
    *,
    radius: int = 8,
    hash_size: int = 8,
) -> Tuple[List[DedupPair], Dict[str, int], DedupIndex]:
    """对 (file_id, hex_hash) 序列做近重复查找。

    返回 (重复对列表, 唯一哈希→file_id 映射, 索引统计)。
    判定顺序严格按传入顺序（调用方应按 file_id 升序），保证结果可复现。
    """
    idx = DedupIndex(radius=radius, hash_size=hash_size)
    uniques: Dict[str, int] = {}
    pairs: List[DedupPair] = []

    for fid, h in items:
        hit = idx.lookup(h)
        if hit is None:
            uniques.setdefault(h, fid)
            idx.insert(h)
            continue
        other_hash, dist = hit
        pairs.append(DedupPair(
            dup_file_id=fid, dup_hash=h,
            matched_file_id=uniques.get(other_hash, -1), matched_hash=other_hash,
            distance=dist,
        ))
    return pairs, uniques, idx


def find_duplicates_naive(
    items: Sequence[Tuple[int, str]],
    *,
    radius: int = 8,
) -> List[DedupPair]:
    """暴力基线（仅用于单测比对，勿用于生产——O(n²)）。"""
    seen: List[Tuple[int, str]] = []
    pairs: List[DedupPair] = []
    for fid, h in items:
        v = hex_to_int(h)
        if v is None:
            continue
        found: Optional[Tuple[str, int, int]] = None
        for ofid, oh in seen:
            ov = hex_to_int(oh)
            if ov is None:
                continue
            d = hamming(v, ov)
            if d <= radius and (found is None or d < found[2]):
                found = (oh, ofid, d)
                if d == 0:
                    break
        if found is None:
            seen.append((fid, h))
        else:
            pairs.append(DedupPair(dup_file_id=fid, dup_hash=h,
                                   matched_file_id=found[1], matched_hash=found[0],
                                   distance=found[2]))
    return pairs
