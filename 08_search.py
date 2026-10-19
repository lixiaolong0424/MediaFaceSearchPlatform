"""08_search.py · 命令行检索（三路路由 + 自适应截断 + RRF + L4 后处理）。

UI 走 app.py；本文件用于无 UI 的冒烟测试与批处理。

用法：
    .venv\\Scripts\\python.exe 08_search.py --text "小孩在草地上跑" --json
    .venv\\Scripts\\python.exe 08_search.py --image "D:/q.jpg" --exact-person
    .venv\\Scripts\\python.exe 08_search.py --video "D:/q.mp4" --exact-person --json
    .venv\\Scripts\\python.exe 08_search.py --face-id 12847 --json      # 找同款
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import db as D  # noqa: E402
from src import search as SE  # noqa: E402
from src.common import base_argparser, emit_summary, get_logger  # noqa: E402
from src.config_loader import cfg_from_args  # noqa: E402
from src.vector_store import open_store  # noqa: E402


def build_parser():
    p = base_argparser("检索：文字 / 图片 / 视频 / 找同款", needs_root=False)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--text", help="文字描述")
    g.add_argument("--image", help="查询图片路径")
    g.add_argument("--video", help="查询视频路径")
    g.add_argument("--face-id", type=int, help="用库内已有 face_id 找同款")
    p.add_argument("--exact-person", action="store_true", help="精确找人（人脸匹配）")
    p.add_argument("--thresh", type=float, default=None, help="覆盖 retrieval.min_score")
    p.add_argument("--k", type=int, default=None, help="召回上限（默认 semantic.recall_k）")
    p.add_argument("--show", type=int, default=10, help="打印前 N 条命中")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    cfg = cfg_from_args(args)
    log = get_logger("08_search", cfg, args.log_level)
    if args.thresh is not None:
        cfg._data.setdefault("retrieval", {})["min_score"] = float(args.thresh)

    D.ensure_schema(cfg.db_path, cfg.root, cfg)
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    store = open_store(cfg)
    ok, msg = store.health()
    if not ok:
        log.error("%s", msg)
        return 2

    try:
        if args.face_id is not None:
            outcome = SE.search_by_face_id(conn, cfg, store, int(args.face_id), k=args.k)
        elif args.text:
            outcome = SE.search(conn, cfg, store, query_mode="text",
                                payload={"text": args.text},
                                exact_person=False, thresh=args.thresh, k=args.k)
        elif args.image:
            outcome = SE.search(conn, cfg, store, query_mode="image",
                                payload={"image": Path(args.image)},
                                exact_person=bool(args.exact_person),
                                thresh=args.thresh, k=args.k)
        else:
            outcome = SE.search(conn, cfg, store, query_mode="video",
                                payload={"video": Path(args.video)},
                                exact_person=bool(args.exact_person),
                                thresh=args.thresh, k=args.k)
    finally:
        conn.close()

    d = outcome.as_dict()
    log.info("模式=%s badge=%s 命中 %d 镜头 / %d 文件",
             outcome.mode, outcome.badge, outcome.total_shots, outcome.total_files)
    if outcome.warning:
        log.warning("%s", outcome.warning)
    log.info("%s", outcome.cut_note)
    for f in d["files"][:args.show]:
        seg = f["segments"][0] if f["segments"] else {}
        log.info("  %s  rank=%.3f best=%.3f  镜头=%d  段=%s~%s  %s",
                 f["kind"], f["rank_score"], f["best_score"], f["n_shots"],
                 seg.get("start"), seg.get("end"), f["path"])
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        emit_summary({"mode": outcome.mode, "shots": outcome.total_shots,
                      "files": outcome.total_files, "cut": outcome.cut_note},
                     False, log, "SEARCH SUMMARY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
