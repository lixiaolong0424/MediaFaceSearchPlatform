import sqlite3
from pathlib import Path
c = sqlite3.connect(r'data\library.db'); c.row_factory = sqlite3.Row
rows = list(c.execute("SELECT file_id,path,size FROM files WHERE kind='photo' AND status='pending' ORDER BY file_id LIMIT 6"))
for r in rows:
    st = c.execute("SELECT stage,status FROM stage_status WHERE file_id=?", (r["file_id"],)).fetchall()
    print(f"  fid={r['file_id']:<7} {r['size']/1e6:>7.1f}MB stage={[(x['stage'],x['status']) for x in st]}")
n = c.execute("SELECT COUNT(*), AVG(size) FROM files WHERE kind='photo' AND status='pending'").fetchone()
print(f"  合计 {n[0]} 个，平均 {n[1]/1e6:.1f} MB")
print("\n=== 207 个 error 的错误类型 ===")
for r in c.execute("SELECT ss.error, COUNT(*) n FROM files f JOIN stage_status ss ON ss.file_id=f.file_id AND ss.stage='extract' WHERE f.kind='photo' AND f.status='error' GROUP BY 1 ORDER BY n DESC LIMIT 4"):
    print(f"  {r['n']:>5}  {str(r['error'])[:78]}")
c.close()
