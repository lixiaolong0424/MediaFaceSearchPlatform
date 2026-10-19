"""L2d 重排序：Qwen3-VL-Reranker-2B（规格第五章 / 第十三章 05_rerank.py）。

在流水线里的位置
----------------
    L2c 语义召回（materials_semantic, recall_k=400）
      → **L2d 重排序（本模块，rerank_input_k → rerank_topk）**
      → L4 聚合（时序聚类 / shot 去重 / 防霸榜）

为什么需要它：召回是**双塔**（查询与素材各自独立编码成 2048 维再算余弦），
快但粗；重排序是**交叉编码**（查询与素材拼成一条序列，模型直接判 yes/no），
准但贵。规格把它放在召回之后，是标准的两阶段排序。

模型加载（与语义编码同一套纪律）
--------------------------------
* 显式路径加载权重目录自带的 `qwen3_vl_reranker.py`（`Qwen3VLReranker`），
  绝不依赖 `AutoModel` 联网查找、绝不触发下载；
* `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1` / `local_files_only=True`；
* 加载后做**自检**：明显相关的一对分数必须高于明显无关的一对，
  否则说明权重没真正加载（对照 src/encode_model.py 的同款自检）。

⚠️ 实测性能与由此产生的默认值（2026-10-16，RTX 3090）
-----------------------------------------------------
权重是 2B，且 vendored 的 `Qwen3VLReranker.process()` 是**逐条**打分
（`for pair in pairs: tokenize([pair]); compute_scores(...)`）—— 400 个候选
就是 400 次单条前向，实测要几分钟/查询，交互上不可接受。

所以本模块：
  * 用 `tokenize(pairs_chunk)` + `compute_scores` 自己实现**批量**打分
    （vendored 的 tokenize 本来就支持 list 并会自动 padding）；
  * 默认只重排 `retrieval.rerank_input_k`（默认 60）条召回结果，而不是规格写的 400。
    想按规格跑满 400 就把它设成 400（`05_rerank.py --k 400` 也行，会打印耗时）。
  * 对 (查询, 文档) 做进程内 LRU 缓存：重复检索同一批素材时不再重复前向。

只在**语义路**上重排。人脸路（materials_faces）的分数是 ArcFace 余弦，
规格 7.4 明确要求「两种模式的分数绝不混排」，所以不在这里动它。
"""

from __future__ import annotations

import hashlib
import importlib.util
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .common import Config, get_logger
from .encode_model import apply_offline_env

LOG = get_logger("rerank")

# 默认指令取自权重目录的默认值（qwen3_vl_reranker.py::default_instruction）
DEFAULT_INSTRUCTION = "Given a search query, retrieve relevant candidates that answer the query."


@dataclass
class RerankStatus:
    loaded: bool = False
    device: str = ""
    backend: str = ""
    error: str = ""


