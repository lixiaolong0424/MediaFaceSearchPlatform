"""快速测：pHash 单张计算成本 vs 去重比较成本（分离测量）。"""

from __future__ import annotations

import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import imagehash  # noqa: E402
from PIL import Image, ImageFile  # noqa: E402

ImageFile.LOAD_TRUNCATED_IMAGES = True

RADIUS = 8
CHUNKS = RADIUS + 1


def specs(total: int, c: int) -> List[Tuple[int, int]]:
    base, rem = divmod(total, c)
    out, shift = [], 0
    for i in range(c):
        w = base + (1 if i < rem else 0)
        out.append((shift, w))
        shift += w
    return out


SPECS = specs(64, CHUNKS)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--hash-n", type=int, default=400)
    ap.add_argument("--dedup-n", type=int, default=20000)
    args = ap.parse_args()

    con = sqlite3.connect(str(ROOT / "data" / "library.db"))
    rows = con.execute(
        "SELECT path FROM files WHERE kind='photo' ORDER BY file_id LIMIT ?",
        (max(args.hash_n, args.dedup_n),)).fetchall()
    con.close()
    paths = [Path(r[0]) for r in rows]

    # ---- 1) pHash 计算成本 ----
    print(f"=== 1) pHash 计算成本（{args.hash_n} 张真实照片）===")
    hs: List[str] = []
    n_ok = n_fail = 0
    t0 = time.monotonic()
    for p in paths[:args.hash_n]:
        try:
            with Image.open(p) as im:
                hs.append(str(imagehash.phash(im.convert("RGB"), hash_size=8)))
            n_ok += 1
        except Exception:  # noqa: BLE001
            n_fail += 1
    dt = time.monotonic() - t0
    rate = n_ok / dt if dt > 0 else 0
    print(f"  成功 {n_ok} / 失败 {n_fail}  耗时 {dt:.2f}s → {rate:,.0f} 张/秒")
    print(f"  单张 {dt / max(n_ok, 1) * 1000:.1f} ms")
    print(f"  外推 41.3 万张 ≈ {413000 / rate / 60:.1f} 分钟（若 rate=0 则无法估）")

    # ---- 2) 去重比较成本（喂随机哈希，隔离 I/O）----
    print(f"\n=== 2) 去重比较成本（{args.dedup_n} 个随机 64 位哈希）===")
    import random
    rnd = random.Random(1)
    fake = [f"{rnd.getrandbits(64):016x}" for _ in range(args.dedup_n)]
    t0 = time.monotonic()
    index: Dict[Tuple[int, int], List[str]] = defaultdict(list)
    result = 0
    for h in fake:
        v = int(h, 16)
        cs = [(v >> s) & ((1 << w) - 1) for s, w in SPECS]
        cands: Set[str] = set()
        for ci, cv in enumerate(cs):
            b = index.get((ci, cv))
            if b:
                cands.update(b)
        found = False
        for ch in cands:
            if bin(v ^ int(ch, 16)).count("1") <= RADIUS:
                found = True
                break
        if found:
            result += 1
        else:
            for ci, cv in enumerate(cs):
                index[(ci, cv)].append(h)
    dt2 = time.monotonic() - t0
    rate2 = args.dedup_n / dt2 if dt2 > 0 else 0
    print(f"  耗时 {dt2:.2f}s → {rate2:,.0f} 个/秒  检出 {result}")
    print(f"  外推 41.3 万张 ≈ {413000 / rate2 / 60:.1f} 分钟")

    # ---- 3) 用真实哈希做去重，看真实重复率 ----
    if hs:
        print(f"\n=== 3) 真实哈希去重（{len(hs)} 张）===")
        t0 = time.monotonic()
        index2: Dict[Tuple[int, int], List[str]] = defaultdict(list)
        dups = 0
        for h in hs:
            v = int(h, 16)
            cs = [(v >> s) & ((1 << w) - 1) for s, w in SPECS]
            cands = set()
            for ci, cv in enumerate(cs):
                b = index2.get((ci, cv))
                if b:
                    cands.update(b)
            if any(bin(v ^ int(ch, 16)).count("1") <= RADIUS for ch in cands):
                dups += 1
            else:
                for ci, cv in enumerate(cs):
                    index2[(ci, cv)].append(h)
        dt3 = time.monotonic() - t0
        print(f"  耗时 {dt3:.3f}s  检出近重复 {dups}/{len(hs)} "
              f"({dups / max(len(hs),1) * 100:.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
