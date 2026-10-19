import sqlite3

conn = sqlite3.connect(r'data\library.db')
conn.row_factory = sqlite3.Row

print("=== files.status 分布 ===")
for r in conn.execute("SELECT status, kind, COUNT(*) n FROM files GROUP BY status, kind ORDER BY n DESC"):
    print(f"  {r['status']:<12} {r['kind']:<6} {r['n']:>8,}")

print("\n=== 之前诊断出的那几条现在怎样了 ===")
for fid in (41073, 41075, 41076):
    f = conn.execute("SELECT file_id, status, kind FROM files WHERE file_id=?", (fid,)).fetchone()
    st = conn.execute("SELECT stage, status FROM stage_status WHERE file_id=?", (fid,)).fetchall()
    print(f"  fid={fid}: files.status={f['status'] if f else '?'} "
          f"stage={[(x['stage'], x['status']) for x in st]}")

print("\n=== pending 照片到底有多少、它们的 stage 情况 ===")
rows = list(conn.execute(
    "SELECT f.file_id AS fid FROM files f WHERE f.kind='photo' AND f.status='pending' LIMIT 20"))
print(f"  样本 {len(rows)} 条：")
for r in rows[:8]:
    st = conn.execute("SELECT stage, status FROM stage_status WHERE file_id=? AND stage='extract'",
                      (r["fid"],)).fetchone()
    print(f"    fid={r['fid']} extract_stage={'无' if not st else st['status']}")

print("\n=== 计数交叉核对 ===")
q = """
SELECT
  (SELECT COUNT(*) FROM files WHERE kind='photo' AND status='pending') AS p1,
  (SELECT COUNT(*) FROM files f JOIN stage_status ex ON ex.file_id=f.file_id
     AND ex.stage='extract' AND ex.status='done'
     WHERE f.kind='photo' AND f.status='pending') AS p2
"""
r = conn.execute(q).fetchone()
print(f"  pending 照片={r['p1']}，其中抽帧已 done 的={r['p2']}")
conn.close()
