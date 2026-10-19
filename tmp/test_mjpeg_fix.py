"""复现并验证 mjpeg「Non full-range YUV」报错的修复方案。

症状（来自 03_extract 实跑日志）：
    [mjpeg] Non full-range YUV is non-standard, set strict_std_compliance
            to at most unofficial to use it.
    ff_frame_thread_encoder_init failed
    Error while opening encoder - maybe incorrect parameters such as ... width or height
    Task finished with error code: -22

根因：解码出来的是 limited-range YUV（如 yuv420p），而 mjpeg 编码器默认要 full-range。
      编码器初始化直接失败 → 整帧丢失。
修复：显式把像素格式转成 mjpeg 原生的 yuvj420p（full-range JPEG 变体）。

本脚本对真实失败过的视频逐个方案试，报告成功率与耗时。
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
OUT = ROOT / "tmp" / "_mjpeg_test"

FFMPEG = Path(r"D:\PROGRAM\1_ffmpeg-9.0.1-essentials_build\bin\ffmpeg.exe")
FFPROBE = Path(r"D:\PROGRAM\1_ffmpeg-9.0.1-essentials_build\bin\ffprobe.EXE")

EDGE = 384
SCALE = (f"scale=w='if(gt(iw,ih),trunc(iw*{EDGE}/ih/2)*2,{EDGE})':"
         f"h='if(gt(iw,ih),{EDGE},trunc(ih*{EDGE}/iw/2)*2)'")

VARIANTS = {
    "原实现（-vf scale + -q:v 3）": ["-vf", SCALE, "-q:v", "3"],
    "A. + -strict unofficial": ["-vf", SCALE, "-q:v", "3", "-strict", "unofficial"],
    "B. scale 后转 yuvj420p": ["-vf", f"{SCALE},format=yuvj420p", "-q:v", "3"],
    "C. B + -strict unofficial": ["-vf", f"{SCALE},format=yuvj420p", "-q:v", "3",
                                  "-strict", "unofficial"],
    "D. scale 后转 yuv420p + -strict": ["-vf", f"{SCALE},format=yuv420p", "-q:v", "3",
                                        "-strict", "unofficial"],
}


def probe_pix_fmt(video: Path) -> str:
    r = subprocess.run([str(FFPROBE), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=pix_fmt,width,height,color_range",
                        "-of", "default=nw=1", str(video)],
                       capture_output=True, text=True, timeout=60)
    return " ".join(r.stdout.split())


def try_variant(video: Path, ts: float, args: list[str], out: Path) -> tuple[bool, str, int, float]:
    if out.exists():
        out.unlink()
    argv = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-threads", "1", "-ss", f"{ts:.3f}", "-i", str(video),
            "-frames:v", "1", *args, str(out)]
    t0 = time.monotonic()
    r = subprocess.run(argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    dt = time.monotonic() - t0
    size = out.stat().st_size if out.exists() else 0
    ok = r.returncode == 0 and size > 2048
    err = (r.stderr or "").strip().splitlines()
    return ok, (err[0][:90] if err else ""), size, dt


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB))
    # 挑出 03_extract 已经处理过的视频（stage_status 有记录），拿它们的真实路径
    rows = con.execute("""
        SELECT f.file_id, f.path, f.duration_sec
          FROM files f
          JOIN stage_status s ON s.file_id = f.file_id AND s.stage='extract'
         WHERE f.kind='video'
         ORDER BY f.file_id LIMIT 12
    """).fetchall()
    con.close()
    if not rows:
        # 还没跑过 extract 就退回到前若干个视频
        con = sqlite3.connect(str(DB))
        rows = con.execute(
            "SELECT file_id, path, duration_sec FROM files WHERE kind='video' "
            "ORDER BY file_id LIMIT 12").fetchall()
        con.close()

    videos = [(int(i), Path(p), d) for i, p, d in rows if Path(p).exists()]
    print(f"取到 {len(videos)} 个真实视频用于对比\n")

    summary: dict[str, list[int]] = {k: [0, 0] for k in VARIANTS}  # [成功, 总数]
    for fid, path, dur in videos:
        ts = 10.0
        print(f"--- file_id={fid}  {probe_pix_fmt(path)}  ({path.name[:44]})")
        for name, args in VARIANTS.items():
            out = OUT / f"{fid}_{abs(hash(name)) % 10000}.jpg"
            ok, err, size, dt = try_variant(path, ts, args, out)
            summary[name][1] += 1
            if ok:
                summary[name][0] += 1
            print(f"    {'OK ' if ok else 'FAIL'} {name:<34} {size:>7}B {dt:>6.2f}s  {err}")

    print("\n=== 汇总（成功/总数）===")
    for name, (ok, total) in summary.items():
        bar = "█" * int(20 * ok / max(1, total))
        print(f"  {name:<36} {ok:>3}/{total:<3} {bar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
