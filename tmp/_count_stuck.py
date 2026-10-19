"""量化 repair 造成的「夹缝」文件：抽帧已完成、编码待做，但 files.status 被改成了 pending。

后果：三条路都进不去
  * 03_extract：`stage_status(extract)='done'` → 被排除
  * 04_encode / 07_face_encode：要求 `files.status='done'` → 被排除
  * 05/06/07 的目标选择同理
即这些文件永远编码不了。
"""

import sqlite3

conn = sqlite3.connect(r'data\library.db')
conn.row_factory = sqlite3.Row

sql = """
SELECT COUNT(*) n FROM files f
  JOIN stage_status ex ON ex.file_id=f.file_id AND ex.stage='extract' AND ex.status='done'
  JOIN file_encoding fe ON fe.file_id=f.file_id AND fe.model=? AND fe.status='pending'
 WHERE f.status='pending'
"""
for model in ("semantic", "face"):
    n = conn.execute(sql, (model,)).fetchone()["n"]
    print(f"  {model}: 抽帧完成 + 编码 pending + files.status=pending  →  {n:,} 个（卡在夹缝里）")

print("\n对照：正常待编码（files.status='done'）")
for model in ("semantic", "face"):
    n = conn.execute(
        "SELECT COUNT(*) n FROM files f JOIN stage_status ex "
        "ON ex.file_id=f.file_id AND ex.stage='extract' AND ex.status='done' "
        "JOIN file_encoding fe ON fe.file_id=f.file_id AND fe.model=? AND fe.status='pending' "
        "WHERE f.status='done'", (model,)).fetchone()["n"]
    print(f"  {model}: {n:,}")

print("\n全部 files.status 分布：")
for r in conn.execute("SELECT status, COUNT(*) n FROM files GROUP BY status"):
    print(f"  {r['status']:<12} {r['n']:,}")
conn.close()
