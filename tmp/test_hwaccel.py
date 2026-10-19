"""确认 hwaccel=cuda 是否会让抽帧失败（决定 config 里 hwaccel 该怎么写）。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.common import load_config  # noqa: E402
from src.extract import extract_frame, locate_tools  # noqa: E402


def main() -> int:
    cfg = load_config()
    cfg.ensure_dirs()
    tools = locate_tools(cfg)
    con = sqlite3.connect(str(ROOT / "data" / "library.db"))
    row = con.execute(
        "SELECT file_id, path FROM files WHERE kind='video' "
        "AND duration_sec BETWEEN 60 AND 900 ORDER BY file_id LIMIT 1").fetchone()
    con.close()
    if not row:
        print("没有可用视频")
        return 1
    path = Path(row[1])
    print(f"视频：{path.name[:60]}\n")

    for hw in ("auto", "cuda", "none"):
        out = ROOT / "tmp" / f"_hw_{hw}.jpg"
        ok, err = extract_frame(tools.ffmpeg, path, 30.0, out, 384, hwaccel=hw)
        size = out.stat().st_size if out.exists() else 0
        print(f"  hwaccel={hw:<5} ok={str(ok):<5} size={size:>7}B  {err[:130]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
