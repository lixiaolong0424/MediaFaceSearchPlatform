"""11_export.py · 三入口 TSV 导出（全库清单 / 照片墙筛选结果 / 命中清单）。

规格 9.3：tab 分隔、UTF-8 BOM、DEAD 校验、幂等排序、--relative、尾部 # DEAD_PATHS。

用法：
    .venv\\Scripts\\python.exe 11_export.py --scope library --out data/export/lib.tsv
    .venv\\Scripts\\python.exe 11_export.py --scope wall_filtered --min-quality 0.3 --relative
    .venv\\Scripts\\python.exe 11_export.py --scope search_hits --face-id 12847
    .venv\\Scripts\\python.exe 11_export.py --scope library --format txt --json
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import db as D  # noqa: E402
from src import export as EX  # noqa: E402
from src.common import base_argparser, emit_summary, get_logger  # noqa: E402
from src.config_loader import cfg_from_args  # noqa: E402


def build_parser():
    p = base_argparser("TSV / TXT 导出（三入口）", needs_root=False)
    p.add_argument("--scope", required=True,
                   choices=[EX.SCOPE_LIBRARY, EX.SCOPE_WALL, EX.SCOPE_HITS],
                   help="library=全库清单 / wall_filtered=当前照片墙筛选结果 / search_hits=命中清单")
    p.add_argument("--out", default=None, help="输出路径（默认 data/export/<scope>_<时间戳>.tsv）")
    p.add_argument("--format", default="tsv", choices=["tsv", "txt"],
                   help="tsv=Excel 友好（tab+BOM）；txt=结构化 TXT（下游 agent）")
    p.add_argument("--relative", action="store_true", help="输出相对 media_root 的路径")
    p.add_argument("--min-quality", type=float, default=0.0, help="wall_filtered 的质量下限")
    p.add_argument("--t-start", default=None, help="wall_filtered 起始日期")
    p.add_argument("--t-end", default=None, help="wall_filtered 结束日期")
    p.add_argument("--file-id", type=int, default=None, help="仅该文件")
    p.add_argument("--face-id", type=int, default=None, help="search_hits：用该 face_id 跑一次找同款再导出")
    p.add_argument("--text", default=None, help="search_hits：用该文字跑一次检索再导出")
    p.add_argument("--no-verify", action="store_true", help="跳过 os.path.exists 校验（不推荐）")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = cfg_from_args(args)
    log = get_logger("11_export", cfg, args.log_level)
    D.ensure_schema(cfg.db_path, cfg.root, cfg)

    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        if args.scope == EX.SCOPE_LIBRARY:
            rows = EX.library_to_rows(conn, cfg)
        elif args.scope == EX.SCOPE_WALL:
            rows = EX.wall_to_rows(conn, cfg, min_quality=args.min_quality,
                                   t_start=args.t_start, t_end=args.t_end,
                                   file_id=args.file_id)
        else:
            rows = _hit_rows(args, cfg, conn, log)
    finally:
        conn.close()

    out = Path(args.out) if args.out else EX.default_out_path(cfg, args.scope)
    if args.format == "txt":
        if out.suffix.lower() != ".txt":
            out = out.with_suffix(".txt")
        res = EX.export_structured_txt(rows, args.scope, out, cfg=cfg, relative=args.relative)
    else:
        res = EX.export_tsv(rows, args.scope, out, relative=args.relative, cfg=cfg,
                            verify_exists=not args.no_verify)

    summary = res.as_dict()
    summary["scope"] = args.scope
    emit_summary(summary, args.json, log, "EXPORT SUMMARY")
    if res.dead:
        log.warning("有 %d 条路径已失效（标记为 DEAD，见文件尾部 # DEAD_PATHS）", len(res.dead))
    return 0


def _hit_rows(args, cfg, conn, log):
    from src import search as SE
    from src.vector_store import open_store

    store = open_store(cfg)
    ok, msg = store.health()
    if not ok:
        log.error("%s", msg)
        raise SystemExit(2)
    if args.face_id is not None:
        outcome = SE.search_by_face_id(conn, cfg, store, int(args.face_id))
    elif args.text:
        outcome = SE.search(conn, cfg, store, query_mode="text",
                            payload={"text": args.text}, exact_person=False)
    else:
        log.error("search_hits 需要 --face-id 或 --text")
        raise SystemExit(2)
    if outcome.warning:
        log.warning("%s", outcome.warning)
    return EX.shots_to_rows(outcome.files)


if __name__ == "__main__":
    raise SystemExit(main())
