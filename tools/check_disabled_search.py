# -*- coding: utf-8 -*-
"""用真实 UI 路径验证：停用 E: 之后，检索结果里不应出现 E: 的文件。

AppTest 里点「开始检索」，再从 session_state 的 outcome 里取 file_id，
逐个查 files.visible —— 这是最接近用户操作的验证。
"""
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def main() -> int:
    from src.common import load_config

    cfg = load_config()
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=600)
    at.run()
    # 输入文字并检索
    at.text_input[0].set_value("海边").run()
    btn = next(b for b in at.button if "开始检索" in b.label)
    t0 = time.monotonic()
    btn.click().run()
    dt = time.monotonic() - t0
    print(f"检索耗时 {dt:.1f}s，异常 {[e.type for e in at.exception]}")

    out = at.session_state.get("outcome") or {}
    files = (out.get("files") if isinstance(out, dict) else None) or []
    ids = [int(f["file_id"]) for f in files if f.get("file_id")]
    print(f"命中文件 {len(ids)} 个")
    check("检索有结果", len(ids) > 0, len(ids))
    check("检索无异常", not at.exception, [e.type for e in at.exception])

    if ids:
        con = sqlite3.connect(str(cfg.db_path))
        q = ",".join("?" * len(ids))
        rows = con.execute(
            f"SELECT root, visible, COUNT(*) FROM files WHERE file_id IN ({q}) "
            f"GROUP BY root, visible", ids).fetchall()
        con.close()
        print("  命中来源（root, visible, 数量）:")
        for r in rows:
            print("   ", r)
        hidden = sum(n for _root, vis, n in rows if int(vis) == 0)
        check("命中里没有停用目录（E:）的文件", hidden == 0, hidden)
        from_e = sum(n for root, _v, n in rows if str(root).upper().startswith("E:"))
        check("命中里没有 E: 的文件", from_e == 0, from_e)

    print("\n" + "=" * 45)
    print(f"PASS={PASS} FAIL={FAIL}")
    print("=" * 45)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
