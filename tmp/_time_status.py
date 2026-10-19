import time, sys
sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')
from src.common import load_config, dir_size_bytes
from src import db as D
cfg = load_config(None, None)
conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)

t0=time.monotonic()
libs = list(conn.execute("SELECT root, last_scan, status FROM libraries"))
print(f"  libraries: {time.monotonic()-t0:.1f}s")

t0=time.monotonic()
n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
print(f"  COUNT files ({n}): {time.monotonic()-t0:.1f}s")

t0=time.monotonic()
r = conn.execute("""SELECT CASE WHEN instr(rel_path,'\\')>0
       THEN substr(rel_path,1,instr(rel_path,'\\')-1) ELSE rel_path END seg, COUNT(*) n
  FROM files WHERE path LIKE 'E:%' AND rel_path IS NOT NULL
  GROUP BY seg ORDER BY n DESC LIMIT 12""").fetchall()
print(f"  top_dirs SQL GROUP BY: {time.monotonic()-t0:.1f}s  -> {[(x[0],x[1]) for x in r][:3]}")

t0=time.monotonic()
px = conn.execute("SELECT COUNT(*) FROM path_index").fetchone()[0]
print(f"  path_index ({px}): {time.monotonic()-t0:.1f}s")

t0=time.monotonic()
b = dir_size_bytes(cfg.path("paths.nvme_root"))
print(f"  dir_size_bytes(nvme) = {b/1e9:.1f} GB: {time.monotonic()-t0:.1f}s   ← 嫌疑")
conn.close()
