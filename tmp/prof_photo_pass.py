"""融合照片管线各步骤耗时拆解（决定全集 ETA 的关键测量）。

E: 是机械盘，所以「读」与「解码」是两个独立瓶颈，必须拆开量：
  read     把文件读进内存（机械盘随机寻道）
  open+exif PIL 打开 + 读 EXIF（顺序读头部）
  phash     全解码 + 64 位感知哈希（CPU）
  cache     整数倍降采样 + JPEG 落 NVMe
"""

import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageFile  # noqa: E402

import imagehash  # noqa: E402

ImageFile.LOAD_TRUNCATED_IMAGES = True


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    offset = int(sys.argv[2]) if len(sys.argv) > 2 else 5000
    conn = sqlite3.connect(str(ROOT / "data" / "library.db"))
    rows = list(conn.execute(
        "SELECT path, size FROM files WHERE kind='photo' ORDER BY file_id LIMIT ? OFFSET ?",
        (n, offset)))
    conn.close()
    avg_mb = sum(r[1] or 0 for r in rows) / max(1, len(rows)) / 1e6
    print(f"{len(rows)} 张照片，平均 {avg_mb:.2f} MB（offset={offset}）")

    out_dir = ROOT / "tmp" / "_prof_cache"
    out_dir.mkdir(parents=True, exist_ok=True)

    t = {"read": 0.0, "open_exif": 0.0, "phash": 0.0, "cache": 0.0}
    nbytes = 0
    t_all = time.monotonic()
    for i, (p, _sz) in enumerate(rows):
        p = Path(p)
        t0 = time.monotonic()
        try:
            with open(p, "rb") as f:
                buf = f.read()
        except OSError:
            continue
        t1 = time.monotonic(); t["read"] += t1 - t0
        nbytes += len(buf)

        import io
        try:
            im = Image.open(io.BytesIO(buf))
            im.load()
        except Exception:
            continue
        t2 = time.monotonic(); t["open_exif"] += t2 - t1

        try:
            imagehash.phash(im.convert("RGB"), hash_size=8)
        except Exception:
            pass
        t3 = time.monotonic(); t["phash"] += t3 - t2

        try:
            w, h = im.size
            short = 768
            scale = short / float(min(w, h))
            tw, th = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
            img = im.convert("RGB")
            factor = max(1, min(w, h) // short)
            if factor > 1:
                img = img.reduce(factor)
            if img.size != (tw, th):
                img = img.resize((tw, th), Image.BILINEAR)
            img.save(out_dir / f"{i}.jpg", "JPEG", quality=88)
        except Exception:
            pass
        t["cache"] += time.monotonic() - t3

    el = time.monotonic() - t_all
    print(f"总计 {el:.1f}s = {len(rows)/el:.1f} 张/秒，{nbytes/el/1e6:.1f} MB/s")
    for k, v in t.items():
        print(f"  {k:<10} {v:6.1f}s  {v/el*100:5.1f}%  {len(rows)/max(v,1e-9):6.1f} 张/秒")
    if len(rows):
        print(f"  外推全集 474,000 张：{474000/max(len(rows)/el,1e-9)/3600:.2f} 小时")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
