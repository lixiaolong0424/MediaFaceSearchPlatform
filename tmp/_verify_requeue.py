import sqlite3

c = sqlite3.connect(r'data\library.db')
print("files.status=error:", c.execute("SELECT COUNT(*) FROM files WHERE status='error'").fetchone()[0])
print("photo pending:", c.execute("SELECT COUNT(*) FROM files WHERE kind='photo' AND status='pending'").fetchone()[0])
print("stage rows for 78326:", c.execute("SELECT stage,status FROM stage_status WHERE file_id=78326").fetchall())
print("dead_letter:", c.execute("SELECT COUNT(*) FROM dead_letter").fetchone()[0])
print("file_encoding by model/status:")
for r in c.execute("SELECT model,status,COUNT(*) FROM file_encoding GROUP BY model,status"):
    print("  ", tuple(r))
c.close()
