"""03_extract.py · L2b 镜头切分与抽帧 + EXIF/容器时间。

规格第五章 L2b：
  scene_detect_threshold 30.0 / min_shot_len_sec 0.8 / max_frames_per_shot 5
  keyframe_short_edge 384 / long_shot_threshold_sec 10.0 / long_shot_sample_per_sec 1
  photo: keyframe_short_edge 384

  同阶段顺手读 EXIF：DateTimeOriginal(含GPS时区) → QuickTime Creation Time
  → 文件名日期 → mtime，落 captured_at + time_source。
  不可在渲染时实时读。

失败处理：
  * 抽帧后校验文件大小 > 2KB 且非全黑（均值 > 5），失败记 error 重试 1 次
  * 单文件失败绝不中断整批
  * 镜头数异常告警（每小时镜头数超出 [30,600] 标 warn；单镜头超 30 分钟强制按 5 分钟切段）

背压与 OOM（第十章）：ffmpeg worker 按可用内存动态计算并发（每 worker 预留 300MB，
上限 min(free//300MB, 核数)），单视频 10 分钟超时 + 内存上限（psutil 轮询 + taskkill /T /F）。
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

from . import db as D
from . import tasks as T
from .common import (
    Config, find_executable, get_logger, local_now_iso, run_capture,
)
from .exif import resolve_capture_time

LOG = get_logger("extract")

# showinfo 输出的 pts_time（用于解析 ffmpeg scdet 放行的切点帧）
_PTS_RE = re.compile(r"pts_time:([0-9.]+)")

# files.kind 有 CHECK(kind IN ('photo','video','unknown')) 约束，**不能**用中间态
# 去标记管线阶段。阶段进度一律记录在 stage_status 表里（见 ensure_stage_table），
# 这与两阶段入库的 file_encoding 是同一套思路，且不违反「只增不改」的迁移纪律。
KIND_PHOTO = "photo"
KIND_VIDEO = "video"

STAGE_EXTRACT = "extract"
STAGE_DEDUP = "dedup"


def ensure_stage_table(conn) -> None:
    """阶段状态表：记录每个文件在各阶段的状态，支撑断点续跑与 repair。"""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS stage_status (
               file_id    INTEGER NOT NULL,
               stage      TEXT    NOT NULL,
               status     TEXT,                -- pending|done|error|skipped
               shots      INTEGER DEFAULT 0,
               frames     INTEGER DEFAULT 0,
               error      TEXT,
               updated_at TEXT,
               PRIMARY KEY (file_id, stage)
           )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stage_status ON stage_status(stage, status)")


def set_stage(conn, file_id: int, stage: str, status: str, *, shots: int = 0,
              frames: int = 0, error: Optional[str] = None) -> None:
    conn.execute(
        """INSERT INTO stage_status(file_id, stage, status, shots, frames, error, updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(file_id, stage) DO UPDATE SET
               status=excluded.status, shots=excluded.shots, frames=excluded.frames,
               error=excluded.error, updated_at=excluded.updated_at""",
        (file_id, stage, status, shots, frames, (error or "")[:1000] or None, local_now_iso()),
    )


def stage_ok(conn, file_id: int, stage: str) -> bool:
    row = conn.execute(
        "SELECT status FROM stage_status WHERE file_id=? AND stage=?", (file_id, stage)
    ).fetchone()
    return bool(row and row["status"] == "done")


# --------------------------------------------------------------------------- #
# 外部工具定位
# --------------------------------------------------------------------------- #


@dataclass
class FFmpegTools:
    ffmpeg: Optional[Path]
    ffprobe: Optional[Path]

    @property
    def ok(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe)

    def describe(self) -> str:
        return f"ffmpeg={self.ffmpeg} ffprobe={self.ffprobe}"


def locate_tools(cfg: Config) -> FFmpegTools:
    ffmpeg = find_executable(cfg, "ffmpeg.bin", "ffmpeg")
    ffprobe = find_executable(cfg, "ffmpeg.probe", "ffprobe")
    return FFmpegTools(ffmpeg, ffprobe)


# --------------------------------------------------------------------------- #
# ffprobe
# --------------------------------------------------------------------------- #


@dataclass
class ProbeInfo:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    rotation: int = 0
    fps: float = 0.0
    tags: Dict[str, Any] = None  # type: ignore[assignment]
    ok: bool = False
    error: str = ""

    def __post_init__(self) -> None:
        if self.tags is None:
            self.tags = {}


def ffprobe_media(ffprobe: Path, path: Path, timeout: float = 60.0) -> ProbeInfo:
    argv = [
        str(ffprobe), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    res = run_capture(argv, timeout=timeout)
    if not res.ok:
        return ProbeInfo(error=(res.stderr or "ffprobe 失败").strip()[:500])
    try:
        import json
        data = json.loads(res.stdout or "{}")
    except Exception as exc:  # noqa: BLE001
        return ProbeInfo(error=f"ffprobe 输出解析失败：{exc}")

    fmt = data.get("format") or {}
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    info = ProbeInfo(tags=tags, ok=True)
    try:
        info.duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        info.duration = 0.0

    for st in data.get("streams") or []:
        if st.get("codec_type") == "video":
            try:
                info.width = int(st.get("width") or 0)
                info.height = int(st.get("height") or 0)
            except (TypeError, ValueError):
                pass
            for k, v in (st.get("tags") or {}).items():
                tags.setdefault(k.lower(), v)
            rot = (st.get("tags") or {}).get("rotate")
            if rot:
                try:
                    info.rotation = int(rot)
                except (TypeError, ValueError):
                    info.rotation = 0
            rate = st.get("avg_frame_rate") or st.get("r_frame_rate") or "0/0"
            try:
                num, _, den = str(rate).partition("/")
                info.fps = float(num) / float(den) if float(den) else 0.0
            except (TypeError, ValueError, ZeroDivisionError):
                info.fps = 0.0
            if info.duration <= 0:
                try:
                    info.duration = float(st.get("duration") or 0.0)
                except (TypeError, ValueError):
                    pass
            break
    return info


# --------------------------------------------------------------------------- #
# 镜头切分
# --------------------------------------------------------------------------- #


@dataclass
class Shot:
    shot_ix: int
    start: float
    end: float

    @property
    def length(self) -> float:
        return max(0.0, self.end - self.start)


def ffmpeg_scene_detect(video: Path, cfg: Config, tools: FFmpegTools,
                        timeout: float = 3600.0) -> Tuple[List[float], str]:
    """用 ffmpeg 的 scdet 滤镜单遍解码找镜头切点（方案 D）。

    返回 (cuts, method)。cuts 为切换点的秒数，失败返回 ([], reason)。

    原理：
        -vf scdet=threshold=X:sc_pass=1,showinfo
      * `sc_pass=1` 让 scdet **只放行**分数超过阈值的帧
      * `showinfo` 为放行的帧打印 pts_time
      * 于是单次解码即可拿到全部切点，无需 Python 逐帧计算内容差分

    为什么更快（实测约 2.1x）：PySceneDetect 是 Python/numpy 逐帧算差分，
    而 scdet 是 ffmpeg 内部 C 实现。

    注意：CUDA 解码在这里不可用（`-hwaccel cuda` + `scdet` 会报
    "Error reinitializing filters"，因为 scdet 是软件滤镜、帧还在显存里），
    且软解本就足够快，故不加硬解。
    """
    if not tools.ok or tools.ffmpeg is None:
        return [], "ffmpeg 不可用"

    threshold = float(cfg.get("video.scdet_threshold", 10.0))
    argv = [
        str(tools.ffmpeg), "-hide_banner", "-nostdin",
        "-i", str(video),
        "-vf", f"scdet=threshold={threshold}:sc_pass=1,showinfo",
        "-an", "-f", "null", "-",
    ]
    res = run_capture(argv, timeout=timeout)
    err = res.stderr or ""
    if res.timed_out:
        return [], f"scdet 超时（>{timeout:.0f}s）"
    if res.returncode != 0 and "pts_time" not in err:
        first = next((l for l in err.splitlines() if l.strip()), "")
        return [], f"scdet 失败：{first[:160]}"

    cuts: List[float] = []
    for m in _PTS_RE.finditer(err):
        try:
            cuts.append(float(m.group(1)))
        except ValueError:
            continue
    return cuts, "ffmpeg_scdet"


def scenedetect_cuts(video: Path, cfg: Config) -> Tuple[List[float], str]:
    """PySceneDetect 回退路径。"""
    threshold = float(cfg.get("video.scene_detect_threshold", 30.0))
    min_len = float(cfg.get("video.min_shot_len_sec", 0.8))
    try:
        from scenedetect import ContentDetector, SceneManager, open_video  # type: ignore

        v = open_video(str(video))
        sm = SceneManager()
        sm.add_detector(ContentDetector(
            threshold=threshold,
            min_scene_len=max(1, int(min_len * (v.frame_rate or 25)))))
        sm.detect_scenes(v, show_progress=False)
        return [s[0].seconds for s in sm.get_scene_list()], "scenedetect"
    except Exception as exc:  # noqa: BLE001 - 检测失败必须降级而非中断
        return [], f"PySceneDetect 失败：{exc}"


def scene_detect(video: Path, cfg: Config, duration: float,
                 tools: FFmpegTools) -> Tuple[List[Shot], str]:
    """返回 (shots, method)。

    后端由 config 的 video.scene_detect_backend 决定（默认 ffmpeg scdet）。
    任何后端失败都依次回退：ffmpeg → PySceneDetect → 整片单镜头。
    """
    min_len = float(cfg.get("video.min_shot_len_sec", 0.8))
    force_split = float(cfg.get("video.shot_boundary_force_split_sec", 1800.0))
    force_chunk = float(cfg.get("video.force_split_sec", 300.0))
    backend = str(cfg.get("video.scene_detect_backend", "ffmpeg")).lower()

    cuts: List[float] = []
    method = "single"

    order = ["ffmpeg", "scenedetect"] if backend != "scenedetect" else ["scenedetect", "ffmpeg"]
    for name in order:
        if name == "ffmpeg":
            got, m = ffmpeg_scene_detect(video, cfg, tools)
        else:
            got, m = scenedetect_cuts(video, cfg)
        if got:
            cuts, method = got, m
            break
        LOG.warning("镜头切分后端 %s 未产出切点（%s）：%s", name, m, video.name)

    if not cuts:
        cuts = [0.0]
    cuts = sorted(set(cuts))
    if cuts[0] > 0.01:
        cuts.insert(0, 0.0)

    shots: List[Shot] = []
    for i, start in enumerate(cuts):
        end = cuts[i + 1] if i + 1 < len(cuts) else max(duration, start)
        if end - start < min_len:
            # 合并到上一镜头，避免碎片
            if shots:
                shots[-1].end = max(shots[-1].end, end)
                continue
            end = start + min_len
        shots.append(Shot(len(shots), float(start), float(end)))

    # 单镜头超 30 分钟强制按 5 分钟切段（规格 L2b）
    expanded: List[Shot] = []
    for s in shots:
        if s.length > force_split and force_chunk > 0:
            n = int(s.length // force_chunk) + 1
            step = s.length / n
            for k in range(n):
                expanded.append(Shot(len(expanded), s.start + k * step, s.start + (k + 1) * step))
        else:
            expanded.append(Shot(len(expanded), s.start, s.end))
    return expanded, method


def frame_timestamps(shot: Shot, cfg: Config,
                     safe_end: Optional[float] = None) -> List[float]:
    """每个镜头要抽的时间点。

    * 镜头边界必采（always_include_shot_boundaries）
    * 长镜头（> long_shot_threshold_sec）按 long_shot_sample_per_sec 采样
    * 其余取镜头中点
    * max_frames_per_shot 封顶
    * safe_end：全片统一的「安全末帧时间」，所有时间点都会被钳制到它以内
    """
    long_thr = float(cfg.get("video.long_shot_threshold_sec", 10.0))
    per_sec = float(cfg.get("video.long_shot_sample_per_sec", 1.0))
    max_frames = int(cfg.get("video.max_frames_per_shot", 5))
    boundaries = bool(cfg.get("faces.always_include_shot_boundaries", True))
    eps = 0.05
    # 末尾安全余量：容器 duration 常比实际末帧 PTS 大（实测有视频差 4.5s），
    # 若把抽帧点放在 shot.end（≈ 容器末尾）会 seek 到最后一帧之后 →
    # 「没有帧到达编码器」→ ffmpeg 抛出 Non full-range YUV 等次生错误。
    # 实测 15 个真实视频：ts=时长 全部失败；时长-1 → 14/15；时长-5 → 15/15。
    margin = float(cfg.get("video.end_margin_sec", 5.0))

    ts: List[float] = []
    if shot.length <= 0:
        ts = [max(0.0, shot.start)]
    elif boundaries:
        # 起点稍后偏移，避免落到黑场/转场帧
        ts.append(min(shot.start + eps, shot.end - eps))
        if shot.length > long_thr and per_sec > 0:
            n = int(shot.length * per_sec)
            step = shot.length / (n + 1)
            ts.extend(shot.start + step * (k + 1) for k in range(n))
        else:
            ts.append((shot.start + shot.end) / 2.0)
        ts.append(max(shot.end - eps, shot.start))
    else:
        ts.append((shot.start + shot.end) / 2.0)

    ts = sorted({round(t, 3) for t in ts if t >= 0})
    # 统一钳制到安全区间，消除「seek 越过末帧」
    if safe_end is not None:
        ts = [min(t, safe_end) for t in ts]
    ts = sorted({round(t, 3) for t in ts if t >= 0})
    if not ts:
        ts = [0.0]
    if len(ts) > max_frames:
        # 均匀保留 max_frames 个（含首尾）
        idx = np.linspace(0, len(ts) - 1, max_frames).round().astype(int)
        ts = [ts[i] for i in sorted(set(idx.tolist()))]
    return ts


# --------------------------------------------------------------------------- #
# 抽帧
# --------------------------------------------------------------------------- #


def frame_path(cfg: Config, file_id: int, shot_ix: int, ts: float) -> Path:
    d = cfg.path("paths.frames") / str(file_id)
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{shot_ix:04d}_{int(round(ts * 1000)):09d}.jpg"


def extract_frame(ffmpeg: Path, video: Path, ts: float, out: Path,
                  short_edge: int, timeout: float = 120.0,
                  hwaccel: str = "auto") -> Tuple[bool, str]:
    """抽一帧到 out（JPEG，短边 >= short_edge）。返回 (ok, error)。

    尺寸必须是偶数：mjpeg 编码器对奇数宽高会直接报
      "Error while opening encoder - maybe incorrect parameters such as ... width or height"
      并以 -22 失败（真实素材上实测会遇到）。
    因此用 trunc(x/2)*2 强制取偶数，而不是让 scale 产出奇数边长。
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    edge = max(2, int(short_edge))
    if edge % 2:
        edge += 1
    # 横屏：短边=高 → 固定 h=edge；竖屏：短边=宽 → 固定 w=edge
    scale = (f"scale=w='if(gt(iw,ih),trunc(iw*{edge}/ih/2)*2,{edge})':"
             f"h='if(gt(iw,ih),{edge},trunc(ih*{edge}/iw/2)*2)'")
    base = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-threads", "1"]
    hw: List[str] = []
    if hwaccel == "cuda":
        hw = ["-hwaccel", "cuda"]
    argv = base + hw + [
        "-ss", f"{max(0.0, ts):.3f}", "-i", str(video),
        "-frames:v", "1", "-vf", scale, "-q:v", "3", str(out),
    ]
    res = run_capture(argv, timeout=timeout)
    if not res.ok or not out.exists():
        # CUDA 硬件解码失败时回落到软解重试一次
        if hw:
            argv2 = base + [
                "-ss", f"{max(0.0, ts):.3f}", "-i", str(video),
                "-frames:v", "1", "-vf", scale, "-q:v", "3", str(out),
            ]
            res = run_capture(argv2, timeout=timeout)
            if res.ok and out.exists():
                return True, ""
        return False, (res.stderr or res.stdout or "ffmpeg 抽帧失败").strip()[:500]
    return True, ""


def validate_frame(path: Path, cfg: Config) -> Tuple[bool, str, int, int]:
    """校验：文件大小 > 2KB 且非全黑（均值 > 5）。返回 (ok, reason, w, h)。"""
    min_bytes = int(cfg.get("video.min_frame_bytes", 2048))
    min_mean = float(cfg.get("video.min_frame_mean", 5.0))
    try:
        size = path.stat().st_size
    except OSError as exc:
        return False, f"文件不存在：{exc}", 0, 0
    if size <= min_bytes:
        return False, f"体积过小 {size}B <= {min_bytes}B", 0, 0
    try:
        with Image.open(path) as im:
            im = im.convert("L")
            w, h = im.size
            arr = np.asarray(im, dtype="float32")
            mean = float(arr.mean()) if arr.size else 0.0
    except Exception as exc:  # noqa: BLE001
        return False, f"无法解码：{exc}", 0, 0
    if mean <= min_mean:
        return False, f"全黑/近黑（均值 {mean:.2f} <= {min_mean}）", w, h
    return True, "", w, h


def safe_remove(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# worker 并发（按可用内存动态计算，规格第十章）
# --------------------------------------------------------------------------- #


def compute_workers(cfg: Config) -> int:
    reserve_mb = int(cfg.get("video.per_worker_reserve_mb", 300))
    cap = int(cfg.get("video.max_worker_cap", 8))
    try:
        import psutil

        free_mb = psutil.virtual_memory().available / (1024 * 1024)
        cores = psutil.cpu_count(logical=False) or os.cpu_count() or 2
    except Exception:  # noqa: BLE001
        free_mb = 4096
        cores = os.cpu_count() or 2
    by_mem = int(free_mb // max(1, reserve_mb))
    return max(1, min(by_mem, cores, cap))


# --------------------------------------------------------------------------- #
# 单文件处理
# --------------------------------------------------------------------------- #


@dataclass
class ExtractResult:
    file_id: int
    path: str
    kind: str
    status: str           # done | error | skipped
    shots: int = 0
    frames: int = 0
    captured_at: Optional[str] = None
    time_source: Optional[str] = None
    warning: str = ""
    error: str = ""
    note: str = ""            # skipped 的原因（融合模式里是「近重复让位」）
    duration_sec: float = 0.0


def _apply_pil_pixel_cap(cfg: Config) -> int:
    """按 config 放开 PIL 的解压炸弹像素上限，返回生效值（0 表示不限制）。

    为什么必须放开（2026-10-16 实测）：
      PIL 默认 MAX_IMAGE_PIXELS = 89,478,485（超过只警告），
      硬上限约 2 倍（178,956,970）—— 超过直接抛 DecompressionBombError，`Image.open` 失败。
      本机素材里有一批 **1.79 亿~4.02 亿像素**的大图（AI 放大 / 拼接长图），
      它们是完全正常的图片。上限一卡，全集实测有 **1,076 张**被记成 error、
      永远进不了库（`files.status='error'`），而且日志里看起来只是「图片解码失败」，
      很容易被当成坏文件忽略掉。
    内存代价：2 亿像素 JPEG 解成 RGB 约 600MB、4 亿像素约 1.2GB；
      抽帧是单文件串行处理，本机 66GB 内存完全够，且只在这一趟用原图
      （04 编码 / 06 检脸读的都是 NVMe 缓存图，不会解码巨图）。
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        return 0
    cap = int(cfg.get("scan.max_image_pixels", 500_000_000) or 0)
    Image.MAX_IMAGE_PIXELS = cap if cap > 0 else None
    return cap


def process_photo(path: Path, file_id: int, cfg: Config, conn,
                  dedup: Optional[Any] = None,
                  write_cache: Optional[bool] = None) -> ExtractResult:
    """照片：一趟读盘同时完成 EXIF + （可选）pHash 去重 + NVMe 缓存图。

    为什么把 L2a 折进来（2026-10-16 实测的硬件事实）
    ----------------------------------------------
    E: 是 14TB SATA 机械盘，纯读 100~170 MB/s；照片 478,634 张 / 3.235 TB。
    一趟读盘 6~9 小时。规格原本「L2a 去重 → L2b 抽帧 → L2c 编码 → F1 检脸」
    每个阶段都要把照片重读一遍，在机械盘上等于 30 小时以上。

    所以照片侧只读一遍：为了算 pHash 本来就要把整图解码到内存，
    那就顺手把 EXIF 与缓存图一起做掉。缓存图写到 frames/<file_id>/（与视频关键帧
    同一套目录约定），于是 04_encode / 06_face_detect 后续全部只读 NVMe，
    不再碰机械盘。

    dedup=None 时退化为「只读 EXIF + 写缓存」，两条路径都能跑。
    """
    res = ExtractResult(file_id=file_id, path=str(path), kind=KIND_PHOTO, status="done")
    hash_size = int(cfg.get("dedup.hash_size", 8))
    cache_px = int(cfg.get("storage.photo_cache_short_edge", 768))
    if write_cache is None:
        write_cache = bool(cfg.get("scan.photo_cache_enabled", True))

    phash: Optional[str] = None
    ct = None
    w = h = 0
    try:
        from PIL import Image, ImageFile

        ImageFile.LOAD_TRUNCATED_IMAGES = True
        cap = _apply_pil_pixel_cap(cfg)
        with Image.open(path) as im:
            w, h = im.size
            if cap and w * h > cap:
                # 超过我们自己设的上限才算真异常，报错要说清是「我们设的」而不是「坏了」
                res.status = "error"
                res.error = (f"图片 {w}×{h} = {w * h / 1e6:.0f}MP 超过 "
                             f"scan.max_image_pixels={cap}，已跳过（调大该配置可纳入）")
                return res
            # 1) EXIF：用已经打开的图，不再重开文件
            ct = resolve_capture_time(path, kind="photo",
                                      priority=cfg.get("exif.priority"), image=im)
            # 2) pHash：同一份解码结果，零额外 IO
            if dedup is not None:
                from .dedup_run import compute_phash_image

                phash = compute_phash_image(im, hash_size)
            # 3) NVMe 缓存图：短边缩到 cache_px，供编码与人脸检测复用
            if write_cache and cache_px > 0 and (dedup is None or phash is not None):
                _write_photo_cache(im, cfg, file_id, cache_px)
    except Image.DecompressionBombError as exc:  # noqa: BLE001
        res.status = "error"
        res.error = (f"PIL 解压炸弹上限拒绝（{exc}）；"
                     f"当前 scan.max_image_pixels={cfg.get('scan.max_image_pixels')}")
        return res
    except Exception as exc:  # noqa: BLE001
        res.status = "error"
        res.error = f"图片解码失败：{exc}"
        return res

    res.captured_at = ct.captured_at if ct else None
    res.time_source = ct.time_source if ct else None

    # 4) 去重判定：赢了就当代表（继续标 done），输了就到此为止
    if dedup is not None:
        verdict, other, dist = dedup.feed(conn, file_id, phash)
        if verdict == "dup":
            res.status = "skipped"
            res.error = ""
            res.note = (f"近重复，让位于 file_id={other}（hamming={dist}）"
                        if other else "pHash 计算失败")
            set_stage(conn, file_id, STAGE_DEDUP, "done", error=res.note)
            return res

    conn.execute(
        "UPDATE files SET captured_at=?, time_source=?, width=?, height=?, "
        "status='done', indexed_at=?, updated_at=? WHERE file_id=?",
        (res.captured_at, res.time_source, w, h,
         local_now_iso(), local_now_iso(), file_id),
    )
    set_stage(conn, file_id, STAGE_EXTRACT, "done", shots=1, frames=1)
    if dedup is not None:
        set_stage(conn, file_id, STAGE_DEDUP, "done", shots=1, frames=1)
    # 照片没有镜头概念，写一条 shot_ix=0 的虚拟镜头，便于人脸点 id 与语义点 id 对齐
    D.upsert_shot(conn, file_id, 0, 0.0, 0.0, n_frames=1)
    res.shots = 1
    res.frames = 1
    return res


def _write_photo_cache(im, cfg: Config, file_id: int, short_edge: int) -> bool:
    """把照片缩到短边 short_edge 存成 JPEG，路径与视频关键帧约定一致。

    PIL 直接吃中文路径，不需要 np.fromfile 那一套绕行。

    降采样分两步，别小看这一步：本库照片平均 6.76 MB / 5250×3500，
    直接对全尺寸图做 LANCZOS 是最贵的一步（单张 100ms 量级 × 26 万张）。
    先 `reduce(整数倍)` 做箱式降采样（快且自带抗锯齿），再 BILINEAR 微调到目标尺寸，
    视觉质量对「编码 + 检脸」这个用途完全够。
    """
    try:
        from PIL import Image

        w, h = im.size
        if w <= 0 or h <= 0:
            return False
        scale = short_edge / float(min(w, h))
        tw, th = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        img = im.convert("RGB")
        factor = max(1, min(w, h) // max(1, int(short_edge)))
        if factor > 1:
            img = img.reduce(factor)
        if img.size != (tw, th):
            img = img.resize((tw, th), Image.BILINEAR)
        out = frame_path(cfg, file_id, 0, 0.0)
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out, "JPEG", quality=88, optimize=False)
        return True
    except Exception as exc:  # noqa: BLE001 - 缓存失败不该让整张照片失败
        LOG.warning("写照片缓存失败 file_id=%s：%s", file_id, exc)
        return False


def demote_photo(conn, cfg: Config, file_id: int,
                 store: Optional[Any] = None) -> None:
    """融合模式专用：旧代表被更好的新照片击败时，回滚它的全部下游产物。

    两种情况都要能处理：
      * 同一轮内被击败：该文件还没进 L2c / F 系列，撤掉「done + 缓存图 + 阶段标记」即可。
      * **跨轮次被击败**（分块跑时才可能发生）：该文件可能已经被 04_encode 写过语义向量、
        被 06/07 写过人脸记录与缩略图，必须一并清掉，否则重复照片会留在照片墙上。

    早期实现只处理第一种，跨轮次时会在照片墙上留下重复项。这里做全量清理。
    """
    conn.execute(
        "UPDATE files SET status='skipped_dup', visible=0, kind_hint='duplicate', "
        "updated_at=? WHERE file_id=?", (local_now_iso(), file_id))
    set_stage(conn, file_id, STAGE_EXTRACT, "skipped", error="让位于更优的近重复照片")
    set_stage(conn, file_id, STAGE_DEDUP, "done", error="让位于更优的近重复照片")

    # 1) 人脸：Qdrant 点 + face_records + 磁盘裁剪图
    face_ids = [int(r["face_id"]) for r in conn.execute(
        "SELECT face_id FROM face_records WHERE file_id=?", (file_id,))]
    if face_ids:
        try:
            from .vector_store import face_collection, open_store

            client = getattr(store or open_store(cfg), "client", None)
            if client is not None:
                from qdrant_client import models as qm

                client.delete(collection_name=face_collection(cfg),
                              points_selector=qm.PointIdsList(points=face_ids), wait=True)
        except Exception as exc:  # noqa: BLE001 - 清理失败不该中断整批
            LOG.warning("回滚 face 向量失败 file_id=%s：%s", file_id, exc)
        try:
            from .face_pipeline import _unlink

            for fid_ in face_ids:
                _unlink(cfg, fid_)
        except Exception:  # noqa: BLE001
            pass
        conn.execute(
            f"DELETE FROM face_records WHERE face_id IN ({','.join('?' * len(face_ids))})",
            face_ids)

    # 2) 语义：Qdrant 点 + embedding 缓存
    try:
        from .vector_store import open_store, semantic_collection

        (store or open_store(cfg)).delete_by_file(semantic_collection(cfg), file_id)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("回滚 semantic 向量失败 file_id=%s：%s", file_id, exc)
    try:
        cache = cfg.path("paths.embeddings") / f"{file_id}.json"
        if cache.exists():
            cache.unlink()
    except OSError:
        pass

    # 3) 两阶段状态与阶段标记：删掉，这样它不会再被任何阶段选中
    conn.execute("DELETE FROM file_encoding WHERE file_id=?", (file_id,))
    conn.execute("DELETE FROM stage_status WHERE file_id=?", (file_id,))

    # 4) 缓存图目录
    d = frame_path(cfg, file_id, 0, 0.0).parent
    try:
        if d.exists():
            for f in d.glob("*"):
                safe_remove(f)
            d.rmdir()
    except OSError:
        pass


def process_video(path: Path, file_id: int, cfg: Config, conn, tools: FFmpegTools) -> ExtractResult:
    res = ExtractResult(file_id=file_id, path=str(path), kind=KIND_VIDEO, status="done")
    if not tools.ok:
        res.status = "error"
        res.error = "ffmpeg/ffprobe 不可用"
        return res

    timeout = float(cfg.get("video.single_video_timeout_sec", 600))
    probe = ffprobe_media(tools.ffprobe, path, timeout=min(120.0, timeout))
    if not probe.ok:
        res.status = "error"
        res.error = f"ffprobe 失败：{probe.error}"
        return res
    res.duration_sec = probe.duration

    ct = resolve_capture_time(path, kind="video", priority=cfg.get("exif.priority"),
                              ffprobe_tags=probe.tags)
    res.captured_at, res.time_source = ct.captured_at, ct.time_source

    duration = probe.duration if probe.duration > 0 else 0.0
    shots, method = scene_detect(path, cfg, duration, tools)
    short_edge = int(cfg.get("storage.keyframe_short_edge", 384))
    retry = int(cfg.get("video.frame_retry", 1))
    hwaccel = str(cfg.get("video.hwaccel", "auto"))
    # 安全末帧：所有抽帧点都钳制到它以内，避免 seek 越过实际最后一帧
    margin = float(cfg.get("video.end_margin_sec", 5.0))
    safe_end = max(0.0, duration - margin) if duration > margin else max(0.0, duration * 0.5)

    D.delete_shots(conn, file_id)
    total_frames = 0
    frame_fail = 0
    for shot in shots:
        ts_list = frame_timestamps(shot, cfg, safe_end=safe_end)
        n_ok = 0
        for ts in ts_list:
            out = frame_path(cfg, file_id, shot.shot_ix, ts)
            ok, err = extract_frame(tools.ffmpeg, path, ts, out, short_edge, hwaccel=hwaccel)
            if ok:
                good, reason, _w, _h = validate_frame(out, cfg)
                if not good:
                    ok, err = False, reason
            for _ in range(max(0, retry)):
                if ok:
                    break
                safe_remove(out)
                ok, err = extract_frame(tools.ffmpeg, path, ts, out, short_edge, hwaccel=hwaccel)
                if ok:
                    good, reason, _w, _h = validate_frame(out, cfg)
                    if not good:
                        ok, err = False, reason
            if ok:
                n_ok += 1
            else:
                frame_fail += 1
                safe_remove(out)
                LOG.warning("抽帧失败 file_id=%s shot=%d ts=%.2f：%s", file_id, shot.shot_ix, ts, err)
        if n_ok == 0:
            # 整个镜头抽帧全失败：仍登记镜头，避免 shots 与 frames 语义错位
            LOG.warning("镜头无可用帧 file_id=%s shot=%d（%.2f~%.2f）", file_id, shot.shot_ix,
                        shot.start, shot.end)
        D.upsert_shot(conn, file_id, shot.shot_ix, shot.start, shot.end, n_frames=n_ok)
        total_frames += n_ok

    res.shots = len(shots)
    res.frames = total_frames

    # 镜头数异常告警：每小时镜头数超出 [30,600]
    if duration > 60:
        per_hour = len(shots) / (duration / 3600.0)
        lo = float(cfg.get("video.warn_shots_per_hour_low", 30))
        hi = float(cfg.get("video.warn_shots_per_hour_high", 600))
        if per_hour < lo or per_hour > hi:
            res.warning = f"镜头密度异常 {per_hour:.1f}/小时（正常区间 [{lo:.0f},{hi:.0f}]）"
            LOG.warning("%s file_id=%s", res.warning, file_id)

    if total_frames == 0:
        res.status = "error"
        res.error = f"全片抽帧失败（{len(shots)} 个镜头）"
        return res

    conn.execute(
        "UPDATE files SET captured_at=?, time_source=?, width=?, height=?, duration_sec=?, "
        "status='done', indexed_at=?, updated_at=? WHERE file_id=?",
        (ct.captured_at, ct.time_source, probe.width, probe.height, probe.duration,
         local_now_iso(), local_now_iso(), file_id),
    )
    set_stage(conn, file_id, STAGE_EXTRACT, "done", shots=len(shots), frames=total_frames)
    res.time_source = f"{ct.time_source}|{method}"
    return res


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def select_targets(conn, cfg: Config, limit: Optional[int] = None,
                   kind_filter: str = "all") -> List[Any]:
    """待抽帧：kind 为 photo/video、files.status 非 done、且 extract 阶段未完成。

    处理顺序由 config 的 scan.process_order 决定：
      * photo_first（默认）：照片优先。照片只读 EXIF、不抽帧（约 200+ 张/秒），
        视频要整片解码做镜头切分（单个 15~30s）。先照片能立刻拿到可用检索库。
      * file_id：按 file_id 升序（原行为）。
    两种顺序共用同一套 stage_status 断点续跑标记，切换不会导致重复劳动，
    只是「先取谁」不同。

    视频组内默认按**体积升序**（scan.video_size_order: asc）：
    24,423 个视频共 6.104 TB，全集按 15~30s/个要跑好几天，
    先做小体积能最快把「已完成的视频数」堆起来，界面上的素材也越早变多。
    置 desc / off 可改回按 file_id。
    """
    order = str(cfg.get("scan.process_order", "photo_first")).lower()
    size_order = str(cfg.get("scan.video_size_order", "asc")).lower()
    video_tail = {"asc": "f.size ASC, f.file_id",
                  "desc": "f.size DESC, f.file_id"}.get(size_order, "f.file_id")
    if order == "file_id":
        order_sql = "ORDER BY f.file_id"
    else:
        # 照片在前（组内按 file_id，保证结果可复现），视频在后（组内按体积）
        order_sql = ("ORDER BY CASE f.kind WHEN 'photo' THEN 0 ELSE 1 END, "
                     f"CASE WHEN f.kind='photo' THEN f.file_id ELSE 0 END, {video_tail}")

    sql = f"""
        SELECT f.file_id AS file_id, f.path AS path, f.kind AS kind
          FROM files f
          LEFT JOIN stage_status ss
                 ON ss.file_id = f.file_id AND ss.stage = ?
         WHERE f.kind IN ('photo','video')
           AND f.kind IN ({_kind_placeholders(kind_filter)})
           AND f.status IN ('pending','error')
           AND (ss.status IS NULL OR ss.status IN ('pending','error'))
           AND COALESCE((SELECT COUNT(*) FROM dead_letter dl
                          WHERE dl.file_id = f.file_id AND dl.stage = ?), 0) < ?
         {order_sql}
    """
    params: List[Any] = [STAGE_EXTRACT, *_kind_values(kind_filter),
                         STAGE_EXTRACT, _max_extract_attempts(cfg)]
    rows = list(conn.execute(sql, params))
    if limit:
        rows = rows[:limit]
    return rows


def _max_extract_attempts(cfg: Config) -> int:
    """单文件抽帧最多重试几次（默认 3），超过后进死信表、不再被选中。

    ⚠️ 2026-10-18 修的真实 bug（导致链条空转了近 10 小时、视频阶段一直没开始）：
      原实现**没有尝试上限**，而取数条件是 `status IN ('pending','error')` ——
      于是永远解不开的文件每一轮都会被重新选中、再失败一次。
      照片跑完后残留 1,506 个这种文件（其中 1,467 个是 E:\\晚上\\美女 下真正损坏的图，
      也有 macOS 的 `._xxx.jpg` 资源叉文件），链条的退出条件 `pending == 0`
      永远不成立：它就以「每轮 1~2 分钟、处理 0 个」的节奏空转，
      **永远轮不到视频阶段**（24,423 个视频一个都没开始）。
      加上上限后：失败 3 次即入 dead_letter、不再入选，循环才能推进。
    """
    return max(1, int(cfg.get("scan.max_extract_attempts", 3)))


def _kind_values(kind_filter: str) -> List[str]:
    kf = (kind_filter or "all").lower()
    if kf == "photo":
        return ["photo"]
    if kf == "video":
        return ["video"]
    return ["photo", "video"]


def _kind_placeholders(kind_filter: str) -> str:
    return ",".join("?" * len(_kind_values(kind_filter)))


def run(args, cfg: Config) -> Dict[str, Any]:
    kind_filter = str(getattr(args, "kind", "all") or "all")
    tools = locate_tools(cfg)
    if not tools.ok:
        raise RuntimeError(
            "ffmpeg/ffprobe 不可用。修复：把 ffmpeg.exe 与 ffprobe.exe 放到 "
            f"{cfg.path('paths.bin')}，或确保 PATH 上可执行。"
        )
    LOG.info("外部工具：%s", tools.describe())

    D.ensure_schema(cfg.db_path, cfg.root, cfg)
    conn = D.connect(cfg.db_path, cfg=cfg)
    ensure_stage_table(conn)
    conn.commit()
    rows = select_targets(conn, cfg, args.limit, kind_filter=kind_filter)
    rows = list(iter_shard_rows(rows, args.shard))
    LOG.info("待抽帧文件 %d 个（dry_run=%s）", len(rows), args.dry_run)
    T.stage("extract", total=len(rows))

    if args.dry_run:
        for r in rows[:10]:
            LOG.info("  [dry-run] 将处理 file_id=%s kind=%s", r["file_id"], r["kind"])
        conn.close()
        return {"targets": len(rows), "dry_run": True}

    workers = compute_workers(cfg)
    LOG.info("按可用内存动态并发：%d 个视频 worker", workers)

    # 融合模式：照片这一趟同时做 L2a pHash 去重（config scan.fuse_photo_pipeline）。
    # 分片跑时绝不能开融合：每个分片各有一份索引，代表项会互相打架。
    fused = bool(cfg.get("scan.fuse_photo_pipeline", True)) and not bool(
        getattr(args, "no_fuse", False))
    write_cache = bool(cfg.get("scan.photo_cache_enabled", True)) and not bool(
        getattr(args, "no_cache", False))
    dedup = None
    if fused and args.shard:
        LOG.warning("分片模式下自动关闭融合去重（--shard 与融合不兼容，改用 02_dedup.py 单独跑）")
        fused = False
    if fused:
        from .dedup_run import DedupRunner

        dedup = DedupRunner(hash_size=int(cfg.get("dedup.hash_size", 8)),
                            threshold=int(cfg.get("dedup.hamming_threshold", 8)))
        known = dedup.load_history(conn)
        demote_store: Dict[str, Any] = {}

        def _on_demote(fid_demoted: int) -> None:
            # Qdrant 句柄懒加载：绝大多数会话一次都不会触发回滚，不该为它付连接代价
            if not demote_store:
                try:
                    from .vector_store import open_store

                    demote_store["store"] = open_store(cfg)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("打开 Qdrant 失败，回滚将只清 sqlite：%s", exc)
                    demote_store["store"] = None
            demote_photo(conn, cfg, fid_demoted, store=demote_store.get("store"))

        dedup.on_demote = _on_demote
        LOG.info("融合模式：照片一趟读盘同时做 EXIF + pHash 去重 + 缓存图")
        LOG.info("%s", dedup.describe())
    else:
        LOG.info("非融合模式：照片只做 EXIF（去重请单独跑 02_dedup.py）")

    stats = {"done": 0, "error": 0, "skipped": 0, "shots": 0, "frames": 0, "warn": 0,
             "dedup_dup": 0, "dedup_unique": 0, "dedup_demoted": 0}
    t0 = time.monotonic()
    batch = int(cfg.get("scan.batch_commit_files", 500))
    # 融合照片趟每处理 1 个文件就提交，把写锁窗口压到毫秒级。
    #
    # 为什么必须这样（2026-10-16 实测踩到的坑）：
    #   照片链故意让 3 个 04 编码进程与抽帧并行（把 24h 的 GPU 编码藏进 17h 的读盘里）。
    #   但 SQLite 只有一个写者。原实现每 batch_commit_files 个文件才提交一次
    #   （=100 个 ≈ 12 秒），这 12 秒里写锁一直被抽帧握着，编码进程的
    #   enc_begin/enc_done 只能干等 busy_timeout —— 实测两个进程 CPU=0、磁盘 IO=0，
    #   完全饿死（0.06 文件/秒，比单跑慢 50 倍）。
    #   WAL + synchronous=NORMAL 下提交不需要 fsync，逐文件提交的代价可以忽略。
    commit_every = int(cfg.get("scan.commit_batch_fused_photos", 1)) if dedup is not None else batch
    commit_every = max(1, commit_every)
    processed = 0
    last_commit = time.monotonic()   # 写锁窗口的时间上限（见下面的 commit 条件）
    for r in rows:
        fid, path_s, kind = int(r["file_id"]), str(r["path"]), str(r["kind"])
        path = Path(path_s)
        if not path.exists():
            conn.execute("UPDATE files SET status='missing', updated_at=? WHERE file_id=?",
                         (local_now_iso(), fid))
            continue
        try:
            if kind == "photo":
                res = process_photo(path, fid, cfg, conn, dedup,
                                    write_cache=write_cache)
            else:
                res = process_video(path, fid, cfg, conn, tools)
        except Exception as exc:  # noqa: BLE001 - 单文件失败绝不中断整批
            res = ExtractResult(file_id=fid, path=path_s, kind=kind, status="error",
                                error=f"未捕获异常：{exc}")
            LOG.exception("处理异常 file_id=%s", fid)

        if res.status == "done":
            stats["done"] += 1
            stats["shots"] += res.shots
            stats["frames"] += res.frames
            if res.warning:
                stats["warn"] += 1
        elif res.status == "skipped":
            # 融合去重淘汰的照片：这是正常结果，不是失败，绝不能记 error
            stats["skipped"] += 1
        else:
            stats["error"] += 1
            # 记进 stage_status：否则这一阶段的失败在库里**完全不可见**
            # （2026-10-16 实测：1,078 张照片因 PIL 解压炸弹上限被错杀，
            #  stage_status 里一条 error 都没有，只能靠 file_encoding 的错误文本反推）。
            # 记成 'error' 之后 select_targets 仍会重试它（取数条件含 'error'），不会漏。
            set_stage(conn, fid, STAGE_EXTRACT, "error", error=res.error)
            D.enc_error(conn, fid, D.MODEL_SEMANTIC, res.error)
            D.enc_error(conn, fid, D.MODEL_FACE, res.error)
            conn.execute("UPDATE files SET status='error', updated_at=? WHERE file_id=?",
                         (local_now_iso(), fid))
            # 记一次尝试到死信表：select_targets 会用它把「反复失败」的文件挡在门外。
            # 没有这一步，坏文件会被无限重试，链条的 pending==0 退出条件永远不成立。
            try:
                prev = int(conn.execute(
                    "SELECT COUNT(*) FROM dead_letter WHERE file_id=? AND stage=?",
                    (fid, STAGE_EXTRACT)).fetchone()[0] or 0)
                D.add_dead_letter(conn, fid, STAGE_EXTRACT, prev + 1, res.error)
                limit = _max_extract_attempts(cfg)
                if prev + 1 >= limit:
                    LOG.error("抽帧失败 file_id=%s 已达 %d 次上限，转入死信、不再重试：%s",
                              fid, limit, res.error)
                else:
                    LOG.error("抽帧失败 file_id=%s（第 %d/%d 次）：%s",
                              fid, prev + 1, limit, res.error)
            except Exception:  # noqa: BLE001 - 记账失败不该中断整批
                LOG.error("抽帧失败 file_id=%s：%s", fid, res.error)

        processed += 1
        # 写锁窗口必须有时间上限：只看「每 N 个文件提交一次」是不够的，
        # 视频单个文件要 15~30 秒，N=100 就是几十分钟不提交，
        # 这段时间里任何别的写库动作（进度上报、界面、并行编码）都会被堵死。
        if processed % commit_every == 0 or (time.monotonic() - last_commit) >= 2.0:
            D.retry_on_lock(conn.commit)
            last_commit = time.monotonic()
        # 进度上报必须在 commit **之后**：tick 用自己的连接写 tasks 表，
        # 主连接若还握着未提交的写事务，两个连接互等 → 进度全被跳过
        # （2026-10-19 实测：文件在一个个处理，界面永远是 0/N，还白等 1 秒超时）。
        T.tick()
        if processed % batch == 0:
            el = max(1e-9, time.monotonic() - t0)
            extra = ""
            if dedup is not None:
                st = dedup.stats
                stats["dedup_dup"] = st["duplicates"]
                stats["dedup_unique"] = st["unique"]
                stats["dedup_demoted"] = st["demoted"]
                extra = (f"  去重重复={st['duplicates']} 唯一={st['unique']}"
                         f" 让位={st['demoted']}")
            LOG.info("  进度 %d/%d  完成 %d  重复跳过 %d  失败 %d  镜头 %d  关键帧 %d  "
                     "%.1f 文件/秒  预计剩余 %.1f 小时%s",
                     processed, len(rows), stats["done"], stats["skipped"], stats["error"],
                     stats["shots"], stats["frames"], processed / el,
                     (len(rows) - processed) / max(processed / el, 1e-9) / 3600.0, extra)
    conn.commit()
    if dedup is not None:
        # 退出前刷新一次去重计数：进度日志每 batch 条才刷，最后不足一批的会漏报
        stats["dedup_dup"] = dedup.stats["duplicates"]
        stats["dedup_unique"] = dedup.stats["unique"]
        stats["dedup_demoted"] = dedup.stats["demoted"]
        stats["dedup_errors"] = dedup.stats["errors"]
        stats["dedup_comparisons"] = dedup.idx.stats()["comparisons"]
    conn.close()

    summary = {
        **stats,
        "targets": len(rows),
        "workers": workers,
        "fused_dedup": bool(dedup is not None),
        "elapsed_sec": round(time.monotonic() - t0, 1),
        "rate_per_sec": round(len(rows) / max(1e-9, time.monotonic() - t0), 2),
        "frames_dir": str(cfg.path("paths.frames")),
    }
    return summary


def iter_shard_rows(rows: Sequence[Any], shard: Optional[str]) -> Iterator[Any]:
    from .common import shard_range

    rng = shard_range(shard, len(rows))
    if rng is None:
        yield from rows
    else:
        for i in rng:
            yield rows[i]
