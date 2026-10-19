"""确定「安全末帧」的收紧量：容器 duration 比实际末帧 PTS 高多少？

生产代码目前用 shot.end（≈ 容器 duration）作为最后抽帧点，
导致 seek 越过实际最后一帧 → 无帧到达编码器 → mjpeg 报错。

本脚本统计 duration 与实际末帧 PTS 的差值分布，据此选一个安全 margin。
"""

from __future__ import annotations

import sqlite3
import statistics
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
FFPROBE = Path(r"D:\PROGRAM\1_ffmpeg-9.0.1-essentials_build\bin\ffprobe.exe")


def last_pts(video: Path, limit: int = 4000) -> tuple[float, float]:
    """返回 (实际末帧 PTS, 容器 duration)。只读前 limit 行以控制耗时。"""
    r = subprocess.run([str(FFPROBE), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "frame=pts_time", "-of",
                        "default=nw=1:nk=1", str(video)],
                       capture_output=True, text=True, timeout=900)
    vals = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if line and line != "N/A":
            try:
                vals.append(float(line))
            except ValueError:
                pass
    last = max(vals) if vals else -1.0
    r2 = subprocess.run([str(FFPROBE), "-v", "error", "-show_entries",
                         "format=duration", "-of", "default=nw=1:nk=1", str(video)],
                        capture_output=True, text=True, timeout=120)
    try:
        dur = float(r2.stdout.strip())
    except ValueError:
        dur = -1.0
    return last, dur


def main() -> int:
    con = sqlite3.connect(str(DB))
    rows = [(int(i), p) for i, p in con.execute(
        "SELECT file_id, path FROM files WHERE kind='video' ORDER BY file_id LIMIT 40")]
    con.close()

    gaps = []
    print(f"{'fid':>6} {'容器时长':>10} {'实际末帧':>10} {'差值':>8}")
    for fid, p in rows:
        path = Path(p)
        if not path.exists():
            continue
        last, dur = last_pts(path)
        if last < 0 or dur < 0:
            continue
        gap = dur - last
        gaps.append(gap)
        print(f"{fid:>6} {dur:>10.2f} {last:>10.2f} {gap:>8.2f}")
        if len(gaps) >= 25:
            break

    if gaps:
        print("\n=== duration - 实际末帧PTS 差值统计 ===")
        print(f"  样本数   : {len(gaps)}")
        print(f"  最大     : {max(gaps):.2f} s")
        print(f"  中位数   : {statistics.median(gaps):.2f} s")
        nz = [g for g in gaps if g > 0.5]
        print(f"  > 0.5s 的: {len(nz)} 个 → {[round(x, 2) for x in sorted(nz, reverse=True)[:8]]}")
        print(f"\n  建议 margin: {max(1.0, min(10.0, max(gaps) if gaps else 1.0)):.1f} s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
