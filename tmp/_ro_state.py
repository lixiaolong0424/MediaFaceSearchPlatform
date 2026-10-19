import sqlite3, os
uri = "file:D:/DATA/PROJECT/建设多媒体整理系统/data/library.db?mode=ro"
c = sqlite3.connect(uri, uri=True, timeout=10)
c.row_factory = sqlite3.Row
def q(sql, *a):
    try: return list(c.execute(sql, a))
    except Exception as e: return [{"err": str(e)}]
print("== files by kind,status ==")
for r in q("SELECT kind,status,COUNT(*) n FROM files GROUP BY kind,status ORDER BY n DESC"):
    print("  ", dict(r))
print("== stage_status ==")
for r in q("SELECT stage,status,COUNT(*) n FROM stage_status GROUP BY stage,status ORDER BY stage,n DESC"):
    print("  ", dict(r))
print("== file_encoding ==")
for r in q("SELECT model,status,COUNT(*) n FROM file_encoding GROUP BY model,status ORDER BY model,n DESC"):
    print("  ", dict(r))
print("== shots / face_records ==")
print("  shots:", q("SELECT COUNT(*) n FROM shots")[0]["n"])
for r in q("SELECT status,has_vector,COUNT(*) n FROM face_records GROUP BY status,has_vector"):
    print("  face:", dict(r))
print("  face with thumb:", q("SELECT COUNT(*) n FROM face_records WHERE thumb_path IS NOT NULL")[0]["n"])
print("== db_version / libraries ==")
for r in q("SELECT * FROM db_version"): print("  ver", dict(r))
for r in q("SELECT * FROM libraries"): print("  lib", dict(r))
