"""量界面首屏各个组件的真实耗时，确认白屏已被消除。

不依赖浏览器：直接调 app.py 用到的同一条数据路径。
    _nvme_used_bytes(str(nvme_root))   ← 曾经的元凶（rglob 遍历 14 万条目）
    D.library_stats(...)               ← 多个 COUNT
    ui_wall.wall_page(...) + wall_facets(...)   ← Tab1 主体
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(r'D:\DATA\PROJECT\建设多媒体整理系统')
sys.path.insert(0, str(ROOT))

from src import db as D  # noqa: E402
from src import ui_wall  # noqa: E402
from src.common import load_config  # noqa: E402


def t(label, fn):
    t0 = time.monotonic()
    try:
        out = fn()
        el = time.monotonic() - t0
        print(f"  {label:<46} {el:7.2f}s   ok")
        return out, el
    except Exception as exc:  # noqa: BLE001
        el = time.monotonic() - t0
        print(f"  {label:<46} {el:7.2f}s   ERR {type(exc).__name__}: {exc}")
        return None, el


cfg = load_config(None, None)
conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)

# 直接内联 app.py 里那个 scandir 版实现（避免 import streamlit）
def nvme_bytes():
    total = 0
    root = cfg.path("paths.nvme_root")
    if not root.exists():
        return 0
    stack = [root]
    import os
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat().st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


print("=== 首屏组件耗时 ===")
b, e1 = t("_nvme_used_bytes（现版：os.scandir）", nvme_bytes)
print(f"      → {b/1e9:.2f} GB")
st, e2 = t("D.library_stats", lambda: D.library_stats(conn, str(cfg.media_root)))
page, e3 = t("ui_wall.wall_page(mode=tile)", lambda: ui_wall.wall_page(
    conn, cfg, limit=60, mode="tile", min_quality=0.3))
fac, e4 = t("ui_wall.wall_facets", lambda: ui_wall.wall_facets(conn, cfg))
ov, e5 = t("ui_wall.wall_page(mode=overview)", lambda: ui_wall.wall_page(
    conn, cfg, limit=60, mode="overview", min_quality=0.3))

print(f"\n  合计 {e1+e2+e3+e4:.2f}s（overview 与 tile 只取其一）")
print(f"  墙上人脸 {page['total']:,} 张，本页 {len(page['items'])} 条"
      if page else "  wall_page 失败")
conn.close()
