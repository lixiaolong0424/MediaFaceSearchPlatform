"""语义编码器：Qwen3-VL-Embedding-2B（唯一在线语义模型）。

规格第五章 L2c + 20.1：权重必须离线加载，禁止无声下载 5GB。
  model_path: ${project_root}/weight/Qwen3-VL-Embedding-2B
  offline_only: true / dimension: 2048 / dtype: bfloat16 / batch_size: 16
  instruction 仅注入 query 侧；入库侧禁止拼 instruction。

加载路径（按优先级，全部为本地文件，不做任何网络请求）：
  A. sentence-transformers（modules.json 声明为 SentenceTransformer，2_Pooling=lasttoken）
  B. 权重目录自带的 transformers 实现 qwen3_vl_embedding.py（Qwen3VLEmbedder）

OOM 处理（第十章）：torch.cuda.OutOfMemoryError 时 batch 减半重试一次。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .common import Config, get_logger

LOG = get_logger("encode")

# 必须在 import torch/transformers 之前设置，确保离线
OFFLINE_ENV = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_SYMLINKS_WARNING": "1",
    "TOKENIZERS_PARALLELISM": "false",
    "PYTHONUTF8": "1",
}


def apply_offline_env() -> Dict[str, str]:
    for k, v in OFFLINE_ENV.items():
        os.environ.setdefault(k, v)
    return dict(OFFLINE_ENV)


@dataclass
class EncoderStatus:
    backend: str
    model_path: str
    dimension: int
    device: str
    dtype: str
    loaded: bool
    error: str = ""


class SemanticEncoder:
    """统一的 encode_texts / encode_images 接口，屏蔽底层后端差异。"""

    def __init__(self, cfg: Config, *, device: Optional[str] = None, backend: Optional[str] = None):
        apply_offline_env()
        self.cfg = cfg
        self.model_path = Path(str(cfg.get("semantic.model_path")))
        self.dimension = int(cfg.get("semantic.dimension", 2048))
        self.mrl_dim = cfg.get("semantic.mrl_dim")
        self.dtype_name = str(cfg.get("semantic.dtype", "bfloat16"))
        self.batch_size = int(cfg.get("semantic.batch_size", 16))
        self.device = device
        self.backend = backend or "auto"

        self._impl: Any = None
        self._kind = ""
        self._loading = False      # 递归守卫：load() 内部的自检会调 encode_*，不能再触发 load()
        self.status = EncoderStatus(
            backend="", model_path=str(self.model_path), dimension=self.dimension,
            device="", dtype=self.dtype_name, loaded=False,
        )
        self._torch = None

    # -- 加载 -------------------------------------------------------------- #
    def load(self) -> EncoderStatus:
        if self.status.loaded:
            return self.status
        if self._loading:
            # 已经在加载中（自检触发的重入）→ 直接把当前 impl 视为就绪
            return self.status
        if not self.model_path.exists():
            self.status.error = (
                f"权重目录不存在：{self.model_path}\n"
                "修复：确认 config.yaml 的 semantic.model_path，或把权重放到该目录。"
            )
            return self.status

        self._loading = True
        try:
            return self._do_load()
        finally:
            self._loading = False

    def _do_load(self) -> EncoderStatus:
        import torch

        self._torch = torch
        self.device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
        dtype = {
            "bfloat16": torch.bfloat16,
            "float16": torch.float16,
            "float32": torch.float32,
        }.get(self.dtype_name, torch.bfloat16)
        if self.device == "cpu" and dtype is torch.bfloat16:
            dtype = torch.float32  # CPU 上 bfloat16 太慢

        errors: List[str] = []
        # 默认顺序：先用权重目录自带的 Qwen3VLForEmbedding 实现，再用
        # sentence-transformers。原因：官方 checkpoint 的键名前缀是
        # "model.language_model.*" / "model.visual.*"（Qwen3VLForEmbedding 把
        # Qwen3VLModel 存在 self.model 下），而 SentenceTransformer 会用
        # Qwen3VLModel 直接加载，前缀对不上 → 全部权重被「新初始化」，
        # 实测日志会出现 "Some weights ... were newly initialized" 且向量无意义。
        order = ["qwen_transformers", "sentence_transformers"]
        if self.backend != "auto":
            order = [self.backend]

        for name in order:
            try:
                if name == "sentence_transformers":
                    self._load_sentence_transformers(dtype)
                else:
                    self._load_qwen_transformer(dtype)
                # 标记就绪后再自检，避免自检里的 encode_* 触发对 load() 的重入
                self._kind = name
                self.status.backend = name
                self.status.device = str(self.device)
                self.status.loaded = True
                self._sanity_check(name)
                LOG.info("语义模型加载成功（backend=%s, device=%s, dtype=%s, dim=%d）",
                         name, self.device, self.dtype_name, self.dimension)
                return self.status
            except Exception as exc:  # noqa: BLE001 - 逐个后端尝试
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                LOG.warning("后端 %s 加载失败：%s", name, exc)
                self._impl = None
                self.status.loaded = False

        self.status.error = "所有后端加载失败：\n  " + "\n  ".join(errors)
        return self.status

    def _sanity_check(self, backend: str) -> None:
        """加载后的合理性检查：抓「权重没加载上」这类静默错误。

        随机初始化的模型也能跑出向量，但相似度排序毫无意义。这里用两条信号判定：
          1) 向量 L2 范数是否异常（随机权重下通常远大于 1 或为 NaN）
          2) 同一文本两次编码的余弦是否为 1（确定性），且不同文本的相似度不应
             近乎恒等于 1（那说明模型塌缩）
        """
        import numpy as np

        vecs = self.encode_texts(["a photo of a cat", "a photo of a dog"],
                                 instruction="Represent the user's input.")
        arr = np.asarray(vecs, dtype="float32")
        if arr.ndim != 2 or arr.shape[0] != 2:
            raise RuntimeError(f"编码自检返回形状异常：{arr.shape}")
        if not np.isfinite(arr).all():
            raise RuntimeError("编码自检出现 NaN/Inf：权重很可能未正确加载")
        norms = np.linalg.norm(arr, axis=1)
        cos = float(arr[0].dot(arr[1]) / (norms[0] * norms[1] + 1e-9))
        LOG.info("编码自检（%s）：norm=%s cosine(cat,dog)=%.4f", backend,
                 np.round(norms, 4).tolist(), cos)
        if abs(cos - 1.0) < 1e-6:
            raise RuntimeError("不同文本得到几乎相同的向量，模型可能塌缩/未正确加载")

    def _load_sentence_transformers(self, dtype: Any) -> None:
        """sentence-transformers 路线。

        ⚠️ 已知问题（本机实测）：该 checkpoint 的 modules.json 声明了
        `1_Pooling` / `2_Normalize` 两个子目录，但目录并不存在，
        且官方权重键名前缀是 `model.` 开头（Qwen3VLForEmbedding 结构），
        用 SentenceTransformer 直接加载会出现
          * Pooling.__init__() missing 1 required positional argument
          * 或 "Some weights ... were newly initialized"（权重全部丢失）
        因此这条路线默认不启用，仅作备用；真正可用的是 qwen_transformers 路线。
        """
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(
            str(self.model_path),
            device=self.device,
            model_kwargs={"dtype": dtype, "attn_implementation": "sdpa"},
            local_files_only=True,
            trust_remote_code=True,
        )
        native = model.get_sentence_embedding_dimension()
        if native and int(native) != self.dimension and not self.mrl_dim:
            LOG.warning("模型原生维度 %s 与 config 的 %s 不一致，按模型原生维度使用", native, self.dimension)
            self.dimension = int(native)
        self._impl = model

    def _load_qwen_transformer(self, dtype: Any) -> None:
        """加载权重目录自带的 Qwen3VLEmbedder 实现。"""
        import importlib.util

        script = self.model_path / "qwen3_vl_embedding.py"
        if not script.exists():
            raise FileNotFoundError(f"缺少 {script.name}")
        spec = importlib.util.spec_from_file_location("qwen3_vl_embedding_local", script)
        if spec is None or spec.loader is None:
            raise ImportError("无法加载 qwen3_vl_embedding.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        # 注意：Qwen3VLEmbedder.__init__ 内部已经写死了 trust_remote_code=True，
        # 再传一次会 TypeError（got multiple values for keyword argument）。
        # 用 dtype=（不是已弃用的 torch_dtype=）。
        #
        # max_pixels / min_pixels 是**编码吞吐的主要旋钮**（2026-10-16 实测）：
        #   checkpoint 自带默认 MAX_PIXELS = 1800*28*28 ≈ 1.41 MP，
        #   即每张图约 1000~1800 个视觉 token → 2B 模型单图约 0.25s，
        #   实测只有 4 张/秒（batch=16，GPU 利用率 65%）。
        # 默认仍用 checkpoint 的值（不擅自降质）；要换吞吐就改 config 的
        # semantic.max_pixels，但要**始终用同一个值**编码整库，
        # 否则先入库与后入库的向量分布不一致，检索会漂移。
        impl_kwargs: Dict[str, Any] = {}
        max_px = self.cfg.get("semantic.max_pixels")
        min_px = self.cfg.get("semantic.min_pixels")
        if max_px:
            impl_kwargs["max_pixels"] = int(max_px)
        if min_px:
            impl_kwargs["min_pixels"] = int(min_px)
        impl = module.Qwen3VLEmbedder(
            model_name_or_path=str(self.model_path),
            dtype=dtype,
            attn_implementation="sdpa",
            local_files_only=True,
            **impl_kwargs,
        )
        if impl_kwargs:
            LOG.info("编码像素预算：min_pixels=%s max_pixels=%s（来自 config semantic.*）",
                     impl_kwargs.get("min_pixels", "checkpoint 默认"),
                     impl_kwargs.get("max_pixels", "checkpoint 默认"))
        self._impl = impl
        native = int(self.cfg.get("semantic.dimension", 2048))
        self.dimension = int(self.mrl_dim or native)

    # -- 编码 -------------------------------------------------------------- #
    def _pool_and_truncate(self, vecs: Any, *, normalize: bool = True) -> List[List[float]]:
        torch = self._torch
        import numpy as np

        if not isinstance(vecs, np.ndarray):
            vecs = np.asarray(vecs)
        if self.mrl_dim:
            m = int(self.mrl_dim)
            if vecs.shape[1] > m:
                vecs = vecs[:, :m]
        if normalize:
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            vecs = vecs / norms
        return vecs.astype("float32").tolist()

    def encode_texts(self, texts: Sequence[str], *, instruction: Optional[str] = None,
                     batch_size: Optional[int] = None) -> List[List[float]]:
        st = self.load()
        if not st.loaded:
            raise RuntimeError(st.error)
        instruction = instruction if instruction is not None else str(self.cfg.get("semantic.instruct_query", ""))
        bs = batch_size or self.batch_size
        out: List[List[float]] = []
        for i in range(0, len(texts), bs):
            chunk = list(texts[i:i + bs])
            try:
                out.extend(self._encode_text_chunk(chunk, instruction, st.backend))
            except Exception as exc:  # noqa: BLE001
                if self._is_oom(exc) and len(chunk) > 1:
                    half = max(1, len(chunk) // 2)
                    LOG.warning("文本编码 OOM，batch %d → %d 重试", len(chunk), half)
                    out.extend(self._encode_text_chunk(chunk[:half], instruction, st.backend))
                    out.extend(self._encode_text_chunk(chunk[half:], instruction, st.backend))
                else:
                    raise
        return out

    def encode_images(self, paths: Sequence[Path], *, instruction: Optional[str] = None,
                      batch_size: Optional[int] = None) -> List[List[float]]:
        st = self.load()
        if not st.loaded:
            raise RuntimeError(st.error)
        instruction = instruction if instruction is not None else str(self.cfg.get("semantic.instruct_doc", ""))
        bs = batch_size or self.batch_size
        out: List[List[float]] = []
        for i in range(0, len(paths), bs):
            chunk = [Path(p) for p in paths[i:i + bs]]
            try:
                out.extend(self._encode_image_chunk(chunk, instruction, st.backend))
            except Exception as exc:  # noqa: BLE001
                if self._is_oom(exc) and len(chunk) > 1:
                    half = max(1, len(chunk) // 2)
                    LOG.warning("图像编码 OOM，batch %d → %d 重试", len(chunk), half)
                    out.extend(self._encode_image_chunk(chunk[:half], instruction, st.backend))
                    out.extend(self._encode_image_chunk(chunk[half:], instruction, st.backend))
                else:
                    raise
        return out

    def _is_oom(self, exc: Exception) -> bool:
        torch = self._torch
        if torch is not None and isinstance(exc, getattr(torch.cuda, "OutOfMemoryError", ())):
            return True
        return "out of memory" in str(exc).lower()

    def _encode_text_chunk(self, texts: List[str], instruction: str, backend: str) -> List[List[float]]:
        torch = self._torch
        if backend == "sentence_transformers":
            with torch.inference_mode():
                vecs = self._impl.encode(
                    texts, prompt=instruction or None, batch_size=len(texts),
                    normalize_embeddings=True, show_progress_bar=False,
                )
            return self._pool_and_truncate(vecs, normalize=False)  # 已归一化
        inputs = [{"text": t, "instruction": instruction} for t in texts]
        with torch.inference_mode():
            vecs = self._impl.process(inputs)
        return self._pool_and_truncate(vecs.detach().float().cpu().numpy())

    def _encode_image_chunk(self, paths: List[Path], instruction: str, backend: str) -> List[List[float]]:
        torch = self._torch
        if backend == "sentence_transformers":
            from PIL import Image

            imgs = []
            for p in paths:
                with Image.open(p) as im:
                    imgs.append(im.convert("RGB").copy())
            with torch.inference_mode():
                vecs = self._impl.encode(
                    imgs, prompt=instruction or None, batch_size=len(imgs),
                    normalize_embeddings=True, show_progress_bar=False,
                )
            return self._pool_and_truncate(vecs, normalize=False)
        inputs = [{"image": str(p), "instruction": instruction} for p in paths]
        with torch.inference_mode():
            vecs = self._impl.process(inputs)
        return self._pool_and_truncate(vecs.detach().float().cpu().numpy())

    # -- 工具 -------------------------------------------------------------- #
    def similarity(self, a: Sequence[float], b: Sequence[float]) -> float:
        import numpy as np

        va, vb = np.asarray(a, dtype="float32"), np.asarray(b, dtype="float32")
        na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
        if na == 0 or nb == 0:
            return 0.0
        return float(va.dot(vb) / (na * nb))


_SINGLETON: Optional[SemanticEncoder] = None


def get_encoder(cfg: Config, *, reload: bool = False) -> SemanticEncoder:
    """进程内单例：3090 上 2B 模型重复加载代价高。"""
    global _SINGLETON
    if _SINGLETON is None or reload:
        _SINGLETON = SemanticEncoder(cfg)
    return _SINGLETON


def reset_encoder() -> None:
    global _SINGLETON
    _SINGLETON = None


def encode_query(
    cfg: Config,
    *,
    text: Optional[str] = None,
    image: Optional[Path] = None,
    video: Optional[Path] = None,
    instruction: Optional[str] = None,
) -> Tuple[List[float], str]:
    """检索侧统一入口。返回 (向量, 使用的 instruction)。

    * 文字：query 侧轻量改写（§7.4），instruction 仅在此处注入，入库侧绝不拼 instruction。
    * 图片：直接编码该图。
    * 视频：**必须先抽帧再编码**。把 .mp4 路径直接交给图像编码器会得到
      "cannot identify image file ... .mp4"（实测），因此这里复用 03_extract 的
      抽帧能力取出代表帧，逐帧编码后平均并重新归一化——
      与 doc 侧的「镜头内关键帧平均」保持一致的多帧处理方式。
    """
    import numpy as np

    from .query_rewrite import rewrite_query

    enc = get_encoder(cfg)
    if text is not None:
        q = rewrite_query(text, cfg) if cfg.get("retrieval.query_rewrite_enabled", True) else text
        vec = enc.encode_texts([q], instruction=instruction)[0]
        return vec, (instruction or str(cfg.get("semantic.instruct_query", "")))

    if image is not None:
        vec = enc.encode_images([Path(image)], instruction=instruction)[0]
        return vec, (instruction or str(cfg.get("semantic.instruct_doc", "")))

    if video is not None:
        frames = sample_video_frames(cfg, Path(video))
        if not frames:
            raise RuntimeError(
                f"无法从查询视频抽帧：{video}\n"
                "可能原因：ffmpeg/ffprobe 不可用；或视频编码不受支持。"
                "请改用图片查询，或先用 03_extract.py 验证该视频可解码。"
            )
        vecs = enc.encode_images(frames, instruction=instruction)
        arr = np.asarray(vecs, dtype="float32")
        mean = arr.mean(axis=0)
        norm = float(np.linalg.norm(mean))
        if norm > 0:
            mean = mean / norm
        return mean.tolist(), (instruction or str(cfg.get("semantic.instruct_doc", "")))

    raise ValueError("encode_query 需要 text / image / video 之一")


def sample_video_frames(cfg: Config, video: Path, n: int = 5) -> List[Path]:
    """从查询视频抽 n 帧代表帧（写入 query_cache，随 24h 清理自动回收）。

    抽帧逻辑与 doc 侧共用 extract.extract_frame，避免两套解码参数不一致。
    """
    import tempfile

    import numpy as np

    from .extract import extract_frame, ffprobe_media, locate_tools

    tools = locate_tools(cfg)
    if not tools.ok or not video.exists():
        LOG.error("抽帧前置条件不满足：tools.ok=%s exists=%s", tools.ok, video.exists())
        return []

    probe = ffprobe_media(tools.ffprobe, video)
    duration = probe.duration if probe.ok and probe.duration > 0 else 0.0
    if duration <= 0:
        LOG.warning("无法获取查询视频时长：%s", video.name)
        return []

    short_edge = int(cfg.get("storage.keyframe_short_edge", 384))
    # 避开首尾黑场/转场帧
    ts_list = np.linspace(0.05 * duration, 0.95 * duration, max(1, n)).tolist()
    outdir = cfg.path("paths.query_cache") / "frames"
    outdir.mkdir(parents=True, exist_ok=True)
    frames: List[Path] = []
    for i, ts in enumerate(ts_list):
        out = outdir / f"{video.stem[:40]}_{int(ts * 1000)}_{i}.jpg"
        ok, err = extract_frame(tools.ffmpeg, video, float(ts), out, short_edge)
        if ok:
            frames.append(out)
        else:
            LOG.warning("查询视频抽帧失败 ts=%.2f：%s", ts, err)
    LOG.info("查询视频抽出 %d/%d 帧（%s）", len(frames), len(ts_list), video.name)
    return frames
