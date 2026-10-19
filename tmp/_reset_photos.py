import sqlite3
c = sqlite3.connect(r'data\library.db')
c.execute("PRAGMA busy_timeout=30000")
tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
print("has stage_status:", "stage_status" in tables)
c.execute("UPDATE files SET status='pending', visible=1, phash=NULL, kind_hint=NULL WHERE kind='photo'")
c.execute("DELETE FROM file_encoding")
if "stage_status" in tables:
    c.execute("DELETE FROM stage_status")
c.commit()
for r in c.execute("SELECT kind, status, COUNT(*) FROM files GROUP BY kind, status"):
    print("  ", tuple(r))
print("file_encoding rows:", c.execute("SELECT COUNT(*) FROM file_encoding").fetchone()[0])
print("phash non-null:", c.execute("SELECT COUNT(*) FROM files WHERE phash IS NOT NULL").fetchone()[0])
c.close()
