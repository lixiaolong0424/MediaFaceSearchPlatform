"""真实素材「以图搜图」冒烟：取库内一张照片作为查询图，验证跨模态检索链路。

用法：.venv\\Scripts\\python.exe tests\probe_real_search.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import db as D  # noqa: E402
from src import search as SE  # noqa: E402
from src.common import load_config  # noqa: E402
from src.vector_store import open_store  # noqa: E402

DB = ROOT / "tests" / "_real" / "library_real.db"


def main() -> int:
    cfg = load_config(None, None, overlay=[str(ROOT / "tests" / "config_real.yaml")])
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT path FROM files WHERE kind='photo' LIMIT 1").fetchone()
    vid = con.execute("SELECT path FROM files WHERE kind='video' LIMIT 1").fetchone()
    con.close()
    if row is None:
        print("库中没有照片可用作查询图")
        return 1

    qimg = Path(str(row["path"]))
    print(f"查询图：{qimg}")
    print(f"（其自身应当在结果首位，相似度 ≈ 1.0）\n")

    store = open_store(cfg)
    ok, msg = store.health()
    if not ok:
        print(msg)
        return 1
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        for label, kwargs in (
            ("以图搜图（语义）", dict(query_mode="image", payload={"image": qimg},
                                      exact_person=False)),
            ("视频查询（语义）", dict(query_mode="video", payload={"video": Path(str(vid["path"]))},
                                      exact_person=False) if vid else None),
        ):
            if kwargs is None:
                continue
            print(f"===== {label} =====")
            outcome = SE.search(conn, cfg, store, thresh=0.05, k=200, **kwargs)
            print(f"  模式={outcome.mode} 命中 {outcome.total_shots} 镜头 / {outcome.total_files} 文件")
            print(f"  {outcome.cut_note}")
            if outcome.warning:
                print(f"  warning: {outcome.warning}")
            for f in outcome.files[:5]:
                seg = f.segments[0] if f.segments else {}
                print(f"    {f.kind:<6} rank={f.rank_score:.3f} best={f.best_score:.3f} "
                      f"镜头={f.n_shots} 段={seg.get('start')}~{seg.get('end')} "
                      f"{Path(str(f.path)).name[:50]}")
            print()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
