"""05_rerank.py · L2d 重排序（Qwen3-VL-Reranker-2B）。

规格第五章：召回是双塔（查询与素材各自编码成 2048 维再算余弦），
重排序是交叉编码（查询与素材拼成一条序列，模型直接判 yes/no），准但贵，
所以放在**召回之后、L4 聚合之前**。规格写的是「k=400 → Top-50」。

用法：
    # 1) 纯自检：权重能不能加载、相关对是否高于无关对（不碰库）
    .venv\\Scripts\\python.exe 05_rerank.py --self-test

    # 2) 文字查询：召回 → 重排 → 打印前后对比
    .venv\\Scripts\\python.exe 05_rerank.py --text "一只趴在窗台上的橘猫" --k 200 --topk 20

    # 3) 图片/视频查询
    .venv\\Scripts\\python.exe 05_rerank.py --image "E:/some/photo.jpg" --k 100
    .venv\\Scripts\\python.exe 05_rerank.py --video "E:/some/clip.mp4" --k 50

    # 4) 测吞吐（决定 rerank_input_k 该设多少）
    .venv\\Scripts\\python.exe 05_rerank.py --benchmark --k 100

    # 5) 看库里还没有语义向量时也能自检
    .venv\\Scripts\\python.exe 05_rerank.py --json --self-test
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import db as D  # noqa: E402
from src.common import base_argparser, emit_summary, get_logger  # noqa: E402
from src.config_loader import cfg_from_args  # noqa: E402
from src.rerank import Reranker, doc_media_for_shot  # noqa: E402


def _print_table(rows: List[Dict[str, Any]], log, title: str) -> None:
    log.info("=== %s ===", title)
    for r in rows:
        log.info("  #%02d  rerank=%s  recall=%s  file_id=%-7s shot=%-4s  %s",
                 r["rank"], r.get("rerank_score"), r.get("recall_score"),
                 r["file_id"], r["shot_ix"], Path(str(r.get("path") or "")).name)


def cmd_self_test(cfg, args, log) -> Dict[str, Any]:
    eng = Reranker(cfg)
    st = eng.load()
    out: Dict[str, Any] = {"loaded": st.loaded, "device": st.device,
                           "backend": st.backend, "model_path": str(eng.model_path),
                           "error": st.error}
    if st.loaded:
        scores = eng.score({"text": "一只猫"},
                           [{"text": "一只趴在窗台上的橘猫"},
                            {"text": "波音747客机起飞时的发动机尾焰"}])
        out["score_relevant"] = round(float(scores[0]), 4)
        out["score_irrelevant"] = round(float(scores[1]), 4)
        out["relevant_gt_irrelevant"] = bool(scores[0] > scores[1])
        log.info("相关=%.4f  无关=%.4f  相关>无关=%s",
                 scores[0], scores[1], scores[0] > scores[1])
    return out


def _build_query(cfg, args, log):
    """返回 (query_media, 查询向量)。文字走 encode_query，图片/视频同理。"""
    from src.encode_model import encode_query

    if args.text:
        vec, _ = encode_query(cfg, text=args.text)
        return {"text": args.text}, vec
    if args.image:
        vec, _ = encode_query(cfg, image=Path(args.image))
        return {"image": str(args.image)}, vec
    if args.video:
        vec, _ = encode_query(cfg, video=Path(args.video))
        return {"video": str(args.video)}, vec
    raise SystemExit("必须给 --text / --image / --video 之一（或 --self-test）")


def cmd_search(cfg, args, log) -> Dict[str, Any]:
    from src.search import (_apply_rerank, _final_cut, enrich, semantic_recall)
    from src.vector_store import open_store, semantic_collection

    if args.min_score is not None:
        cfg.override("retrieval.min_score", float(args.min_score))
    if args.rerank_min_score is not None:
        cfg.override("retrieval.rerank_min_score", float(args.rerank_min_score))
    if args.gap_min is not None:
        cfg.override("retrieval.gap_min", float(args.gap_min))
    if args.no_rerank:
        cfg.override("retrieval.rerank_enabled", False)
    if args.input_k is not None:
        cfg.override("retrieval.rerank_input_k", int(args.input_k))

    query_media, vec = _build_query(cfg, args, log)
    store = open_store(cfg)
    ok, msg = store.health()
    if not ok:
        raise RuntimeError(msg)
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        coll = semantic_collection(cfg)
        n_points = store.count(coll)
        log.info("召回集合 %s（%d 个向量），k=%d", coll, n_points, args.k)
        if n_points == 0:
            return {"error": "语义向量库还是空的，先跑 04_encode.py", "points": 0}

        # 用**不截断**的召回：重排必须在截断之前（否则阈值一砍，
        # 重排就没东西可排了，见 src/search.py::semantic_recall 的注释）
        shots = semantic_recall(conn, cfg, store, query_vec=vec, recall_k=args.k)
        enrich(conn, cfg, shots)
        before = [{"rank": i + 1, "file_id": h.file_id, "shot_ix": h.shot_ix,
                   "recall_score": round(float(h.score), 4),
                   "rerank_score": None, "path": h.path}
                  for i, h in enumerate(shots[:args.topk])]

        if args.benchmark:
            return _benchmark(cfg, shots, query_media, log, before,
                              f"召回 {len(shots)} 条")

        shots, reranked, rr_note = _apply_rerank(
            conn, cfg, shots, query_media=query_media, topk=args.topk)
        log.info("重排序报告：%s", reranked and rr_note or rr_note)
        after = [{"rank": i + 1, "file_id": h.file_id, "shot_ix": h.shot_ix,
                  "recall_score": h.recall_score if h.recall_score is not None
                  else round(float(h.score), 4),
                  "rerank_score": h.rerank_score, "path": h.path}
                 for i, h in enumerate(shots[:args.topk])]
        final, cut = _final_cut(cfg, shots, reranked=reranked)
        rows = [{"rank": i + 1, "file_id": h.file_id, "shot_ix": h.shot_ix,
                 "recall_score": h.recall_score, "rerank_score": h.rerank_score,
                 "path": h.path}
                for i, h in enumerate(final[:args.topk])]

        _print_table(before, log, "重排序前（按召回余弦）")
        _print_table(after, log, "重排序后（按交叉编码分数）")
        _print_table(rows, log, f"最终返回（自适应截断：{cut.note}）")

        b_ids = [r["file_id"] for r in before]
        a_ids = [r["file_id"] for r in after]
        return {
            "mode": "text" if args.text else ("image" if args.image else "video"),
            "recall_k": args.k,
            "topk": args.topk,
            "rerank_input_k": args.input_k,
            "recalled": len(shots),
            "reranked": bool(reranked),
            "rerank_note": rr_note,
            "min_score_used": cfg.get("retrieval.rerank_min_score" if reranked
                                       else "retrieval.min_score"),
            "final_kept": len(final),
            "cut_note": cut.note,
            "rank_changed_positions": sum(1 for x, y in zip(b_ids, a_ids) if x != y),
            "overlap_in_topk": len(set(b_ids) & set(a_ids)),
            "top": rows,
        }
    finally:
        conn.close()


def _benchmark(cfg, shots, query_media, log, before, cut_note) -> Dict[str, Any]:
    """量「每条候选的重排序耗时」，用来决定 rerank_input_k。"""
    from src.rerank import get_reranker

    eng = get_reranker(cfg)
    if not eng.status.loaded:
        return {"error": f"重排序模型不可用：{eng.status.error}"}
    n = min(len(shots), 40)
    docs = [doc_media_for_shot(h) for h in shots[:n]]
    usable = [(h, d) for h, d in zip(shots[:n], docs) if d]
    if not usable:
        return {"error": "候选没有可用的关键帧/原图"}
    t0 = time.monotonic()
    eng.score(query_media, [d for _h, d in usable], use_cache=False)
    el = max(1e-9, time.monotonic() - t0)
    per = el / len(usable)
    out = {
        "benchmark": True, "n_docs": len(usable), "elapsed_sec": round(el, 2),
        "sec_per_doc": round(per, 4), "docs_per_sec": round(1.0 / per, 2),
        "batch_size": eng.batch_size, "device": eng.status.device,
        "eta_60_docs_sec": round(per * 60, 1),
        "eta_200_docs_sec": round(per * 200, 1),
        "eta_400_docs_sec": round(per * 400, 1),
        "recall_k": len(shots), "cut_note": cut_note,
    }
    for sec in (60, 200, 400):
        log.info("  %d 条候选 ≈ %.1f 秒", sec, per * sec)
    return out


def main(argv=None) -> int:
    p = base_argparser("L2d 重排序（Qwen3-VL-Reranker-2B）", needs_root=False)
    p.add_argument("--text", default=None, help="文字查询")
    p.add_argument("--image", default=None, help="图片查询路径")
    p.add_argument("--video", default=None, help="视频查询路径")
    p.add_argument("--k", type=int, default=None, help="召回条数（默认 semantic.recall_k）")
    p.add_argument("--input-k", type=int, default=None,
                   help="重排前多少条（默认 retrieval.rerank_input_k）")
    p.add_argument("--topk", type=int, default=None, help="输出条数（默认 retrieval.rerank_topk）")
    p.add_argument("--self-test", action="store_true", help="只做权重加载与自检")
    p.add_argument("--benchmark", action="store_true", help="量每条候选的重排序耗时")
    p.add_argument("--no-rerank", action="store_true", help="关掉重排，只看召回+截断的基线")
    p.add_argument("--min-score", type=float, default=None,
                   help="覆盖 retrieval.min_score（召回余弦空间的阈值）")
    p.add_argument("--rerank-min-score", type=float, default=None,
                   help="覆盖 retrieval.rerank_min_score（重排分空间的阈值）")
    p.add_argument("--gap-min", type=float, default=None, help="覆盖 retrieval.gap_min")
    p.add_argument("--rerank-max-pixels", type=int, default=None,
                   help="覆盖 semantic.rerank_max_pixels（重排的图像像素预算，"
                        "只影响查询期、不动库里的向量；越小越快）")
    args = p.parse_args(argv)
    cfg = cfg_from_args(args)
    if args.rerank_max_pixels:
        cfg.override("semantic.rerank_max_pixels", int(args.rerank_max_pixels))
    log = get_logger("05_rerank", cfg, args.log_level)

    if args.k is None:
        args.k = int(cfg.get("semantic.recall_k", 400))
    if args.topk is None:
        args.topk = int(cfg.get("retrieval.rerank_topk", 50))
    if args.input_k is None:
        args.input_k = int(cfg.get("retrieval.rerank_input_k", 60))

    try:
        if args.self_test:
            summary = cmd_self_test(cfg, args, log)
        elif args.text or args.image or args.video:
            summary = cmd_search(cfg, args, log)
        else:
            log.error("必须给 --self-test 或 --text/--image/--video 之一；-h 看用法")
            return 2
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2
    emit_summary(summary, args.json, log, "RERANK SUMMARY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
