import sqlite3
con = sqlite3.connect(r"file:D:/DATA/PROJECT/建设多媒体整理系统/data/library.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row
print("① 各阶段进度（断点续跑标记）")
for r in con.execute("SELECT stage,status,COUNT(*) n FROM stage_status GROUP BY stage,status ORDER BY stage,n DESC"):
    print(f"   {r['stage']:<16}{r['status']:<10}{r['n']:>9,}")
print("\n② 两阶段入库（铁律 3）")
for r in con.execute("SELECT model,status,COUNT(*) n FROM file_encoding GROUP BY model,status ORDER BY model,n DESC"):
    print(f"   {r['model']:<10}{r['status']:<13}{r['n']:>9,}")
print("\n③ 按路径找 file_id")
q = "%IMG_0579%"
for r in con.execute("SELECT file_id,status,kind,width,height FROM files WHERE path LIKE ? LIMIT 3",(q,)):
    print(f"   file_id={r['file_id']} {r['status']} {r['kind']} {r['width']}x{r['height']}")
print("\n④ 质量最高的 3 张脸")
for r in con.execute("SELECT face_id,file_id,quality,sec FROM face_records WHERE status='alive' ORDER BY quality DESC LIMIT 3"):
    print(f"   face_id={r['face_id']:<7} file_id={r['file_id']:<7} quality={r['quality']:.4f} sec={r['sec']}")
print("\n⑤ 死信里的失败原因（前 3）")
for r in con.execute("SELECT stage,COUNT(*) n FROM dead_letter GROUP BY stage"):
    print(f"   stage={r['stage']}  {r['n']:,} 条")
con.close()
