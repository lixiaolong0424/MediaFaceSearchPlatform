"""盘点「用旧参数跑过的文件」，为清理做准备（只读，不改任何东西）。

背景：
  * 03_extract 在旧参数下（min_frame_mean=5.0、无末尾钳制）完成了 1,500 个文件
  * 之后参数改为 min_frame_mean=2.0 + end_margin_sec=5.0
  * 旧结果不含末尾那一帧（被 seek 越界丢掉），需要清掉重跑才能统一

清理需要同时处理三处：
  1) stage_status  stage='extract' 的行   → 让 03 重新拾取
  2) shots         该 file_id 的镜头行     → 会被 03 重建（D.delete_shots）
  3) files         该文件的 status 需回到 pending（原为 done）
  4) frames/<file_id>/ 目录                → 旧关键帧文件
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
FRAMES = ROOT / "data" / "nvme" / "frames"


def main() -> int:
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row

    print("=== 当前整体状态 ===")
    for row in con.execute("SELECT status, COUNT(*) n FROM files GROUP BY status"):
        print(f"  files.status={row['status']:<12} {row['n']:>8,}")

    print()
    print("=== stage_status 各阶段 ===")
    try:
        for row in con.execute(
            "SELECT stage, status, COUNT(*) n FROM stage_status GROUP BY stage, status"):
            print(f"  {row['stage']}:{row['status']:<8} {row['n']:>8,}")
    except sqlite3.OperationalError:
        print("  （表不存在，说明 03_extract 还没在任何文件上成功过）")

    # 旧参数跑过的文件 = stage_status('extract') 存在的那些
    try:
        old = con.execute(
            "SELECT COUNT(*) FROM stage_status WHERE stage='extract'").fetchone()[0]
        old_done = con.execute(
            "SELECT COUNT(*) FROM stage_status WHERE stage='extract' AND status='done'"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        old = old_done = 0
    print(f"\n=== 待清理范围 ===")
    print(f"  stage_status(extract) 有记录的文件 : {old:,}")
    print(f"  其中 status='done'                : {old_done:,}")

    # 这些文件的 files.status 分布
    if old:
        print("  这些文件的 files.status 分布：")
        for row in con.execute("""
            SELECT f.status, COUNT(*) n
              FROM files f JOIN stage_status s ON s.file_id = f.file_id
             WHERE s.stage='extract' GROUP BY f.status"""):
            print(f"    {row['status']:<12} {row['n']:>8,}")

    # 关联产物规模
    if old:
        r = con.execute("""
            SELECT COUNT(DISTINCT s.file_id) files,
                   COALESCE(SUM(s.shots),0) shots,
                   COALESCE(SUM(s.frames),0) frames
              FROM stage_status s WHERE s.stage='extract'""").fetchone()
        print(f"\n  stage_status 自报：{r['files']:,} 文件 / {r['shots']:,} 镜头 / "
              f"{r['frames']:,} 关键帧")
        sj = con.execute("""
            SELECT COUNT(*) shots, COALESCE(SUM(n_frames),0) frames
              FROM shots WHERE file_id IN
                   (SELECT file_id FROM stage_status WHERE stage='extract')""").fetchone()
        print(f"  shots 表实测：   {sj[0]:,} 镜头 / {sj[1]:,} 关键帧")

    # frames 目录
    dirs = sorted(p for p in FRAMES.iterdir() if p.is_dir()) if FRAMES.exists() else []
    n_files = sum(1 for d in dirs for _ in d.glob("*.jpg"))
    total = sum(p.stat().st_size for d in dirs for p in d.glob("*.jpg"))
    print(f"\n=== frames/ 目录 ===")
    print(f"  file_id 目录数 : {len(dirs):,}")
    print(f"  关键帧文件数   : {n_files:,}")
    print(f"  占用           : {total / 1024 / 1024:.1f} MB")

    if old:
        # 校验：frames 目录里的 file_id 是否都在待清理集合内
        ids = {int(r[0]) for r in con.execute(
            "SELECT file_id FROM stage_status WHERE stage='extract'")}
        dir_ids = {int(d.name) for d in dirs if d.name.isdigit()}
        print(f"\n  待清理 file_id 数      : {len(ids):,}")
        print(f"  frames 目录的 file_id 数: {len(dir_ids):,}")
        print(f"  frames 里但不在待清理集: {len(dir_ids - ids):,}" 
              f"  {sorted(dir_ids - ids)[:10]}")
        print(f"  待清理但无 frames 目录  : {len(ids - dir_ids):,}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
