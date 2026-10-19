"""用真正的取数函数看抽帧队列的头部，判断 1.4 文件/秒 是暂时的还是长期的。

上次我用 `files.status='pending'` 直接查，绕过了 select_targets 的
`stage_status(extract)` 条件，结论是错的。这次走同一个函数。
"""

import sys
import time
from pathlib import Path

from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = None
sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')

from src import db as D
from src.common import load_config
from src.extract import select_targets

cfg = load_config(None, None)
conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
rows = select_targets(conn, cfg, limit=15)
total = len(select_targets(conn, cfg))
print(f"抽帧队列总长 {total:,}，头部 15 个：")
big = 0
sz = 0
for r in rows:
    p = Path(str(r["path"]))
    try:
        with Image.open(p) as im:
            w, h = im.size
        px = f"{w}x{h}={w*h/1e6:.0f}MP"
        if w * h > 20_000_000:
            big += 1
    except Exception as e:
        px = f"打不开({type(e).__name__})"
    try:
        b = p.stat().st_size
    except OSError:
        b = 0
    sz += b
    print(f"  fid={r['file_id']:<7} {b/1e6:>6.2f}MB  {px}")

# 队列里有多大比例的「大图」——这决定了 1.4/s 会持续多久
allrows = select_targets(conn, cfg)
n_big = 0
n = 0
for r in allrows[:20000]:
    n += 1
    p = Path(str(r["path"]))
    try:
        if p.stat().st_size > 6_000_000:
            n_big += 1
    except OSError:
        pass
print(f"\n队列前 {n:,} 个里 >6MB 的：{n_big:,}（{100*n_big/max(1,n):.1f}%）")
print(f"头部 15 个平均体积 {sz/15/1e6:.1f} MB —— 机械盘上读这些本身就慢")
conn.close()
