# -*- coding: utf-8 -*-
"""实测视频抽帧：串行 vs 并行（同一批真实视频片段）。

用临时库 + 真实视频，只跑 03_extract，比较「帧并行度 1」与「帧并行度 8」的耗时。
为了不做整片 scdet（太慢），设置 video.scdet_max_duration_sec 很小，
强制走定长切段路径 —— 两条路径的帧数完全一样，可以直接比时间。

用法：.venv\\Scripts\\python.exe tmp\bench_video_parallel.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import db as D  # noqa: E402
from src import extract as E  # noqa: E402
from src import manifest as M  # noqa: E402
from src.common import load_config  # noqa: E402

SAMPLES = [
    r"E:\晚上\迅雷下载\天使萌\SNIS-352_UC.mp4",
]


def pick_video() -> Path | None:
    import sqlite3

    cfg = load_config()
    con = sqlite3.connect(str(cfg.db_path))
    try:
        for r in con.execute(
                "SELECT path FROM files WHERE kind='video' AND status='done' "
                "AND size BETWEEN 3000000 AND 40000000 LIMIT 5"):
            p = Path(str(r[0]))
            if p.exists():
                return p
    finally:
        con.close()
    return None


def run(cfg, video: Path, workers: int) -> tuple[float, int, int]:
    cfg.override("performance.mode", "turbo")
    cfg.override("performance.frame_workers", workers)
    cfg.override("video.scdet_max_duration_sec", 1)      # 强制走定长切段
    cfg.override("video.force_split_sec", 60)
    ea = SimpleNamespace(kind="video", limit=None, shard=None, dry_run=False,
                         json=False, log_level=None, task_id=None,
                         no_fuse=True, no_cache=True)
    t0 = time.monotonic()
    s = E.run(ea, cfg)
    return time.monotonic() - t0, s.get("done", 0), s.get("frames", 0)


def main() -> int:
    video = Path(SAMPLES[0]) if Path(SAMPLES[0]).exists() else pick_video()
    if video is None:
        print("没有可用的测试视频")
        return 1
    print("测试视频:", video.name, f"{video.stat().st_size/1e6:.1f} MB")

    tmp = Path(tempfile.mkdtemp(prefix="vp_"))
    results = {}
    for w in (1, 8, 1, 8):
        media = tmp / f"m{w}_{time.time_ns()}"
        media.mkdir(parents=True)
        # 用符号链接不稳定，直接复制太慢；改为软链式：把视频复制到临时目录
        target = media / video.name
        shutil.copy2(video, target)
        cfg = load_config()
        cfg.root = tmp
        cfg.override("db.path", str(tmp / f"lib_{w}_{time.time_ns()}.db"))
        cfg.override("scan.min_photo_bytes", 1)
        cfg.override("scan.min_video_bytes", 1)
        D.ensure_schema(cfg.db_path, ROOT, cfg)
        args = SimpleNamespace(root=str(media), dry_run=False, limit=None, shard=None,
                               json=False, log_level=None, task_id=None, reprocess_all=False)
        M.run(args, cfg)
        dt, done, frames = run(cfg, target, w)
        print(f"  并行度={w}: {dt:.1f}s  完成 {done} 个文件 / {frames} 帧")
        results.setdefault(w, []).append(dt)

    a = min(results.get(1, [0]))
    b = min(results.get(8, [0]))
    print(f"\n串行最快 {a:.1f}s，并行最快 {b:.1f}s → 提速 {a/b:.1f}×")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
