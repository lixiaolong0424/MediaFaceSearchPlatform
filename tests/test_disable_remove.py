# -*- coding: utf-8 -*-
"""验证新语义：
  停用 → 该目录的素材在「搜索 / 照片墙 / 统计 / 人脸分组」里全部消失，但记录还在
  启用 → 一切恢复
  移除 → 记录与向量从库里删掉，磁盘文件不动

用临时库 + 临时素材目录真跑一遍。
用法：.venv\\Scripts\\python.exe tests\\test_disable_remove.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import db as D  # noqa: E402
from src import manifest as M  # noqa: E402
from src import media_roots as MR  # noqa: E402
from src.common import load_config  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def scan(cfg, root: Path) -> dict:
    args = SimpleNamespace(root=str(root), dry_run=False, limit=None, shard=None,
                           json=False, log_level=None, task_id=None,
                           reprocess_all=False)
    return M.run(args, cfg)


def counts(cfg):
    con = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        rows = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        vis = con.execute("SELECT COUNT(*) FROM files WHERE visible=1").fetchone()[0]
        done = con.execute(
            "SELECT COUNT(*) FROM files WHERE status='done' AND visible=1").fetchone()[0]
    finally:
        con.close()
    return rows, vis, done


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dr_"))
    a = tmp / "A"
    b = tmp / "B"
    a.mkdir()
    b.mkdir()
    cfg = load_config()
    cfg.root = tmp
    cfg.override("db.path", str(tmp / "library.db"))
    cfg.override("scan.min_photo_bytes", 1)
    cfg.override("scan.min_video_bytes", 1)
    D.ensure_schema(cfg.db_path, ROOT, cfg)

    (a / "a1.jpg").write_bytes(b"a" * 3000)
    (a / "a2.jpg").write_bytes(b"a" * 3100)
    (b / "b1.jpg").write_bytes(b"b" * 3200)
    MR.add(cfg, str(a), who="test")
    MR.add(cfg, str(b), who="test")
    scan(cfg, a)
    scan(cfg, b)

    rows, vis, done = counts(cfg)
    print(f"  初始：记录 {rows}，可见 {vis}，done且可见 {done}")
    check("两个根都已入库", rows == 3, rows)

    # ---- 停用 B ----
    res = MR.set_enabled(cfg, str(b), False, who="test")
    rows2, vis2, done2 = counts(cfg)
    print(f"  停用 B 后：记录 {rows2}，可见 {vis2}，done且可见 {done2}"
          f"（改动 visible {res.get('visible_rows')} 行）")
    check("停用后记录仍在库里", rows2 == rows, rows2)
    check("停用后可见数减少", vis2 == vis - 1, f"{vis} -> {vis2}")
    con = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        hidden = con.execute(
            "SELECT COUNT(*) FROM files WHERE visible=0").fetchone()[0]
    finally:
        con.close()
    check("停用目录的行都 visible=0", hidden == 1, hidden)

    # ---- 搜索候选集应排除它 ----
    con = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        allowed = {r[0] for r in con.execute(
            "SELECT file_id FROM files WHERE status='done' AND visible=1")}
        allb = {r[0] for r in con.execute(
            "SELECT file_id FROM files WHERE visible=0")}
    finally:
        con.close()
    check("检索候选集里没有停用目录的文件", not (allowed & allb), allb & allowed)

    # ---- 重新启用 ----
    MR.set_enabled(cfg, str(b), True, who="test")
    _, vis3, _ = counts(cfg)
    check("重新启用后可见数恢复", vis3 == vis, f"{vis} -> {vis3}")

    # ---- 移除 A：默认 purge ----
    before_files = {p.name for p in a.iterdir()}
    res2 = MR.remove(cfg, str(a), who="test")
    rows4, vis4, _ = counts(cfg)
    print(f"  移除 A 后：记录 {rows4}，可见 {vis4}，purged={res2.get('purged')}")
    check("移除后记录被删干净", rows4 == 1, rows4)
    check("purged 数量正确", int(res2.get("purged") or 0) == 2, res2.get("purged"))
    check("磁盘文件一个都没动", {p.name for p in a.iterdir()} == before_files,
          [p.name for p in a.iterdir()])
    regs = [r["path"] for r in MR.list_roots(cfg)]
    check("登记列表里已没有 A", not any("A" == Path(x).name for x in regs), regs)

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 55)
    print(f"PASS={PASS} FAIL={FAIL}")
    print("=" * 55)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
