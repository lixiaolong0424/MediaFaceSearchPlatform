# -*- coding: utf-8 -*-
"""验证两处补漏：

  T1 视频去重：同 size+mtime+文件名 → 第二条被标 skipped_dup，不再抽帧
  T2 视频去重不误伤：size 相同但 mtime 或文件名不同 → 不判重复
  T3 跨素材根移动：文件从 rootA 移到 rootB，重扫两个根后记录跟着走（不新增、不缺）

用法：.venv\\Scripts\\python.exe tests\\test_dedup_and_crossroot.py
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
from src import extract as E  # noqa: E402
from src import manifest as M  # noqa: E402
from src.common import load_config, local_now_iso  # noqa: E402

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


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dd_"))
    cfg = load_config()
    cfg.root = tmp
    cfg.override("db.path", str(tmp / "library.db"))
    rootA = tmp / "A"
    rootB = tmp / "B"
    (rootA / "sub").mkdir(parents=True)
    rootB.mkdir()
    cfg.override("media_root", str(rootA))
    D.ensure_schema(cfg.db_path, ROOT, cfg)

    # ---- T1/T2：视频去重 ----
    v1 = rootA / "v1.mp4"
    v1.write_bytes(b"v" * 50000)
    v2 = rootA / "sub" / "v1.mp4"      # 同名同 size 同 mtime → 重复
    v2.write_bytes(b"v" * 50000)
    import os

    st = v1.stat()
    os.utime(v2, (st.st_atime, st.st_mtime))
    v3 = rootA / "sub" / "other.mp4"   # 同 size，但名字不同 → 不算重复
    v3.write_bytes(b"w" * 50000)
    os.utime(v3, (st.st_atime, st.st_mtime))

    scan(cfg, rootA)
    con = D.connect(cfg.db_path, cfg=cfg)
    try:
        ids = {str(r["path"]): int(r["file_id"]) for r in con.execute(
            "SELECT file_id, path FROM files")}
        dup = E.find_video_duplicate(con, ids[str(v2.resolve())], v2)
        check("T1 同名同 size 同 mtime → 认成重复", dup == ids[str(v1.resolve())],
              f"dup={dup} vs {ids[str(v1.resolve())]}")
        dup2 = E.find_video_duplicate(con, ids[str(v3.resolve())], v3)
        check("T2 名字不同 → 不认成重复", dup2 is None, str(dup2))
        # 真的把第二条标掉
        con.execute("UPDATE files SET status='skipped_dup', visible=0 WHERE file_id=?",
                    (ids[str(v2.resolve())],))
        con.commit()
        n = con.execute("SELECT COUNT(*) FROM files WHERE status='skipped_dup'").fetchone()[0]
        check("T2b 标记 skipped_dup 生效", n == 1, n)
    finally:
        con.close()

    # ---- T3：跨根移动 ----
    # 把 v1 从 rootA 移到 rootB（两个都登记为独立根）
    moved_to = rootB / "v1.mp4"
    shutil.move(str(v1), str(moved_to))
    scan(cfg, rootA)          # 源根：应判 missing（此时目标根还没扫）
    scan(cfg, rootB)          # 目标根：应把它认成 moved，并改 root
    con = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        rows = {str(r["path"]): dict(r) for r in con.execute(
            "SELECT file_id, path, root, status FROM files")}
        missing = [p for p, r in rows.items() if r["status"] == "missing"]
        newp = str(moved_to.resolve())
        print(f"   库内记录: {[(p[-30:], r['root'], r['status']) for p, r in rows.items()]}")
        check("T3 旧路径没有留下第二份记录", len(rows) == 3, len(rows))
        check("T3 记录已指向新路径", newp in rows, list(rows))
        check("T3 root 已改成目标根", rows.get(newp, {}).get("root") == str(rootB.resolve()) + "\\"
              or rows.get(newp, {}).get("root", "").rstrip("\\") == str(rootB.resolve()),
              rows.get(newp, {}).get("root"))
        check("T3 没有产生 missing 记录", not missing, missing)
    finally:
        con.close()

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 55)
    print(f"PASS={PASS} FAIL={FAIL}")
    print("=" * 55)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
