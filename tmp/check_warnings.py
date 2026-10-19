"""核对这两条警告是否正常：
  1) 「镜头切分后端 ffmpeg 未产出切点」
  2) 「镜头密度异常 15.6/小时（正常区间 [30,600]）」

做法：找出被警告的文件，看它的真实时长、切分结果、以及密度是怎么算出来的。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"


def main() -> int:
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row

    # 阈值来自 config
    lo, hi = 30.0, 600.0
    try:
        from src.common import load_config
        cfg = load_config()
        lo = float(cfg.get("video.warn_shots_per_hour_low", 30))
        hi = float(cfg.get("video.warn_shots_per_hour_high", 600))
    except Exception:  # noqa: BLE001
        pass
    print(f"告警区间来自 config：[{lo:.0f}, {hi:.0f}] 镜头/小时\n")

    print("=== 当前进度 ===")
    print("  files.status :",
          {r["status"]: r["n"] for r in con.execute(
              "SELECT status, COUNT(*) n FROM files GROUP BY status")})
    try:
        print("  stage_status :",
              {f"{r['stage']}:{r['status']}": r["n"] for r in con.execute(
                  "SELECT stage, status, COUNT(*) n FROM stage_status GROUP BY stage,status")})
    except sqlite3.OperationalError:
        pass

    # 找出「镜头数很少但时长很长」的文件 —— 这是密度告警的来源
    print("\n=== 已处理视频里「密度低于下限」的样本 ===")
    rows = con.execute("""
        SELECT f.file_id, f.path, f.duration_sec,
               COUNT(s.shot_ix) n_shots,
               COALESCE(SUM(s.n_frames),0) n_frames
          FROM files f JOIN shots s ON s.file_id = f.file_id
         WHERE f.kind='video' AND f.duration_sec > 60
         GROUP BY f.file_id
         HAVING (COUNT(s.shot_ix) / (f.duration_sec/3600.0)) < ?
         ORDER BY f.duration_sec DESC LIMIT 12
    """, (lo,)).fetchall()
    if not rows:
        print("  （暂无）")
    print(f"  {'fid':>6} {'时长h':>7} {'镜头':>5} {'帧':>5} {'密度':>8}  文件")
    for r in rows:
        d = float(r["duration_sec"] or 0)
        dens = r["n_shots"] / (d / 3600.0) if d > 0 else 0
        print(f"  {r['file_id']:>6} {d/3600:>7.2f} {r['n_shots']:>5} {r['n_frames']:>5} "
              f"{dens:>8.1f}  {Path(r['path']).name[:40]}")

    # 切点数为 0 的视频（即「未产出切点」告警的来源）
    print("\n=== 切点数为 0（整片判为单镜头）的视频有多少 ===")
    tot = con.execute("""SELECT COUNT(DISTINCT f.file_id) FROM files f
                           JOIN shots s ON s.file_id=f.file_id
                          WHERE f.kind='video'""").fetchone()[0]
    one = con.execute("""SELECT COUNT(*) FROM (
                            SELECT f.file_id FROM files f JOIN shots s ON s.file_id=f.file_id
                             WHERE f.kind='video'
                             GROUP BY f.file_id HAVING COUNT(*)=1)""").fetchone()[0]
    print(f"  已抽帧视频总数      : {tot:,}")
    print(f"  判为单镜头（1 个）  : {one:,}  ({one/max(1,tot)*100:.0f}%)")

    # 密度告警的总数
    nd = con.execute("""SELECT COUNT(*) FROM (
                          SELECT f.file_id FROM files f JOIN shots s ON s.file_id=f.file_id
                           WHERE f.kind='video' AND f.duration_sec > 60
                           GROUP BY f.file_id
                          HAVING (COUNT(s.shot_ix)/(f.duration_sec/3600.0)) < ? OR
                                 (COUNT(s.shot_ix)/(f.duration_sec/3600.0)) > ?)""",
                     (lo, hi)).fetchone()[0]
    print(f"  触发密度告警的视频  : {nd:,}")

    # 密度告警里，有多少其实是「长视频只有 1 个镜头」
    long_single = con.execute("""SELECT COUNT(*) FROM (
                                   SELECT f.file_id FROM files f JOIN shots s ON s.file_id=f.file_id
                                    WHERE f.kind='video' AND f.duration_sec > 3600
                                    GROUP BY f.file_id HAVING COUNT(*)=1)""").fetchone()[0]
    print(f"  其中「>1 小时且仅 1 镜头」: {long_single:,}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
