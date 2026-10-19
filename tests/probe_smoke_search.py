r"""冒烟库上的检索链路探针：以图搜图 / 视频查询，断言自身命中且相似度合理。

用于 tests\p0_smoke.py 的回归断言。用法：
    .venv\Scripts\python.exe tests\probe_smoke_search.py --mode image
    .venv\Scripts\python.exe tests\probe_smoke_search.py --mode video
退出码 0 = 通过。
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import db as D  # noqa: E402
from src import search as SE  # noqa: E402
from src.common import load_config  # noqa: E402
from src.vector_store import open_store  # noqa: E402

OVERLAY = ROOT / "tests" / "config_smoke.yaml"
DB = ROOT / "tests" / "_smoke" / "library_smoke.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", required=True, choices=["image", "video"])
    args = ap.parse_args()

    cfg = load_config(None, None, overlay=[str(OVERLAY)])
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    kind = "photo" if args.mode == "image" else "video"
    row = con.execute(
        "SELECT file_id, path FROM files WHERE kind=? AND status='done' LIMIT 1", (kind,)
    ).fetchone()
    con.close()
    if row is None:
        print(f"库中没有可用的 {kind} 素材")
        return 1

    src = Path(str(row["path"]))
    store = open_store(cfg)
    ok, msg = store.health()
    if not ok:
        print(msg)
        return 1

    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        kwargs = (dict(query_mode="image", payload={"image": src})
                  if args.mode == "image"
                  else dict(query_mode="video", payload={"video": src}))
        outcome = SE.search(conn, cfg, store, exact_person=False, thresh=0.05, k=200, **kwargs)
    finally:
        conn.close()

    print(f"mode={outcome.mode} 命中 {outcome.total_shots} 镜头 / {outcome.total_files} 文件")
    print(f"  {outcome.cut_note}")
    if not outcome.files:
        print("  ✗ 未命中（查询素材自身应当命中）")
        return 1
    top = outcome.files[0]
    print(f"  top: file_id={top.file_id} score={top.best_score:.3f} "
          f"{Path(str(top.path)).name[:60]}")
    if top.file_id != int(row["file_id"]):
        print(f"  ✗ 首位不是查询素材自身（期望 file_id={row['file_id']}）")
        return 1
    if top.best_score < 0.80:
        print(f"  ✗ 自身相似度偏低 {top.best_score:.3f} < 0.80")
        return 1
    print(f"  ✓ {args.mode} 查询通过（自身相似度 {top.best_score:.3f}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
