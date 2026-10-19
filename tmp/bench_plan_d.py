"""方案 D 前置基准：对比「当前 PySceneDetect 两遍解码」与「ffmpeg 单遍」的视频处理耗时。

对比 4 种组合：
  1. baseline        : PySceneDetect 检测 + 逐帧 ffmpeg 抽帧（现状）
  2. ffmpeg-scdet    : ffmpeg scdet 单遍检测（软解）+ 逐帧抽帧（软解）
  3. ffmpeg+cuda     : ffmpeg scdet 单遍检测（-hwaccel cuda）+ 逐帧抽帧（软解）
  4. ffmpeg+cuda2    : 检测与抽帧都走 cuda（若 scale_cuda 可用）

只读真实视频，不写库、不改任何生产文件。
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
OUT = ROOT / "tmp" / "_bench_d"
FFMPEG = Path(r"D:\PROGRAM\1_ffmpeg-9.0.1-essentials_build\bin\ffmpeg.exe")
FFPROBE = Path(r"D:\PROGRAM\1_ffmpeg-9.0.1-essentials_build\bin\ffprobe.exe")

SCDET_THRESHOLD = 10.0   # ffmpeg scdet 的 0~100 尺度（约等价 PySceneDetect 30）
MIN_SHOT_SEC = 0.8
EDGE = 384
SCALE_SW = (f"scale=w='if(gt(iw,ih),trunc(iw*{EDGE}/ih/2)*2,{EDGE})':"
            f"h='if(gt(iw,ih),{EDGE},trunc(ih*{EDGE}/iw/2)*2)'")
SCALE_CUDA = (f"scale_cuda=w='if(gt(iw,ih),trunc(iw*{EDGE}/ih/2)*2,{EDGE})':"
              f"h='if(gt(iw,ih),{EDGE},trunc(ih*{EDGE}/iw/2)*2)',hwdownload,format=nv12")

SHOWINFO_T = re.compile(r"pts_time:([0-9.]+)")
SCDET = re.compile(r"lavfi\.scd\.score:([0-9.]+)")


def probe(path: Path) -> float:
    r = subprocess.run([str(FFPROBE), "-v", "error", "-show_entries",
                        "format=duration", "-of", "default=nw=1:nk=1", str(path)],
                       capture_output=True, text=True, timeout=120)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def scdet_ffmpeg(path: Path, cuda: bool) -> tuple[list[float], float]:
    """单遍解码：用 scdet 找切点，同时用 showinfo 统计帧时间戳。"""
    pre = ["-hwaccel", "cuda"] if cuda else []
    argv = [str(FFMPEG), "-hide_banner", "-nostdin", *pre, "-i", str(path),
            "-vf", f"scdet=threshold={SCDET_THRESHOLD},showinfo",
            "-an", "-f", "null", "-"]
    t0 = time.monotonic()
    r = subprocess.run(argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=3600)
    dt = time.monotonic() - t0
    err = r.stderr or ""
    cuts: list[float] = []
    prev = None
    for line in err.splitlines():
        m = SCDET.search(line)
        if not m:
            continue
        try:
            score = float(m.group(1))
        except ValueError:
            continue
        if score >= SCDET_THRESHOLD:
            mm = SHOWINFO_T.search(line)
            if mm:
                t = float(mm.group(1))
                if prev is None or t - prev >= MIN_SHOT_SEC:
                    cuts.append(t)
                    prev = t
    return cuts, dt


def extract_frames(path: Path, times: list[float], use_cuda: bool) -> tuple[int, float]:
    """按给定时间点逐帧抽图，返回 (成功数, 耗时)。"""
    scale = SCALE_CUDA if use_cuda else SCALE_SW
    ok = 0
    t0 = time.monotonic()
    for i, ts in enumerate(times):
        out = OUT / f"{path.stem[:16]}_{i}.jpg".replace(" ", "_")
        pre = ["-hwaccel", "cuda"] if use_cuda else []
        argv = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-threads", "1", *pre, "-ss", f"{ts:.3f}", "-i", str(path),
                "-frames:v", "1", "-vf", scale, "-q:v", "3", str(out)]
        r = subprocess.run(argv, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600)
        if r.returncode == 0 and out.exists() and out.stat().st_size > 2048:
            ok += 1
    return ok, time.monotonic() - t0


def scenedetect_current(path: Path) -> tuple[list[float], float]:
    """现状：PySceneDetect 全片检测。"""
    t0 = time.monotonic()
    try:
        from scenedetect import ContentDetector, SceneManager, open_video
        v = open_video(str(path))
        sm = SceneManager()
        sm.add_detector(ContentDetector(threshold=30.0))
        sm.detect_scenes(v, show_progress=False)
        scenes = sm.get_scene_list()
        cuts = [s[0].get_seconds() for s in scenes]
    except Exception as exc:  # noqa: BLE001
        print(f"    PySceneDetect 失败：{exc}")
        cuts = []
    return cuts, time.monotonic() - t0


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB))
    rows = [(int(i), p) for i, p in con.execute(
        "SELECT file_id, path FROM files WHERE kind='video' "
        "AND duration_sec BETWEEN 60 AND 900 ORDER BY file_id LIMIT 4")]
    con.close()
    if not rows:
        rows = [(int(i), p) for i, p in sqlite3.connect(str(DB)).execute(
            "SELECT file_id, path FROM files WHERE kind='video' ORDER BY file_id LIMIT 4")]

    for fid, p in rows:
        path = Path(p)
        if not path.exists():
            continue
        dur = probe(path)
        print(f"\n{'=' * 78}\nfile_id={fid}  时长={dur:.1f}s  {path.name[:52]}\n{'=' * 78}")

        cuts_py, t_py = scenedetect_current(path)
        print(f"  1) PySceneDetect 检测      : {t_py:7.2f}s  切点 {len(cuts_py)}")

        cuts_sw, t_sw = scdet_ffmpeg(path, cuda=False)
        print(f"  2) ffmpeg scdet（软解）    : {t_sw:7.2f}s  切点 {len(cuts_sw)}"
              f"   [{t_py / max(t_sw, 0.01):.1f}x]")

        cuts_cu, t_cu = scdet_ffmpeg(path, cuda=True)
        print(f"  3) ffmpeg scdet（cuda）    : {t_cu:7.2f}s  切点 {len(cuts_cu)}"
              f"   [{t_py / max(t_cu, 0.01):.1f}x]")

        # 抽帧耗时（取前 8 个切点，控制实验时长）
        sample = (cuts_sw or cuts_py)[:8]
        if sample:
            n_sw, t_ex_sw = extract_frames(path, sample, use_cuda=False)
            print(f"     ├ 抽帧 软解 {n_sw}/{len(sample)} 帧 : {t_ex_sw:7.2f}s")
            n_cu, t_ex_cu = extract_frames(path, sample, use_cuda=True)
            print(f"     └ 抽帧 cuda {n_cu}/{len(sample)} 帧 : {t_ex_cu:7.2f}s")

        print(f"  合计现状(Py+软抽) ≈ {t_py + (t_ex_sw if sample else 0):.2f}s")
        print(f"  合计方案D(ffmpeg单遍+cuda抽) ≈ "
              f"{t_cu + (t_ex_cu if sample else 0):.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
