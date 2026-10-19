"""性能诊断：为什么 pHash 只有 3 张/秒？对比「全尺寸解码」与「draft 降采样解码」。

pHash 只需要 32x32 灰度信息，却先去解码一张 1 亿像素的 JPEG —— 这是瓶颈所在。
本脚本只读，不改项目。
"""

from __future__ import annotations

import random
import sqlite3
import time
from pathlib import Path

import imagehash
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
N = 40


def sample_paths(n: int) -> list[tuple[str, int]]:
    con = sqlite3.connect(str(DB))
    rows = list(con.execute(
        "SELECT path, COALESCE(size,0) FROM files WHERE kind='photo' ORDER BY file_id"))
    con.close()
    random.seed(11)
    picked = random.sample(rows, min(n, len(rows)))
    return [(str(p), int(s)) for p, s in picked]


def way_current(p: Path):
    """当前 02_dedup 的做法：全尺寸解码 → convert('RGB') → phash。"""
    with Image.open(p) as im:
        return str(imagehash.phash(im.convert("RGB"), hash_size=8))


def way_draft(p: Path):
    """优化做法：JPEG 用 draft() 让解码器直接按 1/2、1/4、1/8 出图（DCT 域降采样）。

    pHash 内部本来就要把图缩到 32x32，完全不需要原始分辨率。
    """
    with Image.open(p) as im:
        try:
            im.draft("L", (64, 64))   # 提示解码器：我只要很小的图
        except Exception:
            pass
        return str(imagehash.phash(im.convert("L"), hash_size=8))


def bench(label: str, fn, paths) -> tuple[float, int, int]:
    t0 = time.monotonic()
    ok = 0
    fail = 0
    for p, _s in paths:
        try:
            fn(Path(p))
            ok += 1
        except Exception:
            fail += 1
    dt = time.monotonic() - t0
    print(f"  {label:<28} {dt:6.1f}s  {ok:>3} 张成功  {ok/max(dt,1e-6):6.1f} 张/秒  失败 {fail}")
    return dt, ok, fail


def main() -> int:
    paths = sample_paths(N)
    total_gb = sum(s for _p, s in paths) / 1024**3
    print(f"抽样 {len(paths)} 张，合计 {total_gb:.2f} GB\n")
    t1, ok1, _ = bench("当前做法（全尺寸解码）", way_current, paths)
    t2, ok2, _ = bench("优化做法（draft 降采样）", way_draft, paths)

    print(f"\n  加速比：{t1/max(t2,1e-6):.1f}x")
    if ok2:
        eta1 = 412933 / (ok1 / max(t1, 1e-6)) / 3600
        eta2 = 412933 / (ok2 / max(t2, 1e-6)) / 3600
        print(f"  按 41.3 万张估算：当前 {eta1:.1f} 小时 → 优化后 {eta2:.1f} 小时")

    # 校验两种做法结果是否一致（降采样会不会改变指纹？）
    print("\n  指纹一致性抽查（前 12 张）：")
    same = 0
    diff = 0
    for p, _s in paths[:12]:
        try:
            a = way_current(Path(p))
            b = way_draft(Path(p))
            if a == b:
                same += 1
            else:
                diff += 1
                d = bin(int(a, 16) ^ int(b, 16)).count("1")
                print(f"    汉明距离 {d:2}  {Path(p).name[:50]}")
        except Exception as e:  # noqa: BLE001
            print(f"    失败 {Path(p).name[:40]}: {type(e).__name__}")
    print(f"  → 完全相同 {same} 张，有差异 {diff} 张")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
