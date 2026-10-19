"""快速测量：容器 duration 与实际末帧 PTS 的差值，并验证 margin=5s 是否安全。

方法：不做全帧扫描（太长），而是
  1) ffprobe 取容器 duration
  2) ffmpeg 从 duration-20s 处解码，用 -vf showinfo 取该窗口内最后一帧的 pts
  3) 统计 gap = duration - 该帧 pts（近似实际尾部空洞）
  4) 再验证 margin 候选值处的抽帧是否成功
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
OUT = ROOT / "tmp" / "_eof_test"
FFMPEG = Path(r"D:\PROGRAM\1_ffmpeg-9.0.1-essentials_build\bin\ffmpeg.exe")
FFPROBE = Path(r"D:\PROGRAM\1_ffmpeg-9.0.1-essentials_build\bin\ffprobe.exe")

EDGE = 384
SCALE = (f"scale=w='if(gt(iw,ih),trunc(iw*{EDGE}/ih/2)*2,{EDGE})':"
         f"h='if(gt(iw,ih),{EDGE},trunc(ih*{EDGE}/iw/2)*2)'")

PTS_RE = re.compile(r"pts_time:([0-9.]+)")


def duration(path: Path) -> float:
    r = subprocess.run([str(FFPROBE), "-v", "error", "-show_entries",
                        "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                       capture_output=True, text=True, timeout=120)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return -1.0


def tail_last_pts(path: Path, dur: float, window: float = 30.0) -> float:
    """解码尾部 window 秒，返回窗口内最后出现的 pts_time（近似真实末帧）。"""
    start = max(0.0, dur - window)
    argv = [str(FFMPEG), "-hide_banner", "-nostdin", "-ss", f"{start:.3f}",
            "-i", str(path), "-vf", "showinfo", "-an", "-f", "null", "-"]
    r = subprocess.run(argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    vals = [float(m) for m in PTS_RE.findall(r.stderr or "")]
    return max(vals) if vals else -1.0


def try_frame(path: Path, ts: float, tag: str) -> tuple[bool, int]:
    out = OUT / f"{path.stem[:20]}_{tag}.jpg".replace(" ", "_")
    if out.exists():
        out.unlink()
    argv = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-threads", "1", "-ss", f"{ts:.3f}", "-i", str(path),
            "-frames:v", "1", "-vf", SCALE, "-q:v", "3", str(out)]
    r = subprocess.run(argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    size = out.stat().st_size if out.exists() else 0
    return (r.returncode == 0 and size > 2048), size


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB))
    rows = [(int(i), p) for i, p in con.execute(
        "SELECT file_id, path FROM files WHERE kind='video' ORDER BY file_id LIMIT 15")]
    con.close()

    print(f"{'fid':>6} {'时长':>9} {'尾部末帧':>9} {'gap':>7}  "
          f"{'ts=时长':>8} {'时长-1':>8} {'时长-3':>8} {'时长-5':>8}")
    for fid, p in rows:
        path = Path(p)
        if not path.exists():
            continue
        dur = duration(path)
        if dur <= 0:
            continue
        last = tail_last_pts(path, dur)
        gap = dur - last if last > 0 else float("nan")
        res = {}
        for tag, t in (("d0", dur), ("d1", dur - 1), ("d3", dur - 3), ("d5", dur - 5)):
            res[tag] = try_frame(path, max(0.1, t), f"{fid}{tag}")[0]
        m = lambda k: f"{'OK' if res[k] else 'FAIL':>8}"  # noqa: E731
        print(f"{fid:>6} {dur:>9.2f} {last:>9.2f} {gap:>7.2f}  "
              f"{m('d0')} {m('d1')} {m('d3')} {m('d5')}")

    print("\n结论：看哪一列全 OK —— 那一列对应的 margin 就是安全值。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
