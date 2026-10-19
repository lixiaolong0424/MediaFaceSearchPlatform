import sqlite3

conn = sqlite3.connect(r'data\library.db')

done = conn.execute("SELECT COUNT(*) FROM files WHERE kind='video' AND status='done'").fetchone()[0]
sem = conn.execute(
    "SELECT COUNT(*) FROM files f JOIN file_encoding fe "
    "ON fe.file_id=f.file_id AND fe.model='semantic' AND fe.status='done' "
    "WHERE f.kind='video' AND f.status='done'").fetchone()[0]
fac = conn.execute(
    "SELECT COUNT(*) FROM files f JOIN file_encoding fe "
    "ON fe.file_id=f.file_id AND fe.model='face' AND fe.status='done' "
    "WHERE f.kind='video' AND f.status='done'").fetchone()[0]
noenc = conn.execute(
    "SELECT COUNT(*) FROM files f WHERE f.kind='video' AND f.status='done' "
    "AND NOT EXISTS (SELECT 1 FROM file_encoding fe "
    "                WHERE fe.file_id=f.file_id AND fe.model='semantic')").fetchone()[0]
with_shots = conn.execute(
    "SELECT COUNT(DISTINCT s.file_id) FROM shots s JOIN files f ON f.file_id=s.file_id "
    "WHERE f.kind='video'").fetchone()[0]
with_fd = conn.execute(
    "SELECT COUNT(*) FROM stage_status ss JOIN files f ON f.file_id=ss.file_id "
    "WHERE f.kind='video' AND ss.stage='face_detect' AND ss.status='done'").fetchone()[0]
nshots = conn.execute(
    "SELECT COUNT(*) FROM shots s JOIN files f ON f.file_id=s.file_id "
    "WHERE f.kind='video'").fetchone()[0]

print(f"video status='done'（抽帧完成）  : {done:,}")
print(f"  有 shots 记录的视频           : {with_shots:,}   （{nshots:,} 个镜头）")
print(f"  其中 semantic 编码 done       : {sem:,}")
print(f"  其中 face 编码 done           : {fac:,}")
print(f"  其中 face_detect 阶段 done    : {with_fd:,}")
print(f"  其中连编码记录都还没有        : {noenc:,}")
conn.close()
