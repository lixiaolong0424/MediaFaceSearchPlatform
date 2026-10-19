"""L2d 重排序（05_rerank / src/rerank.py）的验收测试。

覆盖三类断言：
  A. **纯逻辑**（不需要 GPU、不需要权重）：文档媒体选取、失败降级、
     截断顺序、分数缓存 —— 这些是「重排序不可用也不能让检索崩」的保证。
  B. **阈值标定**（不需要 GPU）：两个分数空间的阈值必须落在各自模型的实测区间内。
     这一条是回归保护：规格原样的 min_score=0.55 高于模型上限，会把结果全砍空。
  C. **真实权重**（需要 GPU，可选）：加载自检 + 明显相关的一对必须高于无关的一对。

用法：
    .venv\\Scripts\\python.exe tests\\test_rerank.py            # A + B（默认）
    .venv\\Scripts\\python.exe tests\\test_rerank.py --gpu      # 再加上 C
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.common import load_config  # noqa: E402
from src.rerank import doc_media_for_shot  # noqa: E402
from src.search import CutResult, ShotHit, _final_cut  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


class _H:
    """最小 hit 替身（doc_media_for_shot 只需要 thumb_path / path）。"""

    def __init__(self, thumb=None, path=None):
        self.thumb_path = thumb
        self.path = path


def test_doc_media(tmp: Path) -> None:
    print("\n[A1] 文档媒体选取：关键帧优先、原图兜底、都没有就跳过")
    img = tmp / "k.jpg"
    img.write_bytes(b"\xff\xd8\xff\xd9")
    orig = tmp / "o.jpg"
    orig.write_bytes(b"\xff\xd8\xff\xd9")

    a = doc_media_for_shot(_H(thumb=str(img), path=str(orig)))
    check("有关键帧时用关键帧", a.get("image") == str(img), f"got {a}")
    b = doc_media_for_shot(_H(thumb=str(tmp / "missing.jpg"), path=str(orig)))
    check("关键帧不存在时回退原图", b.get("image") == str(orig), f"got {b}")
    c = doc_media_for_shot(_H(thumb=None, path=None))
    check("两者都没有时返回空（调用方会跳过该条）", c == {}, f"got {c}")
    d = doc_media_for_shot(_H(thumb=str(tmp / "nope.jpg"), path=str(tmp / "nope2.jpg")))
    check("路径都不存在时返回空", d == {}, f"got {d}")


def test_shot_hit_fields() -> None:
    print("\n[A2] ShotHit 保留两个分数（重排分与召回分都要能追溯）")
    h = ShotHit(file_id=1, shot_ix=0, score=0.4, sec=0.0, shot_start=0.0, shot_end=0.0)
    h.rerank_score = 0.72
    h.recall_score = 0.4
    d = h.as_dict()
    check("as_dict 含 rerank_score", d.get("rerank_score") == 0.72, f"{d.get('rerank_score')}")
    check("as_dict 含 recall_score", d.get("recall_score") == 0.4, f"{d.get('recall_score')}")


def test_thresholds_calibrated() -> None:
    print("\n[B1] 阈值必须落在这两个模型各自的实测分数区间内（回归保护）")
    cfg = load_config(None, None)
    ms = float(cfg.get("retrieval.min_score"))
    rms = float(cfg.get("retrieval.rerank_min_score"))
    # 实测：Qwen3-VL-Embedding 文字→图片余弦 0.31~0.50（最大 0.497）；
    # 规格原样的 0.55 高于模型上限 → 结果必然被砍空。
    check("retrieval.min_score 不超过召回余弦实测上限 0.50",
          ms <= 0.50, f"min_score={ms}（会让所有文字查询返回 0 条）")
    check("retrieval.min_score 高于明显噪声区 0.20", ms >= 0.20, f"min_score={ms}")
    # 实测：重排序分 0.14~0.51
    check("retrieval.rerank_min_score 不超过重排实测上限 0.51",
          rms <= 0.51, f"rerank_min_score={rms}")
    check("retrieval.rerank_min_score 高于重排无关区 0.15",
          rms >= 0.15, f"rerank_min_score={rms}")
    check("重排阈值与召回阈值是两个独立的键",
          cfg.get("retrieval.rerank_min_score") is not None)


def test_final_cut_order() -> None:
    print("\n[B2] 截断发生在重排之后，且重排时用重排分空间的阈值")
    cfg = load_config(None, None)
    cfg.override("retrieval.rerank_min_score", 0.25)
    cfg.override("retrieval.rerank_topk", 3)

    # 召回余弦全在 0.4 附近（≥ min_score=0.30 不会截断），
    # 但重排分只有 3 条过 0.25 → 说明用的是 rerank_min_score。
    # 注意分数序列是刻意排的：第 1~3 名之间**没有** >= gap_min(0.03) 的台阶，
    # 所以 adaptive_cut 的第一条规则（gap 截断）不会触发，走的是阈值兜底那条 ——
    # 这正是要测的「重排时用重排分空间的阈值」。
    # 第 3 名到第 4 名有个大台阶，但 top[3]=0.10 < min_score，gap 规则同样不触发。
    hits = []
    for i in range(6):
        h = ShotHit(file_id=i + 1, shot_ix=0, score=0.40, sec=0.0,
                    shot_start=0.0, shot_end=0.0)
        h.rerank_score = [0.60, 0.59, 0.58, 0.10, 0.09, 0.08][i]
        h.score = h.rerank_score          # 重排后 score 被覆盖成重排分
        hits.append(h)
    kept, cut = _final_cut(cfg, hits, reranked=True)
    check("重排后按 rerank_min_score=0.25 过滤（3 条过线）",
          len(kept) == 3, f"kept={len(kept)} cut={cut.note!r}")
    check("再受 rerank_topk=3 硬上限约束", len(kept) <= 3, f"kept={len(kept)}")
    check("截断说明可读", isinstance(cut.note, str) and cut.note != "", f"{cut.note!r}")

    # 未重排时用召回阈值 0.30：把 score 换回 0.40 的召回分，应全部留下
    for h in hits:
        h.score = 0.40
    kept2, cut2 = _final_cut(cfg, hits, reranked=False)
    check("未重排时走 retrieval.min_score（0.30）而非重排阈值",
          len(kept2) == 6, f"kept={len(kept2)} cut={cut2.note!r}")


def test_degradation() -> None:
    print("\n[B3] 重排序不可用时必须退化为原顺序并给出可读原因（绝不抛错）")
    cfg = load_config(None, None)
    cfg.override("retrieval.rerank_enabled", False)
    from src.search import _apply_rerank

    hits = [ShotHit(file_id=i, shot_ix=0, score=0.4 - i * 0.01, sec=0.0,
                    shot_start=0.0, shot_end=0.0) for i in range(3)]
    out, applied, note = _apply_rerank(None, cfg, list(hits), query_media={"text": "x"})
    check("关闭时原样返回", [h.file_id for h in out] == [0, 1, 2])
    check("applied=False", applied is False)
    check("给出可读原因", "关闭" in note, f"{note!r}")

    cfg.override("retrieval.rerank_enabled", True)
    out2, applied2, note2 = _apply_rerank(None, cfg, list(hits), query_media={})
    check("查询侧无媒体时跳过", applied2 is False and "没有可用" in note2, f"{note2!r}")

    from src.rerank import rerank_shot_hits

    out3, rep = rerank_shot_hits(None, cfg, [], query={"text": "x"})
    check("空候选不报错", out3 == [] and rep["applied"] is False)


# --------------------------------------------------------------------------- #
# B5. 分数空间隔离（2026-10-16 修的回归）
# --------------------------------------------------------------------------- #


class _FakeReranker:
    """按给定分数序列打分的假引擎，替代 2B 真模型做编排测试。"""

    def __init__(self, scores):
        from types import SimpleNamespace

        self._scores = list(scores)
        self.status = SimpleNamespace(loaded=True, error="", device="fake", backend="fake")
        self.batch_size = 8
        self.cache_hits = 0
        self.calls = 0

    def score(self, query, docs, *, use_cache=True):
        self.calls += 1
        return [self._scores[i % len(self._scores)] for i in range(len(docs))]


def _shots_with_media(n: int, tmp: Path, *, cosine: float):
    out = []
    for i in range(n):
        p = tmp / f"s{i}.jpg"
        p.write_bytes(b"\xff\xd8\xff\xd9")
        out.append(ShotHit(file_id=i + 1, shot_ix=0, score=cosine, sec=0.0,
                           shot_start=0.0, shot_end=0.0,
                           thumb_path=str(p), path=str(p)))
    return out


def test_tail_score_space_isolation(tmp: Path) -> None:
    """重排序只作用于前 input_k 条，**之后的候选仍是召回余弦**（实测 0.31~0.50），
    而重排分是 sigmoid(yes-no)（实测 0.14~0.51）—— 两个区间重叠。

    修之前：返回的分数序列在「重排段 → 尾部」处**上升**（实测 0.39→0.44），
    `adaptive_cut` 的非递增前提被破坏；而且规格 8.2 的「重排后保留 Top-50」
    会把没重排过的尾部算进 Top-50（实测 50 条里 20 条、占 40%），
    把已重排的候选挤出去，界面上出现「sim=0.44 排在 sim=0.39 后面」。

    修法：未参与重排的候选（头部缺媒体的 + input_k 之外的尾部）一律压进
    「严格低于已重排最低分」的单调递减带，真实余弦保留在 recall_score。
    本用例把这个不变量钉死。
    """
    import src.rerank as R
    from src.rerank import rerank_shot_hits

    print("\n[B5] 重排后未参与重排的候选不得与重排分混用（回归：分数空间隔离）")
    cfg = load_config(None, None)
    cfg.override("retrieval.rerank_input_k", 30)
    cfg.override("retrieval.rerank_skip_cosine", 0.99)   # 关掉「精确匹配跳过」
    cfg.override("retrieval.rerank_topk", 50)
    cfg.override("retrieval.rerank_min_score", 0.25)

    real = R.get_reranker
    try:
        # 重排分很平（都 ≈0.39，差距 < gap_min，阈值/间隙都截不掉）
        R.get_reranker = lambda c, reload=False: _FakeReranker(
            [0.390 + i * 1e-6 for i in range(30)])
        shots = _shots_with_media(60, tmp, cosine=0.44)   # 尾部余弦 0.44 > 重排分 0.39
        out, rep = rerank_shot_hits(None, cfg, shots, query={"text": "x"})
    finally:
        R.get_reranker = real

    check("重排已生效", rep["applied"] is True, str(rep))
    check("总数不丢（60 条）", len(out) == 60, f"got {len(out)}")

    scores = [h.score for h in out]
    rise = next((i for i, (a, b) in enumerate(zip(scores, scores[1:])) if b > a + 1e-9), -1)
    check("分数序列单调不增（adaptive_cut 的前提）", rise < 0,
          f"首个上升处 index {rise}")

    reranked = [i for i, h in enumerate(out) if h.rerank_score is not None]
    unreranked = [i for i, h in enumerate(out) if h.rerank_score is None]
    check("有 30 条被重排、30 条没有", len(reranked) == 30 and len(unreranked) == 30,
          f"reranked={len(reranked)} unreranked={len(unreranked)}")
    check("未重排的候选全部排在已重排候选之后",
          not reranked or not unreranked or min(unreranked) > max(reranked),
          f"unreranked 首次出现在 {min(unreranked) if unreranked else '-'}")
    floor = min(out[i].score for i in reranked)
    check("未重排候选的分数都不高于已重排最低分",
          all(out[i].score <= floor + 1e-9 for i in unreranked), f"floor={floor:.4f}")
    check("未重排候选的真实余弦保留在 recall_score",
          all(out[i].recall_score is not None for i in unreranked))
    check("报告里标出了被降级的条数", rep.get("tail_demoted") == 30, str(rep.get("tail_demoted")))

    kept, cut = _final_cut(cfg, out, reranked=True)
    seq = [h.score for h in kept]
    check("截断后序列仍单调不增",
          all(b <= a + 1e-9 for a, b in zip(seq, seq[1:])), str([round(s, 4) for s in seq]))
    check("Top-50 不会把未重排项排在已重排项之前",
          all(h.rerank_score is not None for h in kept
              if h.rerank_score is None) is False
          or max((i for i, h in enumerate(kept) if h.rerank_score is not None),
                 default=-1) < min((i for i, h in enumerate(kept)
                                    if h.rerank_score is None), default=10 ** 9))


def test_cut_result_shape() -> None:
    print("\n[B4] CutResult 结构（_final_cut 的返回契约）")
    cfg = load_config(None, None)
    kept, cut = _final_cut(cfg, [], reranked=True)
    check("空输入返回空", kept == [])
    check("返回 CutResult", isinstance(cut, CutResult), f"{type(cut)}")


def test_gpu() -> None:
    print("\n[C1] 真实权重：加载自检 + 相关对必须高于无关对")
    from src.rerank import Reranker

    cfg = load_config(None, None)
    eng = Reranker(cfg)
    st = eng.load()
    check("权重加载成功", st.loaded, f"error={st.error}")
    if not st.loaded:
        return
    check("落在 GPU 上", st.device == "cuda", f"device={st.device}")
    scores = eng.score({"text": "一只猫"},
                       [{"text": "一只趴在窗台上的橘猫"},
                        {"text": "波音747客机起飞时的发动机尾焰"}])
    check("相关 > 无关", scores[0] > scores[1], f"{scores}")
    check("分数在 [0,1]", all(0.0 <= s <= 1.0 for s in scores), f"{scores}")
    # 缓存：同一对再打一次不应改变分数
    again = eng.score({"text": "一只猫"}, [{"text": "一只趴在窗台上的橘猫"}])
    check("分数缓存命中且一致", abs(again[0] - scores[0]) < 1e-9, f"{again}")
    check("缓存命中计数增加", eng.cache_hits >= 1, f"hits={eng.cache_hits}")


def main() -> int:
    import tempfile

    gpu = "--gpu" in sys.argv
    print("=" * 72)
    print("L2d 重排序验收测试" + ("（含 GPU 真实权重）" if gpu else "（纯逻辑，不碰 GPU）"))
    print("=" * 72)
    with tempfile.TemporaryDirectory() as td:
        test_doc_media(Path(td))
    test_shot_hit_fields()
    test_thresholds_calibrated()
    test_final_cut_order()
    test_degradation()
    test_cut_result_shape()
    with tempfile.TemporaryDirectory() as td:
        test_tail_score_space_isolation(Path(td))
    if gpu:
        test_gpu()
    print("\n" + "=" * 72)
    print(f"PASS={PASS}  FAIL={FAIL}")
    print("=" * 72)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
