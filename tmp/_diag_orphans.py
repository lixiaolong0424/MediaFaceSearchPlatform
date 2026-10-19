import sqlite3, sys
sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')
ids = [3181,3183,3184,3187,3188,3191,3192,3193,13769,23867,31089,31091,31093]
c = sqlite3.connect(r'data\library.db'); c.row_factory = sqlite3.Row
print("file status / phash / kind_hint:")
for i in ids:
    r = c.execute("SELECT file_id,status,visible,kind_hint,phash FROM files WHERE file_id=?", (i,)).fetchone()
    e = c.execute("SELECT model,status FROM file_encoding WHERE file_id=?", (i,)).fetchall()
    s = c.execute("SELECT stage,status FROM stage_status WHERE file_id=?", (i,)).fetchall()
    print(f"  {i}: {dict(r) if r else None} enc={[tuple(x) for x in e]} stage={[tuple(x) for x in s]}")
print()
print("demoted count in extract logs:")
import re, pathlib
for p in sorted(pathlib.Path('logs').glob('chain_r*_extract_photo.log')):
    t = p.read_text(encoding='utf-8', errors='replace')
    m = re.findall(r'让位=(\d+)', t)
    if m: print(f"  {p.name}: last 让位={m[-1]}")
c.close()
