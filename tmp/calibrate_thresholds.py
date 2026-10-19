"""阈值标定：召回余弦 与 重排序分数 两个空间的真实分布。

背景（2026-10-16 实测）：规格写死的 retrieval.min_score=0.55 是按「CLIP 类模型」
的直觉给的，但本项目的语义模型是 Qwen3-VL-Embedding-2B：
实测文字查询「一只猫」的**最高**余弦只有 0.50 → 自适应截断把结果全砍成 0 条，
表现就是「搜什么都空」。重排序的分数空间又完全不同（sigmoid 后的 yes 概率，
自检里相关对 0.55 / 无关对 0.20），用同一个 0.55 同样会把结果砍空。

本脚本用真实库跑几组查询，输出两个空间的分布，作为 config 里阈值的依据。

用法：
    .venv\\Scripts\\python.exe tmp\\calibrate_thresholds.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')

from src import db as D
from src.common import load_config
from src.encode_model import encode_query
from src.rerank import doc_media_for_shot, get_reranker
from src.search import enrich, semantic_search
from src.vector_store import open_store, semantic_collection

QUERIES = ["一只猫", "人物", "室内", "手机截图", "文字", "屏幕", "合影", "夜景"]
TOP_N = 40


def pct(a, q):
    return float(np.percentile(a, q)) if len(a) else float("nan")


def main() -> int:
    cfg = load_config(None, None)
    store = open_store(cfg)
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    coll = semantic_collection(cfg)
    print(f"collection={coll} points={store.count(coll)}")

    eng = get_reranker(cfg)
    if not eng.status.loaded:
        print("重排序模型不可用：", eng.status.error)

    recall_stats = []
    rerank_stats = []
    try:
        for q in QUERIES:
            vec, _ = encode_query(cfg, text=q)
            # 用超大 k 绕过自适应截断，拿到完整召回分布
            shots, _cut = semantic_search(conn, cfg, store, query_vec=vec, recall_k=200)
            raw = store.search_knn(coll, vec, 200)
            cos = np.asarray([h.score for h in raw], dtype="float32")
            if cos.size == 0:
                print(f"{q!r}: 召回 0 条（库还太小？）")
                continue
            recall_stats.append(cos)
            print(f"{q!r}: n={cos.size} max={cos.max():.4f} "
                  f"p99={pct(cos,99):.4f} p90={pct(cos,90):.4f} p50={pct(cos,50):.4f}")

            if eng.status.loaded:
                hits = store.search_knn(coll, vec, TOP_N)
                done = {int(r["file_id"]) for r in conn.execute(
                    "SELECT file_id FROM files WHERE status='done'")}
                hits = [h for h in hits if h.file_id in done]
                from src.search import hits_to_shot_hits

                shot_hits = hits_to_shot_hits([(h, h.score, 1) for h in hits], badge="sim")
                enrich(conn, cfg, shot_hits)
                docs, keep = [], []
                for h in shot_hits:
                    d = doc_media_for_shot(h)
                    if d:
                        docs.append(d)
                        keep.append(h)
                if docs:
                    sc = np.asarray(eng.score({"text": q}, docs), dtype="float32")
                    rerank_stats.append(sc)
                    print(f"      重排 n={sc.size} max={sc.max():.4f} "
                          f"p90={pct(sc,90):.4f} p50={pct(sc,50):.4f} min={sc.min():.4f}")
    finally:
        conn.close()

    if recall_stats:
        allc = np.concatenate(recall_stats)
        print("\n=== 召回余弦（全部查询合并）===")
        for q in (99, 95, 90, 75, 50, 25, 10):
            print(f"  p{q:<3} = {pct(allc,q):.4f}")
        print(f"  max = {allc.max():.4f}  n = {allc.size}")
        print("\n  建议 retrieval.min_score：取「相关查询 p90 附近」，"
              f"当前 p90={pct(allc,90):.4f}，p75={pct(allc,75):.4f}")
    if rerank_stats:
        allr = np.concatenate(rerank_stats)
        print("\n=== 重排序分数（全部查询合并）===")
        for q in (99, 90, 75, 50, 25, 10):
            print(f"  p{q:<3} = {pct(allr,q):.4f}")
        print(f"  max = {allr.max():.4f}  min = {allr.min():.4f}  n = {allr.size}")
        print("\n  建议 retrieval.rerank_min_score：约 p75 附近，"
              f"当前 p75={pct(allr,75):.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
