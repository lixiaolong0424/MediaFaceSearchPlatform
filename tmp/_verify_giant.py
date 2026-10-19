"""验证 max_image_pixels 提到 9 亿后，771MP 那批巨图能不能处理。

只做一次真实处理并回滚，不落库，避免污染统计。
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')

from src import db as D
from src.common import load_config
from src.dedup_run import DedupRunner
from src.extract import ensure_stage_table, process_photo

cfg = load_config(None, None)
print("max_image_pixels =", cfg.get("scan.max_image_pixels"))
conn = D.connect(cfg.db_path, cfg=cfg)
ensure_stage_table(conn)

# 找几张 771MP 的（之前 error 的）
rows = list(conn.execute(
    "SELECT f.file_id AS fid, f.path AS path, fe.error AS err FROM files f "
    "JOIN file_encoding fe ON fe.file_id=f.file_id AND fe.model='semantic' "
    "WHERE f.status='error' AND fe.error LIKE '%max_image_pixels%' LIMIT 3"))
print(f"找到 {len(rows)} 张超限图")
for r in rows:
    p = Path(str(r["path"]))
    print(f"\n  fid={r['fid']}  {p.stat().st_size/1e6:.1f}MB  {p.name[:40]}")
    t0 = time.monotonic()
    runner = DedupRunner(hash_size=8, threshold=8)
    res = process_photo(p, int(r["fid"]), cfg, conn, runner, write_cache=True)
    el = time.monotonic() - t0
    cache = cfg.path("paths.frames") / str(r["fid"]) / "0000_000000000.jpg"
    print(f"    → status={res.status}  {el:.1f}s  cache={cache.exists()} "
          f"{cache.stat().st_size//1024 if cache.exists() else 0}KB")
    if res.error:
        print(f"    error={res.error[:80]}")
    conn.rollback()

print("\n=== 顺便统计：还剩多少张超限 / 多少张真坏 ===")
n_big = conn.execute(
    "SELECT COUNT(*) FROM file_encoding WHERE model='semantic' AND status='error' "
    "AND error LIKE '%max_image_pixels%'").fetchone()[0]
n_bad = conn.execute(
    "SELECT COUNT(*) FROM file_encoding WHERE model='semantic' AND status='error' "
    "AND error LIKE '%cannot identify image file%'").fetchone()[0]
n_other = conn.execute(
    "SELECT COUNT(*) FROM file_encoding WHERE model='semantic' AND status='error' "
    "AND error NOT LIKE '%max_image_pixels%' AND error NOT LIKE '%cannot identify%'"
).fetchone()[0]
print(f"  超像素上限（新上限下应能处理）= {n_big}")
print(f"  真·无法识别（重试 3 次后入死信）= {n_bad}")
print(f"  其它错误 = {n_other}")
print(f"  dead_letter 现有行数 = {conn.execute('SELECT COUNT(*) FROM dead_letter').fetchone()[0]}")
conn.close()
