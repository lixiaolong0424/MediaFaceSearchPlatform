# -*- coding: utf-8 -*-
"""**紧急恢复**：把路径变更向导改坏的 path 前缀还原回 E:\\。

事故经过（2026-10-19 20:16，任务 20261019-2031 的 root 是 F:）：
  1) 有人（界面）用「更改素材文件夹」向导把根从 E:\\ 改成了
     F:\\LARGE\\...\\20250828照片 → apply() 一次性把 503,064 条 path 前缀换掉了；
  2) 20:20 又点了「扫描新文件」，扫描根是那个 F 目录 → 全局 missing 差集把
     所有 path 对不上的行（正好是刚被换掉前缀的那 503,064 条）全标成 missing。

为什么能**零损失**还原：换前缀只改了 path 的根，`rel_path` 一个字都没动，
而 rel_path 本来就是相对 E:\\ 的路径。所以 path := 'E:\\' || rel_path 即可复原。

只读判断 + 单条 UPDATE，不动 file_encoding / Qdrant / 缩略图。
用法：
    .venv\\Scripts\\python.exe tmp\\restore_paths.py            # 只报告
    .venv\\Scripts\\python.exe tmp\\restore_paths.py --apply    # 真的改
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.common import load_config, local_now_iso  # noqa: E402

F_ROOT = "F:\\LARGE\\2_LIFE\\20250829手机和平板照片备份\\20250828照片"
OLD_PREFIX = F_ROOT + "\\"
NEW_PREFIX = "E:\\"


def main() -> int:
    apply = "--apply" in sys.argv
    cfg = load_config()
    db = cfg.db_path
    print(f"库：{db}")
    print(f"当前 config media_root：{cfg.media_root}")

    con = sqlite3.connect(str(db), timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=60000")

    total = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    by_status = dict(con.execute(
        "SELECT status, COUNT(*) FROM files GROUP BY status").fetchall())
    print(f"\n行数 {total:,}  状态 {by_status}")

    n_bad = con.execute(
        "SELECT COUNT(*) FROM files WHERE path LIKE ? AND status='missing'",
        (OLD_PREFIX + "%",)).fetchone()[0]
    n_pending = con.execute(
        "SELECT COUNT(*) FROM files WHERE path LIKE ? AND status='pending'",
        (OLD_PREFIX + "%",)).fetchone()[0]
    n_empty_rel = con.execute(
        "SELECT COUNT(*) FROM files WHERE path LIKE ? AND (rel_path IS NULL OR rel_path='')",
        (OLD_PREFIX + "%",)).fetchone()[0]
    print(f"\n需要把前缀还原的行（missing）: {n_bad:,}")
    print(f"已经是真 F 盘文件的行（pending）: {n_pending:,}  ← 保持不动")
    print(f"rel_path 为空的行（无法还原）  : {n_empty_rel:,}")

    # 抽样验证：新前缀 + rel_path 在磁盘上确实存在
    sample = con.execute(
        "SELECT path, rel_path FROM files WHERE path LIKE ? AND status='missing' "
        "AND rel_path NOT LIKE 'Xiaomi %' LIMIT 300", (OLD_PREFIX + "%",)).fetchall()
    ok = sum(1 for r in sample if os.path.exists(NEW_PREFIX + r["rel_path"]))
    print(f"抽样 {len(sample)} 条：还原后在 E 盘真实存在 {ok} 条")
    if sample and ok == 0:
        print("!! 抽样全部不存在 —— 说明假设不成立，停止。")
        return 2

    if not apply:
        print("\n（这是只读报告。确认无误后加 --apply 真正执行）")
        con.close()
        return 0

    # 先备份
    bak = db.parent / "backup" / f"library_before_restore_{datetime.now():%Y%m%d_%H%M%S}.db"
    bak.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    shutil.copy2(db, bak)
    print(f"\n已备份 → {bak.name}（{bak.stat().st_size/2**20:.0f} MB，"
          f"{time.monotonic()-t0:.1f}s）")

    cur = con.execute(
        "UPDATE files SET path = ? || rel_path, updated_at=? "
        "WHERE path LIKE ? AND status='missing' AND rel_path IS NOT NULL AND rel_path != ''",
        (NEW_PREFIX, local_now_iso(), OLD_PREFIX + "%"))
    print(f"已还原 {cur.rowcount:,} 条路径 → {NEW_PREFIX} + rel_path")

    # 库根登记改回 E:\
    con.execute("DELETE FROM libraries WHERE root LIKE ?", ("F:\\LARGE\\%",))
    con.execute("INSERT INTO libraries(root, last_scan, status) VALUES (?,?,?) "
                "ON CONFLICT(root) DO UPDATE SET last_scan=excluded.last_scan",
                (NEW_PREFIX, local_now_iso(), "idle"))
    con.commit()

    left = con.execute("SELECT COUNT(*) FROM files WHERE path LIKE ?",
                       (OLD_PREFIX + "%",)).fetchone()[0]
    print(f"仍以 F 前缀开头的行：{left:,}（应等于原来的真 F 盘文件数）")
    con.close()

    # 配置改回 E:/
    import yaml
    from src.common import CONFIG_LOCAL_FILENAME

    p = ROOT / CONFIG_LOCAL_FILENAME
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    data = data or {}
    data["media_root"] = "E:/"
    p.write_text("# 由界面自动生成，请勿手工编辑。\n"
                 f"# 最后修改：{local_now_iso()}\n"
                 + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                 encoding="utf-8")
    print(f"已把 config.local.yaml 的 media_root 改回 E:/")
    print("\n下一步：跑 12_maintain.py fix-status 把 done / skipped_dup 状态还原。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
