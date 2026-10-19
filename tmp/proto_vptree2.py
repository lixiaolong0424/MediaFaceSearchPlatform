"""VP-tree 正确用法：一次性构建 + 批量查询，对比分块索引。

关键设计修正：
  索引结构（VP-tree / BK-tree）**不适合逐条「插入+查询」**，因为每次插入都可能
  需要重新平衡。正确用法是：
    1) 一次性用全部点的 (值, 原始下标) 构建
    2) 对每个点查询 k 近邻（radius 内），得到候选对
    3) 再用「并查集」把候选对聚成簇，每簇留一个代表
  这样是 O(n log n) 构建 + n 次查询，而不是 n 次「插入后重建」。

本脚本验证：
  * VP-tree 在批量模式下能否给出与暴力/分块法相同的**重复集合**
  * 查询速度（每秒多少次、平均访问节点数）
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.dedup_core import find_duplicates, find_duplicates_naive, hamming  # noqa: E402

RADIUS = 8


# --------------------------------------------------------------------------- #
# VP-tree
# --------------------------------------------------------------------------- #


class _Node:
    __slots__ = ("v", "ix", "mu", "inner", "outer")

    def __init__(self, v: int, ix: int, mu: int = 0,
                 inner: Optional["_Node"] = None, outer: Optional["_Node"] = None):
        self.v, self.ix, self.mu, self.inner, self.outer = v, ix, mu, inner, outer


class VPTree:
    def __init__(self, radius: int = RADIUS):
        self.radius = radius
        self.root: Optional[_Node] = None
        self.visited = 0
        self.queries = 0

    def build(self, points: Sequence[Tuple[int, int]]) -> None:
        """points: [(value, original_index)]"""
        self.root = self._build(list(points))

    def _build(self, pts: List[Tuple[int, int]]) -> Optional[_Node]:
        if not pts:
            return None
        if len(pts) == 1:
            return _Node(pts[0][0], pts[0][1])
        vp = pts[0]
        rest = pts[1:]
        dists = sorted(((hamming(vp[0], p[0]), p) for p in rest), key=lambda t: t[0])
        mid = len(dists) // 2
        mu = dists[mid][0]
        inner = [p for _d, p in dists[:mid]]
        outer = [p for _d, p in dists[mid:]]
        if not inner:
            inner, outer = outer[:1], outer[1:] or outer[:1]
        if not outer:
            outer, inner = inner[-1:], inner[:-1] or inner[:1]
        return _Node(vp[0], vp[1], mu, self._build(inner), self._build(outer))

    def query(self, q: int, exclude_ix: Optional[int] = None) -> List[Tuple[int, int]]:
        """返回 radius 内所有 (distance, index)。"""
        self.queries += 1
        out: List[Tuple[int, int]] = []
        r = self.radius
        stack = [self.root]
        while stack:
            node = stack.pop()
            if node is None:
                continue
            self.visited += 1
            d = hamming(q, node.v)
            if d <= r and node.ix != exclude_ix:
                out.append((d, node.ix))
            if d < node.mu + r and node.inner is not None:
                stack.append(node.inner)
            if d + r >= node.mu and node.outer is not None:
                stack.append(node.outer)
        return out


# --------------------------------------------------------------------------- #
# 并查集
# --------------------------------------------------------------------------- #


class DSU:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)   # 让小编号当根 → 稳定


def vp_duplicates(items: List[Tuple[int, str]]) -> Tuple[Set[int], Dict[str, object]]:
    """返回 (被判为重复的原始下标集合, 统计)。"""
    vals = []
    for i, (_fid, h) in enumerate(items):
        try:
            vals.append((int(h, 16), i))
        except ValueError:
            vals.append((None, i))
    valid = [(v, i) for v, i in vals if v is not None]

    tree = VPTree()
    t0 = time.monotonic()
    tree.build(valid)
    t_build = time.monotonic() - t0

    dsu = DSU(len(items))
    t0 = time.monotonic()
    edge_count = 0
    for v, i in valid:
        for _d, j in tree.query(v, exclude_ix=i):
            dsu.union(i, j)
            edge_count += 1
    t_query = time.monotonic() - t0

    # 每簇的最小下标作为代表；其余判为重复
    groups: Dict[int, List[int]] = {}
    for v, i in valid:
        groups.setdefault(dsu.find(i), []).append(i)
    dup_ix: Set[int] = set()
    for root, members in groups.items():
        for m in members:
            if m != root:
                dup_ix.add(m)
    return dup_ix, {
        "build_s": t_build, "query_s": t_query,
        "visited": tree.visited, "queries": tree.queries,
        "avg_visited": tree.visited / max(1, tree.queries),
        "edges": edge_count, "clusters": len(groups),
    }


def make_corpus(n_pairs: int, n_rand: int, seed: int,
                hash_size: int = 8) -> List[Tuple[int, str]]:
    rnd = random.Random(seed)
    items: List[Tuple[int, str]] = []
    fid = 1
    for _ in range(n_pairs):
        b = rnd.getrandbits(64)
        items.append((fid, f"{b:016x}")); fid += 1
        for _k in range(2):
            flip = 0
            for pos in rnd.sample(range(64), rnd.randint(1, RADIUS)):
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
    print("=== 正确性：VP-tree（并查集聚类）vs 分块法 ===")
    for seed in (1, 3):
        items = make_corpus(200, 1200, seed)
        dup_ix, st = vp_duplicates(items)
        # 分块法的「重复」= 不在 uniques 里的条目
        pf, uniq, _ist = find_duplicates(items)
        n = len(items)
        # 分块法：uniques 是按 hash 记录的，把它转成下标集合
        unique_ix = set()
        seen: Set[str] = set()
        for i, (_fid, h) in enumerate(items):
            if h in seen:
                continue
            seen.add(h)
            unique_ix.add(i)
        # 分块判定重复的条目（按 file_id 顺序）——用近似指标：重复总数
        print(f"  seed={seed} n={n}")
        print(f"    VP-tree 判为重复 {len(dup_ix):>5}  簇数 {st['clusters']:>5}  "
              f"边 {st['edges']:>6}")
        print(f"    分块法 判为重复 {len(pf):>5}")
        print(f"    VP: 构建 {st['build_s']:.2f}s 查询 {st['query_s']:.2f}s "
              f"平均访问 {st['avg_visited']:.0f} 节点/次")

    print("\n=== 速度：三种方法规模扫描 ===")
    print(f"  {'n':>8} {'朴素':>10} {'分块':>10} {'VP-tree':>12} {'VP加速':>9} "
          f"{'VP访问/次':>10}")
    for n_pairs, n_rand in ((300, 2000), (600, 4000), (1500, 10000)):
        items = make_corpus(n_pairs, n_rand, seed=1)
        n = len(items)

        t0 = time.monotonic()
        find_duplicates_naive(items)
        t_naive = time.monotonic() - t0

        t0 = time.monotonic()
        find_duplicates(items)
        t_chunk = time.monotonic() - t0

        t0 = time.monotonic()
        _d, st = vp_duplicates(items)
        t_vp = time.monotonic() - t0

        print(f"  {n:>8,} {t_naive:>9.2f}s {t_chunk:>9.2f}s {t_vp:>11.2f}s "
              f"{t_chunk / max(t_vp, 1e-9):>8.1f}x {st['avg_visited']:>10.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
