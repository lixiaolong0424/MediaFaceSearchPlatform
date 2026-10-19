import sqlite3
c = sqlite3.connect(r'data\library.db'); c.row_factory = sqlite3.Row
for i in (3181, 13769, 31089, 34113):
    e = c.execute("SELECT model,status FROM file_encoding WHERE file_id=?", (i,)).fetchall()
    s = c.execute("SELECT stage,status FROM stage_status WHERE file_id=?", (i,)).fetchall()
    print(i, "enc=", [tuple(x) for x in e], "stage=", [tuple(x) for x in s])
print("semantic stage rows done:", c.execute("SELECT COUNT(*) FROM stage_status WHERE stage='semantic_encode' AND status='done'").fetchone()[0])
c.close()
