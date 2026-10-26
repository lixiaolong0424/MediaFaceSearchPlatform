# -*- coding: utf-8 -*-
"""分析「调低清晰度下限，仍有 298,723 张人脸没参与分组」是否正常。

要分清三件不同的事：
  1. 照片墙「清晰度下限」= 照片墙展示过滤（ui.*），**不影响分组**
  2. 分组门槛 = cluster.quality_gate，决定哪些脸**能进**聚类
  3. 分组是快照：改了门槛必须「重新分组」才会重算
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import cluster as C  # noqa: E402
from src.common import load_config  # noqa: E402

cfg = load_config()
con = sqlite3.connect(str(cfg.db_path))
con.row_factory = sqlite3.Row


def one(sql, *a):
    return int(con.execute(sql, a).fetchone()[0] or 0)


print("=== 1) 相关设置当前值 ===")
for k in ("cluster.quality_gate", "ui.wall_min_quality", "ui.wall_quality_floor",
          "cluster.low_confidence_threshold"):
    try:
        print(f"  {k} = {cfg.get(k)}")
    except Exception as exc:  # noqa: BLE001
        print(f"  {k} 读取失败 {exc}")

print("\n=== 2) 分组版本 ===\n", end="")
v = C.current_version(cfg)
print("  当前版本:", v)
for r in con.execute("SELECT version, n_faces, n_clusters, n_noise, created_at "
                     "FROM cluster_runs ORDER BY created_at DESC LIMIT 3"):
    print(f"  {r['version']}  参与={r['n_faces']:,}  组={r['n_clusters']}  "
          f"噪声={r['n_noise']}  时间={r['created_at']}")

print("\n=== 3) 人脸质量分布（alive + 有向量 + 可见）===")
base = ("SELECT COUNT(*) FROM face_records fr JOIN files f ON f.file_id=fr.file_id "
        "WHERE fr.status='alive' AND fr.has_vector=1 AND f.visible=1 AND f.status!='missing'")
total = one(base)
print(f"  可显示人脸总数: {total:,}")
gate = float(cfg.get("cluster.quality_gate", 0.3))
print(f"  当前分组门槛 quality_gate = {gate}")
for thr in (0.0, 0.1, 0.2, 0.3, 0.5):
    n = one(base + " AND fr.quality >= ?", thr)
    print(f"    quality >= {thr}: {n:,}")
print(f"  → 若门槛 {gate}，能进分组的是 {one(base + ' AND fr.quality >= ?', gate):,}，"
      f"被挡在门外 {total - one(base + ' AND fr.quality >= ?', gate):,}")

if v:
    in_ver = one("SELECT COUNT(*) FROM face_clusters WHERE version=?", v)
    print("\n=== 4) 版本覆盖 ===")
    print(f"  版本里记录了 {in_ver:,} 张脸")
    not_in = one(base + " AND fr.face_id NOT IN (SELECT face_id FROM face_clusters WHERE version=?)", v)
    print(f"  可显示但不在版本里: {not_in:,}（= 门槛挡掉的 + 分组后新增的）")
    low = one(base + " AND fr.quality < ? AND fr.face_id NOT IN "
               "(SELECT face_id FROM face_clusters WHERE version=?)", gate, v)
    print(f"    其中 quality < {gate}（门槛挡掉）: {low:,}")
    print(f"    其中 quality >= {gate} 却不在版本里（分组后新增/漏算）: {not_in - low:,}")

print("\n=== 5) cluster_meta 实际可见组 ===")
live = C.live_meta_stats(cfg, v) if v else {"groups": 0, "faces": 0}
print(f"  可见组 {live['groups']:,}，覆盖 {live['faces']:,} 张脸")
con.close()
