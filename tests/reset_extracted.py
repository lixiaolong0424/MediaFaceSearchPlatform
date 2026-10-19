"""清理「用旧参数跑过的抽帧结果」，让 03_extract 用新参数重跑。

背景
----
03_extract 曾在旧参数下完成 1,500 个文件（当时 min_frame_mean=5.0、
没有末尾钳制 end_margin_sec）。旧结果的特征是：每个镜头的最后一帧被
seek 越界丢掉（日志里表现为 mjpeg "Non full-range YUV" 次生错误）。
参数已修正，这批结果需要重跑才能统一。

清理什么
--------
要让 03_extract 重新拾取这些文件，最少只需两步；但为了不留下**旧时间点的
孤儿 JPEG**（例如旧实现用 `shot.end-0.05` 产出 `xxxx_000587250.jpg`，
新实现钳制后产出 `xxxx_000582750.jpg`，两者会同时留在目录里），
必须把旧的 frames/<file_id>/ 目录一并删掉。

  1) stage_status  删除 stage='extract' 的行      → 03 才会重新拾取
  2) files         该 file_id 的 status 回 'pending'
  3) frames/<file_id>/  删除整个目录              → 清掉旧时间点的 JPEG
  4) shots         删除该 file_id 的镜头行（03 也会重建，但先清更干净）

注意：**不删任何源素材**，只删本系统的派生物。

用法
----
  # 1. 先看会删什么（默认 dry-run，不写任何东西）
  .venv\\Scripts\\python.exe tests\\reset_extracted.py

  # 2. 确认无误后执行（自动备份 library.db）
  .venv\\Scripts\\python.exe tests\\reset_extracted.py --confirm

  # 只清理指定文件
  .venv\\Scripts\\python.exe tests\\reset_extracted.py --file-id 1 --file-id 3 --confirm

  # 只清 stage_status 与 files.status，保留 frames（不推荐，会留孤儿 JPEG）
  .venv\\Scripts\\python.exe tests\\reset_extracted.py --keep-frames --confirm
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STAGE = "extract"


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0 or unit == "TB":
            return f"{n:,.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def resolve_paths(explicit_db: Optional[str], explicit_frames: Optional[str]
                  ) -> Tuple[Path, Path, str]:
    """确定 db 与 frames 路径；优先用用户显式指定，否则读 config.yaml。"""
    db = Path(explicit_db) if explicit_db else None
    frames = Path(explicit_frames) if explicit_frames else None
    src = "命令行参数"
    if db is None or frames is None:
        try:
            from src.common import load_config

            cfg = load_config()
            db = db or cfg.db_path
            frames = frames or cfg.path("paths.frames")
            src = f"config.yaml（{cfg.source}）"
        except Exception as exc:  # noqa: BLE001
            print(f"[错误] 无法从 config.yaml 解析路径：{exc}")
            print("       请用 --db 与 --frames 显式指定。")
            raise SystemExit(2)
    return Path(db), Path(frames), src


def dir_stats(d: Path) -> Tuple[int, int]:
    """返回 (文件数, 字节数)。"""
    n = 0
    b = 0
    if not d.exists():
        return 0, 0
    for p in d.glob("*.jpg"):
        try:
            n += 1
            b += p.stat().st_size
        except OSError:
            pass
    return n, b


def main() -> int:
    ap = argparse.ArgumentParser(
        description="清理旧参数下的抽帧结果，使 03_extract 可重跑",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help="SQLite 库路径（默认取 config.yaml）")
    ap.add_argument("--frames", default=None, help="frames 目录（默认取 config.yaml）")
    ap.add_argument("--file-id", action="append", type=int, default=None,
                    help="只处理这些 file_id（可重复；默认处理全部已完成的）")
    ap.add_argument("--confirm", action="store_true",
                    help="真正执行（不加则只输出 dry-run 报告）")
    ap.add_argument("--keep-frames", action="store_true",
                    help="保留 frames/<file_id>/ 目录（不推荐：会留下旧时间点的孤儿 JPEG）")
    ap.add_argument("--kind", choices=["all", "video", "photo"], default="all",
                    help="只处理某类文件。视频才受本次参数变更影响；"
                         "照片不抽帧，重跑结果相同，故 --kind video 可省下重复工作（默认 all）")
    ap.add_argument("--no-backup", action="store_true", help="跳过库备份（不推荐）")
    args = ap.parse_args()

    db, frames_root, src = resolve_paths(args.db, args.frames)
    if not db.exists():
        print(f"[错误] 库不存在：{db}")
        return 2

    print(f"库        : {db}")
    print(f"frames    : {frames_root}")
    print(f"路径来源  : {src}")
    print(f"模式      : {'执行' if args.confirm else 'DRY-RUN（不写任何东西）'}")
    print()

    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        # ---- 1. 找出目标文件 ----
        if args.file_id:
            ids = sorted(set(args.file_id))
            ph = ",".join("?" * len(ids))
            rows = list(con.execute(
                f"SELECT file_id, kind, status FROM files WHERE file_id IN ({ph})", ids))
            found = {int(r["file_id"]) for r in rows}
            missing = [i for i in ids if i not in found]
            if missing:
                print(f"[警告] 以下 file_id 不在库中，已忽略：{missing}")
        else:
            sql = """SELECT f.file_id, f.kind, f.status
                       FROM files f JOIN stage_status s ON s.file_id = f.file_id
                      WHERE s.stage = ?"""
            params: List[object] = [STAGE]
            if args.kind != "all":
                sql += " AND f.kind = ?"
                params.append(args.kind)
            rows = list(con.execute(sql, params))

        if not rows:
            print("没有需要清理的文件（stage_status 里没有 extract 记录）。")
            return 0

        targets = [int(r["file_id"]) for r in rows]
        kinds: Dict[str, int] = {}
        for r in rows:
            kinds[str(r["kind"])] = kinds.get(str(r["kind"]), 0) + 1

        print(f"=== 目标文件 ===")
        print(f"  数量     : {len(targets):,}")
        print(f"  类型分布 : {kinds}")

        # ---- 2. 统计将要删除的数据库行 ----
        ph = ",".join("?" * len(targets))
        n_stage = con.execute(
            f"SELECT COUNT(*) FROM stage_status WHERE stage=? AND file_id IN ({ph})",
            (STAGE, *targets)).fetchone()[0]
        try:
            n_shots = con.execute(
                f"SELECT COUNT(*) FROM shots WHERE file_id IN ({ph})", targets).fetchone()[0]
            n_frames_db = con.execute(
                f"SELECT COALESCE(SUM(n_frames),0) FROM shots WHERE file_id IN ({ph})",
                targets).fetchone()[0]
        except sqlite3.OperationalError:
            n_shots = n_frames_db = 0

        print(f"\n=== 将删除的数据库行 ===")
        print(f"  stage_status({STAGE}) : {n_stage:,}")
        print(f"  shots                 : {n_shots:,}（自报 {int(n_frames_db):,} 帧）")
        print(f"  files.status → pending: {len(targets):,}")

        # ---- 3. 统计将删除的磁盘目录 ----
        dirs: List[Path] = []
        n_jpg = 0
        n_bytes = 0
        missing_dir = 0
        for fid in targets:
            d = frames_root / str(fid)
            if d.is_dir():
                c, b = dir_stats(d)
                dirs.append(d)
                n_jpg += c
                n_bytes += b
            else:
                missing_dir += 1
        print(f"\n=== 将删除的 frames 目录 ===")
        if args.keep_frames:
            print("  （--keep-frames：跳过，保留目录）")
        else:
            print(f"  目录数   : {len(dirs):,}")
            print(f"  JPEG 数  : {n_jpg:,}")
            print(f"  占用     : {human(n_bytes)}")
            print(f"  无目录的 : {missing_dir:,}（照片类，抽帧不用 frames/）")

        if not args.confirm:
            print("\n" + "=" * 72)
            print("  DRY-RUN：以上均未执行。确认后加 --confirm 重跑本命令。")
            print("=" * 72)
            return 0

        # ---- 4. 备份 ----
        if not args.no_backup:
            ts = time.strftime("%Y%m%d_%H%M%S")
            bak = db.with_name(f"{db.name}.bak_{ts}")
            try:
                # 用 sqlite 的 backup API，能正确处理 WAL（直接复制文件会漏 -wal）
                dst = sqlite3.connect(str(bak))
                with dst:
                    con.backup(dst)
                dst.close()
                print(f"\n[备份] {bak}（{human(bak.stat().st_size)}）")
            except Exception as exc:  # noqa: BLE001
                print(f"\n[错误] 备份失败，已中止以免误操作：{exc}")
                return 2
        else:
            print("\n[备份] 已按 --no-backup 跳过")

        # ---- 5. 执行 ----
        print("\n=== 执行清理 ===")
        con.execute("BEGIN IMMEDIATE")
        try:
            d1 = con.execute(
                f"DELETE FROM stage_status WHERE stage=? AND file_id IN ({ph})",
                (STAGE, *targets)).rowcount
            try:
                d2 = con.execute(
                    f"DELETE FROM shots WHERE file_id IN ({ph})", targets).rowcount
            except sqlite3.OperationalError:
                d2 = 0
            d3 = con.execute(
                f"""UPDATE files SET status='pending', updated_at=?
                     WHERE file_id IN ({ph})""",
                (time.strftime("%Y-%m-%dT%H:%M:%S"), *targets)).rowcount
            con.commit()
        except Exception as exc:  # noqa: BLE001
            con.rollback()
            print(f"[错误] 数据库清理失败并已回滚：{exc}")
            return 2
        print(f"  stage_status 删除 : {d1:,}")
        print(f"  shots 删除        : {d2:,}")
        print(f"  files 置 pending  : {d3:,}")

        removed = 0
        freed = 0
        if not args.keep_frames:
            for d in dirs:
                c, b = dir_stats(d)
                try:
                    shutil.rmtree(d, ignore_errors=False)
                    removed += 1
                    freed += b
                except OSError as exc:
                    print(f"  [警告] 删除失败 {d}：{exc}")
            print(f"  frames 目录删除   : {removed:,}（释放 {human(freed)}）")

        print("\n完成。现在重跑 03_extract 即可用新参数处理这些文件：")
        print("    .venv\\Scripts\\python.exe 03_extract.py")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
