"""测量 03_extract 的真实推进速率（数据库 + frames 目录双口径）。"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
FRAMES = ROOT / "data" / "nvme" / "frames"


def snap() -> tuple[int, int, int, int]:
    con = sqlite3.connect(str(DB))
    try:
        stage = con.execute(
            "SELECT COUNT(*) FROM stage_status WHERE stage='extract'").fetchone()[0]
        shots = con.execute("SELECT COUNT(*) FROM shots").fetchone()[0]
    finally:
        con.close()
    dirs = sum(1 for p in FRAMES.iterdir() if p.is_dir()) if FRAMES.exists() else 0
    files = sum(1 for p in FRAMES.rglob("*.jpg")) if FRAMES.exists() else 0
    return int(stage), int(shots), dirs, files


def main() -> int:
    a = snap()
    print(f"T0   stage_status={a[0]:,}  shots={a[1]:,}  frame目录={a[2]:,}  关键帧={a[3]:,}")
    time.sleep(60)
    b = snap()
    print(f"T+60 stage_status={b[0]:,}  shots={b[1]:,}  frame目录={b[2]:,}  关键帧={b[3]:,}")
    print(f"\n60 秒增量：stage_status +{b[0] - a[0]}  shots +{b[1] - a[1]}  "
          f"目录 +{b[2] - a[2]}  关键帧 +{b[3] - a[3]}")
    rate = (b[0] - a[0]) / 60.0
    print(f"速率 = {rate * 60:.0f} 文件/分钟")
    if rate > 0:
        remaining = 435082 - b[0]
        eta_min = remaining / rate / 60
        print(f"剩余 {remaining:,} 个 → 预计 {eta_min:.1f} 小时")
    else:
        print("速率 0：说明这一分钟没有写入（可能仍在处理大视频，或已卡住）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
