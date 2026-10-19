"""任务1监视 + 任务2前置：一次拿齐
  1) 03_extract 当前进度（是否在推进、是否真完成）
  2) 02_dedup 修复所需的数据画像（真实 pHash 的分桶占用偏斜度）
"""

from __future__ import annotations

import collections
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
FRAMES = ROOT / "data" / "nvme" / "frames"


def snapshot() -> dict:
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    d: dict = {}
    d["files"] = {r["status"]: r["n"] for r in con.execute(
        "SELECT status, COUNT(*) n FROM files GROUP BY status")}
    try:
        d["stage"] = {f"{r['stage']}:{r['status']}": r["n"] for r in con.execute(
            "SELECT stage, status, COUNT(*) n FROM stage_status GROUP BY stage, status")}
    except sqlite3.OperationalError:
        d["stage"] = {}
    d["shots"] = con.execute("SELECT COUNT(*) FROM shots").fetchone()[0]
    d["frames_db"] = con.execute(
        "SELECT COALESCE(SUM(n_frames),0) FROM shots").fetchone()[0]
    d["zero_frame_shots"] = con.execute(
        "SELECT COUNT(*) FROM shots WHERE COALESCE(n_frames,0)=0").fetchone()[0]
    d["phash_done"] = con.execute(
        "SELECT COUNT(*) FROM files WHERE phash IS NOT NULL").fetchone()[0]
    con.close()
    d["frame_dirs"] = sum(1 for p in FRAMES.iterdir() if p.is_dir()) if FRAMES.exists() else 0
    d["frame_files"] = sum(1 for p in FRAMES.rglob("*.jpg")) if FRAMES.exists() else 0
    return d


def main() -> int:
    a = snapshot()
    print("=== 03_extract 进度快照 ===")
    print(f"  files.status      : {a['files']}")
    print(f"  stage_status      : {a['stage']}")
    print(f"  shots             : {a['shots']:,}")
    print(f"  关键帧（DB 计）   : {a['frames_db']:,}")
    print(f"  0 帧镜头          : {a['zero_frame_shots']:,}")
    print(f"  frames 目录       : {a['frame_dirs']:,}")
    print(f"  frames 文件       : {a['frame_files']:,}")
    print(f"  phash 已写        : {a['phash_done']:,}")
    print("\n  等待 60 秒测增量…")
    time.sleep(60)
    b = snapshot()
    print(f"\n  60s 增量：frames 文件 +{b['frame_files'] - a['frame_files']}  "
          f"目录 +{b['frame_dirs'] - a['frame_dirs']}  "
          f"shots +{b['shots'] - a['shots']}  "
          f"stage_status +{sum(b['stage'].values()) - sum(a['stage'].values())}")
    print(f"  files.status      : {b['files']}")
    active = (b["frame_files"] - a["frame_files"]) > 0 or \
             sum(b["stage"].values()) > sum(a["stage"].values())
    print(f"  → {'仍在推进' if active else '⚠ 60 秒无进展（可能卡住或在处理长视频）'}")

    # ---- 02_dedup 修复所需：真实 pHash 分桶占用 ----
    print("\n=== 02_dedup 修复前置：真实 pHash 分桶占用 ===")
    print("  算法：把 64bit 哈希切成 4 段 16bit。两哈希汉明距离≤8 时，")
    print("        鸽巢原理保证「至少一段完全相同」→ 每段建索引，查 4 次即可。")
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    rows = [r[0] for r in con.execute(
        "SELECT phash FROM files WHERE phash IS NOT NULL LIMIT 200000")]
    con.close()
    if not rows:
        print("  库里还没有 phash（02 未跑过），用已有照片现场算一批来画像…")
        import imagehash
        from PIL import Image, ImageFile
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
        paths = [r[0] for r in con.execute(
            "SELECT path FROM files WHERE kind='photo' AND status='done' LIMIT 3000")]
        con.close()
        for p in paths:
            try:
                with Image.open(p) as im:
                    rows.append(str(imagehash.phash(im.convert("RGB"), hash_size=8)))
            except Exception:  # noqa: BLE001
                continue
        print(f"  现场算出 {len(rows):,} 个 phash")

    if not rows:
        print("  无样本可分析")
        return 0

    ints = [int(h, 16) for h in rows]
    for shift in (48, 32, 16, 0):
        cnt = collections.Counter((v >> shift) & 0xFFFF for v in ints)
        occ = sorted(cnt.values(), reverse=True)
        print(f"  段 shift={shift:>2}: 桶数={len(cnt):,}  最大={occ[0]:,}  "
              f"前10大={occ[:10]}  >100的桶={sum(1 for o in occ if o > 100):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
