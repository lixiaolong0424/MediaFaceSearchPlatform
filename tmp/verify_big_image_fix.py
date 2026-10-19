"""验证 PIL 解压炸弹上限已放宽，且真实的失败文件现在能解码。

对照：修复前这些文件报
    图片解码失败：Image size (201326592 pixels) exceeds limit of 178956970 pixels,
                   could be decompression bomb DOS attack.
修复后应当能完整走通 03_extract 的照片路径（EXIF + pHash + 768px 缓存）。

用法：
    .venv\\Scripts\\python.exe tmp\\verify_big_image_fix.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import src.common as C  # noqa: E402  先 import 以触发上限放宽

from PIL import Image  # noqa: E402


def main() -> int:
    print(f"PIL.MAX_IMAGE_PIXELS = {Image.MAX_IMAGE_PIXELS:,}"
          f"（PIL 默认 89,478,485；抛出 Error 的阈值是它的 2 倍 = 178,956,970）")

    conn = sqlite3.connect(str(ROOT / "data" / "library.db"))
    rows = list(conn.execute(
        "SELECT file_id, path, size FROM files WHERE status='error' AND kind='photo' "
        "ORDER BY size DESC LIMIT 6"))
    n_err = conn.execute(
        "SELECT COUNT(*) FROM files WHERE status='error' AND kind='photo'").fetchone()[0]
    conn.close()
    print(f"库里目前 photo error = {n_err} 个；取最大的 {len(rows)} 个来验：\n")

    from src.dedup_run import compute_phash_image
    from src.extract import _write_photo_cache
    from src.common import load_config

    cfg = load_config(None, None)
    ok = bad = 0
    for fid, path, size in rows:
        p = Path(path)
        t0 = time.monotonic()
        try:
            with Image.open(p) as im:
                w, h = im.size
                ph = compute_phash_image(im, 8)
            with tempfile.TemporaryDirectory() as td:
                out_cfg = cfg
                wrote = _write_photo_cache(
                    Image.open(p), cfg, fid, int(cfg.get("storage.photo_cache_short_edge", 768)))
            el = time.monotonic() - t0
            mp = w * h / 1e6
            print(f"  ✓ file_id={fid}  {w}×{h} = {mp:.1f} MP  文件 {size/1e6:.1f} MB  "
                  f"pHash={ph}  缓存={wrote}  {el:.1f}s")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ file_id={fid}  {p.name}  仍然失败：{type(exc).__name__}: {exc}")
            bad += 1

    print(f"\n可解码 {ok} / 失败 {bad}")
    if bad == 0:
        print("结论：解压炸弹上限已放宽，原先被错杀的大图现在能正常走完抽帧路径。")
        print("      这些文件 status='error'，而 03_extract 的取数条件含 'error'，"
              "所以下一轮抽帧会自动重试并入库。")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
