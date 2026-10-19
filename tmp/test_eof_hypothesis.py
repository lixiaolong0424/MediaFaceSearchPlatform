"""验证假设：这些失败点的 ts 已经越过视频最后一帧（seek 到 EOF 之后）。

证据链：
  * 日志里的 ts 与 ffprobe 报的 duration 几乎相等（如 587.25 vs 587.3）
  * ffmpeg 完整输出首行是 "No filtered frames for output stream, trying to
    initialize anyway"，随后才是 mjpeg 的 Non full-range YUV 抱怨
  * → 真正原因是「没有帧到达编码器」，mjpeg 报错只是次生现象

若假设成立：把这些 ts 往前挪一点就能成功，且挪到 ts-0.5 / ts-1.0 都行。
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
OUT = ROOT / "tmp" / "_mjpeg_test4"
FFMPEG = Path(r"D:\PROGRAM\1_ffmpeg-9.0.1-essentials_build\bin\ffmpeg.exe")
FFPROBE = Path(r"D:\PROGRAM\1_ffmpeg-9.0.1-essentials_build\bin\ffprobe.exe")

EDGE = 384
SCALE = (f"scale=w='if(gt(iw,ih),trunc(iw*{EDGE}/ih/2)*2,{EDGE})':"
         f"h='if(gt(iw,ih),{EDGE},trunc(ih*{EDGE}/iw/2)*2)'")

CASES = [(1, 587.25), (3, 1312.83), (4, 3003.03), (22, 280.35), (29, 720.86),
         (34, 902.35), (55, 19.27), (77, 1643.57), (84, 686.55), (950, 8.17),
         (953, 6.41), (960, 25.17), (1329, 8.59), (1330, 15.02)]


def last_frame_ts(video: Path) -> float:
    """用 ffprobe 数出实际最后一帧的时间戳（最可靠）。"""
    r = subprocess.run([str(FFPROBE), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "frame=pts_time", "-of",
                        "default=nw=1:nk=1", str(video)],
                       capture_output=True, text=True, timeout=600)
    vals = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if line and line != "N/A":
            try:
                vals.append(float(line))
            except ValueError:
                pass
    return max(vals) if vals else -1.0


def run(video: Path, ts: float, out: Path) -> tuple[bool, int]:
    if out.exists():
        out.unlink()
    argv = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-threads", "1", "-ss", f"{ts:.3f}", "-i", str(video),
            "-frames:v", "1", "-vf", SCALE, "-q:v", "3", str(out)]
    r = subprocess.run(argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    size = out.stat().st_size if out.exists() else 0
    return (r.returncode == 0 and size > 2048), size


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB))
    meta = {int(i): (p, d) for i, p, d in
            con.execute("SELECT file_id, path, duration_sec FROM files")}
    con.close()

    print(f"{'fid':>6} {'日志ts':>9} {'容器时长':>9} {'实际末帧':>9}  "
          f"{'ts原值':>8} {'ts-0.5':>8} {'ts-1.0':>8} {'末帧-0.1':>9}")
    n_orig_fail = n_shift_ok = 0
    for fid, ts in CASES:
        if fid not in meta:
            continue
        path = Path(meta[fid][0])
        if not path.exists():
            continue
        dur = meta[fid][1] or 0.0
        last = last_frame_ts(path)
        res = {}
        for label, t in (("orig", ts), ("m05", ts - 0.5), ("m10", ts - 1.0),
                         ("last", last - 0.1)):
            if t <= 0:
                res[label] = (False, 0)
                continue
            res[label] = run(path, t, OUT / f"{fid}_{label}.jpg")
        if not res["orig"][0]:
            n_orig_fail += 1
        if res["last"][0]:
            n_shift_ok += 1

        def mark(k):
            ok, size = res[k]
            return f"{'OK' if ok else 'FAIL':>8}"

        print(f"{fid:>6} {ts:>9.2f} {dur:>9.2f} {last:>9.2f}  "
              f"{mark('orig')} {mark('m05')} {mark('m10')} {mark('last')}")

    print(f"\n日志 ts 处失败：{n_orig_fail}/{len(CASES)}")
    print(f"改用「实际末帧 -0.1s」成功：{n_shift_ok}/{len(CASES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
