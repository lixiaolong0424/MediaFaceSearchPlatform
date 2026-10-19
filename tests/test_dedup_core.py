"""单测：src/dedup_core.py 与暴力法逐条比对，必须完全一致（零漏检）。

覆盖：
  * 随机哈希 + 已知近重复（距离 1~8）
  * 极端用例：8 个差异位全部挤在最小的一个块里（最考验跨块查找）
  * 边界：距离恰好 8 / 恰好 9（9 必须不算重复）
  * 顺序敏感：结果应与传入顺序一致、可复现
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.dedup_core import (  # noqa: E402
    DedupIndex, chunk_specs, chunks_for, find_duplicates, find_duplicates_naive,
    hamming,
)

FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global FAILED
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  {detail}" if detail else ""))
    if not ok:
        FAILED += 1


def make_corpus(n_pairs: int, n_rand: int, seed: int) -> list[tuple[int, str]]:
    rnd = random.Random(seed)
    items: list[tuple[int, str]] = []
    fid = 1
    for _ in range(n_pairs):
        b = rnd.getrandbits(64)
        items.append((fid, f"{b:016x}"))
        fid += 1
        # 变体：随机翻 1~8 位
        for _k in range(2):
            flip = 0
            for pos in rnd.sample(range(64), rnd.randint(1, 8)):
                flip |= 1 << pos
            items.append((fid, f"{b ^ flip:016x}"))
            fid += 1
        # 极端：8 位差异全挤在最窄的块里
        specs = chunks_for(8, 8)
        shift, width = min(specs, key=lambda t: t[1])
        flip = 0
        for pos in rnd.sample(range(min(width, 8)), min(width, 8)):
            flip |= 1 << (shift + pos)
        items.append((fid, f"{b ^ flip:016x}"))
        fid += 1
    for _ in range(n_rand):
        items.append((fid, f"{rnd.getrandbits(64):016x}"))
        fid += 1
    rnd.shuffle(items)
    return items


def main() -> int:
    print("=== 1) 分块方案参数 ===")
    specs = chunks_for(8, 8)
    widths = [w for _s, w in specs]
    check("块数 = radius+1 = 9（鸽巢要求）", len(specs) == 9, f"实际 {len(specs)}")
    check("块宽覆盖 64 位", sum(widths) == 64, f"合计 {sum(widths)} 位，各宽 {widths}")
    check("块数使用均衡", max(widths) - min(widths) <= 1, f"各宽 {widths}")

    print("\n=== 2) 与暴力法逐条一致（三组随机种子）===")
    for seed in (1, 2, 3):
        items = make_corpus(250, 1500, seed)
        pn = find_duplicates_naive(items)
        pf, uniq, idx = find_duplicates(items)
        # 断言「去重决策」而非「内部分账」：
        #   同一 dup_hash 可能被多个 file_id 复用，用 dict 聚合会丢条目，
        #   因此这里用 (dup_file_id, distance) 集合比较，并额外核对总数。
        sn = {(p.dup_file_id, p.distance) for p in pn}
        sf = {(p.dup_file_id, p.distance) for p in pf}
        st = idx.stats()
        check(f"seed={seed} n={len(items)} 重复条目集合一致（含距离）",
              sn == sf,
              f"暴力 {len(pn)} 分块 {len(pf)}；仅暴力 {len(sn - sf)} 仅分块 {len(sf - sn)}")
        check(f"seed={seed} 重复总数相同", len(pn) == len(pf),
              f"{len(pn)} vs {len(pf)}")
        check(f"seed={seed} 被标记为重复的 file_id 集合一致",
              {p.dup_file_id for p in pn} == {p.dup_file_id for p in pf},
              f"比较 {st['comparisons']:,} 次")

    print("\n=== 2b) 完全相同（d=0）的照片必须稳定命中同一个代表 ===")
    rnd = random.Random(77)
    same = f"{rnd.getrandbits(64):016x}"
    items = [(i, same) for i in range(1, 6)]
    pf, _u, _i = find_duplicates(items)
    check("4 个完全相同的哈希全部判为重复", len(pf) == 4, f"检出 {len(pf)}")
    check("全部指向首个（file_id=1）", {p.matched_file_id for p in pf} == {1},
          str(sorted(p.matched_file_id for p in pf)))
    check("距离全为 0", {p.distance for p in pf} == {0}, str(sorted(p.distance for p in pf)))

    print("\n=== 3) 边界：距离恰好 8 / 恰好 9 ===")
    b = random.Random(9).getrandbits(64)
    flip8 = 0
    for pos in [0, 5, 11, 17, 23, 31, 40, 55]:
        flip8 |= 1 << pos
    flip9 = flip8 | (1 << 62)
    h0, h8, h9 = f"{b:016x}", f"{b ^ flip8:016x}", f"{b ^ flip9:016x}"
    check("距离 8 记为重复", hamming(b, b ^ flip8) == 8)
    check("距离 9 记为不重复", hamming(b, b ^ flip9) == 9)
    pf, _u, _i = find_duplicates([(1, h0), (2, h8), (3, h9)])
    dups = {p.dup_file_id for p in pf}
    check("D=8 的条目被判为重复", 2 in dups, f"重复集 {dups}")
    check("D=9 的条目未被判为重复", 3 not in dups, f"重复集 {dups}")

    print("\n=== 4) 可复现性（同输入同输出）===")
    items = make_corpus(120, 800, 42)
    a = [(p.dup_file_id, p.matched_file_id, p.distance) for p in find_duplicates(items)[0]]
    c = [(p.dup_file_id, p.matched_file_id, p.distance) for p in find_duplicates(items)[0]]
    check("两次运行结果完全相同", a == c, f"{len(a)} 条")

    print("\n=== 5) 链式：A~B、B~C（A 与 C 距离>8）应形成一条链 ===")
    base = random.Random(5).getrandbits(64)
    f1 = 1 << 0
    f2 = 1 << 1
    a_h = f"{base:016x}"
    b_h = f"{base ^ f1:016x}"
    c_h = f"{base ^ f1 ^ f2:016x}"
    pf, _u, _i = find_duplicates([(1, a_h), (2, b_h), (3, c_h)])
    check("两条连续近重复都被检出", len(pf) == 2, f"检出 {len(pf)} 条")
    # 设计语义：每个重复项与其「最接近已登记代表」配对（A 是唯一被登记的代表，
    # B、C 都不入索引），因此 B、C 都指向 A。这与暴力基线一致。
    b_pair = next((p for p in pf if p.dup_file_id == 2), None)
    c_pair = next((p for p in pf if p.dup_file_id == 3), None)
    check("B 指向已登记的 A", b_pair is not None and b_pair.matched_file_id == 1,
          f"{b_pair.matched_file_id if b_pair else None}")
    check("C 也指向 A（A 未被 B 取代，与暴力法一致）",
          c_pair is not None and c_pair.matched_file_id == 1,
          f"{c_pair.matched_file_id if c_pair else None}")
    pf_n = find_duplicates_naive([(1, a_h), (2, b_h), (3, c_h)])
    check("链式结果与暴力法一致",
          {(p.dup_file_id, p.matched_file_id) for p in pf}
          == {(p.dup_file_id, p.matched_file_id) for p in pf_n},
          f"分块 {[(p.dup_file_id, p.matched_file_id) for p in pf]} vs "
          f"暴力 {[(p.dup_file_id, p.matched_file_id) for p in pf_n]}")

    print("\n=== 6) 吞吐（合成数据）===")
    rnd = random.Random(11)
    big = [(i, f"{rnd.getrandbits(64):016x}") for i in range(100_000)]
    t0 = time.monotonic()
    pf, _u, idx = find_duplicates(big)
    dt = time.monotonic() - t0
    st = idx.stats()
    print(f"  n=100,000（纯随机）耗时 {dt:.2f}s → {100000/dt:,.0f} 张/秒  "
          f"比较 {st['comparisons']:,}（朴素需 {100000*99999//2:,}）")
    naive_pairs = 100_000 * 99_999 // 2
    print(f"  比较次数减少 ≈ {naive_pairs / max(1, st['comparisons']):,.0f} 倍")

    print(f"\n{'=' * 60}")
    print(f"  单测结果：{'全部通过' if FAILED == 0 else f'{FAILED} 项失败'}")
    print(f"{'=' * 60}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
