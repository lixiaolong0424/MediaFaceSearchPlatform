"""方案 D 端到端回归：在隔离库上跑真实视频，验证
  1) 生产路径 process_video 正常（shots/关键帧入库）
  2) mjpeg 次生错误（Non full-range YUV）不再出现
  3) 耗时与预期一致（scdet 生效）
不触碰正式库 data/library.db。
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import db as D  # noqa: E402
from src.common import load_config  # noqa: E402
from src.extract import (  # noqa: E402
    ensure_stage_table, locate_tools, process_video,
)

WORK = ROOT / "tests" / "_plan_d"
N = 6


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)

    overlay = WORK / "cfg.yaml"
    overlay.write_text(
        f'project_root: "{ROOT.as_posix()}"\n'
        f'media_root: "E:/"\n'
        "paths:\n"
        f'  nvme_root: "{(WORK / "nvme").as_posix()}"\n'
        f'  frames: "{(WORK / "nvme" / "frames").as_posix()}"\n'
        f'  thumbs: "{(WORK / "nvme" / "thumbs").as_posix()}"\n'
        f'  faces_raw: "{(WORK / "nvme" / "faces" / "raw").as_posix()}"\n'
        f'  faces_thumbs: "{(WORK / "nvme" / "faces" / "thumbs").as_posix()}"\n'
        f'  embeddings: "{(WORK / "nvme" / "embeddings").as_posix()}"\n'
        f'  query_cache: "{(WORK / "nvme" / "query_cache").as_posix()}"\n'
        f'  export: "{(WORK / "export").as_posix()}"\n'
        f'  logs: "{(WORK / "logs").as_posix()}"\n'
        f'  tmp: "{(WORK / "tmp").as_posix()}"\n'
        "db:\n"
        f'  path: "{(WORK / "regress.db").as_posix()}"\n',
        encoding="utf-8")

    cfg = load_config(None, None, overlay=[str(overlay)])
    cfg.ensure_dirs()
    tools = locate_tools(cfg)
    D.ensure_schema(cfg.db_path, cfg.root, cfg)
    conn = D.connect(cfg.db_path, cfg=cfg)
    ensure_stage_table(conn)
    conn.commit()

    # 从正式库借真实视频路径，写入隔离库
    src = D.connect((ROOT / "data" / "library.db"), cfg=cfg, readonly=True)
    rows = list(src.execute(
        "SELECT path, kind, size, mtime, duration_sec FROM files WHERE kind='video' "
        "AND duration_sec BETWEEN 30 AND 600 ORDER BY file_id LIMIT ?", (N,)))
    src.close()

    print(f"ffmpeg = {tools.ffmpeg}")
    print(f"隔离库 = {cfg.db_path}\n")

    t_all = time.monotonic()
    print(f"{'fid':>5} {'时长':>8} {'镜头':>5} {'帧':>4} {'耗时':>8}  状态")
    for i, r in enumerate(rows, 1):
        p = Path(str(r["path"]))
        if not p.exists():
            continue
        fid = D.upsert_file(conn, path=str(p), rel_path=p.name, kind="video",
                            size=int(r["size"] or 0), mtime=int(r["mtime"] or 0),
                            status="pending")
        conn.commit()
        t0 = time.monotonic()
        res = process_video(p, fid, cfg, conn, tools)
        dt = time.monotonic() - t0
        conn.commit()
        print(f"{fid:>5} {r['duration_sec'] or 0:>8.1f} {res.shots:>5} {res.frames:>4} "
              f"{dt:>7.2f}s  {res.status}{'  ' + res.error[:60] if res.error else ''}")

    print(f"\n总耗时 {time.monotonic() - t_all:.1f}s")

    # 校验产物
    shots = conn.execute("SELECT COUNT(*), COALESCE(SUM(n_frames),0) FROM shots").fetchone()
    zero = conn.execute("SELECT COUNT(*) FROM shots WHERE COALESCE(n_frames,0)=0").fetchone()[0]
    jpgs = list((WORK / "nvme" / "frames").rglob("*.jpg"))
    small = [p for p in jpgs if p.stat().st_size <= 2048]
    conn.close()

    print(f"\n=== 产物校验 ===")
    print(f"  shots            : {shots[0]:,}")
    print(f"  关键帧（DB 计）  : {shots[1]:,}")
    print(f"  0 帧镜头         : {zero}")
    print(f"  关键帧文件       : {len(jpgs):,}（<=2KB 的 {len(small)} 个）")

    # 日志里是否还有 mjpeg 次生错误
    logf = WORK / "logs"
    hits = []
    for f in logf.glob("*.log") if logf.exists() else []:
        txt = f.read_text(encoding="utf-8", errors="replace")
        hits += [l for l in txt.splitlines() if "Non full-range YUV" in l]
    print(f"  Non full-range YUV 报错 : {len(hits)} 条"
          f"  {'✓ 已消除' if not hits else '✗ 仍存在'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
