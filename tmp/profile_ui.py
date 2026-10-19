# -*- coding: utf-8 -*-
"""量一下界面里最贵的几个操作到底多慢 —— 决定「变白不能点」该修哪里。

变白/卡住的机制：Streamlit 每次脚本重跑期间前端会置灰等待。
所以「一次重跑多久」= 「屏幕被锁多久」。要定位就必须逐项计时。

用法：
    .venv\\Scripts\\python.exe tmp\\profile_ui.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import db as D  # noqa: E402
from src import ui_wall  # noqa: E402
from src.common import dir_size_bytes, load_config  # noqa: E402


def t(label, fn, n=1):
    fn()                       # 预热（文件系统缓存、sqlite 页缓存）
    t0 = time.monotonic()
    for _ in range(n):
        out = fn()
    dt = (time.monotonic() - t0) / n
    print(f"  {label:<42} {dt*1000:8.1f} ms")
    return out, dt


def main() -> int:
    cfg = load_config()
    D.ensure_schema(cfg.db_path, cfg.root, cfg)
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)

    print("单项耗时（第 2 次及以后，含 FS 缓存）")
    t("library_stats(conn, media_root)", lambda: D.library_stats(conn, str(cfg.media_root)), 3)
    t("  ↳ 其中 by_status", lambda: conn.execute(
        "SELECT status, COUNT(*) FROM files GROUP BY status").fetchall(), 3)
    t("  ↳ 其中 photos/videos done",
      lambda: conn.execute("SELECT kind, status, COUNT(*) FROM files GROUP BY 1,2").fetchall(), 3)
    t("ui_wall.wall_page(overview, 60)",
      lambda: ui_wall.wall_page(conn, cfg, limit=60, sort=ui_wall.SORT_CAPTURED_DESC,
                                mode="overview"), 3)
    t("ui_wall.wall_facets", lambda: ui_wall.wall_facets(conn, cfg), 3)
    t("tasks: latest_event_id", lambda: _events(cfg), 3)
    t("tasks: running()", lambda: _running(cfg), 3)

    print("\n贵操作（第 1 次 vs 第 2 次）")
    nvme = Path(str(cfg.get("paths.nvme_root")))
    for i in (1, 2):
        t0 = time.monotonic()
        sz = dir_size_bytes(nvme)
        print(f"  dir_size_bytes(nvme) 第 {i} 次            "
              f"{(time.monotonic()-t0)*1000:8.1f} ms   {sz/2**30:.1f} GiB")

    conn.close()
    print("\n说明：Streamlit 每次交互都会重跑整个脚本，"
          "上面这些如果不在缓存里，就会被加进「屏幕被锁住」的时间里。")
    return 0


def _events(cfg):
    from src import tasks as T

    T.init(cfg)
    return T.latest_event_id(cfg)


def _running(cfg):
    from src import tasks as T

    T.init(cfg)
    return T.running(cfg)


if __name__ == "__main__":
    raise SystemExit(main())
