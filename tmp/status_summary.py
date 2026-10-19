"""汇总当前完成情况（只读，随时可跑）。"""

import sqlite3
from pathlib import Path

ROOT = Path(r'D:\DATA\PROJECT\建设多媒体整理系统')
c = sqlite3.connect(str(ROOT / 'data' / 'library.db'))
c.row_factory = sqlite3.Row

print("=== 素材总览 ===")
for r in c.execute("SELECT kind, status, COUNT(*) n FROM files GROUP BY kind,status ORDER BY n DESC"):
    print(f"  {r['kind']:<6} {r['status']:<12} {r['n']:>8,}")
print(f"  合计 {c.execute('SELECT COUNT(*) FROM files').fetchone()[0]:,}")

print("\n=== 各阶段进度（stage_status） ===")
for r in c.execute("SELECT stage, status, COUNT(*) n FROM stage_status GROUP BY stage,status ORDER BY stage,n DESC"):
    print(f"  {r['stage']:<16} {r['status']:<10} {r['n']:>8,}")

print("\n=== 两阶段入库（铁律 3） ===")
for r in c.execute("SELECT model,status,COUNT(*) n FROM file_encoding GROUP BY model,status ORDER BY model,n DESC"):
    print(f"  {r['model']:<9} {r['status']:<12} {r['n']:>8,}")

alive = c.execute("SELECT COUNT(*) FROM face_records WHERE status='alive'").fetchone()[0]
vec = c.execute("SELECT COUNT(*) FROM face_records WHERE status='alive' AND has_vector=1").fetchone()[0]
print(f"\n=== 人脸 ===\n  face_records alive={alive:,}  with_vector={vec:,}")
print("  shots:", f"{c.execute('SELECT COUNT(*) FROM shots').fetchone()[0]:,}")
print("  dead_letter:", c.execute("SELECT COUNT(*) FROM dead_letter").fetchone()[0])
print("  库内人脸照片来源文件数:",
      c.execute("SELECT COUNT(DISTINCT file_id) FROM face_records WHERE status='alive'").fetchone()[0])
c.close()

print("\n=== NVMe 派生数据 ===")
for d in ("frames", "faces/raw", "faces/thumbs", "embeddings"):
    p = ROOT / "data" / "nvme" / d
    if p.exists():
        fs = [f for f in p.rglob("*") if f.is_file()]
        s = sum(f.stat().st_size for f in fs)
        print(f"  {d:<14} {len(fs):>8,} 个文件  {s/1e9:>7.2f} GB")

print("\n=== 库文件 ===")
for name in ("data/library.db", "data/library.db-wal"):
    p = ROOT / name
    if p.exists():
        print(f"  {name:<24} {p.stat().st_size/1e9:.2f} GB")
