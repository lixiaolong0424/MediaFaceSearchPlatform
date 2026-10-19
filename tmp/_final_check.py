import sqlite3
conn = sqlite3.connect(r'data\library.db')
q = """
SELECT
 (SELECT COUNT(*) FROM files WHERE kind='video' AND status='done') AS done_v,
 (SELECT COUNT(*) FROM files f WHERE f.kind='video' AND f.status='done'
    AND NOT EXISTS (SELECT 1 FROM shots s WHERE s.file_id=f.file_id)) AS done_no_shots,
 (SELECT COUNT(*) FROM files f WHERE f.kind='photo' AND f.status='done'
    AND NOT EXISTS (SELECT 1 FROM shots s WHERE s.file_id=f.file_id)) AS photo_no_shots,
 (SELECT COUNT(*) FROM face_records WHERE status='alive' AND has_vector=1) AS faces,
 (SELECT COUNT(*) FROM file_encoding WHERE model='semantic' AND status='done') AS sem_done
"""
r = conn.execute(q).fetchone()
print(f"  video done={r[0]:,}  其中没抽到帧={r[1]}   photo done 但无镜头={r[2]}")
print(f"  墙上人脸(有向量)={r[3]:,}   语义向量={r[4]:,}")
conn.close()
