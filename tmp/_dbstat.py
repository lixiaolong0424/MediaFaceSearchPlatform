import sqlite3, sys
c = sqlite3.connect(r'data\library.db'); c.row_factory = sqlite3.Row
print("kind x status:")
for r in c.execute("SELECT kind, status, COUNT(*) n FROM files GROUP BY kind, status ORDER BY n DESC"):
    print(f"  {r['kind']:<8} {r['status']:<10} {r['n']:>8,}")
print("total:", c.execute("SELECT COUNT(*) FROM files").fetchone()[0])
print("size GB:", round(c.execute("SELECT SUM(size) FROM files").fetchone()[0]/1e9,1))
print("top dirs:")
for r in c.execute("SELECT rel_path FROM files LIMIT 0"): pass
import collections
cnt = collections.Counter()
for (rp,) in c.execute("SELECT rel_path FROM files"):
    seg = (rp or "").replace("/","\\").lstrip("\\").split("\\",1)[0]
    cnt[seg]+=1
for k,v in cnt.most_common(15):
    print(f"  {k:<40} {v:>8,}")
