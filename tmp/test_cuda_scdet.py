"""验证 cuda 解码与 scdet/抽帧的兼容性，并测速。

要点：-hwaccel cuda 时帧在显存，scdet 是软件滤镜，必须先 hwdownload。
本脚本逐个组合实测，确认哪种能真正跑通、哪种更快。
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
OUT = ROOT / "tmp" / "_cuda_test"
FFMPEG = Path(r"D:\PROGRAM\1_ffmpeg-9.0.1-essentials_build\bin\ffmpeg.exe")
FFPROBE = Path(r"D:\PROGRAM\1_ffmpeg-9.0.1-essentials_build\bin\ffprobe.exe")

EDGE = 384
SCALE_SW = (f"scale=w='if(gt(iw,ih),trunc(iw*{EDGE}/ih/2)*2,{EDGE})':"
            f"h='if(gt(iw,ih),{EDGE},trunc(ih*{EDGE}/iw/2)*2)'")
SCALE_CUDA = (f"scale_cuda=w='if(gt(iw,ih),trunc(iw*{EDGE}/ih/2)*2,{EDGE})':"
              f"h='if(gt(iw,ih),{EDGE},trunc(ih*{EDGE}/iw/2)*2)',hwdownload,format=nv12")


def pick(n: int = 3) -> list[tuple[int, Path]]:
    con = sqlite3.connect(str(DB))
    rows = con.execute(
        "SELECT file_id, path, duration_sec FROM files WHERE kind='video' "
        "AND duration_sec BETWEEN 60 AND 900 ORDER BY file_id LIMIT ?", (n,)).fetchall()
    con.close()
    return [(int(i), Path(p)) for i, p, _d in rows if Path(p).exists()]


def run(argv: list[str], timeout: int = 1800) -> tuple[int, str, float]:
    t0 = time.monotonic()
    r = subprocess.run(argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return r.returncode, (r.stderr or ""), time.monotonic() - t0


def detect_soft(path: Path) -> tuple[int, float, int]:
    """软解 + scdet（单遍）"""
    argv = [str(FFMPEG), "-hide_banner", "-nostdin", "-i", str(path),
            "-vf", "scdet=threshold=10", "-an", "-f", "null", "-"]
    rc, err, dt = run(argv)
    return rc, dt, len(re.findall(r"lavfi\.scd\.score:", err))


def detect_hw(path: Path) -> tuple[int, float, int, str]:
    """硬解 + hwdownload + scdet（单遍）"""
    argv = [str(FFMPEG), "-hide_banner", "-nostdin", "-hwaccel", "cuda",
            "-i", str(path),
            "-vf", "hwdownload,format=nv12,scdet=threshold=10", "-an", "-f", "null", "-"]
    rc, err, dt = run(argv)
    tail = " | ".join(l for l in err.splitlines() if "Error" in l or "error" in l)[:100]
    return rc, dt, len(re.findall(r"lavfi\.scd\.score:", err)), tail


def extract(path: Path, ts: float, cuda: bool) -> tuple[bool, float, str]:
    out = OUT / f"{path.stem[:14]}_{int(ts * 100)}.jpg".replace(" ", "_")
    if out.exists():
        out.unlink()
    if cuda:
        pre = ["-hwaccel", "cuda"]
        vf = SCALE_CUDA
    else:
        pre = []
        vf = SCALE_SW
    argv = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-threads", "1", *pre, "-ss", f"{ts:.3f}", "-i", str(path),
            "-frames:v", "1", "-vf", vf, "-q:v", "3", str(out)]
    rc, err, dt = run(argv, timeout=600)
    size = out.stat().st_size if out.exists() else 0
    return (rc == 0 and size > 2048), dt, (err.strip().splitlines()[0][:70] if err.strip() else "")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    vids = pick(3)
    print(f"测试视频 {len(vids)} 个\n")

    print("=== 1) cuda 解码 + scdet 是否可用 ===")
    for fid, path in vids:
        rc, dt, n, tail = detect_hw(path)
        print(f"  file_id={fid:<5} rc={rc:<4} {dt:7.2f}s  切点候选={n:<5} {tail}")

    print("\n=== 2) 软解 scdet 基准 ===")
    for fid, path in vids:
        rc, dt, n = detect_soft(path)
        print(f"  file_id={fid:<5} rc={rc:<4} {dt:7.2f}s  切点候选={n}")

    print("\n=== 3) 抽帧：软解 vs cuda ===")
    for fid, path in vids:
        r = subprocess.run([str(FFPROBE), "-v", "error", "-show_entries",
                            "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                           capture_output=True, text=True, timeout=120)
        try:
            dur = float(r.stdout.strip())
        except ValueError:
            continue
        ts = max(1.0, dur / 2)
        ok_sw, t_sw, e_sw = extract(path, ts, cuda=False)
        ok_cu, t_cu, e_cu = extract(path, ts, cuda=True)
        print(f"  file_id={fid:<5} 软解 {'OK' if ok_sw else 'FAIL'} {t_sw:6.2f}s | "
              f"cuda {'OK' if ok_cu else 'FAIL'} {t_cu:6.2f}s"
              + (f"  [{e_cu}]" if not ok_cu else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
