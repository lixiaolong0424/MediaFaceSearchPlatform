"""用已验证可用的 scdet=threshold=X:sc_pass=1,showinfo 逐阈值统计切点数，
与 PySceneDetect(threshold=30) 对比，选出最接近的 ffmpeg 阈值。

（放弃 metadata=print：实测它不产出文件，不可靠。）
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
FFMPEG = Path(r"D:\PROGRAM\1_ffmpeg-9.0.1-essentials_build\bin\ffmpeg.exe")
PTS_RE = re.compile(r"pts_time:([0-9.]+)")
MIN_SHOT = 0.8


def pick(n: int = 3) -> list[tuple[int, Path]]:
    con = sqlite3.connect(str(DB))
    rows = con.execute(
        "SELECT file_id, path FROM files WHERE kind='video' "
        "AND duration_sec BETWEEN 60 AND 900 ORDER BY file_id LIMIT ?", (n,)).fetchall()
    con.close()
    return [(int(i), Path(p)) for i, p in rows if Path(p).exists()]


def scdet_cuts(path: Path, threshold: float) -> tuple[int, list[float]]:
    argv = [str(FFMPEG), "-hide_banner", "-nostdin", "-i", str(path),
            "-vf", f"scdet=threshold={threshold}:sc_pass=1,showinfo",
            "-an", "-f", "null", "-"]
    r = subprocess.run(argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=3600)
    ts = []
    for m in PTS_RE.finditer(r.stderr or ""):
        try:
            ts.append(float(m.group(1)))
        except ValueError:
            pass
    # 应用 min_shot_len 过滤
    cuts: list[float] = []
    prev = None
    for t in ts:
        if prev is None or t - prev >= MIN_SHOT:
            cuts.append(t)
            prev = t
    return len(cuts), cuts


def pyscenedetect(path: Path) -> int:
    from scenedetect import ContentDetector, SceneManager, open_video
    v = open_video(str(path))
    sm = SceneManager()
    sm.add_detector(ContentDetector(threshold=30.0))
    sm.detect_scenes(v, show_progress=False)
    return len(sm.get_scene_list())


def main() -> int:
    thresholds = [5.0, 8.0, 10.0, 12.0, 15.0, 20.0]
    totals = {th: 0 for th in thresholds}
    py_total = 0
    for fid, path in pick(3):
        try:
            py = pyscenedetect(path)
        except Exception as exc:  # noqa: BLE001
            print(f"file_id={fid} PySceneDetect 失败：{exc}")
            continue
        py_total += py
        line = [f"file_id={fid:<5} Py={py:<5}"]
        for th in thresholds:
            n, _ = scdet_cuts(path, th)
            totals[th] += n
            line.append(f"T{th:g}={n:<4}")
        print("  ".join(line))
    print(f"\n合计 PySceneDetect(30) = {py_total}")
    print(f"{'ffmpeg阈值':>10} {'合计切点':>9} {'与Py比值':>9}")
    for th in thresholds:
        ratio = totals[th] / max(1, py_total)
        mark = "  ← 最接近" if abs(ratio - 1.0) < 0.25 else ""
        print(f"{th:>10.1f} {totals[th]:>9} {ratio:>9.2f}{mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
