"""用 03_extract 日志里**真正失败过的帧时间点**复现 mjpeg 报错，并验证修复方案。

关键：之前随机挑的时间点全部成功 → 说明该错误只发生在特定帧（typically 那些
源帧本身是 limited-range 的转场/淡入帧），必须精确命中原始失败点才能复现。
这些失败点可以从 shots 表 + 日志中的 ts 反推出来。
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
OUT = ROOT / "tmp" / "_mjpeg_test2"
FFMPEG = Path(r"D:\PROGRAM\1_ffmpeg-9.0.1-essentials_build\bin\ffmpeg.exe")

EDGE = 384
SCALE = (f"scale=w='if(gt(iw,ih),trunc(iw*{EDGE}/ih/2)*2,{EDGE})':"
         f"h='if(gt(iw,ih),{EDGE},trunc(ih*{EDGE}/iw/2)*2)'")

# 日志里真实失败的 (file_id, ts)
FAILED = [
    (1, 587.25),
    (3, 1312.83),
    (4, 3003.03),
    (22, 280.35),
    (29, 720.86),
    (34, 902.35),
    (55, 19.27),
    (77, 1643.57),
    (84, 686.55),
    (950, 8.17),
    (953, 6.41),
    (960, 25.17),
    (962, 6.41),
    (964, 21.17),
    (1329, 8.59),
    (1330, 15.02),
    (1525, 4.01),
    (1527, 6.71),
]

VARIANTS = {
    "原实现": ["-vf", SCALE, "-q:v", "3"],
    "修复: format=yuvj420p": ["-vf", f"{SCALE},format=yuvj420p", "-q:v", "3"],
    "修复: + -strict unofficial": ["-vf", f"{SCALE},format=yuvj420p", "-q:v", "3",
                                   "-strict", "unofficial"],
}


def run(video: Path, ts: float, args: list[str], out: Path) -> tuple[bool, str, int]:
    if out.exists():
        out.unlink()
    argv = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-threads", "1", "-ss", f"{ts:.3f}", "-i", str(video),
            "-frames:v", "1", *args, str(out)]
    r = subprocess.run(argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=240)
    size = out.stat().st_size if out.exists() else 0
    err = ""
    if r.returncode != 0:
        lines = [l for l in (r.stderr or "").splitlines() if l.strip()]
        err = lines[0][:95] if lines else f"rc={r.returncode}"
    return (r.returncode == 0 and size > 0), err, size


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB))
    paths = {int(fid): p for fid, p in con.execute("SELECT file_id, path FROM files")}
    con.close()

    stats = {k: [0, 0] for k in VARIANTS}
    reproduced = 0
    print(f"{'file_id':>8} {'ts':>9}  " + "  ".join(f"{k:<24}" for k in VARIANTS))
    for fid, ts in FAILED:
        p = paths.get(fid)
        if not p or not Path(p).exists():
            continue
        cells = []
        base_failed = False
        for name, args in VARIANTS.items():
            out = OUT / f"{fid}_{int(ts * 100)}.jpg"
            ok, err, size = run(Path(p), ts, args, out)
            stats[name][1] += 1
            if ok:
                stats[name][0] += 1
            else:
                cells.append(f"{name}:FAIL {err[:40]}")
            if name == "原实现" and not ok:
                base_failed = True
        if base_failed:
            reproduced += 1
        mark = " ←复现" if base_failed else ""
        print(f"{fid:>8} {ts:>9.2f}  " + "  ".join(f"{c[:40]:<40}" for c in cells) or "", mark)

    print(f"\n=== 汇总 ===")
    print(f"  原实现失败并成功复现的点数：{reproduced}")
    for name, (ok, total) in stats.items():
        print(f"  {name:<28} 成功 {ok:>3}/{total:<3}  失败 {total - ok}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
