"""只读演示：仓库里照片的 pHash 近重复率（不写任何数据、不调用项目模块）。

用与 02_dedup.py 完全相同的算法参数（hash_size=8 → 64bit，汉明距离 <= 8）。
为了快，只抽样 N 张，并按「候选对」方式比较而不是两两全比。
"""

from __future__ import annotations

import random
import sqlite3
import sys
import time
from pathlib import Path

import imagehash
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
HASH_SIZE = 8          # 64 bit
THRESHOLD = 8          # 汉明距离 <= 8 视为近重复
SAMPLE = 1200          # 抽样张数（全量 41 万张太慢）


def phash(path: Path) -> str | None:
    try:
        with Image.open(path) as im:
            return str(imagehash.phash(im.convert("RGB"), hash_size=HASH_SIZE))
    except Exception:
        return None


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def main() -> int:
    if not DB.exists():
        print("库里没有数据")
        return 1
    con = sqlite3.connect(str(DB))
    rows = list(con.execute(
        "SELECT file_id, path, size, width, height, mtime FROM files "
        "WHERE kind='photo' ORDER BY file_id"))
    con.close()
    print(f"库中照片总数：{len(rows):,}")

    random.seed(7)
    sample = random.sample(rows, min(SAMPLE, len(rows)))
    print(f"抽样：{len(sample):,} 张（随机）\n")

    t0 = time.monotonic()
    hashed: list[tuple[int, str, str, int]] = []   # (file_id, phash, path, size)
    fail = 0
    for fid, path, size, _w, _h, _m in sample:
        h = phash(Path(str(path)))
        if h is None:
            fail += 1
            continue
        hashed.append((int(fid), h, str(path), int(size or 0)))
    dt = time.monotonic() - t0
    print(f"计算 {len(hashed):,} 个指纹，耗时 {dt:.1f}s"
          f"（{len(hashed)/max(dt,1e-6):.0f} 张/秒），解码失败 {fail}")
    if not hashed:
        return 1

    # 用「前缀分桶」加速：汉明距离 <= 8 时，高 16 位（4 个 hex）至少有一段相同。
    # 这里简单点：按高 8 位分桶，桶内两两比较（演示足够）。
    buckets: dict[str, list[int]] = {}
    for i, (_fid, h, _p, _s) in enumerate(hashed):
        buckets.setdefault(h[:2], []).append(i)

    dup_pairs: list[tuple[int, int, int]] = []
    for idxs in buckets.values():
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                i, j = idxs[a], idxs[b]
                d = hamming(hashed[i][1], hashed[j][1])
                if d <= THRESHOLD:
                    dup_pairs.append((i, j, d))

    dup_ids = {i for i, _j, _d in dup_pairs} | {j for _i, j, _d in dup_pairs}
    dup_bytes = sum(hashed[i][3] for i in dup_ids)
    total_bytes = sum(h[3] for h in hashed)

    print(f"\n=== 结果（64bit pHash，汉明距离 <= {THRESHOLD}）===")
    print(f"  近重复对        : {len(dup_pairs):,}")
    print(f"  涉及照片        : {len(dup_ids):,} / {len(hashed):,} "
          f"（{len(dup_ids)/len(hashed)*100:.1f}%）")
    print(f"  这些照片体积    : {dup_bytes/1024**3:.2f} GB / {total_bytes/1024**3:.2f} GB "
          f"（{dup_bytes/max(total_bytes,1)*100:.1f}%）")
    print(f"  → 若全库同比例，41 万张里约 {len(dup_ids)/len(hashed)*len(rows):,.0f} 张是重复")

    print("\n=== 近重复样例（前 8 对）===")
    for i, j, d in sorted(dup_pairs, key=lambda t: t[2])[:8]:
        print(f"  距离={d:2}  {Path(hashed[i][2]).name[:42]}")
        print(f"            {Path(hashed[j][2]).name[:42]}")
        print(f"            {hashed[i][3]/1024:.0f} KB vs {hashed[j][3]/1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