class Reranker:
    """Qwen3-VL-Reranker-2B 包装：显式路径 + 批量打分 + 成绩缓存。"""

    def __init__(self, cfg: Config, *, device: Optional[str] = None) -> None:
        self.cfg = cfg
        self.model_path = Path(str(cfg.get(
            "semantic.rerank_model_path",
            str(Path(str(cfg.get("semantic.model_path", ""))).parent / "Qwen3-VL-Reranker-2B"))))
        self.dtype_name = str(cfg.get("semantic.rerank_dtype", cfg.get("semantic.dtype", "bfloat16")))
        self.device = device
        self.batch_size = int(cfg.get("semantic.rerank_batch_size", 8))
        self.instruction = str(cfg.get("semantic.rerank_instruct", DEFAULT_INSTRUCTION))
        self._impl: Any = None
        self._torch: Any = None
        self.status = RerankStatus()
        self._cache: "OrderedDict[str, float]" = OrderedDict()
        self.cache_hits = 0
        self.cache_misses = 0

    # -- 加载 -------------------------------------------------------------- #
    def load(self) -> RerankStatus:
        if self.status.loaded:
            return self.status
        try:
            import torch
        except ImportError:
            self.status.error = "缺少 torch"
            return self.status
        self._torch = torch

        apply_offline_env()

        if not self.model_path.exists():
            self.status.error = (
                f"缺少重排序权重目录：{self.model_path}\n"
                "修复：把 Qwen3-VL-Reranker-2B 整个目录放到 weight/ 下，"
                "或改 config 的 semantic.rerank_model_path。"
            )
            return self.status
        script = self.model_path / "qwen3_vl_reranker.py"
        if not script.exists():
            self.status.error = f"权重目录缺少 {script.name}（官方实现随权重一起发布）"
            return self.status

        self.device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
        dtype = {
            "bfloat16": torch.bfloat16, "bf16": torch.bfloat16,
            "float16": torch.float16, "fp16": torch.float16,
            "float32": torch.float32, "fp32": torch.float32,
        }.get(self.dtype_name, torch.bfloat16)
        if self.device == "cpu" and dtype is torch.bfloat16:
            dtype = torch.float32  # CPU 上 bfloat16 极慢

        try:
            spec = importlib.util.spec_from_file_location("qwen3_vl_reranker_local", script)
            if spec is None or spec.loader is None:
                raise ImportError("无法加载 qwen3_vl_reranker.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            kwargs: Dict[str, Any] = {}
            max_px = self.cfg.get("semantic.rerank_max_pixels")
            min_px = self.cfg.get("semantic.rerank_min_pixels")
            if max_px:
                kwargs["max_pixels"] = int(max_px)
            if min_px:
                kwargs["min_pixels"] = int(min_px)
            # 防呆：像素预算是「像素总数」而不是边长。权重目录里
            # MAX_PIXELS = 1280*32*32 = 1,310,720、MIN_PIXELS = 4*32*32 = 4096。
            # 把边长（如 1024）误当成像素数传进来会让 max < min，
            # 处理器直接报错或产出退化结果 —— 早期实测踩到过，这里显式拦住。
            eff_min = int(kwargs.get("min_pixels", 4 * 32 * 32))
            eff_max = int(kwargs.get("max_pixels", 1280 * 32 * 32))
            if eff_max < eff_min:
                self.status.error = (
                    f"semantic.rerank_max_pixels={eff_max} 小于 min_pixels={eff_min}。"
                    "像素预算是**像素总数**（如 1280*32*32=1310720），不是边长；"
                    "想按边长调就写 边长*32*32。"
                )
                LOG.error("%s", self.status.error)
                return self.status
            self._impl = module.Qwen3VLReranker(
                model_name_or_path=str(self.model_path),
                dtype=dtype,
                attn_implementation="sdpa",
                local_files_only=True,
                **kwargs,
            )
            self.status.backend = "qwen_vl_reranker"
            self.status.device = str(self.device)
        except Exception as exc:  # noqa: BLE001
            self.status.error = f"加载重排序模型失败：{exc}"
            LOG.exception("加载重排序模型失败")
            return self.status

        ok, msg = self.sanity_check()
        if not ok:
            self.status.error = f"重排序模型自检未通过：{msg}"
            LOG.error("%s", self.status.error)
            return self.status

        self.status.loaded = True
        LOG.info("重排序模型加载成功（backend=%s, device=%s, dtype=%s, batch=%d）",
                 self.status.backend, self.device, self.dtype_name, self.batch_size)
        return self.status

    # -- 自检 -------------------------------------------------------------- #
    def sanity_check(self) -> Tuple[bool, str]:
        """明显相关的一对必须高于明显无关的一对，否则权重八成没加载上。"""
        try:
            scores = self._score_pairs_batch([(
                {"text": "一只猫"},
                {"text": "一只趴在窗台上的橘猫"},
            ), (
                {"text": "一只猫"},
                {"text": "波音747客机起飞时的发动机尾焰"},
            )])
        except Exception as exc:  # noqa: BLE001
            return False, f"前向失败：{exc}"
        if len(scores) != 2:
            return False, f"自检返回 {len(scores)} 个分数"
        good, bad = float(scores[0]), float(scores[1])
        LOG.info("重排序自检：相关=%.4f 无关=%.4f", good, bad)
        if not (0.0 <= good <= 1.0 and 0.0 <= bad <= 1.0):
            return False, f"分数不在 [0,1]：{good}, {bad}"
        if good <= bad:
            return False, f"相关对({good:.4f}) 未高于 无关对({bad:.4f})"
        return True, ""

    # -- 打分 -------------------------------------------------------------- #
    def _key(self, query: Dict[str, Any], doc: Dict[str, Any]) -> str:
        h = hashlib.sha1()
        for side in (query, doc):
            for k in ("text", "image", "video"):
                v = side.get(k)
                if v is None:
                    continue
                h.update(f"{k}=".encode())
                h.update(str(v).encode("utf-8", "replace"))
                h.update(b"\x00")
        return h.hexdigest()

    def _score_pairs_batch(self, pairs: Sequence[Tuple[Dict[str, Any], Dict[str, Any]]],
                           *, use_cache: bool = True) -> List[float]:
        """批量打分。pairs 是 [(query_media, doc_media), ...]，返回等长分数列表。"""
        torch = self._torch
        impl = self._impl
        if impl is None:
            raise RuntimeError("重排序模型未加载")

        scores: List[Optional[float]] = [None] * len(pairs)
        todo: List[int] = []
        for i, (q, d) in enumerate(pairs):
            if use_cache:
                hit = self._cache.get(self._key(q, d))
                if hit is not None:
                    self._cache.move_to_end(self._key(q, d))
                    scores[i] = float(hit)
                    self.cache_hits += 1
                    continue
            self.cache_misses += 1
            todo.append(i)

        # 分数缓存上限：一次会话几百条足够，超出按 LRU 淘汰
        max_cache = int(self.cfg.get("semantic.rerank_cache_size", 4096))

        for start in range(0, len(todo), self.batch_size):
            idxs = todo[start:start + self.batch_size]
            built = [impl.format_mm_instruction(
                pairs[i][0].get("text"), pairs[i][0].get("image"), pairs[i][0].get("video"),
                pairs[i][1].get("text"), pairs[i][1].get("image"), pairs[i][1].get("video"),
                instruction=self.instruction,
            ) for i in idxs]
            # vendored tokenize 支持 list，会自行 padding（且做了 max_length 截断保护）
            inputs = impl.tokenize(built)
            inputs = inputs.to(impl.model.device)
            with torch.inference_mode():
                got = impl.compute_scores(inputs)
            for i, sc in zip(idxs, got):
                scores[i] = float(sc)
                if use_cache:
                    self._cache[self._key(pairs[i][0], pairs[i][1])] = float(sc)
            while len(self._cache) > max_cache:
                self._cache.popitem(last=False)

        return [float(s) if s is not None else 0.0 for s in scores]

    def score(self, query: Dict[str, Any], docs: Sequence[Dict[str, Any]],
              *, use_cache: bool = True) -> List[float]:
        """给一批候选文档打分。query/docs 形如 {"text":..} / {"image":路径} / {"video":路径}。"""
        return self._score_pairs_batch([(query, d) for d in docs], use_cache=use_cache)

    def status_dict(self) -> Dict[str, Any]:
        return {
            "loaded": self.status.loaded,
            "device": self.status.device,
            "backend": self.status.backend,
            "model_path": str(self.model_path),
            "error": self.status.error,
            "cache_size": len(self._cache),
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
        }


# --------------------------------------------------------------------------- #
# 与检索层的接线
# --------------------------------------------------------------------------- #

_SINGLETON: Optional[Reranker] = None


def get_reranker(cfg: Config, *, reload: bool = False) -> Reranker:
    """进程内单例：加载一次 2B 权重很贵，检索/UI 必须复用。"""
    global _SINGLETON
    if _SINGLETON is None or reload:
        r = Reranker(cfg)
        r.load()
        _SINGLETON = r
    return _SINGLETON


def rerank_enabled(cfg: Config) -> bool:
    return bool(cfg.get("retrieval.rerank_enabled", True))


def build_query_media(cfg: Config, *, text: Optional[str] = None,
                      image: Optional[str] = None,
                      video: Optional[str] = None) -> Dict[str, Any]:
    """构造重排序的「查询侧媒体」。

    视频**必须降级成一张关键帧图**，不能直接把 .mp4 交给重排序模型。
    实测（2026-10-16）：权重目录自带的 `qwen3_vl_reranker.py::tokenize` 会调用
    `process_vision_info(..., return_video_metadata=True)`，而本机安装的
    qwen-vl-utils 版本不认这个参数，报
        open() got an unexpected keyword argument 'metadata_errors'
    更麻烦的是它**把异常吞掉了**：内部 catch 之后退化成一条 "NULL" 文本对，
    于是所有候选都会拿到无意义的分数，把召回顺序彻底打乱（实测以视频为查询时
    自身相似度从 ~1.0 掉到 0.459）。
    所以这里先在查询素材里抽一帧（走的是和查询编码同一条 ffmpeg 路径），
    用图像对去重排 —— 语义上足够（判的是「画面是否相关」），而且完全绕开那个坑。
    """
    if text:
        return {"text": text}
    if image:
        return {"image": str(image)}
    if video:
        try:
            from .extract import extract_frame, locate_tools

            tools = locate_tools(cfg)
            if not tools.ok:
                return {}
            out_dir = cfg.path("paths.query_cache") / "rerank_query"
            out_dir.mkdir(parents=True, exist_ok=True)
            frame = out_dir / (Path(str(video)).stem + "_rerank.jpg")
            # 取第 1 秒：避开开头的黑场/台标，也比首帧更能代表内容
            ok, _err = extract_frame(tools.ffmpeg, Path(str(video)), 1.0, frame, 384)
            if ok and frame.exists():
                return {"image": str(frame)}
            LOG.warning("视频查询抽帧失败，重排序将跳过：%s", video)
            return {}
        except Exception as exc:  # noqa: BLE001
            LOG.warning("视频查询构造重排媒体失败：%s", exc)
            return {}
    return {}


def doc_media_for_shot(hit: Any, cfg: Optional[Config] = None) -> Dict[str, Any]:
    """把一条镜头命中变成「文档侧媒体」。

    优先用**关键帧**（语义 payload 的 thumb_path，照片侧就是 03_extract 写的
    768px NVMe 缓存图）：重排序要看的是「这个镜头长什么样」，
    而关键帧正是当初编码进向量的那一帧，重排与召回看到的是同一份像素。
    没有关键帧（老数据 / 关掉缓存）才退回原图。

    ⚠️ 两者都不存在时返回 **{}**（调用方跳过该条、保留它的召回顺序），
    **不回退到文件名文本**：让交叉编码器去判「查询 vs IMG_1234.jpg」纯属噪声，
    还可能因为文件名里恰好有相同字样而把一条坏记录顶到前面。
    """
    thumb = getattr(hit, "thumb_path", None)
    path = getattr(hit, "path", None)
    for cand in (thumb, path):
        if not cand:
            continue
        try:
            if Path(str(cand)).exists():
                return {"image": str(cand)}
        except OSError:
            continue
    return {}


def rerank_shot_hits(
    conn,
    cfg: Config,
    hits: List[Any],
    *,
    query: Dict[str, Any],
    topk: Optional[int] = None,
    input_k: Optional[int] = None,
) -> Tuple[List[Any], Dict[str, Any]]:
    """对召回结果重排序，返回 (新顺序的 hits, 报告)。

    设计取舍（重要）：
      * 只重排前 `input_k` 条 —— 后面的候选本来就不会进结果，
        重排它们纯属浪费 GPU（默认 60，规格写的是 400，见模块 docstring）；
      * **被截断在 input_k 之外的部分按原顺序接在后面**，绝不丢弃：
        重排序只负责「把更相关的提前」，不该改变召回集合的大小；
      * 任何失败（权重缺失 / OOM / 无可用文档）都退化为**原顺序**并给出可读原因，
        绝不因为重排序不可用就让检索失败。
    """
    report: Dict[str, Any] = {"applied": False, "reason": "", "input_k": 0,
                              "topk": int(topk or cfg.get("retrieval.rerank_topk", 50)),
                              "elapsed_sec": 0.0, "n_docs": 0}
    if not hits:
        report["reason"] = "没有候选"
        return hits, report

    # 近重复/精确匹配护航：召回余弦的**正常区间**是 0.31~0.50（见 config 里
    # min_score 的实测），所以 >= skip 阈值只可能是「库里就是这张图/这段画面」。
    # 这种情况下余弦是权威信号，而交叉编码器给的是绝对 yes 概率
    # （实测自我匹配只有 0.789，反而低于余弦），重排只会把它压下去、
    # 还会白花 8~12 秒。所以直接保留召回顺序。
    top_cos = max(float(getattr(h, "score", 0.0)) for h in hits)
    skip_cos = float(cfg.get("retrieval.rerank_skip_cosine", 0.85))
    if skip_cos > 0 and top_cos >= skip_cos:
        report["reason"] = (f"最高召回余弦 {top_cos:.3f} ≥ {skip_cos}（几乎就是查询素材本身），"
                            f"已跳过重排序以保留精确匹配信号")
        report["skipped_exact"] = True
        LOG.info("%s", report["reason"])
        return hits, report

    ik = int(input_k or cfg.get("retrieval.rerank_input_k", 60))
    head = hits[:ik]
    tail = hits[ik:]
    report["input_k"] = len(head)

    eng = get_reranker(cfg)
    if not eng.status.loaded:
        report["reason"] = f"重排序模型不可用，已按召回分数排序：{eng.status.error}"
        LOG.warning("%s", report["reason"])
        return hits, report

    docs = [doc_media_for_shot(h) for h in head]
    usable = [i for i, d in enumerate(docs) if d]
    if not usable:
        report["reason"] = "候选没有可用的关键帧/原图，跳过重排序"
        return hits, report

    t0 = time.monotonic()
    try:
        scores = eng.score(query, [docs[i] for i in usable])
    except Exception as exc:  # noqa: BLE001
        report["reason"] = f"重排序失败，已按召回分数排序：{exc}"
        LOG.warning("%s", report["reason"])
        return hits, report
    report["elapsed_sec"] = round(time.monotonic() - t0, 2)
    report["n_docs"] = len(usable)

    # 「退化分数」兜底：如果整批分数完全一样，说明组合输入被模型拒了
    # （vendored 实现会吞掉异常并退化成 NULL 文本对，实测视频查询就是这条路径），
    # 这种分数没有区分能力，直接用会把召回顺序打乱，因此按失败处理。
    if len(scores) >= 2 and max(scores) - min(scores) < 1e-6:
        report["reason"] = ("重排序分数全部相同（输入可能被模型拒绝），"
                            "已按召回分数排序")
        LOG.warning("%s", report["reason"])
        return hits, report

    # 未参与重排的（没有媒体）保持原相对顺序排在重排结果之后
    scored: List[Tuple[float, int, Any]] = []
    for i, sc in zip(usable, scores):
        scored.append((float(sc), i, head[i]))
    scored.sort(key=lambda t: (-t[0], t[1]))

    # ---- 分数空间隔离（2026-10-16 修，复现见 tmp/repro_rerank_score_space.py）----
    # 已重排的用交叉编码分；**没重排过的**（头部缺媒体的 + input_k 之外的尾部）
    # 必须被压进「严格低于已重排最低分」的一条单调递减带，两个原因：
    #   ① `adaptive_cut` 的前提是分数序列非递增。尾部带着**召回余弦**（实测 0.31~0.50）
    #      接在**重排分**（实测 0.14~0.51）后面会出现上升段 —— 实测 0.39→0.44，
    #      截断位置就不可靠了；
    #   ② 规格 8.2「重排后保留 Top-50」是硬上限，若尾部余弦高于重排分，
    #      会把**没重排过**的候选算进 Top-50，把已重排的挤出去
    #      （实测 50 条里有 20 条是未重排的，占 40%）。
    # 真实召回余弦保留在 recall_score 里，界面仍可追溯，信息不丢。
    reranked_scores = [s for s, _i, _h in scored]
    floor = min(reranked_scores) if reranked_scores else 0.0
    step = max(abs(floor), 1e-6) * 1e-3

    new_head: List[Any] = []
    for sc, _i, h in scored:
        try:
            h.rerank_score = round(float(sc), 4)
            h.recall_score = round(float(getattr(h, "score", 0.0)), 4)
            h.score = float(sc)          # 结果按重排分数排序（badge 仍是 sim）
        except AttributeError:
            pass
        new_head.append(h)

    # 未参与重排的：先把头部里缺媒体的按原顺序接上，再接 input_k 之外的尾部
    unscored: List[Any] = [head[i] for i, d in enumerate(docs) if not d] + list(tail)
    for j, h in enumerate(unscored):
        try:
            h.rerank_score = None
            h.recall_score = round(float(getattr(h, "score", 0.0)), 4)
            h.score = max(0.0, floor - step * (j + 1))
        except AttributeError:
            pass
    new_head.extend(unscored)

    report["applied"] = True
    report["tail_demoted"] = len(unscored)
    report["reason"] = (f"已重排序前 {len(usable)} 条（{report['elapsed_sec']}s，"
                        f"batch={eng.batch_size}，缓存命中 {eng.cache_hits}）"
                        + (f"；{len(unscored)} 条未重排的已降到重排分之下"
                           if unscored else ""))
    return new_head, report
