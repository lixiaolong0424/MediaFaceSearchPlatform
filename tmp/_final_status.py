import sqlite3
c = sqlite3.connect(r'data\library.db'); c.row_factory = sqlite3.Row
print("=== 照片（已收尾）===")
for r in c.execute("SELECT status,COUNT(*) n FROM files WHERE kind='photo' GROUP BY status ORDER BY n DESC"):
    print(f"  {r['status']:<12} {r['n']:>8,}")
print("=== 视频（正在跑）===")
for r in c.execute("SELECT status,COUNT(*) n FROM files WHERE kind='video' GROUP BY status ORDER BY n DESC"):
    print(f"  {r['status']:<12} {r['n']:>8,}")
print("=== 已入库内容 ===")
print("  语义向量(semantic done):", f"{c.execute(chr(83)+'ELECT COUNT(*) FROM file_encoding WHERE model=' + chr(39)+'semantic'+chr(39) + ' AND status=' + chr(39)+'done'+chr(39)).fetchone()[0]:,}")
print("  人脸(with_vector):", f"{c.execute('SELECT COUNT(*) FROM face_records WHERE status=' + chr(39)+'alive'+chr(39) + ' AND has_vector=1').fetchone()[0]:,}")
print("  镜头 shots:", f"{c.execute('SELECT COUNT(*) FROM shots').fetchone()[0]:,}")
print("  死信 dead_letter:", f"{c.execute('SELECT COUNT(*) FROM dead_letter').fetchone()[0]:,}",
      "(其中抽帧阶段:", f"{c.execute('SELECT COUNT(*) FROM dead_letter WHERE stage=' + chr(39)+'extract'+chr(39)).fetchone()[0]:,})")
c.close()
