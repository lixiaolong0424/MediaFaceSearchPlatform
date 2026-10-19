"""查清 repair 之后 stage_status 的真实状态。

要回答的问题：repair 只应该清掉**编码阶段**那一行（semantic_encode / face_encode），
不该把 `extract` 的 done 一起抹掉 —— 抹掉了就意味着这些文件要重新读一遍机械盘，
在 14TB 机械盘上是实打实的时间浪费。
"""

import sqlite3

conn = sqlite3.connect(r'data\library.db')
conn.row_factory = sqlite3.Row

for fid in (3181, 3183, 8651, 78326, 34113, 67855):
    row = conn.execute("SELECT file_id, status FROM files WHERE file_id=?", (fid,)).fetchone()
    st = conn.execute("SELECT stage, status FROM stage_status WHERE file_id=? ORDER BY stage",
                      (fid,)).fetchall()
    enc = conn.execute("SELECT model, status FROM file_encoding WHERE file_id=?", (fid,)).fetchall()
    cache = conn.execute("SELECT 1").fetchone()
    from pathlib import Path
    frames = Path(r'data\nvme\frames') / str(fid)
    ncache = len(list(frames.glob('*.jpg'))) if frames.exists() else 0
    print(f"fid={fid}: files.status={row['status'] if row else '?'}  "
          f"cache={ncache} 张")
    print(f"    stage: {[(r['stage'], r['status']) for r in st]}")
    print(f"    enc  : {[(r['model'], r['status']) for r in enc]}")

# 统计：有多少 pending 照片其实已经有缓存图（= 抽帧白做过，正在被重做）
n_pend_cache = 0
n_pend = 0
for r in conn.execute("SELECT file_id FROM files WHERE kind='photo' AND status='pending'"):
    fid = int(r["file_id"])
    n_pend += 1
    frames = __import__("pathlib").Path(r'data\nvme\frames') / str(fid)
    if frames.exists() and any(frames.glob('*.jpg')):
        n_pend_cache += 1
print(f"\npending 照片 {n_pend:,} 个，其中已有缓存图（说明抽帧其实做过）= {n_pend_cache:,}")
conn.close()
