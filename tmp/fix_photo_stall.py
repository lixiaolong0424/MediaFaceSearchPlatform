"""收尾修复：让照片阶段真正「结束」，循环才能推进到视频。

两件事：
  A. 1,506 个抽帧永远失败的文件 —— 给它们补 dead_letter 记录（attempt 已达上限），
     这样 03_extract 的取数条件会把它们挡在门外，不再每轮重试。
     它们本来就已经被重试了两百多轮，远超上限 3 次。
  B. 50 个 files.status='pending' 但**所有阶段都完成**的文件 —— 它们是历史遗留的
     「夹缝」状态（数据其实齐全），恢复成 done，否则 pending 永远不归零。

两者都只改状态记账，不动任何向量/缓存/人脸数据。
"""

import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')

conn = sqlite3.connect(r'data\library.db')
conn.row_factory = sqlite3.Row
now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

# ---------------------------------------------------------------- A
rows = list(conn.execute(
    "SELECT f.file_id, ss.error FROM files f "
    "LEFT JOIN stage_status ss ON ss.file_id=f.file_id AND ss.stage='extract' "
    "WHERE f.kind='photo' AND f.status='error'"))
print(f"A. 抽帧失败文件 {len(rows)} 个")
already = 0
added = 0
for r in rows:
    fid = int(r["file_id"])
    n = conn.execute("SELECT COUNT(*) FROM dead_letter WHERE file_id=? AND stage='extract'",
                     (fid,)).fetchone()[0]
    if n >= 3:
        already += 1
        continue
    err = (r["error"] or "历史遗留：抽帧反复失败")[:500]
    # 补到 3 次，标记为已耗尽
    for k in range(n, 3):
        conn.execute(
            "INSERT INTO dead_letter(file_id, stage, attempt, error, at) VALUES (?,?,?,?,?)",
            (fid, "extract", k + 1, f"{err}（补记：该文件已在本轮之前反复失败）", now))
    added += 1
print(f"   补记 dead_letter：{added} 个（已有记录跳过 {already} 个）")

# ---------------------------------------------------------------- B
pend = list(conn.execute(
    "SELECT f.file_id FROM files f WHERE f.kind='photo' AND f.status='pending'"))
fixed = 0
skipped = []
for r in pend:
    fid = int(r["file_id"])
    st = {str(x["stage"]): str(x["status"]) for x in conn.execute(
        "SELECT stage,status FROM stage_status WHERE file_id=?", (fid,))}
    enc = {str(x["model"]): str(x["status"]) for x in conn.execute(
        "SELECT model,status FROM file_encoding WHERE file_id=?", (fid,))}
    # 只有「抽帧 + 两个模型编码都 done」才敢改成 done
    if (st.get("extract") == "done" and st.get("face_detect") == "done"
            and enc.get("semantic") == "done"):
        conn.execute("UPDATE files SET status='done', updated_at=? WHERE file_id=?", (now, fid))
        fixed += 1
    else:
        skipped.append((fid, st, enc))
print(f"B. pending 照片 {len(pend)} 个 → 恢复为 done {fixed} 个，未满足条件 {len(skipped)} 个")
for fid, st, enc in skipped[:5]:
    print(f"   跳过 fid={fid} stage={st} enc={enc}")

conn.commit()

# ---------------------------------------------------------------- 复查
print("\n=== 复查 ===")
for r in conn.execute("SELECT status, COUNT(*) n FROM files WHERE kind='photo' GROUP BY status"):
    print(f"  photo {r['status']:<12} {r['n']:,}")
print("  dead_letter(extract):", conn.execute(
    "SELECT COUNT(*) FROM dead_letter WHERE stage='extract'").fetchone()[0])
conn.close()
