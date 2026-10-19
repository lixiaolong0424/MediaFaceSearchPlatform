import sqlite3

conn = sqlite3.connect(r'data\library.db')

print("=== 文件状态 ===")
for r in conn.execute("SELECT kind,status,COUNT(*) n FROM files "
                      "GROUP BY kind,status ORDER BY kind,n DESC"):
    print(f"  {r[0]:<6} {r[1]:<12} {r[2]:>8,}")

print("\n=== 各阶段完成数 ===")
for r in conn.execute("SELECT stage,status,COUNT(*) n FROM stage_status "
                      "GROUP BY stage,status ORDER BY stage,n DESC"):
    print(f"  {r[0]:<16} {r[1]:<8} {r[2]:>8,}")

faces = conn.execute("SELECT COUNT(*) FROM face_records "
                     "WHERE status='alive' AND has_vector=1").fetchone()[0]
shots = conn.execute("SELECT COUNT(*) FROM shots").fetchone()[0]
dead = conn.execute("SELECT COUNT(DISTINCT file_id) FROM dead_letter "
                    "WHERE stage='extract'").fetchone()[0]
sem = conn.execute("SELECT COUNT(*) FROM file_encoding "
                   "WHERE model='semantic' AND status='done'").fetchone()[0]
fenc = conn.execute("SELECT COUNT(*) FROM file_encoding "
                    "WHERE model='face' AND status='done'").fetchone()[0]
print("\n=== 其它 ===")
print(f"  照片墙人脸(有向量) : {faces:,}")
print(f"  镜头总数           : {shots:,}")
print(f"  抽帧已放弃(死信)   : {dead:,}")
print(f"  语义编码 done      : {sem:,}")
print(f"  人脸编码 done      : {fenc:,}")
conn.close()
