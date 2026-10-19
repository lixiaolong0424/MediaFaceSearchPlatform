"""并行 pHash 吞吐基准（用于决定全集去重/抽帧的并发度）。

为什么必须测：E: 是 14TB SATA 机械盘，
  * 纯读冷数据实测 34~42 文件/秒（≈140~170 MB/s，已到盘的上限）
  * 但「读 + pHash 解码」串行只有 3.8~12.6 文件/秒
说明瓶颈是 PIL 解码（CPU 单核），不是磁盘。
本机 32 逻辑核 / 16 物理核，所以多进程解码应该能把吞吐拉回磁盘上限。

用法：
    .venv\\Scripts\\python.exe tmp\\bench_phash_par.py
"""

from __future__ import annotations

import io
import sqlite3
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageFile  # noqa: E402

import imagehash  # noqa: E402

ImageFile.LOAD_TRUNCATED_IMAGES = True


def phash_plain(path: str):
    try:
        with Image.open(path) as im:
            return str(imagehash.phash(im.convert("RGB"), hash_size=8))
    except Exception:  # noqa: BLE001
        return None


def phash_draft(path: str):
    try:
        with Image.open(path) as im:
            try:
                im.draft("L", (64, 64))
            except Exception:  # noqa: BLE001
                pass
            return str(imagehash.phash(im.convert("RGB"), hash_size=8))
    except Exception:  # noqa: BLE001
        return None


def bench(fn, sel, workers: int, label: str) -> float:
    t0 = time.monotonic()
    if workers == 1:
        n = sum(1 for p in sel if fn(p))
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            n = sum(1 for h in ex.map(fn, sel, chunksize=4) if h)
    el = max(1e-9, time.monotonic() - t0)
    fps = len(sel) / el
    print(f"  {label:<34} {fps:7.1f} 文件/秒   ({el:6.1f}s / {len(sel)}, ok={n})", flush=True)
    return fps


def main() -> int:
    conn = sqlite3.connect(str(ROOT / "data" / "library.db"))
    paths = [r[0] for r in conn.execute(
        "SELECT path FROM files WHERE kind='photo' ORDER BY file_id")]
    conn.close()
    print(f"照片总数 {len(paths):,}")

    N = 500
    base = 120000
    slices = {i: paths[base + i * N: base + (i + 1) * N] for i in range(6)}

    print("\n=== 冷数据：不同并发度 ===")
    r1 = bench(phash_draft, slices[0], 1, "draft,  1 进程（冷）")
    r12 = bench(phash_draft, slices[1], 12, "draft, 12 进程（冷）")
    r24 = bench(phash_draft, slices[2], 24, "draft, 24 进程（冷）")
    rp = bench(phash_plain, slices[3], 12, "plain, 12 进程（冷）")

    print(f"\n加速比：12 进程 / 1 进程 = {r12 / max(r1,1e-9):.1f}x，"
          f"24 进程 = {r24 / max(r1,1e-9):.1f}x")
    print(f"draft 相对 plain（同为 12 进程）：{r12 / max(rp,1e-9):.2f}x")

    print("\n=== 正确性：draft 与 plain 必须给出完全一样的哈希 ===")
    smp = slices[4]
    a = [phash_plain(p) for p in smp]
    b = [phash_draft(p) for p in smp]
    same = sum(1 for x, y in zip(a, b) if x == y and x is not None)
    print(f"  逐条一致 {same}/{len(smp)}  -> {'一致' if same == len(smp) else '⚠️ 不一致'}")

    print("\n=== 全集外推（478,634 张）===")
    for name, fps in (("1 进程", r1), ("12 进程", r12), ("24 进程", r24)):
        print(f"  {name:<8} {478634 / max(fps,1e-9) / 3600:5.2f} 小时")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
