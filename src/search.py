"""08_search.py · 检索层（三路路由 + 自动降级 + adaptive_cut + RRF + L4 后处理）。

规格第七章：
  7.1 路由真值表（UI 核心契约，不许自由发挥）
      | 输入        | 精确找人 ☐                    | 精确找人 ☑
      | 文字        | text → materials_semantic     | 控件物理隐藏，不支持人脸匹配
      | 图片/视频   | visual → materials_semantic   | 抽帧检脸→点选→ArcFace → materials_faces
                                                 检不出脸则降级 semantic + 返回 warning 串
  7.2 自适应截断：k=400 → 按分数降序 → top-50 内找最大 gap 自然截断，阈值兜底。
      前端必须显示「已截断至 N 条（第 N+1 条分数 0.61）」
  7.3 多向量 OR 融合：多人点选时用 RRF，支持 -face_id 排除（差集）
  7.4 文字检索鲁棒性：query 侧轻量改写

第五章 L4 后处理（永远挂，与模型无关）：
  1) 文件级聚合
  2) 时序一致性聚类（相邻且间隔 < 3s 的命中镜头合并为一个时间段）
  3) 防长视频霸榜（排序依据 = 文件内命中镜头数 × 平均分）
  4) 检索侧 face→shot 去重（同 (file_id, shot_ix) 多张脸取最高分代表，标注「该镜头内出现 N 次」）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import db as D
from .common import Config, get_logger, make_point_id, split_point_id
from .vector_store import Hit, VectorStore, face_collection, open_store, semantic_collection

LOG = get_logger("search")

MODE_TEXT = "text"
MODE_IMAGE = "image"
MODE_VIDEO = "video"

BADGE_SEMANTIC = "sim"
BADGE_FACE = "face"


# --------------------------------------------------------------------------- #
# 7.2 自适应截断
# --------------------------------------------------------------------------- #


@dataclass
class CutResult:
    scores: List[float]
    kept: int
    cut_index: int
    next_score: Optional[float]
    truncated: bool
    reason: str

    @property
    def note(self) -> str:
        if self.truncated and self.next_score is not None:
            return f"已截断至 {self.kept} 条（第 {self.kept + 1} 条分数 {self.next_score:.2f}）"
        return f"未截断，返回 {self.kept} 条"


def adaptive_cut(
    scores: Sequence[float],
    k_hard: int = 200,
    min_score: float = 0.55,
    gap_min: float = 0.03,
) -> CutResult:
    """规格 7.2 的实现（与文档中的伪代码逐行等价，但保留被截断的位置信息）。"""
    top = list(scores[:min(k_hard, len(scores))])
    limit = min(50, len(top))
    for i in range(1, limit):
        if top[i - 1] - top[i] >= gap_min and top[i] >= min_score:
            return CutResult(top[:i], kept=i, cut_index=i,
                             next_score=top[i] if i < len(scores) else None,
                             truncated=True, reason=f"gap {top[i-1]-top[i]:.3f} >= {gap_min}")
    kept = [s for s in top if s >= min_score]
    return CutResult(kept, kept=len(kept), cut_index=-1,
                     next_score=(top[len(kept)] if len(top) > len(kept) else None),
                     truncated=len(kept) < len(scores),
                     reason=f"阈值兜底 min_score={min_score}")


# --------------------------------------------------------------------------- #
# 7.3 RRF 融合
# --------------------------------------------------------------------------- #


def rrf_fuse(
    hit_lists: Sequence[Sequence[Hit]],
    *,
    k: int = 60,
    exclude_face_ids: Sequence[int] = (),
    include_face_ids: Sequence[int] = (),
) -> List[Tuple[Hit, float, int]]:
    """倒数排名融合。返回 [(代表 Hit, rrf 分数, 命中该 shot 的目标脸数)]。

    支持 -face_id 排除（RRF 合并时做差集，规格 7.3）。
    """
    scores: Dict[Tuple[int, int], float] = {}
    reps: Dict[Tuple[int, int], Hit] = {}
    matched: Dict[Tuple[int, int], set] = {}
    excl = set(int(x) for x in exclude_face_ids)
    incl = set(int(x) for x in include_face_ids)

    for lst_ix, hits in enumerate(hit_lists):
        for rank, h in enumerate(hits, start=1):
            fid = h.payload.get("face_id")
            if fid is not None:
                fid = int(fid)
                if fid in excl:
                    continue
                if incl and fid not in incl:
                    continue
            key = (h.file_id, h.shot_ix)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            matched.setdefault(key, set())
            if fid is not None:
                matched[key].add(fid)
            prev = reps.get(key)
            if prev is None or h.score > prev.score:
                reps[key] = h

    fused = [(reps[key], scores[key], len(matched[key])) for key in scores]
    fused.sort(key=lambda t: (-t[1], -t[0].score))
    return fused


# --------------------------------------------------------------------------- #
# L4 后处理
# --------------------------------------------------------------------------- #


@dataclass
class ShotHit:
    file_id: int
    shot_ix: int
    score: float
    sec: float
    shot_start: float
    shot_end: float
    path: Optional[str] = None
    thumb_path: Optional[str] = None
    kind: Optional[str] = None
    badge: str = BADGE_SEMANTIC
    face_id: Optional[int] = None
    faces_in_shot: int = 1
    captured_at: Optional[str] = None
    # 重排序（L2d）留下的两个分数：rerank_score 是交叉编码器给的 yes/no 概率，
    # recall_score 是双塔召回的余弦。检索结果按 rerank_score 重排（score 字段会被覆盖），
    # 但两个分数都留着 —— 界面要能回答「它为什么排前面」。
    rerank_score: Optional[float] = None
    recall_score: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "file_id": self.file_id, "shot_ix": self.shot_ix, "score": round(self.score, 4),
            "sec": self.sec, "shot_start": self.shot_start, "shot_end": self.shot_end,
            "path": self.path, "thumb_path": self.thumb_path, "kind": self.kind,
            "badge": self.badge, "face_id": self.face_id,
            "faces_in_shot": self.faces_in_shot, "captured_at": self.captured_at,
            "rerank_score": self.rerank_score, "recall_score": self.recall_score,
        }


@dataclass
class FileHit:
    file_id: int
    path: Optional[str]
    kind: Optional[str]
    captured_at: Optional[str]
    shots: List[ShotHit] = field(default_factory=list)
    segments: List[Dict[str, Any]] = field(default_factory=list)
    best_score: float = 0.0
    avg_score: float = 0.0
    rank_score: float = 0.0
    n_shots: int = 0
    badge: str = BADGE_SEMANTIC

    def as_dict(self) -> Dict[str, Any]:
        return {
            "file_id": self.file_id, "path": self.path, "kind": self.kind,
            "captured_at": self.captured_at, "n_shots": self.n_shots,
            "best_score": round(self.best_score, 4), "avg_score": round(self.avg_score, 4),
            "rank_score": round(self.rank_score, 4), "badge": self.badge,
            "segments": self.segments, "shots": [s.as_dict() for s in self.shots],
        }


def temporal_cluster(shots: Sequence[ShotHit], gap_sec: float = 3.0) -> List[Dict[str, Any]]:
    """时序一致性聚类：相邻且间隔 < gap 的命中镜头合并为一个时间段。"""
    if not shots:
        return []
    ordered = sorted(shots, key=lambda s: (s.shot_start, s.shot_ix))
    segs: List[Dict[str, Any]] = []
    cur = {"start": ordered[0].shot_start, "end": ordered[0].shot_end,
           "shots": [ordered[0].shot_ix], "best_score": ordered[0].score, "n": 1}
    for s in ordered[1:]:
        if s.shot_start - cur["end"] < gap_sec:
            cur["end"] = max(cur["end"], s.shot_end)
            cur["shots"].append(s.shot_ix)
            cur["best_score"] = max(cur["best_score"], s.score)
            cur["n"] += 1
        else:
            segs.append(cur)
            cur = {"start": s.shot_start, "end": s.shot_end, "shots": [s.shot_ix],
                   "best_score": s.score, "n": 1}
    segs.append(cur)
    for seg in segs:
        seg["start"] = round(float(seg["start"]), 3)
        seg["end"] = round(float(seg["end"]), 3)
        seg["best_score"] = round(float(seg["best_score"]), 4)
    return segs


def aggregate_by_file(
    hits: Sequence[ShotHit],
    *,
    gap_sec: float = 3.0,
    anti_dominate: bool = True,
) -> List[FileHit]:
    """L4 第 1/2/3 步：文件级聚合 → 时序聚类 → 防长视频霸榜排序。"""
    by_file: Dict[int, List[ShotHit]] = {}
    for h in hits:
        by_file.setdefault(h.file_id, []).append(h)

    out: List[FileHit] = []
    for fid, lst in by_file.items():
        # L4-4：同 (file_id, shot_ix) 去重取最高分，记录「该镜头内出现 N 次」
        best_per_shot: Dict[int, ShotHit] = {}
        count_per_shot: Dict[int, int] = {}
        for h in lst:
            count_per_shot[h.shot_ix] = count_per_shot.get(h.shot_ix, 0) + 1
            cur = best_per_shot.get(h.shot_ix)
            if cur is None or h.score > cur.score:
                best_per_shot[h.shot_ix] = h
        shots = []
        for shot_ix, h in best_per_shot.items():
            h.faces_in_shot = count_per_shot.get(shot_ix, 1)
            shots.append(h)
        shots.sort(key=lambda s: (-s.score, s.shot_start))

        sample = shots[0]
        scores = [s.score for s in shots]
        avg = float(np.mean(scores)) if scores else 0.0
        fh = FileHit(
            file_id=fid,
            path=sample.path,
            kind=sample.kind,
            captured_at=sample.captured_at,
            shots=shots,
            segments=temporal_cluster(shots, gap_sec=gap_sec),
            best_score=max(scores) if scores else 0.0,
            avg_score=avg,
            n_shots=len(shots),
            badge=sample.badge,
        )
        # L4-3：排序依据 = 文件内命中镜头数 × 平均分（防长视频霸榜）
        fh.rank_score = (fh.n_shots * fh.avg_score) if anti_dominate else fh.best_score
        out.append(fh)

    # L4-3 排序：主键 rank_score 降序，次键拍摄时间新→旧。
    # 用两次稳定排序实现：先按次键排，再按主键排（Python 的 sort 是稳定的）。
    # 注意不能用 -(captured_at) —— 那是字符串，一元负号会 TypeError。
    out.sort(key=lambda f: (f.captured_at or "", f.file_id), reverse=True)
    out.sort(key=lambda f: f.rank_score, reverse=True)
    return out


# --------------------------------------------------------------------------- #
# 结果富化（path / captured_at 从 sqlite 取，绝不用 Qdrant 刷详情）
# --------------------------------------------------------------------------- #


def enrich(conn, cfg: Config, shot_hits: Sequence[ShotHit]) -> None:
    """用 sqlite 补齐 path / captured_at；payload 里缺失的时间段从 shots 表回捞。"""
    ids = sorted({h.file_id for h in shot_hits})
    info = D.files_by_ids(conn, ids)
    need_shot: Dict[int, set] = {}
    for h in shot_hits:
        if h.shot_start == 0.0 and h.shot_end == 0.0 and h.kind != "photo":
            need_shot.setdefault(h.file_id, set()).add(h.shot_ix)

    shot_info: Dict[Tuple[int, int], Any] = {}
    for fid, ixs in need_shot.items():
        q = ",".join("?" * len(ixs))
        for r in conn.execute(
            f"SELECT shot_ix, shot_start, shot_end FROM shots WHERE file_id=? AND shot_ix IN ({q})",
            (fid, *sorted(ixs)),
        ):
            shot_info[(fid, int(r["shot_ix"]))] = r

    for h in shot_hits:
        row = info.get(h.file_id)
        if row is not None:
            h.path = str(row["path"])
            h.kind = row["kind"]
            h.captured_at = row["captured_at"]
        sr = shot_info.get((h.file_id, h.shot_ix))
        if sr is not None:
            h.shot_start = float(sr["shot_start"] or 0.0)
            h.shot_end = float(sr["shot_end"] or 0.0)
            if h.sec == 0.0:
                h.sec = (h.shot_start + h.shot_end) / 2.0


def hits_to_shot_hits(
    fused: Sequence[Tuple[Hit, float, int]],
    *,
    badge: str,
) -> List[ShotHit]:
    out: List[ShotHit] = []
    for h, _rrf, n_faces in fused:
        p = h.payload
        out.append(ShotHit(
            file_id=h.file_id,
            shot_ix=h.shot_ix,
            score=float(h.score),
            sec=float(p.get("sec") or 0.0),
            shot_start=float(p.get("shot_start") or 0.0),
            shot_end=float(p.get("shot_end") or 0.0),
            thumb_path=p.get("thumb_path"),
            kind=p.get("kind"),
            badge=badge,
            face_id=int(p["face_id"]) if p.get("face_id") is not None else None,
            faces_in_shot=max(1, int(n_faces)),
        ))
    return out


# --------------------------------------------------------------------------- #
# 检索主入口（路由真值表）
# --------------------------------------------------------------------------- #


@dataclass
class SearchOutcome:
    mode: str                       # semantic | face
    badge: str
    warning: str
    files: List[FileHit]
    cut_note: str
    degraded: bool = False
    effective_query: str = ""
    total_shots: int = 0
    total_files: int = 0
    reranked: bool = False
    rerank_note: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode, "badge": self.badge, "warning": self.warning,
            "cut_note": self.cut_note, "degraded": self.degraded,
            "effective_query": self.effective_query,
            "total_shots": self.total_shots, "total_files": self.total_files,
            "reranked": self.reranked, "rerank_note": self.rerank_note,
            "files": [f.as_dict() for f in self.files],
        }


def _apply_rerank(
    conn,
    cfg: Config,
    shots: List[ShotHit],
    *,
    query_media: Dict[str, Any],
    topk: Optional[int] = None,
) -> Tuple[List[ShotHit], bool, str]:
    """语义路上的 L2d 重排序（可选、可降级、绝不抛错）。

    只在语义路调用：人脸路的分数是 ArcFace 余弦，规格 7.4 要求两种分数绝不混排。
    """
    if not bool(cfg.get("retrieval.rerank_enabled", True)):
        return shots, False, "重排序已关闭（retrieval.rerank_enabled: false）"
    if not query_media:
        return shots, False, "查询侧没有可用的媒体，跳过重排序"
    # 延迟导入：src.rerank 会拉起 encode_model（离线环境变量等），
    # 检索层被 UI / 测试大量 import，不该在模块加载时就付这个代价。
    from .rerank import rerank_shot_hits

    new_shots, report = rerank_shot_hits(conn, cfg, shots, query=query_media, topk=topk)
    return new_shots, bool(report.get("applied")), str(report.get("reason") or "")


def _final_cut(cfg: Config, shots: List[ShotHit], *, reranked: bool) -> Tuple[List[ShotHit], CutResult]:
    """最终自适应截断。**必须用在重排之后的分数上**，并按分数空间选阈值。

    两个分数空间的刻度完全不同，共用一个阈值就是错的（2026-10-16 实测标定）：
      * 双塔召回余弦：Qwen3-VL-Embedding 实测整体挤在 0.31~0.50，
        连故意编的无关查询词都能拿到 0.405（真查询 0.497）→ 几乎不能区分相关与否；
        用 0.55 当阈值会**把结果全砍空**（这是原实现「搜什么都空」的根因）。
      * 交叉编码重排分：sigmoid(yes-no) 实测 0.14~0.51，但**能区分**相关与无关
        （自检：相关 0.547 / 无关 0.195）。
    所以：重排生效时用 retrieval.rerank_min_score，否则退回 retrieval.min_score。
    """
    if not shots:
        return shots, CutResult([], 0, -1, None, False, "没有候选")
    min_score = float(cfg.get("retrieval.rerank_min_score", 0.25) if reranked
                      else cfg.get("retrieval.min_score", 0.30))
    cut = adaptive_cut(
        [h.score for h in shots],
        k_hard=int(cfg.get("retrieval.k_hard", 200)),
        min_score=min_score,
        gap_min=float(cfg.get("retrieval.gap_min", 0.03)),
    )
    kept = shots[:cut.kept]
    if reranked:
        # 规格 8.2：「重排后保留 Top-50」。这必须是**硬上限**，
        # 不能只靠阈值：这个模型的分数区间很窄，阈值兜底那条规则
        # （keep >= min_score）几乎不会截断，实测会原样返回 100 条。
        topk = max(1, int(cfg.get("retrieval.rerank_topk", 50)))
        if len(kept) > topk:
            kept = kept[:topk]
            cut = CutResult(kept, kept=len(kept), cut_index=topk,
                            next_score=shots[topk].score if len(shots) > topk else None,
                            truncated=True, reason=f"重排后取 Top-{topk}")
    return kept, cut


def semantic_recall(
    conn,
    cfg: Config,
    store: VectorStore,
    *,
    query_vec: Sequence[float],
    recall_k: Optional[int] = None,
) -> List[ShotHit]:
    """L2c 语义召回（**不截断**）：只做 KNN + 铁律 3 的 done 过滤 + 转 ShotHit。

    为什么要把「召回」与「截断」拆开（2026-10-16 实测的标定结论）：
      Qwen3-VL-Embedding-2B 的文字→图片余弦整体挤在 0.31~0.50，连一个**故意编的
      无关查询词**都能拿到 0.405 的最高分（真查询 0.497）—— 也就是说
      **绝对余弦在这个模型上几乎不能区分相关与无关**，用阈值去截断必然二选一：
      要么砍空（min_score=0.55 高于模型上限），要么把两百条倒出来。
      真正能区分的是重排序（交叉编码，自检里相关 0.547 / 无关 0.195）。
      所以顺序必须是「**先召回 → 再重排 → 最后按重排分数截断**」，
      这也正是规格第五章「k=400 → Top-50」的原意。
    """
    coll = semantic_collection(cfg)
    k = int(recall_k or cfg.get("semantic.recall_k", 400))
    raw = store.search_knn(coll, query_vec, k)
    # 只保留 done 的文件（铁律 3）
    done_ids = {int(r["file_id"]) for r in conn.execute(
        "SELECT file_id FROM files WHERE status='done'")}
    hits = [h for h in raw if h.file_id in done_ids]
    return hits_to_shot_hits([(h, h.score, 1) for h in hits], badge=BADGE_SEMANTIC)


def semantic_search(
    conn,
    cfg: Config,
    store: VectorStore,
    *,
    query_vec: Sequence[float],
    recall_k: Optional[int] = None,
) -> Tuple[List[ShotHit], CutResult]:
    """召回 + 自适应截断（保留原签名，供只想要「召回+阈值」的调用方/测试使用）。

    检索主路径（_search_impl）走的是 semantic_recall + 重排 + 再截断，
    因为截断必须发生在重排**之后**（见 semantic_recall 的注释）。
    """
    shots = semantic_recall(conn, cfg, store, query_vec=query_vec, recall_k=recall_k)
    scores = [h.score for h in shots]
    cut = adaptive_cut(
        scores,
        k_hard=int(cfg.get("retrieval.k_hard", 200)),
        min_score=float(cfg.get("retrieval.min_score", 0.30)),
        gap_min=float(cfg.get("retrieval.gap_min", 0.03)),
    )
    return shots[:cut.kept], cut


def face_search_single(
    conn,
    cfg: Config,
    store: VectorStore,
    *,
    face_vecs: Sequence[Sequence[float]],
    recall_k: Optional[int] = None,
    exclude_face_ids: Sequence[int] = (),
    include_face_ids: Sequence[int] = (),
) -> Tuple[List[ShotHit], CutResult]:
    coll = face_collection(cfg)
    k = int(recall_k or cfg.get("retrieval.k_hard", 200))
    hit_lists = [store.search_knn(coll, v, k) for v in face_vecs]
    fused = rrf_fuse(hit_lists, k=int(cfg.get("retrieval.rrf_k", 60)),
                     exclude_face_ids=exclude_face_ids, include_face_ids=include_face_ids)
    scores = [h.score for h, _r, _n in fused]
    cut = adaptive_cut(
        scores,
        k_hard=int(cfg.get("retrieval.k_hard", 200)),
        min_score=float(cfg.get("retrieval.min_score", 0.55)),
        gap_min=float(cfg.get("retrieval.gap_min", 0.03)),
    )
    kept = fused[:cut.kept] if cut.kept else fused
    return hits_to_shot_hits(kept, badge=BADGE_FACE), cut


def search(
    conn,
    cfg: Config,
    store: VectorStore,
    **kwargs: Any,
) -> SearchOutcome:
    """检索门面：执行 + 记录命中时间戳（frames LRU 清理依据）。"""
    outcome = _search_impl(conn, cfg, store, **kwargs)
    record_hits(cfg, [s for f in outcome.files for s in f.shots])
    return outcome


def _search_impl(
    conn,
    cfg: Config,
    store: VectorStore,
    *,
    query_mode: str,
    payload: Dict[str, Any],
    exact_person: bool = False,
    thresh: Optional[float] = None,
    k: Optional[int] = None,
) -> SearchOutcome:
    """三路路由 + 自动降级（签名与规格第十三章一致）。

    payload：
      text 模式  -> {"text": "小孩在草地上跑"}
      image 模式 -> {"image": Path}
      video 模式 -> {"video": Path}
    精确找人：
      仅图片/视频输入可用。抽帧检脸 →（若无点选则取最大脸）→ ArcFace → materials_faces；
      检不出脸 → 降级 semantic 并返回 warning 串（绝不报错卡死、绝不静默切换）。
    """
    from .encode_model import encode_query

    query_mode = (query_mode or MODE_TEXT).lower()
    warning = ""

    # --- 文字输入：精确找人开关必须物理隐藏（这里再兜一层：忽略该开关） ----- #
    if query_mode == MODE_TEXT:
        if exact_person:
            warning = "文字检索不支持人脸匹配，已按语义检索执行。"
        text = str(payload.get("text") or "")
        if not text.strip():
            return SearchOutcome("semantic", BADGE_SEMANTIC, "请输入检索文字", [], "无输入")
        from .query_rewrite import rewrite_query

        effective = rewrite_query(text, cfg) if cfg.get("retrieval.query_rewrite_enabled", True) else text
        vec, _ins = encode_query(cfg, text=text)
        # L2c 召回（不截断）→ L2d 重排 → 最后才截断（见 semantic_recall 的注释）
        shots = semantic_recall(conn, cfg, store, query_vec=vec, recall_k=k)
        enrich(conn, cfg, shots)
        # 文字查询的「查询侧媒体」就是那段文字（交叉编码器同时吃文本与图像）
        shots, reranked, rr_note = _apply_rerank(
            conn, cfg, shots, query_media={"text": text}, topk=payload.get("rerank_topk"))
        shots, cut = _final_cut(cfg, shots, reranked=reranked)
        files = aggregate_by_file(
            shots, gap_sec=float(cfg.get("retrieval.temporal_merge_gap_sec", 3.0)),
            anti_dominate=True)
        return SearchOutcome("semantic", BADGE_SEMANTIC, warning, files, cut.note,
                             effective_query=effective, total_shots=len(shots),
                             total_files=len(files), reranked=reranked, rerank_note=rr_note)

    # --- 图片 / 视频输入 -------------------------------------------------- #
    src = payload.get("image") or payload.get("video")
    if src is None:
        return SearchOutcome("semantic", BADGE_SEMANTIC, "缺少查询素材", [], "无输入")

    if exact_person:
        from .face_search import detect_query_faces

        faces = detect_query_faces(cfg, Path(src), query_mode=query_mode)
        if not faces:
            warning = ("未在查询素材中检出人脸，已自动降级为画面语义检索。"
                       "建议：换一张人脸更清晰/更近的照片，或关闭「精确找人」。")
            LOG.warning("精确找人未检出人脸，降级语义检索：%s", src)
        else:
            picked = payload.get("picked_face_ids") or []
            chosen = [f for f in faces if f.index in picked] if picked else [faces[0]]
            if not chosen:
                chosen = [faces[0]]
            face_vecs = [f.embedding for f in chosen]
            shots, cut = face_search_single(
                conn, cfg, store, face_vecs=face_vecs, recall_k=k,
                exclude_face_ids=payload.get("exclude_face_ids") or [],
                include_face_ids=payload.get("include_face_ids") or [],
            )
            enrich(conn, cfg, shots)
            files = aggregate_by_file(
                shots, gap_sec=float(cfg.get("retrieval.temporal_merge_gap_sec", 3.0)),
                anti_dominate=False)
            n = len(chosen)
            if len(face_vecs) > 1:
                warning = f"命中原因：匹配到 {n} 张目标脸中的多张（RRF 融合）"
            return SearchOutcome("face", BADGE_FACE, warning, files, cut.note,
                                 total_shots=len(shots), total_files=len(files))

    # 语义（含「精确找人未检出人脸」的降级路径）
    # 视频查询必须先抽帧：直接把 .mp4 交给图像编码器会报
    # "cannot identify image file ... .mp4"（实测）。
    if query_mode == MODE_VIDEO:
        vec, _ins = encode_query(cfg, video=Path(src))
        # 重排序的查询侧**不能**直接用 .mp4：vendored 的 reranker 会走
        # return_video_metadata 那条与本机 qwen-vl-utils 不兼容的路径，
        # 并且会静默退化成 NULL 对把顺序打乱（实测自身相似度 1.0 → 0.459）。
        # 这里改成抽一帧当图像查询。
        from .rerank import build_query_media

        q_media = build_query_media(cfg, video=str(src))
    else:
        vec, _ins = encode_query(cfg, image=Path(src))
        q_media = {"image": str(src)}
    shots = semantic_recall(conn, cfg, store, query_vec=vec, recall_k=k)
    enrich(conn, cfg, shots)
    shots, reranked, rr_note = _apply_rerank(
        conn, cfg, shots, query_media=q_media, topk=payload.get("rerank_topk"))
    shots, cut = _final_cut(cfg, shots, reranked=reranked)
    files = aggregate_by_file(
        shots, gap_sec=float(cfg.get("retrieval.temporal_merge_gap_sec", 3.0)),
        anti_dominate=True)
    return SearchOutcome("semantic", BADGE_SEMANTIC, warning, files, cut.note,
                         degraded=bool(warning), total_shots=len(shots),
                         total_files=len(files), reranked=reranked, rerank_note=rr_note)


def search_by_face_id(
    conn,
    cfg: Config,
    store: VectorStore,
    face_id: int,
    *,
    k: Optional[int] = None,
) -> SearchOutcome:
    """[找同款] 门面：执行 + 记录命中时间戳。"""
    outcome = _search_by_face_id_impl(conn, cfg, store, face_id, k=k)
    record_hits(cfg, [s for f in outcome.files for s in f.shots])
    return outcome


def _search_by_face_id_impl(
    conn,
    cfg: Config,
    store: VectorStore,
    face_id: int,
    *,
    k: Optional[int] = None,
) -> SearchOutcome:
    """[找同款]：用库里已有的人脸向量做召回，排除它自己（规格 8.4 互跳）。"""
    row = conn.execute("SELECT * FROM face_records WHERE face_id=?", (face_id,)).fetchone()
    if row is None:
        return SearchOutcome("face", BADGE_FACE, f"face_id={face_id} 不存在", [], "")
    coll = face_collection(cfg)
    vec = _fetch_face_vector(store, coll, int(face_id))
    if vec is None:
        return SearchOutcome("face", BADGE_FACE,
                             f"face_id={face_id} 的向量不在 Qdrant 中（可能未完成 F3 入库）", [], "")
    raw = store.search_knn(coll, vec, int(k or cfg.get("retrieval.k_hard", 200)) + 1)
    raw = [h for h in raw if int(h.payload.get("face_id") or -1) != int(face_id)]
    done_ids = {int(r["file_id"]) for r in conn.execute(
        "SELECT file_id FROM files WHERE status='done'")}
    raw = [h for h in raw if h.file_id in done_ids]
    scores = [h.score for h in raw]
    cut = adaptive_cut(scores, k_hard=int(cfg.get("retrieval.k_hard", 200)),
                       min_score=float(cfg.get("retrieval.min_score", 0.55)),
                       gap_min=float(cfg.get("retrieval.gap_min", 0.03)))
    kept = raw[:cut.kept] if cut.kept else raw
    shots = hits_to_shot_hits([(h, h.score, 1) for h in kept], badge=BADGE_FACE)
    enrich(conn, cfg, shots)
    files = aggregate_by_file(shots, gap_sec=float(cfg.get("retrieval.temporal_merge_gap_sec", 3.0)),
                              anti_dominate=False)
    return SearchOutcome("face", BADGE_FACE, "", files, cut.note,
                         total_shots=len(shots), total_files=len(files))


def _fetch_face_vector(store: VectorStore, collection: str, face_id: int) -> Optional[List[float]]:
    """按 point_id 取回向量（人脸 collection 的 point_id 就是 face_id）。"""
    try:
        client = getattr(store, "client", None)
        if client is None:
            return None
        recs = client.retrieve(collection_name=collection, ids=[int(face_id)], with_vectors=True)
        if not recs:
            return None
        vec = recs[0].vector
        if isinstance(vec, dict):
            vec = next(iter(vec.values()))
        return [float(x) for x in vec]
    except Exception as exc:  # noqa: BLE001
        LOG.warning("取回 face_id=%s 向量失败：%s", face_id, exc)
        return None


# --------------------------------------------------------------------------- #
# 命中记录（frames LRU 清理的依据）
# --------------------------------------------------------------------------- #


def record_hits(cfg: Config, shot_hits: Sequence[ShotHit]) -> int:
    """把本次命中写回 files.last_hit_at / hit_count。

    必须在**独立的写连接**上做（检索用的是只读连接，不能污染 WAL snapshot）。
    失败只告警，绝不因为统计写不进去而让检索失败。
    """
    ids = sorted({h.file_id for h in shot_hits})
    if not ids:
        return 0
    try:
        conn = D.connect(cfg.db_path, cfg=cfg)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("无法写入命中记录（不影响检索结果）：%s", exc)
        return 0
    try:
        n = D.touch_hits(conn, ids)
        conn.commit()
        return n
    except Exception as exc:  # noqa: BLE001
        LOG.warning("写入命中记录失败（不影响检索结果）：%s", exc)
        return 0
    finally:
        conn.close()
