import sqlite3
from collections import Counter
c = sqlite3.connect(r'data\library.db')
cnt = Counter()
for r in c.execute("SELECT fe.error FROM files f JOIN file_encoding fe ON fe.file_id=f.file_id AND fe.model='semantic' WHERE f.kind='video' AND f.status='error'"):
    e = (r[0] or "?")
    cnt[e[:70]] += 1
for k, n in cnt.most_common(8):
    print(f"  {n:>4}  {k}")
print("  video error 总数:", c.execute("SELECT COUNT(*) FROM files WHERE kind='video' AND status='error'").fetchone()[0])
c.close()
