"""恢复后核对：状态分布、检索可见性、照片墙可见性。"""

import sqlite3
import sys

sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')

from src import db as D
from src import ui_wall
from src.common import load_config

cfg = load_config(None, None)
conn = sqlite3.connect(r'data\library.db')
conn.row_factory = sqlite3.Row

print("=== files.status 分布 ===")
tot = 0
for r in conn.execute("SELECT kind,status,COUNT(*) n FROM files GROUP BY kind,status ORDER BY kind,n DESC"):
    print(f"  {r['kind']:<6} {r['status']:<12} {r['n']:>8,}")
    tot += r["n"]
print(f"  合计 {tot:,}")

print("\n=== 关键计数 ===")
for label, sql in (
    ("skipped_dup 已恢复", "SELECT COUNT(*) FROM files WHERE status='skipped_dup'"),
    ("done 照片", "SELECT COUNT(*) FROM files WHERE kind='photo' AND status='done'"),
    ("done 视频", "SELECT COUNT(*) FROM files WHERE kind='video' AND status='done'"),
    ("missing（F: 那批）", "SELECT COUNT(*) FROM files WHERE status='missing'"),
    ("语义编码 done", "SELECT COUNT(*) FROM file_encoding WHERE model='semantic' AND status='done'"),
    ("人脸有向量", "SELECT COUNT(*) FROM face_records WHERE status='alive' AND has_vector=1"),
):
    print(f"  {label:<20} {conn.execute(sql).fetchone()[0]:>9,}")
conn.close()

# 走真实代码路径验证可见性
rconn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
st = D.library_stats(rconn, str(cfg.media_root))
print("\n=== library_stats（界面读的就是它）===")
for k in ("files_total", "files_done", "files_missing", "photos", "photos_done",
          "videos", "videos_done", "videos_pending", "shots", "faces", "next_step"):
    print(f"  {k:<16} {st.get(k)}")

page = ui_wall.wall_page(rconn, cfg, limit=5, mode="tile", min_quality=0.0)
print(f"\n=== 照片墙 ===\n  total={page['total']:,} 本页 {len(page['items'])} 条")
for it in page["items"][:3]:
    print(f"    face#{it['face_id']} fid={it['file_id']} q={it['quality']:.3f} {it['timecode']}")

fac = ui_wall.wall_facets(rconn, cfg)
print(f"  时间范围 {fac['date_range'][0]} → {fac['date_range'][1]}")
rconn.close()
