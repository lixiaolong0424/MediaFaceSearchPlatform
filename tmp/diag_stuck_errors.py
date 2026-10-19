"""1,506 个「抽帧失败」到底是什么文件？以及为什么永远不会结束。

关键：`03_extract` 的取数条件是 `files.status IN ('pending','error')` 且 extract 阶段未 done，
**没有尝试次数上限**。于是失败的文件每一轮都会被再选一次、再失败一次，
链条的退出条件 `pending == 0` 永远不成立 → 空转。
"""

import sqlite3
import sys
from collections import Counter
from pathlib import Path

from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = None
sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')

conn = sqlite3.connect(r'data\library.db')
conn.row_factory = sqlite3.Row

rows = list(conn.execute(
    "SELECT f.file_id, f.path, f.size, ss.error FROM files f "
    "LEFT JOIN stage_status ss ON ss.file_id=f.file_id AND ss.stage='extract' "
    "WHERE f.kind='photo' AND f.status='error'"))
print(f"抽帧失败照片：{len(rows)} 个")

missing = 0
kinds = Counter()
samples = {}
for r in rows:
    p = Path(str(r["path"]))
    if not p.exists():
        missing += 1
        kinds["文件不存在"] += 1
        samples.setdefault("文件不存在", []).append(str(p)[:80])
        continue
    try:
        with Image.open(p) as im:
            im.size
        kinds["其实能打开"] += 1
        samples.setdefault("其实能打开", []).append(str(p)[:80])
    except Exception as e:
        k = f"{type(e).__name__}: {str(e)[:50]}"
        kinds[k] += 1
        samples.setdefault(k, []).append(str(p)[:80])

print("\n分类：")
for k, v in kinds.most_common():
    print(f"  {v:>6,}  {k}")
    for s in samples.get(k, [])[:2]:
        print(f"          例：{s}")

print("\n死信表（dead_letter）条数：",
      conn.execute("SELECT COUNT(*) FROM dead_letter").fetchone()[0])
print("说明：0 条 → 抽帧阶段**没有尝试次数上限**，所以每轮都重试、永远不结束。")

# 这 1506 个是不是都集中在少数几个目录？
dirs = Counter()
for r in rows:
    parts = str(r["path"]).replace("/", "\\").split("\\")
    dirs["\\".join(parts[:3])] += 1
print("\n按前 3 层目录分布（前 8）：")
for d, n in dirs.most_common(8):
    print(f"  {n:>6,}  {d}")
conn.close()
