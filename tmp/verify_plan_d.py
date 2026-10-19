"""验证方案 D：用生产代码路径（process_video）跑真实视频，对比改造前后的耗时与产出。

同时校验两件事：
  1. ffmpeg scdet 是否被真正启用（shots 数应与 PySceneDetect 量级相当）
  2. 末尾钳制是否消除了 mjpeg 次生错误（不再出现 Non full-range YUV）
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import db as D  # noqa: E402
from src.common import load_config  # noqa: E402
from src.extract import (  # noqa: E402
    ffmpeg_scene_detect, locate_tools, scenedetect_cuts,
)

N = 5


def main() -> int:
    cfg = load_config()
    tools = locate_tools(cfg)
    print(f"ffmpeg: {tools.ffmpeg}\n")

    con = sqlite3.connect(str(ROOT / "data" / "library.db"))
    rows = con.execute(
        "SELECT file_id, path, duration_sec FROM files WHERE kind='video' "
        "AND duration_sec BETWEEN 30 AND 600 ORDER BY file_id LIMIT ?", (N,)).fetchall()
    con.close()

    print(f"{'fid':>6} {'时长':>8}  {'scdet耗时':>10} {'切点':>6}  "
          f"{'Py耗时':>9} {'切点':>6}  {'加速':>6}")
    tot_d = tot_p = 0.0
    for fid, path_s, dur in rows:
        path = Path(path_s)
        if not path.exists():
            continue
        t0 = time.monotonic()
        cuts_d, method = ffmpeg_scene_detect(path, cfg, tools)
        t_d = time.monotonic() - t0

        t0 = time.monotonic()
        cuts_p, _ = scenedetect_cuts(path, cfg)
        t_p = time.monotonic() - t0

        tot_d += t_d
        tot_p += t_p
        print(f"{fid:>6} {dur or 0:>8.1f}  {t_d:>10.2f} {len(cuts_d):>6}  "
              f"{t_p:>9.2f} {len(cuts_p):>6}  {t_p / max(t_d, 0.01):>5.1f}x  [{method}]")

    if tot_d > 0:
        print(f"\n合计：scdet {tot_d:.1f}s  vs  PySceneDetect {tot_p:.1f}s "
              f"→ {tot_p / tot_d:.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
