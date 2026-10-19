"""针对日志里的两条警告，取具体文件用新代码复现并判断是否正常。

警告对象：
  * "镜头切分后端 ffmpeg 未产出切点（ffmpeg_scdet）：1 (3).mp4"
  * "镜头密度异常 15.6/小时 ... file_id=4"
"""

from __future__ import annotations

import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.common import load_config  # noqa: E402
from src.extract import (  # noqa: E402
    ffmpeg_scene_detect, locate_tools, scene_detect, scenedetect_cuts,
)

FRAMES = ROOT / "data" / "nvme" / "frames"


def main() -> int:
    cfg = load_config()
    tools = locate_tools(cfg)
    con = sqlite3.connect(str(ROOT / "data" / "library.db"))
    con.row_factory = sqlite3.Row

    print("=== 当前库状态 ===")
    print("  files.status :",
          {r["status"]: r["n"] for r in con.execute(
              "SELECT status, COUNT(*) n FROM files GROUP BY status")})
    try:
        print("  stage_status :",
              {f"{r['stage']}:{r['status']}": r["n"] for r in con.execute(
                  "SELECT stage, status, COUNT(*) n FROM stage_status GROUP BY stage,status")})
    except sqlite3.OperationalError:
        pass
    dirs = sorted(p for p in FRAMES.iterdir() if p.is_dir()) if FRAMES.exists() else []
    print(f"  frames 目录  : {len(dirs):,} 个，"
          f"{sum(1 for d in dirs for _ in d.glob('*.jpg')):,} 帧")
    print(f"  （03 正在跑，未提交的批次不会出现在 stage_status 里）")

    targets = []
    r4 = con.execute("SELECT file_id, path, duration_sec FROM files WHERE file_id=4").fetchone()
    if r4:
        targets.append(r4)
    rn = con.execute(
        "SELECT file_id, path, duration_sec FROM files WHERE kind='video' AND path LIKE '%1 (3).mp4' "
        "LIMIT 1").fetchone()
    if rn and (not r4 or rn["file_id"] != r4["file_id"]):
        targets.append(rn)

    for row in targets:
        path = Path(str(row["path"]))
        fid = int(row["file_id"])
        dur = float(row["duration_sec"] or 0)
        print(f"\n{'=' * 74}")
        print(f"file_id={fid}  时长={dur:.1f}s ({dur/60:.1f}min)  {path.name[:50]}")
        print(f"{'=' * 74}")
        if not path.exists():
            print("  文件不存在")
            continue

        t0 = time.monotonic()
        cuts_d, m = ffmpeg_scene_detect(path, cfg, tools)
        t_d = time.monotonic() - t0
        print(f"  ffmpeg scdet : {len(cuts_d):>3} 个切点  耗时 {t_d:.2f}s  切点示例 "
              f"{[round(c, 1) for c in cuts_d[:8]]}")

        t0 = time.monotonic()
        cuts_p, _ = scenedetect_cuts(path, cfg)
        t_p = time.monotonic() - t0
        print(f"  PySceneDetect: {len(cuts_p):>3} 个切点  耗时 {t_p:.2f}s  切点示例 "
              f"{[round(c, 1) for c in cuts_p[:8]]}")

        shots, method = scene_detect(path, cfg, dur, tools)
        dens = len(shots) / (dur / 3600.0) if dur > 0 else 0
        lo = float(cfg.get("video.warn_shots_per_hour_low", 30))
        hi = float(cfg.get("video.warn_shots_per_hour_high", 600))
        print(f"  最终 scene_detect: {len(shots)} 个镜头  method={method}")
        print(f"  镜头密度      : {dens:.1f}/小时   告警区间 [{lo:.0f},{hi:.0f}]  "
              f"→ {'会告警' if (dens < lo or dens > hi) else '不告警'}")
        if len(shots) <= 3:
            print("  各镜头时长    :", [round(s.length, 1) for s in shots])

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
