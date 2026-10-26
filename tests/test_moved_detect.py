# -*- coding: utf-8 -*-
"""端到端验证「移动文件后，库里的记录跟着走（不新增、不变缺失）」。

流程（在临时素材目录里真做一遍）：
  1. 造 3 个文件 → 扫描入库
  2. 把其中一个 **移动** 到另一个子目录（同盘 rename）
  3. 再扫一次
  4. 断言：总行数不变、记录路径已更新到新位置、没有出现 missing、
          且 file_id 保持不变（说明向量/人脸都还能复用）
  5. 再验一次「复制不误判」：复制一份到新目录，扫描后应判为 skipped_dup 或 keep 两张，
     原记录不许被挪走。

用法：
    .venv\\Scripts\\python.exe tests\\test_moved_detect.py
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


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="moved_"))
    media = tmp / "media"
    (media / "A").mkdir(parents=True)
    (media / "B").mkdir(parents=True)
    cfg = load_config()
    cfg.root = tmp
    cfg.override("db.path", str(tmp / "library.db"))
    cfg.override("media_root", str(media))
    D.ensure_schema(cfg.db_path, ROOT, cfg)

    def mk(p: Path, payload: bytes) -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(payload)

    # 三个文件，内容各不相同（避免被判重复）
    mk(media / "A" / "one.jpg", b"x" * 3000)
    mk(media / "A" / "two.jpg", b"y" * 3100)
    mk(media / "B" / "three.jpg", b"z" * 3200)
    s1 = scan(cfg, media)
    print(f"首次扫描: new={s1.get('new')} missing={s1.get('missing')}")

    con = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        rows = {r["path"]: dict(r) for r in con.execute(
            "SELECT file_id, path, inode, status FROM files")}
    finally:
        con.close()
    old_path = str((media / "A" / "one.jpg").resolve())
    fid = rows[old_path]["file_id"]
    print(f"  入库 {len(rows)} 条；one.jpg 的 file_id={fid} inode={rows[old_path]['inode']}")
    check("首次扫描后 inode 不是 0（真 inode 已取到）",
          int(rows[old_path]["inode"] or 0) != 0, str(rows[old_path]["inode"]))

    # ---- 移动 ----
    new_dir = media / "B" / "moved_here"
    new_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(old_path, str(new_dir / "one.jpg"))
    s2 = scan(cfg, media)
    print(f"移动后再扫: new={s2.get('new')} moved={s2.get('moved')} "
          f"missing={s2.get('missing')} unchanged={s2.get('unchanged')}")

    con = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        rows2 = {r["path"]: dict(r) for r in con.execute(
            "SELECT file_id, path, status FROM files")}
        missing = [r for r in con.execute(
            "SELECT path FROM files WHERE status='missing'")]
    finally:
        con.close()
    new_path = str((new_dir / "one.jpg").resolve())
    check("总行数没变（没有新增一条重复记录）", len(rows2) == len(rows),
          f"{len(rows)} -> {len(rows2)}")
    check("记录路径已更新到新位置", new_path in rows2, list(rows2))
    check("file_id 保持不变（向量/人脸可复用）",
          rows2.get(new_path, {}).get("file_id") == fid,
          f"{fid} vs {rows2.get(new_path, {}).get('file_id')}")
    check("旧路径不再存在记录", old_path not in rows2)
    check("没有产生 missing", len(missing) == 0, [r["path"] for r in missing])
    check("第二次扫描判为 moved ≥1", int(s2.get("moved") or 0) >= 1, s2.get("moved"))

    # ---- 复制不应被误判成移动 ----
    copy_src = str(new_dir / "one.jpg")
    copy_dst = media / "A" / "one_copy.jpg"
    shutil.copy2(copy_src, str(copy_dst))
    s3 = scan(cfg, media)
    con = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        rows3 = {r["path"]: dict(r) for r in con.execute(
            "SELECT file_id, path, status FROM files")}
    finally:
        con.close()
    check("复制后原记录仍在原位（没被挪走）", new_path in rows3)
    check("复制出来的新文件被单独登记", str(copy_dst.resolve()) in rows3,
          list(rows3)[:4])
    check("复制那一步 moved 为 0（没误判成移动）", int(s3.get("moved") or 0) == 0,
          s3.get("moved"))

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 60)
    print(f"PASS={PASS} FAIL={FAIL}")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
