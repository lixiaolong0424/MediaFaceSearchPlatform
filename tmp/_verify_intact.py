"""确认「变 missing」只是记账，物理文件与派生数据都还在。"""

import random
import sqlite3
from pathlib import Path

conn = sqlite3.connect(r'data\library.db')
conn.row_factory = sqlite3.Row

# E: 侧随机抽样，看原文件是否真的还在
rows = list(conn.execute(
    "SELECT file_id, path, kind, status FROM files WHERE status='missing' "
    "AND path LIKE 'E:%' ORDER BY file_id LIMIT 3000"))
random.seed(7)
sample = random.sample(rows, min(200, len(rows)))
exist = sum(1 for r in sample if Path(str(r["path"])).exists())
print(f"E: 侧 missing 文件随机抽 {len(sample)} 个 → 物理存在 {exist} 个")

# 派生数据抽样
import itertools
cand = [int(r["file_id"]) for r in sample[:10]]
n_frames = n_thumb = 0
for fid in cand:
    d = Path("data/nvme/frames") / str(fid)
    if d.exists() and any(d.glob("*.jpg")):
        n_frames += 1
print(f"  抽样 10 个里，NVMe 关键帧目录还在的：{n_frames}/10")

# 向量与人脸
sem = conn.execute("SELECT COUNT(*) FROM file_encoding WHERE model='semantic' AND status='done'").fetchone()[0]
fac = conn.execute("SELECT COUNT(*) FROM face_records WHERE status='alive' AND has_vector=1").fetchone()[0]
skip = conn.execute("SELECT COUNT(*) FROM file_encoding WHERE model='semantic' AND status='skipped_dup'").fetchone()[0]
print(f"  语义编码 done 记录（仍在）：{sem:,}")
print(f"  人脸记录（有向量，仍在）：{fac:,}")
print(f"  去重 skipped_dup 记录（仍在）：{skip:,}")

print("\n=== F: 盘是否还在 ===")
f = Path("F:/")
print(f"  F:\\ 存在: {f.exists()}  可列出: {f.exists() and any(f.iterdir()) if f.exists() else False}")
print(f"  F: 下登记的文件数: {conn.execute(chr(83)+'ELECT COUNT(*) FROM files WHERE path LIKE '+chr(39)+'F:%'+chr(39)).fetchone()[0]}")

print("\n=== E: 下登记的文件数 vs 物理 ===")
print(f"  E: 登记: {conn.execute(chr(83)+'ELECT COUNT(*) FROM files WHERE path LIKE '+chr(39)+'E:%'+chr(39)).fetchone()[0]:,}")
conn.close()
