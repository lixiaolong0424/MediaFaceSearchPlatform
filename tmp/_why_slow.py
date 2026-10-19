"""确认抽帧速率 1.4/s 是「正在啃刚回队的巨图」还是真回退了。

判据：看接下来待处理的 file_id 是什么、体积多大、像素多少。
如果是一整块 6~9MB / 1.6 亿~4 亿像素的巨图，那 1.4/s 完全合理（单张解码 ~1s），
过完这 1,100 张就会回到 5/s 量级。
"""

import sqlite3
import sys
from pathlib import Path

from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = None
sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')

conn = sqlite3.connect(r'data\library.db')
conn.row_factory = sqlite3.Row

rows = list(conn.execute(
    "SELECT file_id, path, size FROM files WHERE kind='photo' AND status='pending' "
    "ORDER BY file_id LIMIT 12"))
print("接下来 12 个待处理照片：")
big = 0
for r in rows:
    p = Path(str(r["path"]))
    px = "?"
    try:
        with Image.open(p) as im:
            w, h = im.size
            px = f"{w}x{h}={w*h/1e6:.0f}MP"
            if w * h > 100_000_000:
                big += 1
    except Exception as e:
        px = f"打不开({type(e).__name__})"
    print(f"  fid={r['file_id']:<7} {r['size']/1e6:>7.2f}MB  {px}")

# 回队的那批到底有多少张、总体积多大
requeued = [34113, 67855]
rows2 = list(conn.execute(
    "SELECT COUNT(*) n, SUM(size) s FROM files WHERE kind='photo' AND status='pending' "
    "AND file_id BETWEEN 78000 AND 80000"))
print(f"\nfile_id 78000~80000 区间待处理：{rows2[0]['n']} 个，{rows2[0]['s']/1e9:.2f} GB"
      if rows2 and rows2[0]['n'] else "")
print(f"样本里 >100MP 的：{big}/12")
conn.close()
