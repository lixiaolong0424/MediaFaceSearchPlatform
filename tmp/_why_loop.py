"""查清为什么链会无限循环：50 个 pending + 1,506 个 error 为什么永远处理不掉。"""

import sqlite3
from pathlib import Path

conn = sqlite3.connect(r'data\library.db')
conn.row_factory = sqlite3.Row

print("=== 50 个 pending 照片：为什么抽帧选不到它们 ===")
rows = list(conn.execute(
    "SELECT file_id, path, size FROM files WHERE kind='photo' AND status='pending' LIMIT 5"))
for r in rows:
    st = conn.execute("SELECT stage, status FROM stage_status WHERE file_id=?",
                      (r["file_id"],)).fetchall()
    enc = conn.execute("SELECT model,status FROM file_encoding WHERE file_id=?",
                       (r["file_id"],)).fetchall()
    p = Path(str(r["path"]))
    print(f"  fid={r['file_id']} exists={p.exists()} size={r['size']}")
    print(f"      stage={[(x['stage'], x['status']) for x in st]}")
    print(f"      enc  ={[(x['model'], x['status']) for x in enc]}")

print("\n=== 1,506 个 error 照片：错误原因分布 ===")
from collections import Counter
c = Counter()
for r in conn.execute(
    "SELECT f.file_id AS fid, fe.error AS err FROM files f "
    "JOIN file_encoding fe ON fe.file_id=f.file_id AND fe.model='semantic' "
    "WHERE f.status='error'"):
    e = (r["err"] or "?")
    key = e[:78]
    c[key] += 1
for k, n in c.most_common(12):
    print(f"  {n:>5}  {k}")

print("\n=== 这些 error 文件的体积/存在性 ===")
n_noexist = 0
n_big = 0
tot = 0
for r in conn.execute("SELECT file_id, path, size FROM files WHERE status='error'"):
    tot += 1
    p = Path(str(r["path"]))
    if not p.exists():
        n_noexist += 1
    elif (r["size"] or 0) > 50_000_000:
        n_big += 1
print(f"  共 {tot}；路径不存在 {n_noexist}；>50MB {n_big}")

print("\n=== face pending 187 ===")
print("  ", conn.execute(
    "SELECT COUNT(*) FROM file_encoding WHERE model='face' AND status='pending'").fetchone()[0])
conn.close()
