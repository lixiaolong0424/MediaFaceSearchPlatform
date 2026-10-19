import sys, time
sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')
import os
from pathlib import Path

# 1) 新的 scandir 遍历（不带 streamlit 缓存，测纯遍历耗时）
def walk(nvme_root):
    total = 0
    stack = [Path(nvme_root)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat().st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total

t0 = time.monotonic(); b = walk(r'D:\DATA\PROJECT\建设多媒体整理系统\data\nvme')
print(f"  scandir 遍历 data/nvme : {time.monotonic()-t0:5.1f}s   {b/1e9:.2f} GB")

from src import db as D, ui_wall
from src.common import load_config
cfg = load_config(None, None)

t0 = time.monotonic(); D.ensure_schema(cfg.db_path, cfg.root, cfg)
print(f"  ensure_schema          : {time.monotonic()-t0:5.1f}s")

conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
t0 = time.monotonic(); st = D.library_stats(conn, str(cfg.media_root))
print(f"  library_stats          : {time.monotonic()-t0:5.1f}s  (faces={st['faces']})")

t0 = time.monotonic(); page = ui_wall.wall_page(conn, cfg, limit=60, mode="overview")
print(f"  wall_page(overview,60) : {time.monotonic()-t0:5.1f}s  (total={page['total']})")

t0 = time.monotonic(); fac = ui_wall.wall_facets(conn, cfg)
print(f"  wall_facets            : {time.monotonic()-t0:5.1f}s  (files={len(fac['files'])})")
conn.close()
