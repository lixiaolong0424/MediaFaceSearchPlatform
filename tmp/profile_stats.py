# -*- coding: utf-8 -*-
"""量 library_stats / 墙面查询在加索引 + 合并查询之后是否达标。

目标：单次 < 150ms（Streamlit 重跑才不会让用户觉得卡）。
"""
from __future__ import annotations

import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import db as D  # noqa: E402
from src import ui_wall  # noqa: E402
from src.common import load_config  # noqa: E402


def main() -> int:
    cfg = load_config()
    print(f"迁移前版本 → {D.ensure_schema(cfg.db_path, cfg.root, cfg)}")
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        for i in range(3):
            t0 = time.monotonic()
            s = D.library_stats(conn, str(cfg.media_root))
            dt = time.monotonic() - t0
            print(f"  library_stats 第{i+1}次 {dt*1000:7.0f} ms  "
                  f"photos={s['photos']:,} videos={s['videos']:,} "
                  f"faces={s['faces']:,} photo_done={s['photos_done']:,} "
                  f"video_done={s['videos_done']:,} video_pending={s['videos_pending']:,}")
        for i in range(2):
            t0 = time.monotonic()
            p = ui_wall.wall_page(conn, cfg, limit=60, sort=ui_wall.SORT_CAPTURED_DESC,
                                  mode="overview")
            print(f"  wall_page      第{i+1}次 {(time.monotonic()-t0)*1000:7.0f} ms  "
                  f"total={p['total']:,} items={len(p['items'])}")
        t0 = time.monotonic()
        f = ui_wall.wall_facets(conn, cfg)
        print(f"  wall_facets    {(time.monotonic()-t0)*1000:7.0f} ms  files={len(f['files'])}")
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
