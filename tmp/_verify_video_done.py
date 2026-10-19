"""核查「video done」是否真实：这些视频有没有真的抽到帧、有没有编码入库。

如果只是 status 被某个对账命令改成了 done，而 shots/frames/向量都没有，
那就是假完成 —— 界面上会出现点开很慢、预览为空、检索命中却无缩略图的记录。
"""

import sqlite3
from pathlib import Path

conn = sqlite3.connect(r'data\library.db')
conn.row_factory = sqlite3.Row

print("=== video 状态分布 ===")
for r in conn.execute("SELECT status, COUNT(*) n FROM files WHERE kind='video' GROUP BY status"):
    print(f"  {r['status']:<12} {r['n']:>7,}")

print("\n=== 'done' 视频的真实性抽样（20 个）===")
rows = list(conn.execute(
    "SELECT file_id, path, size, duration_sec FROM files "
    "WHERE kind='video' AND status='done' ORDER BY file_id LIMIT 20"))
real = 0
for r in rows:
    fid = int(r["file_id"])
    nshot = conn.execute("SELECT COUNT(*) FROM shots WHERE file_id=?", (fid,)).fetchone()[0]
    d = Path("data/nvme/frames") / str(fid)
    nframe = len(list(d.glob("*.jpg"))) if d.exists() else 0
    enc = conn.execute(
        "SELECT model,status FROM file_encoding WHERE file_id=? AND status='done'",
        (fid,)).fetchall()
    ok = nshot > 0 and nframe > 0
    real += ok
    print(f"  fid={fid:<7} {r['size']/1e6:>7.2f}MB dur={r['duration_sec'] or 0:>7.1f}s "
          f"shots={nshot:<3} frames={nframe:<3} enc={[x['model'] for x in enc]} "
          f"{'✓' if ok else '✗ 假完成'}")

print(f"\n  20 个样本里真实完成的 = {real}/20")

print("\n=== 全库口径核对 ===")
q = """
SELECT
 (SELECT COUNT(*) FROM files WHERE kind='video' AND status='done') AS done_v,
 (SELECT COUNT(DISTINCT file_id) FROM shots s JOIN files f ON f.file_id=s.file_id
    WHERE f.kind='video') AS with_shots,
 (SELECT COUNT(*) FROM files f WHERE f.kind='video' AND f.status='done'
    AND NOT EXISTS (SELECT 1 FROM shots s WHERE s.file_id=f.file_id)) AS done_no_shots
"""
r = conn.execute(q).fetchone()
print(f"  video done = {r['done_v']:,}")
print(f"  有 shots 的 video = {r['with_shots']:,}")
print(f"  done 但一帧都没抽到 = {r['done_no_shots']:,}   ← 应为 0")
conn.close()
