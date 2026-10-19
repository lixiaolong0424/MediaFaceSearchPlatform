"""人脸检测 / 对齐 / 识别的查询侧能力。

规格第六章 F1（显式路径加载，禁止依赖 FaceAnalysis 自动查找与联网下载）：
    det = SCRFD(model_file=.../det_10g.onnx)
    rec = ArcFaceONNX(model_file=.../w600k_r50.onnx)
    det.prepare(ctx_id=0, nms_thresh=0.4, det_thresh=0.5, input_size=(512,512))
    rec.prepare(ctx_id=0)          # 输出固定 512 维

对齐必须用模型自带逻辑（自己重写仿射变换会导致 embedding 失配）。
多图脸默认取面积最大者，其余保留带序号供前端点选。

本模块在 P0 阶段只服务「精确找人」的查询侧；F0 全库稠密采样属于 P2（06_face_detect.py）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .common import Config, get_logger, preload_ort_cuda

LOG = get_logger("face")

# insightface 必须显式传 providers，否则它自己会优先选 CPU
PROVIDER_PREFERENCE = ["CUDAExecutionProvider", "TensorrtExecutionProvider", "CPUExecutionProvider"]


@dataclass
class DetectedFace:
    index: int
    bbox: Tuple[float, float, float, float]      # x1,y1,x2,y2
    kps: Optional[np.ndarray]
    det_score: float
    embedding: Optional[List[float]] = None
    quality: float = 0.0
    normed_face: Optional[np.ndarray] = None

    @property
    def area(self) -> float:
        x1, y1, x2, y2 = self.bbox
        return max(0.0, x2 - x1) * max(0.0, y2 - y1)

    @property
    def short_edge(self) -> float:
        x1, y1, x2, y2 = self.bbox
        return float(min(max(0.0, x2 - x1), max(0.0, y2 - y1)))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "bbox": [round(float(v), 2) for v in self.bbox],
            "det_score": round(float(self.det_score), 4),
            "quality": round(float(self.quality), 4),
            "short_edge": round(self.short_edge, 1),
            "area": round(self.area, 1),
        }


@dataclass
class FaceEngine:
    det: Any = None
    rec: Any = None
    providers: List[str] = field(default_factory=list)
    device: str = "cpu"
    loaded: bool = False
    error: str = ""
    ctx_id: int = -1
    _align: Any = None

    # -- 加载 -------------------------------------------------------------- #
    def load(self, cfg: Config) -> "FaceEngine":
        if self.loaded:
            return self
        try:
            import onnxruntime as ort
        except ImportError:
            self.error = ("缺少 onnxruntime-gpu。修复："
                          ".venv\\Scripts\\python.exe -m pip install onnxruntime-gpu")
            return self

        # 关键：必须在创建任何 InferenceSession 之前把 PyTorch 自带的 CUDA/cuDNN DLL
        # 放进搜索路径，否则 onnxruntime 会静默回退到 CPU（规格点名的坑）。
        ok_pre, pre_msg = preload_ort_cuda()
        LOG.info("onnxruntime DLL 预加载：%s（%s）", ok_pre, pre_msg)

        available = list(ort.get_available_providers())
        providers = [p for p in PROVIDER_PREFERENCE if p in available] or ["CPUExecutionProvider"]
        if "CUDAExecutionProvider" not in providers:
            # 规格第十一章【必过】：onnxruntime 未启用 CUDA → 人脸检测将慢 10~30 倍
            self.error = (
                "onnxruntime 未注册 CUDAExecutionProvider（当前只支持 "
                f"{available}）。修复：pip uninstall onnxruntime onnxruntime-gpu -y && "
                "pip install onnxruntime-gpu；确认 nvidia-smi 正常、驱动 >= 55x。"
            )
            LOG.error(self.error)
        self.providers = providers
        self.device = "cuda" if "CUDAExecutionProvider" in providers else "cpu"
        self.ctx_id = 0 if self.device == "cuda" else -1

        det_onnx = Path(str(cfg.get("faces.det_onnx")))
        rec_onnx = Path(str(cfg.get("faces.rec_onnx")))
        for p, name in ((det_onnx, "det_10g.onnx"), (rec_onnx, "w600k_r50.onnx")):
            if not p.exists():
                self.error = f"缺少人脸权重 {name}：{p}"
                return self

        try:
            import insightface
            from insightface.model_zoo import ArcFaceONNX, SCRFD
        except ImportError:
            self.error = ("缺少 insightface。修复："
                          ".venv\\Scripts\\python.exe -m pip install insightface")
            return self

        try:
            det_size = tuple(int(x) for x in cfg.get("faces.det_size", [512, 512]))
            # 注意：insightface 的 SCRFD / ArcFaceONNX **构造器不接受 providers**：
            #   SCRFD.__init__(model_file=None, session=None, ...)
            #   ArcFaceONNX.__init__(model_file=None, session=None)
            # 执行 provider 由 prepare(ctx_id=...) 决定：
            #   ctx_id >= 0 → insightface 内部按可用性自动选 CUDA（GPU）
            #   ctx_id <  0 → 强制 CPU
            # 早期版本误传 providers= 会直接 TypeError，导致人脸引擎完全加载不了。
            self.det = SCRFD(model_file=str(det_onnx))
            self.det.prepare(
                ctx_id=self.ctx_id,
                det_thresh=float(cfg.get("faces.det_thresh", 0.5)),
                nms_thresh=float(cfg.get("faces.nms_thresh", 0.4)),
                input_size=det_size,
            )
            self.rec = ArcFaceONNX(model_file=str(rec_onnx))
            self.rec.prepare(ctx_id=self.ctx_id)

            # 对齐器：用 insightface 自带 FaceAlign，绝不自己写仿射变换
            try:
                from insightface.utils import face_align  # type: ignore

                self._align = face_align.norm_crop
            except Exception:  # noqa: BLE001
                self._align = getattr(self.rec, "get", None)

            # 校验运行期真的落在 GPU（抓「静默回退 CPU」）
            actual = []
            try:
                actual = list(self.det.session.get_providers())
            except Exception:  # noqa: BLE001
                pass
            if actual and "CPUExecutionProvider" == actual[0]:
                LOG.warning("人脸检测会话首个 provider 是 CPU —— 可能已静默回退。"
                            "providers=%s", actual)
            self.loaded = True
            LOG.info("人脸引擎就绪（ctx_id=%s, det providers=%s, det_size=%s）",
                     self.ctx_id, actual, det_size)
        except Exception as exc:  # noqa: BLE001
            self.error = f"加载人脸模型失败：{exc}"
            LOG.exception("加载人脸模型失败")
        return self

    # -- 检测 -------------------------------------------------------------- #
    def detect(self, img_bgr: np.ndarray) -> List[DetectedFace]:
        """返回按人脸面积降序的人脸列表（多的在前，便于默认取最大脸）。"""
        if not self.loaded:
            return []
        try:
            raw = self.det.detect(img_bgr, max_num=0)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("人脸检测失败：%s", exc)
            return []
        if raw is None or len(raw) == 0:
            return []
        bboxes = raw[0] if isinstance(raw, tuple) else raw
        kpss = raw[1] if isinstance(raw, tuple) and len(raw) > 1 else None

        faces: List[DetectedFace] = []
        for i, b in enumerate(bboxes):
            b = np.asarray(b).ravel()
            x1, y1, x2, y2, score = (float(b[0]), float(b[1]), float(b[2]), float(b[3]),
                                     float(b[4]) if b.size > 4 else 1.0)
            kps = None
            if kpss is not None and i < len(kpss):
                kps = np.asarray(kpss[i], dtype="float32")
            faces.append(DetectedFace(index=0, bbox=(x1, y1, x2, y2), kps=kps, det_score=score))
        faces.sort(key=lambda f: -f.area)
        for i, f in enumerate(faces):
            f.index = i
        return faces

    # -- 识别 -------------------------------------------------------------- #
    def embed(self, img_bgr: np.ndarray, face: DetectedFace,
              *, input_size: int = 112) -> Optional[List[float]]:
        """对齐 + ArcFace 取 512 维向量。

        input_size 是**单个整数边长**，不是 (w,h) 元组：
          insightface 的 face_align.norm_crop(img, landmark, image_size) 内部会做
          `assert image_size%112==0 or image_size%128==0`，
          传元组会直接 TypeError: unsupported operand type(s) for %: 'tuple' and 'int'
          （早期版本把 input_size 当 OpenCV 的 (w,h) 传，导致所有人脸识别静默失败）。
          而 _crop_fallback 用的是 cv2.resize，需要 (w,h)，所以在那里再组装。
        """
        if not self.loaded:
            return None
        try:
            side = int(input_size) if not isinstance(input_size, (tuple, list)) \
                else int(input_size[0])
            if face.kps is not None and self._align is not None:
                aligned = self._align(img_bgr, face.kps, side)
            else:
                aligned = _crop_fallback(img_bgr, face.bbox, (side, side))
            face.normed_face = aligned
            feat = self.rec.get_feat(aligned)
            vec = np.asarray(feat, dtype="float32").ravel()
            n = float(np.linalg.norm(vec))
            if n > 0:
                vec = vec / n
            return vec.tolist()
        except Exception as exc:  # noqa: BLE001
            LOG.warning("人脸识别失败：%s", exc)
            return None

    def detect_and_embed(self, img_bgr: np.ndarray, cfg: Config,
                         *, max_faces: Optional[int] = None) -> List[DetectedFace]:
        faces = self.detect(img_bgr)
        if max_faces:
            faces = faces[:max_faces]
        # faces.input_size 在 config 里写成 [112, 112]（便于和其他尺寸参数一致），
        # 这里取首元素作为边长
        raw = cfg.get("faces.input_size", [112, 112])
        side = int(raw[0]) if isinstance(raw, (list, tuple)) else int(raw)
        for f in faces:
            f.embedding = self.embed(img_bgr, f, input_size=side)
            f.quality = quality_score(
                img_bgr, f, lap_norm=float(cfg.get("faces.quality_lap_norm", 150.0)))
        return faces

    def status(self) -> Dict[str, Any]:
        return {
            "loaded": self.loaded,
            "device": self.device,
            "providers": self.providers,
            "error": self.error,
        }


def _crop_fallback(img_bgr: np.ndarray, bbox: Sequence[float], input_size: Tuple[int, int]) -> np.ndarray:
    import cv2

    h, w = img_bgr.shape[:2]
    x1, y1, x2, y2 = (int(max(0, bbox[0])), int(max(0, bbox[1])),
                      int(min(w, bbox[2])), int(min(h, bbox[3])))
    crop = img_bgr[y1:y2, x1:x2]
    if crop.size == 0:
        crop = img_bgr
    return cv2.resize(crop, input_size, interpolation=cv2.INTER_LINEAR)


def quality_score(img_bgr: np.ndarray, face: DetectedFace,
                  lap_norm: float = 150.0) -> float:
    """quality = 人脸像素面积 × 拉普拉斯清晰度 × 正面度 × det置信度（规格 F2）。

    统一归一化到 0~1 区间，便于 UI 用 min_quality 过滤。

    lap_norm 是「清晰度项取满分所需的拉普拉斯方差」。原实现写死 500，
    在真实 384px 关键帧上把绝大多数人脸压到 0.2 附近（实测中位数 0.22），
    于是默认 min_quality=0.3 会把 2/3 的人脸挡在墙外 —— 阈值本身没错，
    是归一化尺度与人脸裁剪的实际尺度不匹配，属于原实现的标定错误。
    现改为可配置（config faces.quality_lap_norm），默认按真实素材校准。

    清晰度项用开方压缩：低方差区间更敏感，避免「稍糊就直接 0 分」把
    整批小尺寸人脸一刀切掉。
    """
    import cv2

    h, w = img_bgr.shape[:2]
    x1, y1, x2, y2 = face.bbox
    x1i, y1i = int(max(0, x1)), int(max(0, y1))
    x2i, y2i = int(min(w, x2)), int(min(h, y2))
    crop = img_bgr[y1i:y2i, x1i:x2i]
    if crop.size == 0:
        return 0.0
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    sharp = min(1.0, (max(0.0, lap_var) / max(1e-6, float(lap_norm))) ** 0.5)
    area = face.area
    area_term = min(1.0, area / (112.0 * 112.0))
    frontal = frontal_ratio(face.kps) if face.kps is not None else 0.5
    score = area_term * sharp * frontal * max(0.0, min(1.0, face.det_score))
    return float(round(score, 4))


def frontal_ratio(kps: Optional[np.ndarray]) -> float:
    """正面度：用 5 点关键点估计（眼距 / 眼到嘴距离），越接近正视越大。"""
    if kps is None or len(kps) < 5:
        return 0.5
    try:
        left_eye, right_eye, nose, left_mouth, right_mouth = kps[:5]
        eye_d = float(np.linalg.norm(right_eye - left_eye)) + 1e-6
        eye_mid = (left_eye + right_eye) / 2.0
        mouth_mid = (left_mouth + right_mouth) / 2.0
        vert = float(np.linalg.norm(mouth_mid - eye_mid)) + 1e-6
        ratio = vert / eye_d
        # 正视时 ratio 约 1.2~1.6；偏离越大越不正
        dev = abs(ratio - 1.4) / 1.4
        return float(max(0.0, min(1.0, 1.0 - dev)))
    except Exception:  # noqa: BLE001
        return 0.5


# --------------------------------------------------------------------------- #
# 查询素材的人脸提取（检索侧入口）
# --------------------------------------------------------------------------- #

_ENGINE: Optional[FaceEngine] = None


def get_engine(cfg: Config, *, reload: bool = False) -> FaceEngine:
    global _ENGINE
    if _ENGINE is None or reload:
        eng = FaceEngine()
        eng.load(cfg)
        _ENGINE = eng
    return _ENGINE


def _imread_unicode(path: Path) -> Optional[np.ndarray]:
    """Windows 中文路径：cv2.imread 会失败，改用 np.fromfile + imdecode。"""
    import cv2

    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
        if buf.size == 0:
            return None
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("读图失败 %s：%s", path, exc)
        return None


def query_frames(cfg: Config, path: Path, query_mode: str = "image") -> List[np.ndarray]:
    """把查询素材变成若干 BGR 帧（视频按 F0 策略抽帧；照片只有一帧）。"""
    if query_mode == "image":
        img = _imread_unicode(path)
        return [img] if img is not None else []

    # 视频：抽首/中/尾 + 等间隔共 5 帧
    from .extract import (extract_frame, locate_tools, ffprobe_media)

    tools = locate_tools(cfg)
    if not tools.ok:
        return []
    probe = ffprobe_media(tools.ffprobe, path)
    if not probe.ok or probe.duration <= 0:
        return []
    import tempfile

    ts_list = np.linspace(0, max(0.0, probe.duration - 0.1), 5).tolist()
    frames: List[np.ndarray] = []
    with tempfile.TemporaryDirectory(dir=str(cfg.tmp_dir)) as td:
        for i, ts in enumerate(ts_list):
            out = Path(td) / f"q{i}.jpg"
            ok, _err = extract_frame(tools.ffmpeg, path, float(ts), out, 384)
            if ok:
                img = _imread_unicode(out)
                if img is not None:
                    frames.append(img)
    return frames


def detect_query_faces(cfg: Config, path: Path, query_mode: str = "image") -> List[DetectedFace]:
    """查询素材检脸。返回全部检出人脸（按面积降序），带 ArcFace 512 维向量。

    多图脸默认取面积最大者（faces[0]），其余保留 index 供前端点选。
    """
    eng = get_engine(cfg)
    if not eng.loaded:
        LOG.error("人脸引擎不可用：%s", eng.error)
        return []

    frames = query_frames(cfg, path, query_mode)
    best: List[DetectedFace] = []
    seen: List[List[float]] = []
    for img in frames:
        faces = eng.detect_and_embed(img, cfg)
        for f in faces:
            if f.embedding is None:
                continue
            # 跨帧去重：与已收集的向量 cosine > 0.85 视为同一人
            dup = False
            for v in seen:
                if _cosine(f.embedding, v) > 0.85:
                    dup = True
                    break
            if not dup:
                seen.append(f.embedding)
                best.append(f)
    best.sort(key=lambda f: -f.area)
    for i, f in enumerate(best):
        f.index = i
    LOG.info("查询素材检出 %d 张人脸（%s）", len(best), path.name)
    return best


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    va, vb = np.asarray(a, dtype="float32"), np.asarray(b, dtype="float32")
    na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
    if na == 0 or nb == 0:
        return 0.0
    return float(va.dot(vb) / (na * nb))


def crop_face(img_bgr: np.ndarray, face: DetectedFace, size: int = 112) -> np.ndarray:
    """按对齐结果（若已算）或 bbox 裁剪人脸，用于写 raw/thumb。"""
    if face.normed_face is not None:
        import cv2

        return cv2.resize(face.normed_face, (size, size), interpolation=cv2.INTER_AREA)
    return _crop_fallback(img_bgr, face.bbox, (size, size))
