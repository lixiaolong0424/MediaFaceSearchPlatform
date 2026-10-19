"""阈值扫描：为「召回余弦」这个压缩得很厉害的分数空间挑 min_score 与 gap_min。

要解决的问题（2026-10-16 实测）：
  Qwen3-VL-Embedding-2B 的文字→图片余弦整体落在 0.31~0.50，
  而规格写的 min_score=0.55 **高于模型能给出的最高分** → 自适应截断永远返回 0 条，
  表现就是「搜什么都空」。
  但只把 min_score 调低也不行：adaptive_cut 的第一条规则是
  「相邻分数差 >= gap_min 且下一名 >= min_score 就截断」，
  200 条平滑分布的平均相邻差只有约 0.0009，gap_min=0.03 相当于 33 倍平均差，
  会在第 1~2 名之间就砍断，只返回 1 条。

本脚本在真实库上扫描 (min_score, gap_min)，输出每种组合保留的条数分布，
据此选一组「不会砍空、也不会把几百条垃圾倒出来」的值。

用法：
    .venv\\Scripts\\python.exe tmp\\sweep_cut.py
"""

from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')

from src import db as D
from src.common import load_config
from src.encode_model import encode_query
from src.search import adaptive_cut
from src.vector_store import open_store, semantic_collection

QUERIES = ["一只猫", "人物", "室内", "手机截图", "文字", "屏幕", "合影", "夜景",
           "完全无关的查询词xyzzy"]
MIN_SCORES = [0.55, 0.40, 0.35, 0.32, 0.30, 0.28, 0.25]
GAP_MINS = [0.03, 0.02, 0.015, 0.01, 0.008]


def main() -> int:
    cfg = load_config(None, None)
    store = open_store(cfg)
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    coll = semantic_collection(cfg)

    score_lists = {}
    for q in QUERIES:
        vec, _ = encode_query(cfg, text=q)
        raw = store.search_knn(coll, vec, 400)
        done = {int(r["file_id"]) for r in conn.execute(
            "SELECT file_id FROM files WHERE status='done'")}
        score_lists[q] = [h.score for h in raw if h.file_id in done]
    conn.close()

    print(f"库内向量 {store.count(coll)}，查询 {len(QUERIES)} 个，k=400\n")
    print("每个查询的分数轮廓：")
    for q, ss in score_lists.items():
        if not ss:
            continue
        a = np.asarray(ss)
        steps = np.diff(a[:50])
        print(f"  {q!r:<18} n={a.size:>3} max={a.max():.3f} p50={np.percentile(a,50):.3f} "
              f"前50名平均相邻差={steps.mean():.5f} 最大相邻差={steps.max():.5f}")

    print("\n=========== 保留条数（行=min_score，列=gap_min）===========")
    hdr = "min_score \\ gap_min | " + " | ".join(f"{g:>16.3f}" for g in GAP_MINS)
    print(hdr)
    print("-" * len(hdr))
    for ms in MIN_SCORES:
        cells = []
        for gm in GAP_MINS:
            kept = []
            for q, ss in score_lists.items():
                if not ss:
                    continue
                r = adaptive_cut(ss, k_hard=200, min_score=ms, gap_min=gm)
                kept.append(r.kept)
            if kept:
                cells.append(f"{int(np.median(kept)):>3}/中 {min(kept):>3}/最 {max(kept):>4}")
        print(f"{ms:>18.2f} | " + " | ".join(f"{c:>16}" for c in cells))

    print("\n（每格：保留条数的 中位数/最小/最大，跨所有查询；含一个故意无关的查询词）")
    print("\n参考判据：无关查询也应只返回很少几条；相关查询希望中位数在 10~60 条之间。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
