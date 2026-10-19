"""复现：重排后「尾部未重排候选」的分数空间与已重排候选混在一起。

问题链条
--------
1. `rerank_shot_hits` 只重排前 `rerank_input_k` 条；**之后的候选原样保留**，
   它们的 `.score` 仍是**召回余弦**（Qwen3-VL-Embedding 实测区间 0.31~0.50）。
2. 已重排的那批 `.score` 被覆盖成**交叉编码分数**（sigmoid(yes-no)，实测 0.14~0.51）。
3. 两个区间**重叠**，于是返回的分数序列**不再单调不增**：
   重排段末尾 0.39 → 尾部第一 0.44，是上升的。
4. `_final_cut(reranked=True)` 拿这条序列做 `adaptive_cut`（前提是序列非递增），
   并执行规格要求的「重排后保留 Top-50」硬上限 —— 于是**没重排过的尾部**
   被算进 Top-50，把已经重排过的候选挤出结果；界面上还会看到
   「第 31 名 sim=0.44」排在「第 30 名 sim=0.39」后面，自相矛盾。

用法：
    .venv\\Scripts\\python.exe tmp\\repro_rerank_score_space.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import rerank as R  # noqa: E402
from src.common import load_config  # noqa: E402
from src.search import ShotHit, _final_cut  # noqa: E402

INPUT_K = 30
N_TAIL = 30
FLAT = 0.390
TAIL_COS = 0.440


class FakeReranker:
    def __init__(self, scores: Sequence[float]) -> None:
        self._scores = list(scores)
        self.status = SimpleNamespace(loaded=True, error="", device="fake", backend="fake")
        self.batch_size = 8
        self.cache_hits = 0

    def score(self, query: Dict[str, Any], docs: Sequence[Dict[str, Any]],
              *, use_cache: bool = True) -> List[float]:
        return [self._scores[i % len(self._scores)] for i in range(len(docs))]


def main() -> int:
    cfg = load_config(None, None)
    cfg.override("retrieval.rerank_input_k", INPUT_K)
    cfg.override("retrieval.rerank_skip_cosine", 0.99)
    cfg.override("retrieval.rerank_topk", 50)
    cfg.override("retrieval.rerank_min_score", 0.25)

    tmpdir = tempfile.TemporaryDirectory()
    tmp = Path(tmpdir.name)
    hits: List[ShotHit] = []
    for i in range(INPUT_K + N_TAIL):
        p = tmp / f"f{i}.jpg"
        p.write_bytes(b"\xff\xd8\xff\xd9")
        hits.append(ShotHit(file_id=i + 1, shot_ix=0, score=TAIL_COS,
                            sec=0.0, shot_start=0.0, shot_end=0.0,
                            thumb_path=str(p), path=str(p)))

    R.get_reranker = lambda c, reload=False: FakeReranker(
        [FLAT + i * 1e-6 for i in range(INPUT_K)])   # type: ignore[assignment]

    out, rep = R.rerank_shot_hits(None, cfg, hits, query={"text": "x"})
    print(f"重排报告：applied={rep['applied']} input_k={rep['input_k']} "
          f"n_docs={rep['n_docs']} 用时 {rep['elapsed_sec']}s")

    scores = [h.score for h in out]
    rise = next((i for i, (a, b) in enumerate(zip(scores, scores[1:])) if b > a + 1e-9), -1)
    print(f"\n返回序列长度 {len(scores)}")
    print(f"  前 3 个分数      : {[round(s, 4) for s in scores[:3]]}")
    print(f"  第 {INPUT_K} 名附近      : {[round(s, 4) for s in scores[INPUT_K-2:INPUT_K+2]]}"
          f"   ← 重排段与尾部的交界")
    print(f"  首次上升发生在 index {rise}"
          f"（{round(scores[rise],4)} → {round(scores[rise+1],4)}）"
          if rise >= 0 else "  序列单调不增")

    kept, cut = _final_cut(cfg, out, reranked=True)
    kept_scores = [h.score for h in kept]
    kept_rise = next((i for i, (a, b) in enumerate(zip(kept_scores, kept_scores[1:]))
                      if b > a + 1e-9), -1)
    n_unreranked = sum(1 for h in kept if h.rerank_score is None)
    print(f"\n_final_cut(reranked=True) → 保留 {len(kept)} 条（{cut.note}）")
    print(f"  其中**未参与重排**的条目：{n_unreranked} 条"
          f"（占保留结果的 {n_unreranked/max(1,len(kept))*100:.0f}%）")
    print(f"  截断后序列仍有上升：index {kept_rise}"
          f"（{round(kept_scores[kept_rise],4)} → {round(kept_scores[kept_rise+1],4)}）"
          if kept_rise >= 0 else "  截断后序列单调不增")

    print("\n" + "=" * 74)
    # 判定用的是**真正的不变量**，而不是「结果里有没有未重排项」：
    # 作者的设计是「input_k 之外的候选绝不丢弃、按原顺序接在后面」，
    # 所以 input_k=30 < topk=50 时，Top-50 里本来就会有 20 条未重排候选。
    # 那不是 bug。bug 是这两条：
    #   ① 分数序列出现上升段（adaptive_cut 的前提被破坏）
    #   ② 未重排项排到了已重排项之前 / 分数高于已重排项（角标自相矛盾）
    min_reranked = min((h.score for h in out if h.rerank_score is not None), default=None)
    first_unreranked = next((i for i, h in enumerate(out) if h.rerank_score is None), None)
    last_reranked = max((i for i, h in enumerate(out) if h.rerank_score is not None),
                        default=None)
    order_ok = (first_unreranked is None or last_reranked is None
                or first_unreranked > last_reranked)
    band_ok = (min_reranked is None or all(
        h.score <= min_reranked + 1e-9 for h in out if h.rerank_score is None))
    print(f"不变量检查：")
    print(f"  分数序列单调不增                      : {'✓' if rise < 0 else '✗'}")
    print(f"  截断后仍单调不增                      : {'✓' if kept_rise < 0 else '✗'}")
    print(f"  未重排项全部排在已重排项之后          : {'✓' if order_ok else '✗'}")
    print(f"  未重排项分数不高于已重排最低分        : {'✓' if band_ok else '✗'}")
    print(f"  （Top-50 里含 {n_unreranked} 条未重排项属于设计选择：input_k={INPUT_K} < topk=50，"
          f"尾部不丢弃）")
    bad = (rise >= 0) or (kept_rise >= 0) or (not order_ok) or (not band_ok)
    if bad:
        print("\n结论：**确认存在分数空间混用问题**（见上面 ✗ 的项）")
    else:
        print("\n结论：**未复现** —— 未重排候选已被压到重排分之下且排在后面，"
              "自适应截断拿到的是单调不增序列。")
    print("=" * 74)
    tmpdir.cleanup()
    return 1 if bad else 0      # 0 = 不变量成立（已修好）；1 = 仍有问题


if __name__ == "__main__":
    raise SystemExit(main())
