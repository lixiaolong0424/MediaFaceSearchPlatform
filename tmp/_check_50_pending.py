import sqlite3

conn = sqlite3.connect(r'data\library.db')
conn.row_factory = sqlite3.Row

print("=== photo status='pending' 的 50 个到底什么状态 ===")
rows = list(conn.execute(
    "SELECT f.file_id, f.path, f.size FROM files f WHERE f.kind='photo' AND f.status='pending' "
    "ORDER BY f.file_id LIMIT 12"))
for r in rows:
    st = conn.execute("SELECT stage,status FROM stage_status WHERE file_id=?", (r["file_id"],)).fetchall()
    enc = conn.execute("SELECT model,status FROM file_encoding WHERE file_id=?", (r["file_id"],)).fetchall()
    print(f"  fid={r['file_id']:<7} size={r['size']/1e6:>6.2f}MB stage={[(x['stage'],x['status']) for x in st]} "
          f"enc={[(x['model'],x['status']) for x in enc]}")

print("\n=== 汇总：pending 照片里按 extract 阶段状态分组 ===")
q = """
SELECT COALESCE(ss.status,'<无>') AS ex, COUNT(*) n
  FROM files f LEFT JOIN stage_status ss
    ON ss.file_id=f.file_id AND ss.stage='extract'
 WHERE f.kind='photo' AND f.status='pending'
 GROUP BY ex
"""
for r in conn.execute(q):
    print(f"  extract={r['ex']:<10} {r['n']:,}")

print("\n=== error 照片里按 extract 阶段状态分组 ===")
q2 = """
SELECT COALESCE(ss.status,'<无>') AS ex, COUNT(*) n
  FROM files f LEFT JOIN stage_status ss
    ON ss.file_id=f.file_id AND ss.stage='extract'
 WHERE f.kind='photo' AND f.status='error'
 GROUP BY ex
"""
for r in conn.execute(q2):
    print(f"  extract={r['ex']:<10} {r['n']:,}")

print("\n=== 视频队列 ===")
print("  video pending:", conn.execute(
    "SELECT COUNT(*) FROM files WHERE kind='video' AND status='pending'").fetchone()[0])
conn.close()
