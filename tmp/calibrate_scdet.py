"""校准 ffmpeg scdet 阈值，使其与现有 PySceneDetect threshold=30 的行为对齐。

方法：
  1) 用 scdet=threshold=0:sc_pass=1 放行所有帧，从 metadata 文件读出每帧的
     lavfi.scd.score（元数据包含真实分数）
  2) 在分数序列上模拟「以 X 为阈值 + min_shot_len」得到切点数
  3) 与 PySceneDetect(threshold=30) 的切点数比较，选出最接近的 X
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
FFMPEG = Path(r"D:\PROGRAM\1_ffmpeg-9.0.1-essentials_build\bin\ffmpeg.exe")

MIN_SHOT = 0.8
SCORE_RE = re.compile(r"lavfi\.scd\.score=([0-9.]+)")
PTS_RE = re.compile(r"pts_time:([0-9.]+)")


def pick(n: int = 3) -> list[tuple[int, Path]]:
    con = sqlite3.connect(str(DB))
    rows = con.execute(
        "SELECT file_id, path FROM files WHERE kind='video' "
        "AND duration_sec BETWEEN 60 AND 900 ORDER BY file_id LIMIT ?", (n,)).fetchall()
    con.close()
    return [(int(i), Path(p)) for i, p in rows if Path(p).exists()]


def scores_via_metadata(path: Path) -> list[tuple[float, float]]:
    """返回 [(pts_time, score), ...]（全部帧）。"""
    with tempfile.TemporaryDirectory(dir=str(ROOT / "tmp")) as td:
        mf = Path(td) / "m.txt"
        subprocess.run(
            [str(FFMPEG), "-hide_banner", "-nostdin", "-i", str(path),
             "-vf", f"scdet=threshold=0:sc_pass=1,metadata=print:file={mf.as_posix()}",
             "-an", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=3600)
        if not mf.exists():
            return []
        out: list[tuple[float, float]] = []
        cur_t: float | None = None
        for line in mf.read_text(encoding="utf-8", errors="replace").splitlines():
            m = PTS_RE.search(line)
            if m:
                try:
                    cur_t = float(m.group(1))
                except ValueError:
                    cur_t = None
                continue
            s = SCORE_RE.search(line)
            if s and cur_t is not None:
                try:
                    out.append((cur_t, float(s.group(1))))
                except ValueError:
                    pass
        return out


def cuts_at(pairs: list[tuple[float, float]], threshold: float) -> list[float]:
    cuts: list[float] = []
    prev = None
    for t, sc in pairs:
        if sc >= threshold and (prev is None or t - prev >= MIN_SHOT):
            cuts.append(t)
            prev = t
    return cuts


def pyscenedetect(path: Path) -> list[float]:
    from scenedetect import ContentDetector, SceneManager, open_video
    v = open_video(str(path))
    sm = SceneManager()
    sm.add_detector(ContentDetector(threshold=30.0))
    sm.detect_scenes(v, show_progress=False)
    return [s[0].seconds for s in sm.get_scene_list()]


def main() -> int:
    for fid, path in pick(3):
        pairs = scores_via_metadata(path)
        print(f"\n=== file_id={fid}  {path.name[:48]} ===")
        print(f"  metadata 帧数={len(pairs)}")
        if not pairs:
            print("  ✗ 没读到分数")
            continue
        vals = sorted((s for _t, s in pairs), reverse=True)
        print(f"  分数分布（前 12 高）：{[round(v, 1) for v in vals[:12]]}")
        try:
            py = pyscenedetect(path)
        except Exception as exc:  # noqa: BLE001
            print(f"  PySceneDetect 失败：{exc}")
            py = []
        print(f"  PySceneDetect(30) 切点 = {len(py)}")
        print(f"  {'ffmpeg阈值':>10} {'切点数':>7}   与 Py 差异")
        for th in (6.0, 8.0, 10.0, 12.0, 15.0, 20.0, 25.0, 30.0):
            c = cuts_at(pairs, th)
            print(f"  {th:>10.1f} {len(c):>7}   {abs(len(c) - len(py)):>6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
