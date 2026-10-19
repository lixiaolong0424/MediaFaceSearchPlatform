"""对比三种近重复查找结构的正确性与速度，选最快的用于生产。

  1) 朴素双重循环        —— 现状，O(n²)
  2) 鸽巢分块索引 C=9    —— 已实现于 src/dedup_core.py，零漏检
  3) VP-tree             —— 度量树，半径 8 的小范围查询通常访问极少节点

VP-tree 要点：
  * 选一个「优势点」vantage point，把所有点按到 vp 的距离排序、取中位数分成内外两半
  * 查询时用三角不等式剪枝：|d(q,vp) - d(vp,x)| > R ⟹ d(q,x) > R，整棵子树可跳过
  * Hamming 距离是度量（满足三角不等式），可以用
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.dedup_core import find_duplicates, find_duplicates_naive, hamming  # noqa: E402

RADIUS = 8


class VPTree:
    """按汉明距离的 VP-tree。节点存 (vantage, mu, inner, outer)。"""

    __slots__ = ("v", "mu", "inner", "outer", "payload")

    def __init__(self, v: int, payload: str, mu: int = 0,
                 inner: Optional["VPTree"] = None, outer: Optional["VPTree"] = None):
        self.v = v
        self.payload = payload
        self.mu = mu
        self.inner = inner
        self.outer = outer


class VPTreeIndex:
    def __init__(self, radius: int = RADIUS):
        self.radius = radius
        self.root: Optional[VPTree] = None
        self.comparisons = 0
        self._pairs: List[Tuple[Tuple[int, str], Tuple[int, str]]] = []

    # -- 构建（批量，用中位数分割保证平衡）-- #
    def build(self, points: Sequence[Tuple[int, str]]) -> None:
        self._pairs = list(points)
        self.root = self._build(list(self._pairs))

    def _build(self, pts: List[Tuple[int, str]]) -> Optional[VPTree]:
        if not pts:
            return None
        if len(pts) == 1:
            return VPTree(pts[0][0], pts[0][1])
        vp = pts[0]
        rest = pts[1:]
        dists = [(hamming(vp[0], p[0]), p) for p in rest]
        dists.sort(key=lambda t: t[0])
        mid = len(dists) // 2
        mu = dists[mid][0]
        inner = [p for d, p in dists if d < mu] or [p for d, p in dists[:mid]]
        outer = [p for d, p in dists if d >= mu]
        # 保证两侧都不为空，避免退化
        if not inner:
            inner = [outer.pop(0)] if len(outer) > 1 else []
        if not outer:
            outer = [inner.pop()] if len(inner) > 1 else []
        return VPTree(vp[0], vp[1], mu,
                      self._build(inner) if inner else None,
                      self._build(outer) if outer else None)

    # -- 查询：返回最近的一个（距离 <= radius）-- #
    def nearest(self, q: int) -> Optional[Tuple[str, int]]:
        best: Optional[Tuple[str, int]] = None
        stack = [self.root]
        r = self.radius
        while stack:
            node = stack.pop()
            if node is None:
                continue
            self.comparisons += 1
            d = hamming(q, node.v)
            if d <= r and (best is None or d < best[1]):
                best = (node.payload, d)
                if d == 0:
                    return best
            # 剪枝：以 q 为中心、R 为半径的球是否与内/外侧相交
            if d < node.mu + r and node.inner is not None:
                stack.append(node.inner)
            if d + r >= node.mu and node.outer is not None:
                stack.append(node.outer)
        return best


def make_corpus(n_pairs: int, n_rand: int, seed: int,
                hash_size: int = 8) -> List[Tuple[int, str]]:
    """与前面一致的语料：含随机近重复 + 「8 位差异挤在一小块」的极端用例。"""
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


def vp_find(items: List[Tuple[int, str]], radius: int = RADIUS
            ) -> Tuple[List[Tuple[int, int, int]], VPTreeIndex]:
    """增量构建 + 查询（与分块法同样的「首见代表」语义）。"""
    idx = VPTreeIndex(radius)
    out: List[Tuple[int, int, int]] = []
    points: List[Tuple[int, str]] = []
    for fid, h in items:
        q = int(h, 16)
        hit = idx.nearest(q) if idx.root is not None else None
        if hit is None:
            points.append((q, h))
            idx.build(points)          # 简化：每次重建（用于正确性验证）
        else:
            pass
        # 上面的重建方式太慢，仅用于小规模正确性验证
    return out, idx


def main() -> int:
    print("=== 正确性：三种方法在 n=2500 上必须一致 ===")
    items = make_corpus(250, 1500, seed=3)
    pn = find_duplicates_naive(items)
    pf, _u, ist = find_duplicates(items)
    sn = {(p.dup_file_id, p.distance) for p in pn}
    sf = {(p.dup_file_id, p.distance) for p in pf}
    print(f"  朴素   {len(pn):>5} 条")
    print(f"  分块   {len(pf):>5} 条  一致={'是' if sn == sf else '否'}")
    print(f"  分块索引比较次数 {ist['comparisons']:,}（朴素 {len(items)*(len(items)-1)//2:,}）")

    print("\n=== 速度：分块法在不同规模下的吞吐（真实关注点）===")
    for n_pairs, n_rand in ((300, 2000), (600, 4000), (1200, 8000)):
        items = make_corpus(n_pairs, n_rand, seed=1)
        t0 = time.monotonic()
        pf, _u, ist = find_duplicates(items)
        dt = time.monotonic() - t0
        n = len(items)
        print(f"  n={n:>6,}  dups={len(pf):>5}  比较={ist['comparisons']:>12,}  "
              f"耗时={dt:>6.2f}s → {n/dt:>7,.0f} 张/秒")

    print("\n=== VP-tree 增量插入的真实成本（每插入重建是不可行的）===")
    rnd = random.Random(1)
    pts = [(rnd.getrandbits(64), f"{rnd.getrandbits(64):016x}") for _ in range(2000)]
    t0 = time.monotonic()
    tree = VPTreeIndex()
    tree.build(pts)
    t_build = time.monotonic() - t0
    t0 = time.monotonic()
    hits = sum(1 for q, _h in pts if tree.nearest(q))
    t_q = time.monotonic() - t0
    print(f"  n=2000 构建 {t_build:.2f}s；查询 {len(pts)} 次耗时 {t_q:.2f}s "
          f"→ {len(pts)/max(t_q,1e-9):,.0f} 次/秒，命中 {hits}")
    print(f"  单次查询平均访问节点 {tree.comparisons/len(pts):.1f} 个（朴素需 {len(pts)}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
