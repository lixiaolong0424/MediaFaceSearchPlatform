"""在**正确的**文件+时间点上复现 mjpeg 报错，并找出真正生效的修复。

之前两次测试的教训：
  * 第一次用随机 ts → 12/12 成功（那帧恰好没问题）
  * 第二次用日志里的 ts，但按 file_id 查路径时把「E:\\迅雷下载」的文件当成
    「E:\\晚上」的测试文件，导致 ts 超出该文件时长 → 报的是
    "No filtered frames for output stream" 而不是 mjpeg 错误，属于假复现

这次严格取「日志中的 file_id 对应路径 + 日志中的 ts」，并对比多种修复方案。
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
OUT = ROOT / "tmp" / "_mjpeg_test3"
FFMPEG = Path(r"D:\PROGRAM\1_ffmpeg-9.0.1-essentials_build\bin\ffmpeg.exe")
FFPROBE = Path(r"D:\PROGRAM\1_ffmpeg-9.0.1-essentials_build\bin\ffprobe.exe")

EDGE = 384
SCALE = (f"scale=w='if(gt(iw,ih),trunc(iw*{EDGE}/ih/2)*2,{EDGE})':"
         f"h='if(gt(iw,ih),{EDGE},trunc(ih*{EDGE}/iw/2)*2)'")

# 日志里真实失败的 (file_id, ts)；这些 file_id 属于顺序扫描时的前若干个视频
FAILED = [(1, 587.25), (3, 1312.83), (4, 3003.03), (22, 280.35), (29, 720.86),
          (34, 902.35), (55, 19.27), (77, 1643.57), (84, 686.55), (950, 8.17),
          (953, 6.41), (960, 25.17), (1329, 8.59), (1330, 15.02)]

VARIANTS = {
    "原实现": ["-vf", SCALE, "-q:v", "3"],
    "A 关帧级线程": ["-threads", "1", "-vf", SCALE, "-q:v", "3"],
    "B format=yuvj420p": ["-threads", "1", "-vf", f"{SCALE},format=yuvj420p", "-q:v", "3"],
    "C +strict unofficial": ["-threads", "1", "-vf", f"{SCALE},format=yuvj420p",
                             "-q:v", "3", "-strict", "unofficial"],
    "D 输出 yuvj420p 参数": ["-threads", "1", "-vf", SCALE, "-q:v", "3",
                             "-pix_fmt", "yuvj420p"],
}


def dur(path: Path) -> float:
    r = subprocess.run([str(FFPROBE), "-v", "error",
                        "-show_entries", "format=duration", "-of",
                        "default=nw=1:nk=1", str(path)],
                       capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def run(video: Path, ts: float, args: list[str], out: Path) -> tuple[bool, str, int]:
    if out.exists():
        out.unlink()
    argv = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-ss", f"{ts:.3f}", "-i", str(video), "-frames:v", "1", *args, str(out)]
    r = subprocess.run(argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    size = out.stat().st_size if out.exists() else 0
    err = ""
    if not (r.returncode == 0 and size > 0):
        lines = [l for l in (r.stderr or "").splitlines() if l.strip()]
        err = " | ".join(lines[:2])[:110]
    return (r.returncode == 0 and size > 0), err, size


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB))
    meta = {int(i): (p, d) for i, p, d in
            con.execute("SELECT file_id, path, duration_sec FROM files")}
    con.close()

    stats = {k: [0, 0] for k in VARIANTS}
    print(f"{'fid':>6} {'ts':>8} {'时长':>8}  " + "  ".join(f"{k}" for k in VARIANTS))
    for fid, ts in FAILED:
        if fid not in meta:
            continue
        p, d = meta[fid]
        path = Path(p)
        if not path.exists():
            continue
        real = dur(path)
        cells = []
        for name, args in VARIANTS.items():
            out = OUT / f"{fid}_{int(ts * 100)}_{abs(hash(name)) % 997}.jpg"
            ok, err, size = run(path, ts, args, out)
            stats[name][1] += 1
            if ok:
                stats[name][0] += 1
                cells.append("OK")
            else:
                cells.append(err.split("]")[0][:34] or "FAIL")
        print(f"{fid:>6} {ts:>8.1f} {real:>8.1f}  " + "  ".join(f"{c[:34]:<34}" for c in cells))

    print("\n=== 汇总（成功/总数）===")
    for name, (ok, total) in stats.items():
        print(f"  {name:<26} {ok:>3}/{total:<3}  {'OK' if ok == total else '有失败'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
