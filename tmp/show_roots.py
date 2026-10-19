# -*- coding: utf-8 -*-
"""看一眼素材目录登记表：每个目录的文件数、扫描状态、是否可访问。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import media_roots as MR  # noqa: E402
from src.common import load_config  # noqa: E402


def main() -> int:
    cfg = load_config()
    print(f"config media_root = {cfg.media_root}")
    roots = MR.list_roots(cfg)
    print(f"登记了 {len(roots)} 个素材文件夹：")
    for r in roots:
        state = "启用" if r["enabled"] else "停用"
        exists = "" if r["exists"] else "  ⚠️ 路径不存在"
        print(f"  - {r['path']}{exists}")
        print(f"      {state} · 文件 {r['files']:,} · 已处理 {r['done']:,} · "
              f"上次扫描 {r['last_scan'] or '从未'}")
    print(f"\n扫描时会用到的目录：{MR.enabled_roots(cfg)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
