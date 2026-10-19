"""用真实照片语料实测修复后的去重：正确性 + 耗时。

语料：从 data/library.db 取真实照片路径（前 N 张，按 file_id）。
在隔离库里跑，不碰正式库，不影响正在运行的 03_extract。

对比：
  * 现有实现（朴素双重循环，十六进制字符串 hamming）—— 只在小子集上跑，用于比对正确性
  * 修复实现（整数哈希 + C=9 分块索引）
正确性判定：两者给出的「近重复集合」完全一致。
"""

from __future__ import annotations

import random
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageFile  # noqa: E402

import imagehash  # noqa: E402

ImageFile.LOAD_TRUNCATED_IMAGES = True
RADIUS = 8
CHUNKS = RADIUS + 1          # 鸽巢：块数必须 > radius
HASH_SIZE = 8


# ---------------- 现有实现（复刻 02_dedup.py） ---------------- #

def hamming_hex(a: str, b: str) -> int:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (ValueError, TypeError):
        return 64


def naive(hashes: List[str]) -> Dict[str, str]:
    """复刻现有实现：每个新哈希与之前所有哈希比。"""
    result: Dict[str, str] = {}
    by_hash: Dict[str, int] = {}
    for i, h in enumerate(hashes):
        dup_of = None
        for oh in by_hash:
            if hamming_hex(h, oh) <= RADIUS:
                dup_of = oh
                break
        if dup_of is None:
            by_hash[h] = i
        else:
            result[h] = dup_of
    return result


# ---------------- 修复实现 ---------------- #

def chunk_specs(total_bits: int, c: int) -> List[Tuple[int, int]]:
    base, rem = divmod(total_bits, c)
    out, shift = [], 0
    for i in range(c):
        w = base + (1 if i < rem else 0)
        out.append((shift, w))
        shift += w
    return out


SPECS = chunk_specs(64, CHUNKS)
print(f"分块方案：{CHUNKS} 块，块宽 "
      f"{sorted({w for _s, w in SPECS})} 位（鸽巢保证零漏检）")


def bucketed(hashes: List[str]) -> tuple[Dict[str, str], int]:
    """返回 ({重复哈希: 代表哈希}, 比较次数)。"""
    index: Dict[Tuple[int, int], List[Tuple[int, str]]] = defaultdict(list)
    result: Dict[str, str] = {}
    comps = 0
    for h in hashes:
        v = int(h, 16)
        cs = [(v >> s) & ((1 << w) - 1) for s, w in SPECS]
        cands: Set[str] = set()
        for ci, cv in enumerate(cs):
            b = index.get((ci, cv))
            if b:
                cands.update(hh for _vv, hh in b)
        found = None
        for ch in cands:
            comps += 1
            if bin(v ^ int(ch, 16)).count("1") <= RADIUS:
                found = ch
                break
        if found is None:
            for ci, cv in enumerate(cs):
                index[(ci, cv)].append((v, h))
        else:
            result[h] = found
    return result, comps


# ---------------- 语料 ---------------- #

def compute_phash(p: Path) -> Optional[str]:
    try:
        with Image.open(p) as im:
            return str(imagehash.phash(im.convert("RGB"), hash_size=HASH_SIZE))
    except Exception:  # noqa: BLE001
        return None


def load_paths(n: int, offset: int = 0) -> List[Path]:
    con = sqlite3.connect(str(ROOT / "data" / "library.db"))
    rows = con.execute(
        "SELECT path FROM files WHERE kind='photo' ORDER BY file_id LIMIT ? OFFSET ?",
        (n, offset)).fetchall()
    con.close()
    return [Path(r[0]) for r in rows]


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--small", type=int, default=3000,
                    help="用于正确性比对的子集大小（朴素法在这上面跑得动）")
    ap.add_argument("--large", type=int, default=40000,
                    help="用于速度外推的较大子集")
    args = ap.parse_args()

    # --- 正确性 ---
    print(f"\n=== A) 正确性：真实照片 {args.small} 张 ===")
    paths = load_paths(args.small)
    t0 = time.monotonic()
    hs: List[str] = []
    for p in paths:
        if not p.exists():
            continue
        h = compute_phash(p)
        if h:
            hs.append(h)
    t_hash = time.monotonic() - t0
    print(f"  成功算 pHash {len(hs):,}/{len(paths):,} 张，耗时 {t_hash:.1f}s "
          f"({len(hs)/max(t_hash,1e-9):.0f} 张/秒)")

    t0 = time.monotonic()
    r_naive = naive(hs)
    t_naive = time.monotonic() - t0
    t0 = time.monotonic()
    r_bucket, comps = bucketed(hs)
    t_bucket = time.monotonic() - t0

    same = r_naive == r_bucket
    print(f"  朴素法 : 检出 {len(r_naive):>5}  耗时 {t_naive:7.2f}s")
    print(f"  修复法 : 检出 {len(r_bucket):>5}  耗时 {t_bucket:7.2f}s  "
          f"比较 {comps:,}  加速 {t_naive/max(t_bucket,1e-6):.1f}x")
    print(f"  结果一致：{'✓ 是（零漏检）' if same else '✗ 否'}")
    if not same:
        print(f"    仅朴素检出 {len(set(r_naive)-set(r_bucket))}，"
              f"仅修复检出 {len(set(r_bucket)-set(r_naive))}")

    # --- 速度外推 ---
    print(f"\n=== B) 速度：真实照片 {args.large} 张（只跑修复法）===")
    paths2 = load_paths(args.large)
    hs2: List[str] = []
    t0 = time.monotonic()
    for p in paths2:
        if not p.exists():
            continue
        h = compute_phash(p)
        if h:
            hs2.append(h)
    t_hash2 = time.monotonic() - t0
    t0 = time.monotonic()
    r2, comps2 = bucketed(hs2)
    t_b2 = time.monotonic() - t0
    n2 = len(hs2)
    print(f"  pHash {n2:,} 张耗时 {t_hash2:6.1f}s → {n2/max(t_hash2,1e-9):,.0f} 张/秒")
    print(f"  去重 {n2:,} 张耗时 {t_b2:6.1f}s → {n2/max(t_b2,1e-9):,.0f} 张/秒  "
          f"检出近重复 {len(r2):,}")
    print(f"\n  外推 41.3 万张：")
    print(f"    pHash 计算 ≈ {413000 / max(n2/t_hash2,1e-9)/60:>5.1f} 分钟")
    print(f"    去重比较   ≈ {413000 / max(n2/t_b2,1e-9)/60:>5.1f} 分钟")
    print(f"    合计       ≈ {(413000 / max(n2/t_hash2,1e-9) + 413000 / max(n2/t_b2,1e-9))/60:>5.1f} 分钟")
    print(f"  （对比：现有朴素实现约 11 小时）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
