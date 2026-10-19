import sqlite3

c = sqlite3.connect(r'data\library.db')
print("file_encoding by model/status:")
for r in c.execute("SELECT model,status,COUNT(*) FROM file_encoding GROUP BY model,status"):
    print("  ", tuple(r))
rows = [r[0] for r in c.execute(
    "SELECT file_id FROM file_encoding WHERE model='semantic' AND status IN ('pending','error')")]
print("semantic pending/error (repair targets): count =", len(rows), rows[:20])
rows2 = [r[0] for r in c.execute(
    "SELECT file_id FROM file_encoding WHERE model='face' AND status IN ('pending','error')")]
print("face pending/error: count =", len(rows2), rows2[:20])
c.close()
