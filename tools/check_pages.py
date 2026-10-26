# -*- coding: utf-8 -*-
"""逐页检查路由：点击导航后 page 是否真的切换、页面是否渲染出预期标题。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402

EXPECT = {
    "wall": "照片墙",
    "search": "搜索",
    "tasks": "任务",
    "settings": "设置",
    "maintain": "维护",
    "about": "关于",
}


def main() -> int:
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300)
    at.run()
    print("首屏 page =", at.session_state.get("page"))
    bad = 0
    for pg, want in EXPECT.items():
        at.session_state["nav_seg"] = pg
        at.run()
        page = at.session_state.get("page")
        heads = [str(m.value) for m in at.markdown if str(m.value).startswith("## ")]
        exc = [f"{e.type}: {str(e.message)[:60]}" for e in at.exception]
        ok = page == pg and any(want in h for h in heads) and not exc
        print(f'  nav={pg:<9} page={page!s:<9} 标题={heads[:2]} 异常={exc} -> {"OK" if ok else "**切换失败**"}')
        bad += 0 if ok else 1
    print(f"\n失败 {bad} 个")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
