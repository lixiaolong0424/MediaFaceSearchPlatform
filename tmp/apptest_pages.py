"""逐页无头渲染：把 6 个页面各跑一遍，确认都没有异常。

用法：
    .venv\\Scripts\\python.exe tmp\\apptest_pages.py            # 全部页面
    .venv\\Scripts\\python.exe tmp\\apptest_pages.py settings    # 只测某页
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(r"D:\DATA\PROJECT\建设多媒体整理系统")
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402

PAGES = ["wall", "search", "tasks", "settings", "maintain", "about"]


def run_page(page: str) -> int:
    t0 = time.monotonic()
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=300)
    at.session_state["page"] = page
    at.session_state["nav_seg"] = page
    if page == "settings":
        at.session_state["show_adv_toggle"] = True
    at.run()
    el = time.monotonic() - t0
    n_md = len(at.markdown)
    n_btn = len(at.button)
    n_cap = len(at.caption)
    if at.exception:
        print(f"[FAIL] {page:<9} {el:5.1f}s")
        for e in at.exception:
            print(f"       {e.type}: {e.message}")
            if getattr(e, "stack_trace", None):
                print("       " + "\n       ".join(str(e.stack_trace).splitlines()[-8:]))
        return 1
    print(f"[ OK ] {page:<9} {el:5.1f}s  markdown={n_md} caption={n_cap} button={n_btn}")
    # 抽查关键文案是否出现（术语清理）
    text = " ".join(m.value for m in at.markdown if isinstance(m.value, str))
    text += " ".join(c.value for c in at.caption if isinstance(c.value, str))
    bad = [w for w in ("规格 8.6", "（规格", "L2c", "F1", "F2", "F3", "tombstone",
                       "upsert", "dead_letter", "06_face_detect", "07_face_encode",
                       "01_manifest", "03_extract", "04_encode", "reconcile")
           if w in text]
    if bad:
        print(f"       [WARN] 界面出现技术名词：{bad}")
    return 0


def main() -> int:
    wanted = sys.argv[1:] or PAGES
    fails = 0
    for p in wanted:
        fails += run_page(p)
    print("=" * 60)
    print("全部通过" if not fails else f"{fails} 个页面有问题")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
