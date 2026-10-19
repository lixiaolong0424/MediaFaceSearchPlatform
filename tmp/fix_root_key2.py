# -*- coding: utf-8 -*-
"""修历史数据（第二版）：path_index 已有重复行，改成「先删重再改」。

用法：.venv\\Scripts\\python.exe tmp\\fix_root_key2.py --apply
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.common import CONFIG_LOCAL_FILENAME, load_config, local_now_iso  # noqa: E402


def main() -> int:
    apply = "--apply" in sys.argv
    cfg = load_config()
    con = sqlite3.connect(str(cfg.db_path), timeout=120)
    con.execute("PRAGMA busy_timeout=120000")

    olds = [r[0] for r in con.execute(
        "SELECT DISTINCT root FROM files WHERE root IS NOT NULL AND root != '' "
        "AND root NOT LIKE '%\\' AND root NOT LIKE '%/'")]
    print("需要补分隔符的 root：", olds)

    for old in olds:
        new = old + os.sep
        n_files = con.execute("SELECT COUNT(*) FROM files WHERE root=?", (old,)).fetchone()[0]
        n_pi_old = con.execute("SELECT COUNT(*) FROM path_index WHERE root=?", (old,)).fetchone()[0]
        n_pi_new = con.execute("SELECT COUNT(*) FROM path_index WHERE root=?", (new,)).fetchone()[0]
        dup = con.execute(
            "SELECT COUNT(*) FROM path_index a JOIN path_index b "
            "ON a.path_hash=b.path_hash AND b.root=? WHERE a.root=?", (new, old)
        ).fetchone()[0]
        print(f"  {old!r}: files {n_files}, path_index 旧 {n_pi_old} / 新 {n_pi_new} / 重复 {dup}")
        if not apply:
            continue
        con.execute("DELETE FROM path_index WHERE root=? AND path_hash IN "
                    "(SELECT path_hash FROM path_index WHERE root=?)", (old, new))
        con.execute("UPDATE path_index SET root=? WHERE root=?", (new, old))
        con.execute("UPDATE files SET root=? WHERE root=?", (new, old))
        print(f"    → 已统一成 {new!r}")

    # libraries 去重
    if apply:
        bases = {r[0].rstrip("\\/") for r in con.execute("SELECT root FROM libraries")}
        for base in bases:
            variants = [x[0] for x in con.execute(
                "SELECT root FROM libraries WHERE root LIKE ?", (base + "%",))]
            if len(variants) <= 1:
                continue
            last = con.execute("SELECT MAX(last_scan) FROM libraries WHERE root LIKE ?",
                               (base + "%",)).fetchone()[0]
            con.execute("DELETE FROM libraries WHERE root LIKE ?", (base + "%",))
            con.execute("INSERT INTO libraries(root, enabled, last_scan, status) "
                        "VALUES (?,1,?,'idle')", (base + os.sep, last))
            print(f"  libraries 合并 {len(variants)} 行 → {base + os.sep!r}")
        con.commit()
    con.close()

    if not apply:
        print("\n（只读报告；加 --apply 执行）")
        return 0

    import yaml

    p = ROOT / CONFIG_LOCAL_FILENAME
    if p.exists():
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        changed = False
        mr = str(data.get("media_root") or "")
        if len(mr) == 2 and mr.endswith(":"):
            data["media_root"] = mr + "/"
            changed = True
        for e in (data.get("media_roots") or []):
            if isinstance(e, dict):
                v = str(e.get("path") or "")
                if len(v) == 2 and v.endswith(":"):
                    e["path"] = v + "/"
                    changed = True
        if changed:
            p.write_text("# 由界面自动生成，请勿手工编辑。\n"
                         f"# 最后修改：{local_now_iso()}\n"
                         + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                         encoding="utf-8")
            print("config.local.yaml：已补上盘符斜杠")
    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
