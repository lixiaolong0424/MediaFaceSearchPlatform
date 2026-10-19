"""分析「照片墙为空」的原因：逐条排查照片墙的过滤链。

照片墙的数据源是 face_records（规格 8.2：sqlite 分页，绝不用 Qdrant 刷墙）。
空的可能原因按优先级：
  1) face_records 表里根本没有记录（F1/F3 人脸管线没跑过）
  2) 有记录但被过滤掉：status/hidden/visible/min_quality
  3) 有记录但 thumb_path 指向的文件不存在（照片墙刷不出图）
  4) 代码层过滤：overview 去重把结果全吃掉
  5) 界面查的是另一个库/根

本脚本把每一层都量化，定位到底卡在哪。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import db as D  # noqa: E402
from src.common import load_config  # noqa: E402


def q(con: sqlite3.Connection, sql: str, *a) -> list:
    try:
        return con.execute(sql, a).fetchall()
    except sqlite3.OperationalError as exc:
        return [("ERR", str(exc))]


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--overlay", default=None, help="配置叠加文件（默认读 config.yaml）")
    args = ap.parse_args()

    cfg = load_config(None, None, overlay=[args.overlay] if args.overlay else None)
    print(f"media_root = {cfg.media_root}")
    print(f"db         = {cfg.db_path}")
    print(f"库存在     = {cfg.db_path.exists()}")

    con = sqlite3.connect(str(cfg.db_path))
    con.row_factory = sqlite3.Row

    section("1) face_records 是照片墙的唯一数据源——它有没有记录")
    for row in q(con, "SELECT COUNT(*) n FROM face_records"):
        print(f"  face_records 总数        : {row['n'] if 'n' in row.keys() else row}")

    section("2) 上游：人脸管线是否跑过（stage_status / file_encoding）")
    print("  stage_status :", {f"{r['stage']}:{r['status']}": r["n"] for r in q(
        con, "SELECT stage, status, COUNT(*) n FROM stage_status GROUP BY stage, status")}
        if not isinstance(q(con, "SELECT 1 FROM stage_status LIMIT 1")[0], tuple)
        else "（表不存在或为空）")
    print("  file_encoding:", {f"{r['model']}:{r['status']}": r["n"] for r in q(
        con, "SELECT model, status, COUNT(*) n FROM file_encoding GROUP BY model, status")}
        if not isinstance(q(con, "SELECT 1 FROM file_encoding LIMIT 1")[0], tuple)
        else "（表不存在或为空）")
    print("\n  → 关键：有没有 F 系列阶段（face_detect / face_encode）的记录？")
    rows = q(con, "SELECT DISTINCT stage FROM stage_status")
    stages = [r[0] for r in rows if not isinstance(r, tuple) or len(r) == 1]
    print(f"     stage_status 里出现过的 stage: {stages}")

    section("3) 逐层过滤计数（模拟 face_wall_count 的条件）")
    filters = [
        ("全部 face_records", "SELECT COUNT(*) FROM face_records"),
        ("status='alive'", "SELECT COUNT(*) FROM face_records WHERE status='alive'"),
        ("+ hidden=0", "SELECT COUNT(*) FROM face_records WHERE status='alive' AND hidden=0"),
        ("+ 关联文件 visible=1",
         "SELECT COUNT(*) FROM face_records fr JOIN files f ON f.file_id=fr.file_id "
         "WHERE fr.status='alive' AND fr.hidden=0 AND f.visible=1"),
        ("+ 文件非 missing",
         "SELECT COUNT(*) FROM face_records fr JOIN files f ON f.file_id=fr.file_id "
         "WHERE fr.status='alive' AND fr.hidden=0 AND f.visible=1 AND f.status!='missing'"),
        ("+ quality>=0.3",
         "SELECT COUNT(*) FROM face_records fr JOIN files f ON f.file_id=fr.file_id "
         "WHERE fr.status='alive' AND fr.hidden=0 AND f.visible=1 AND f.status!='missing' "
         "AND COALESCE(fr.quality,0)>=0.3"),
    ]
    for label, sql in filters:
        r = con.execute(sql).fetchone()
        print(f"  {label:<28} : {r[0]}")

    section("4) 缩略图是否存在（照片墙刷图的硬前提）")
    rows = q(con, "SELECT thumb_path FROM face_records WHERE thumb_path IS NOT NULL LIMIT 200")
    paths = [r["thumb_path"] if hasattr(r, "keys") else r[0] for r in rows]
    if not paths:
        print("  没有任何 thumb_path（F3 未写出人脸缩略图）")
    else:
        exist = sum(1 for p in paths if p and Path(p).exists())
        print(f"  抽查 {len(paths)} 条：存在 {exist}，缺失 {len(paths) - exist}")
        for p in paths[:3]:
            print(f"    {'OK ' if Path(p).exists() else 'MISS'} {p}")

    section("5) 直接调 ui_wall.wall_page（界面真正用的函数）")
    try:
        from src import ui_wall

        page = ui_wall.wall_page(con, cfg, limit=10, min_quality=0.3, mode="overview")
        print(f"  total={page['total']}  items={len(page['items'])}  "
              f"has_more={page['has_more']}  mode={page['mode']}")
        print(f"  facets={page['facets']}")
        if page["items"]:
            for it in page["items"][:3]:
                print(f"    face_id={it['face_id']} kind={it['kind']} "
                      f"thumb={it['thumb_path']}")
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ 调用失败：{type(exc).__name__}: {exc}")

    section("6) 对比：库里有多少「已索引照片」本应产生人脸")
    r = con.execute("SELECT COUNT(*) FROM files WHERE kind='photo' AND status='done'").fetchone()
    print(f"  kind=photo 且 status=done : {r[0]}")
    r = con.execute("SELECT COUNT(*) FROM files WHERE status='done'").fetchone()
    print(f"  status=done（全部）        : {r[0]}")
    print("  → 若上面数字远大于 0 而 face_records=0，说明是「人脸管线未实现/未运行」")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
