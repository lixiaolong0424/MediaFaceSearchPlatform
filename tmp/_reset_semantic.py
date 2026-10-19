import sqlite3
import sys
sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')
from src.common import load_config
from src.vector_store import open_store, semantic_collection

cfg = load_config(None, None)
conn = sqlite3.connect(str(cfg.db_path))
before = conn.execute("SELECT COUNT(*) FROM file_encoding WHERE model='semantic'").fetchone()[0]
store = open_store(cfg)
coll = semantic_collection(cfg)
print("collection:", coll, "points before:", store.count(coll))
try:
    store.client.delete_collection(coll)
    print("dropped", coll)
except Exception as exc:
    print("drop failed:", exc)
conn.execute("DELETE FROM file_encoding WHERE model='semantic'")
conn.execute("DELETE FROM stage_status WHERE stage='semantic_encode'")
conn.commit()
print("semantic file_encoding rows removed:", before)
for r in conn.execute("SELECT status, COUNT(*) FROM files WHERE kind='photo' GROUP BY status"):
    print("  photo", tuple(r))
conn.close()
