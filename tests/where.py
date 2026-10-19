"""按 file_id 或关键词查源文件位置（只读）。

背景：Python 自带的 `python -m sqlite3` 会把结果包成 Python 元组
（`('...',)`）并把反斜杠转义成 `\\\\`，不适合直接复制路径；
sqlite3.org 的独立 sqlite3.exe（支持 -header -column）Windows 上默认没有。
所以这里给一个干净输出的小工具。

用法：
    # 按 file_id 查（可多个）
    .venv\\Scripts\\python.exe tests\\where.py 1
    .venv\\Scripts\\python.exe tests\\where.py 1 2 3 4

    # 按关键词模糊搜（文件名或路径的一部分）
    .venv\\Scripts\\python.exe tests\\where.py --like 白月光

    # 列出某范围
    .venv\\Scripts\\python.exe tests\\where.py --range 1-20

    # 只看视频 / 只看照片
    .venv\\Scripts\\python.exe tests\\where.py --kind video --range 1-20

    # 额外输出：镜头数 / 关键帧数 / 拍摄时间
    .venv\\Scripts\\python.exe tests\\where.py 1 --detail

    # 输出 TSV（便于导入 Excel）
    .venv\\Scripts\\python.exe tests\\where.py --range 1-50 --tsv

    # 只输出路径（每行一条，便于管道）
    .venv\\Scripts\\python.exe tests\\where.py 1 --paths-only
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def parse_range(s: str) -> tuple[int, int]:
    lo, _, hi = s.partition("-")
    return int(lo), int(hi or lo)


def main() -> int:
    ap = argparse.ArgumentParser(description="按 file_id / 关键词查源文件位置")
    ap.add_argument("file_id", nargs="*", type=int, help="要查的 file_id（可多个）")
    ap.add_argument("--like", default=None, help="按路径关键词模糊搜")
    ap.add_argument("--range", dest="rng", default=None, metavar="A-B", help="file_id 范围")
    ap.add_argument("--kind", choices=["photo", "video"], default=None, help="只看某类")
    ap.add_argument("--db", default=None, help="库路径（默认 data/library.db）")
    ap.add_argument("--detail", action="store_true", help="附镜头数/关键帧数/拍摄时间")
    ap.add_argument("--tsv", action="store_true", help="输出 TSV")
    ap.add_argument("--paths-only", action="store_true", help="只输出路径")
    ap.add_argument("--exists", action="store_true", help="额外校验文件是否还在磁盘上")
    args = ap.parse_args()

    db = Path(args.db) if args.db else ROOT / "data" / "library.db"
    if not db.exists():
        print(f"库不存在：{db}")
        return 2

    where, params = [], []
    if args.file_id:
        where.append(f"f.file_id IN ({','.join('?' * len(args.file_id))})")
        params += args.file_id
    if args.rng:
        lo, hi = parse_range(args.rng)
        where.append("f.file_id BETWEEN ? AND ?")
        params += [lo, hi]
    if args.like:
        where.append("f.path LIKE ?")
        params.append(f"%{args.like}%")
    if args.kind:
        where.append("f.kind = ?")
        params.append(args.kind)
    if not where:
        print("请至少给一个 file_id、或 --like、或 --range。用 -h 看用法。")
        return 2

    sql = f"""
        SELECT f.file_id, f.kind, f.status, f.size, f.duration_sec,
               f.captured_at, f.time_source, f.path,
               (SELECT COUNT(*) FROM shots s WHERE s.file_id=f.file_id) AS shots,
               (SELECT COALESCE(SUM(s.n_frames),0) FROM shots s
                 WHERE s.file_id=f.file_id) AS frames
          FROM files f
         WHERE {' AND '.join(where)}
         ORDER BY f.file_id
    """
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = list(con.execute(sql, params))
    finally:
        con.close()

    if not rows:
        print("没有匹配的记录。")
        return 1

    if args.paths_only:
        for r in rows:
            print(r["path"])
        return 0

    if args.tsv:
        cols = ["file_id", "kind", "status", "shots", "frames", "duration_sec",
                "captured_at", "size", "path"]
        print("\t".join(cols))
        for r in rows:
            print("\t".join("" if r[c] is None else str(r[c]) for c in cols))
        return 0

    multi = len(rows) > 1
    for r in rows:
        fid = r["file_id"]
        dur = f"{r['duration_sec']:.1f}s" if r["duration_sec"] else "-"
        print(f"[{fid}] {r['kind']}  {r['status']}  大小={human(r['size'] or 0)}  "
              f"时长={dur}")
        if args.detail:
            cap = r["captured_at"] or "未解析"
            print(f"      镜头数={r['shots']}  关键帧={r['frames']}  "
                  f"拍摄时间={cap} ({r['time_source'] or '-'})")
        print(f"      {r['path']}")
        if args.exists:
            p = Path(str(r["path"]))
            print(f"      磁盘上存在: {'是' if p.exists() else '否（DEAD）'}")
        if multi:
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
