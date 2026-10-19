"""把「只是因为超过像素上限而失败」的文件重新放行。

背景：`select_targets` 用死信表计数做闸门（同一文件抽帧失败 >= max_extract_attempts
次就不再选它）。这条闸门挡住了 10-18 的无限空转，但也意味着：
一旦把 `scan.max_image_pixels` 调大，那批**本来能处理**的巨图仍然被闸门挡在外面，
永远不会被重试。

所以调大上限之后必须显式放行它们：
  1) 清掉这些文件的 extract 死信行（让计数归零）
  2) files.status 置回 pending
  3) 删掉 extract 阶段行（让 03_extract 重新选中）

真·无法识别的（cannot identify image file）**不放行** —— 它们重试多少次都一样，
留在死信表里是可查的证据。
"""

import sys

sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')

from src import db as D
from src.common import load_config, local_now_iso

cfg = load_config(None, None)
conn = D.connect(cfg.db_path, cfg=cfg)

rows = list(conn.execute(
    "SELECT DISTINCT f.file_id AS fid FROM files f "
    "JOIN file_encoding fe ON fe.file_id=f.file_id AND fe.model='semantic' "
    "WHERE fe.status='error' AND fe.error LIKE '%max_image_pixels%'"))
ids = [int(r["fid"]) for r in rows]
print(f"因像素上限失败的文件：{len(ids)} 个")
print(f"  其中已被死信表拦住的："
      f"{conn.execute('SELECT COUNT(DISTINCT file_id) FROM dead_letter').fetchone()[0]} 个（全库）")

if not ids:
    print("无需处理")
    raise SystemExit(0)

now = local_now_iso()
n_dl = 0
for i in range(0, len(ids), 500):
    chunk = ids[i:i + 500]
    q = ",".join("?" * len(chunk))
    n_dl += conn.execute(
        f"DELETE FROM dead_letter WHERE stage='extract' AND file_id IN ({q})", chunk).rowcount
    conn.execute(f"UPDATE files SET status='pending', updated_at=? WHERE file_id IN ({q})",
                 (now, *chunk))
    conn.execute(f"DELETE FROM stage_status WHERE stage='extract' AND file_id IN ({q})", chunk)
    # 编码侧的错误也清掉，等抽帧成功后由 04_encode 重新入队
    conn.execute(f"UPDATE file_encoding SET status='pending', error=NULL "
                 f"WHERE model='semantic' AND file_id IN ({q})", chunk)
    conn.execute(f"UPDATE file_encoding SET status='pending', error=NULL "
                 f"WHERE model='face' AND file_id IN ({q})", chunk)
D.retry_on_lock(conn.commit)
print(f"放行完成：清掉 {n_dl} 条 extract 死信行，{len(ids)} 个文件置回 pending")

print("\n放行后待抽帧数（photo）:", conn.execute(
    "SELECT COUNT(*) FROM files WHERE kind='photo' AND status IN ('pending','error')").fetchone()[0])
print("仍被死信拦住的文件数:", conn.execute(
    "SELECT COUNT(DISTINCT file_id) FROM dead_letter WHERE stage='extract'").fetchone()[0])
conn.close()
