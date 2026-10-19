import sqlite3
from datetime import datetime
conn = sqlite3.connect(r'data\library.db'); conn.row_factory = sqlite3.Row
now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
# 剩下 8 个：抽帧+语义编码都完成，只缺人脸阶段。置 done 让 06/07 能看见它们。
rows = list(conn.execute("SELECT file_id FROM files WHERE kind='photo' AND status='pending'"))
for r in rows:
    fid = int(r["file_id"])
    st = {str(x["stage"]): str(x["status"]) for x in conn.execute(
        "SELECT stage,status FROM stage_status WHERE file_id=?", (fid,))}
    if st.get("extract") == "done":
        conn.execute("UPDATE files SET status='done', updated_at=? WHERE file_id=?", (now, fid))
conn.commit()
print("photo status:")
for x in conn.execute("SELECT status, COUNT(*) n FROM files WHERE kind='photo' GROUP BY status"):
    print(f"  {x['status']:<12} {x['n']:,}")
print("video status:")
for x in conn.execute("SELECT status, COUNT(*) n FROM files WHERE kind='video' GROUP BY status"):
    print(f"  {x['status']:<12} {x['n']:,}")
conn.close()
