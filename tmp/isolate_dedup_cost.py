"""在真实照片子集上隔离测量现有 02_dedup 的瓶颈：pHash 计算 vs 近重复比较。

做法：
  1) 建一个隔离库，从正式库复制前 N 张照片的元数据（不碰正式库、不影响 03_extract）
  2) 用 --config-overlay 指向该隔离库，跑真实的 02_dedup.py
  3) 同时在外部采样进程 CPU 与墙钟，并对关键循环做插桩（通过环境变量开关临时复刻）
由于不能改代码，这里直接调用 02_dedup 的函数并分别计时。
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WORK = ROOT / "tests" / "_dedup_bench"


def build_isolated_db(n: int) -> tuple[Path, Path]:
    """把前 n 张照片的元数据复制到隔离库，返回 (overlay 路径, db 路径)。"""
    import shutil

    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    db = WORK / "bench.db"

    src = sqlite3.connect(str(ROOT / "data" / "library.db"))
    src.row_factory = sqlite3.Row
    rows = src.execute(
        "SELECT path, rel_path, path_hash, inode, size, mtime, kind, status "
        "FROM files WHERE kind='photo' ORDER BY file_id LIMIT ?", (n,)).fetchall()
    src.close()

    dst = sqlite3.connect(str(db))
    from src import db as D
    from src.common import load_config

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
        f'  path: "{db.as_posix()}"\n', encoding="utf-8")

    cfg = load_config(None, None, overlay=[str(overlay)])
    cfg.ensure_dirs()
    D.ensure_schema(db, cfg.root, cfg)
    conn = D.connect(db, cfg=cfg)
    for r in rows:
        D.upsert_file(conn, path=str(r["path"]), rel_path=r["rel_path"],
                      inode=r["inode"], size=r["size"], mtime=r["mtime"],
                      kind="photo", status="pending")
    conn.commit()
    conn.close()
    dst.close()
    return overlay, db


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=4000)
    args = ap.parse_args()

    overlay, db = build_isolated_db(args.n)
    print(f"隔离库: {db}")
    print(f"照片数: {args.n}\n")

    # 直接导入 02_dedup 模块（文件名以数字开头，用 importlib 加载）
    spec = importlib.util.spec_from_file_location("dedup_mod", ROOT / "02_dedup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]

    from src import db as D
    from src.common import load_config

    cfg = load_config(None, None, overlay=[str(overlay)])
    conn = D.connect(db, cfg=cfg)
    rows = list(conn.execute(
        "SELECT file_id, path FROM files WHERE kind='photo' AND status IN ('pending','error')"))

    print("=== 1) 只测 pHash 计算（N 张）===")
    t0 = time.monotonic()
    hashes = {}
    fail = 0
    for r in rows:
        h = mod.compute_phash(Path(str(r["path"])), 8)
        if h is None:
            fail += 1
        else:
            hashes[int(r["file_id"])] = h
    t_hash = time.monotonic() - t0
    n = len(hashes)
    print(f"  成功 {n}  失败 {fail}  耗时 {t_hash:.2f}s → {n/max(t_hash,1e-9):.0f} 张/秒")
    print(f"  外推 41.3 万张 ≈ {413000/max(n/max(t_hash,1e-9),1e-9)/60:.1f} 分钟")

    print(f"\n=== 2) 只测近重复比较（现有朴素实现）===")
    hs = list(hashes.values())
    t0 = time.monotonic()
    comps = 0
    dups = 0
    by_hash: dict[str, int] = {}
    for i, h in enumerate(hs):
        dup = False
        for oh in by_hash:
            comps += 1
            if mod.hamming(h, oh) <= 8:
                dup = True
                break
        if dup:
            dups += 1
        else:
            by_hash[h] = i
    t_cmp = time.monotonic() - t0
    print(f"  比较 {comps:,} 次  耗时 {t_cmp:.2f}s → {comps/max(t_cmp,1e-9):,.0f} 次/秒")
    print(f"  检出近重复 {dups}/{n} ({dups/max(n,1)*100:.1f}%)")
    total_pairs = 413000 * 412999 // 2
    print(f"  外推 41.3 万张（朴素法，{total_pairs:,} 次比较）≈ "
          f"{total_pairs/max(comps/max(t_cmp,1e-9),1e-9)/3600:.1f} 小时")

    print(f"\n=== 3) 合计（当前实现）===")
    print(f"  pHash {t_hash:.1f}s + 比较 {t_cmp:.1f}s = {t_hash+t_cmp:.1f}s "
          f"（{n} 张）")
    print(f"  外推 41.3 万张 ≈ "
          f"{(413000/max(n/max(t_hash,1e-9),1e-9) + total_pairs/max(comps/max(t_cmp,1e-9),1e-9))/3600:.1f} 小时")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
