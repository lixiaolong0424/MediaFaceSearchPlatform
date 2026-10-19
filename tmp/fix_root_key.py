# -*- coding: utf-8 -*-
"""修历史数据：files.root 统一成带结尾分隔符；libraries 去重；media_root 补斜杠。

用法：.venv\\Scripts\\python.exe tmp\\fix_root_key.py [--apply]
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.common import CONFIG_LOCAL_FILENAME, load_config  # noqa: E402


def main() -> int:
    apply = "--apply" in sys.argv
    cfg = load_config()
    con = sqlite3.connect(str(cfg.db_path), timeout=60)
    con.execute("PRAGMA busy_timeout=60000")

    print("== 当前 files.root 分布 ==")
    for r in con.execute("SELECT root, COUNT(*) FROM files GROUP BY root"):
        print(f"  {r[1]:>8,}  {r[0]!r}")

    need = [r[0] for r in con.execute(
        "SELECT DISTINCT root FROM files WHERE root IS NOT NULL AND root != '' "
        "AND root NOT LIKE '%\\' AND root NOT LIKE '%/'")]
    print(f"\n需要补分隔符的 root：{need}")

    print("\n== libraries ==")
    for r in con.execute("SELECT root, enabled, last_scan FROM libraries"):
        print(f"  {r[0]!r} enabled={r[1]} last_scan={r[2]}")

    if not apply:
        print("\n（只读报告；加 --apply 执行）")
        con.close()
        return 0

    for old in need:
        new = old if old.endswith(("\\", "/")) else old + os.sep
        n = con.execute("UPDATE files SET root=? WHERE root=?", (new, old)).rowcount
        print(f"  files.root {old!r} → {new!r}：{n:,} 行")
        con.execute("UPDATE path_index SET root=? WHERE root=?", (new, old))

    # libraries 去重：同一根只留一行（优先保留有 last_scan 的那行）
    for r in con.execute("SELECT DISTINCT root FROM libraries").fetchall():
        base = r[0].rstrip("\\/")
        variants = [x[0] for x in con.execute(
            "SELECT root FROM libraries WHERE root LIKE ?", (base + "%",))]
        if len(variants) <= 1:
            continue
        keep = base + os.sep
        last = con.execute(
            "SELECT MAX(last_scan) FROM libraries WHERE root LIKE ?", (base + "%",)
        ).fetchone()[0]
        con.execute("DELETE FROM libraries WHERE root LIKE ?", (base + "%",))
        con.execute("INSERT INTO libraries(root, enabled, last_scan, status) "
                    "VALUES (?,1,?,'idle')", (keep, last))
        print(f"  libraries 合并 {variants} → {keep!r}")

    con.commit()
    con.close()

    # 配置里的 media_root / media_roots 补斜杠
    import yaml

    p = ROOT / CONFIG_LOCAL_FILENAME
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        changed = False
        mr = str(data.get("media_root") or "")
        if len(mr) == 2 and mr.endswith(":"):
            data["media_root"] = mr + "/"
            changed = True
        roots = data.get("media_roots")
        if isinstance(roots, list):
            for e in roots:
                if isinstance(e, dict):
                    v = str(e.get("path") or "")
                    if len(v) == 2 and v.endswith(":"):
                        e["path"] = v + "/"
                        changed = True
        if changed:
            from src.common import local_now_iso

            p.write_text("# 由界面自动生成，请勿手工编辑。\n"
                         f"# 最后修改：{local_now_iso()}\n"
                         + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                         encoding="utf-8")
            print("  已修正 config.local.yaml 里的盘符斜杠")

    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
