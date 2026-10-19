import sqlite3, sys, time
from pathlib import Path
sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')
from src import db as D
from src.common import load_config
from src.extract import process_photo, ensure_stage_table, STAGE_EXTRACT
from src.dedup_run import DedupRunner
import logging
logging.basicConfig(level=logging.WARNING)

cfg = load_config(None, None)
conn = D.connect(cfg.db_path, cfg=cfg)
ensure_stage_table(conn)
ids = [78326, 78327, 78328]
for fid in ids:
    p = conn.execute("SELECT path FROM files WHERE file_id=?", (fid,)).fetchone()["path"]
    r = DedupRunner(hash_size=8, threshold=8)
    t0 = time.monotonic()
    res = process_photo(Path(p), fid, cfg, conn, r, write_cache=True)
    el = time.monotonic() - t0
    cache = cfg.path("paths.frames") / str(fid) / "0000_000000000.jpg"
    ph = conn.execute("SELECT phash FROM files WHERE file_id=?", (fid,)).fetchone()["phash"]
    print(f"  fid={fid} status={res.status} err={res.error[:60]!r} "
          f"{el:.2f}s phash={ph} cache={cache.exists()} "
          f"cacheKB={cache.stat().st_size//1024 if cache.exists() else 0}")
    conn.rollback()   # 只做验证，不落库
conn.close()
