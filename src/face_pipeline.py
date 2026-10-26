"""B 人脸管线 F1–F3（规格第六章，A 方案 · 稠密预计算）。

阶段划分（与规格第十章脚本命名一致）：
    F1 检测与对齐   SCRFD det_10g（det_size 512, det_thresh 0.5, nms 0.4）
    F2 shot 内 track 去重 + 质量评分 + 代表帧优选
    F3 ArcFace 512 维编码 + 同步写 faces/thumbs/<face_id>.webp + Qdrant upsert

两个根脚本把 F1/F2 与 F3 分开，因为两者成本结构完全不同：
    06_face_detect.py  F1+F2 → 写 face_records(has_vector=0) + faces/raw/<face_id>.webp
    07_face_encode.py  F3    → 读 raw 图算 ArcFace，写 faces/thumbs + Qdrant，置 has_vector=1

为什么 06 要把对齐后的 112×112 人脸落盘（faces/raw）：
    这样 07 不必重新解码视频/原图，也不必重跑检测 —— 断点续跑的成本从天级降到分钟级。
    migrations/001_init.sql 里 faces_raw 的注释就是「人脸 112x112 【永不淘汰】」，
    本模块是它唯一的产出方。

铁律遵守：
    1) 去重跳过的文件由 02_dedup.py 写 face:skipped_dup（本模块不重复写）
    2) 文件删除只标 missing；本模块绝不 DELETE files 行
    3) 两阶段入库：enc_begin(pending) → 写 Qdrant → enc_done(done + encoded_at)

⚠️ 与规格的两处有意偏离（已向用户汇报）：
    a) F0 稠密采样（sample_fps=1.0 / max_per_shot=60）未启用。F1 直接复用 L2b（03_extract.py）
       已抽出的关键帧（每镜头 ≤ video.max_frames_per_shot 张），符合规格
       「F1 直接读 L2b 产物，绝不重新解码视频」这条硬约束，但采样密度低于 F0 的纸面值。
       原因：全集 2.4 万视频按 1fps 稠密抽帧需再解码数万小时，且会把 NVMe 预算（300GB）挤爆。
       照片侧不受影响（照片本来就只有一帧）。
       需要真正稠密采样时，把 config 的 video.max_frames_per_shot /
       video.long_shot_sample_per_sec 调大重跑 03_extract.py 即可，本模块无需改动。
    b) 人脸 collection 的 point_id 就是 face_id（不是 (file_id<<20)|shot_ix）。
       原因：同一 shot 内可能有多张人脸，而 shot_ix 已占满 point_id 的低 20 位，
       无法再编码人脸序号。src/search.py 的 _fetch_face_vector 与 src/ui_wall.py 的
       概览去重都已按「point_id == face_id」实现，payload 里另带 file_id + shot_ix，
       「回退到同一文件+时间段」的能力并未丢失。
"""

from __future__ import annotations

import datetime as _dt
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import db as D
from . import tasks as T
from .common import Config, get_logger, local_now_iso
from .extract import STAGE_EXTRACT, ensure_stage_table, set_stage
from .face_search import DetectedFace, FaceEngine, get_engine
from .vector_store import VectorStore, face_collection, open_store

LOG = get_logger("face_pipeline")

STAGE_FACE_DETECT = "face_detect"
STAGE_FACE_ENCODE = "face_encode"

# 单文件人脸数上限（防御性）：单张图最多保留这么多张代表脸，避免海报/合影把库撑爆
MAX_FACES_PER_FILE = 400


# --------------------------------------------------------------------------- #
# 图 IO（Windows 中文路径：cv2.imread/imwrite 会静默失败）
# --------------------------------------------------------------------------- #


def imread_bgr(path: Path) -> Optional[np.ndarray]:
    """读图成 BGR。cv2 读不了的格式（HEIC/HEIF）回退到 PIL。"""
    import cv2

    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
        if buf.size == 0:
            return None
        img = cv2.imdecode(buf, cv2.IMREAD_COLOR)
        if img is not None:
            return img
    except Exception as exc:  # noqa: BLE001
        LOG.debug("cv2 读图失败 %s：%s", path, exc)

    # 回退：OpenCV 不支持 HEIC/HEIF，但注册过 pillow-heif 之后 PIL 可以
    try:
        from PIL import Image

        with Image.open(path) as im:
            rgb = np.asarray(im.convert("RGB"))
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    except Exception as exc:  # noqa: BLE001
        LOG.debug("PIL 读图也失败 %s：%s", path, exc)
        return None


def imwrite_unicode(path: Path, img: np.ndarray, ext: str = ".webp",
                    quality: int = 80) -> bool:
    import cv2

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        params: List[int] = []
        low = ext.lower()
        if low in (".jpg", ".jpeg"):
            params = [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)]
        elif low == ".webp":
            params = [int(cv2.IMWRITE_WEBP_QUALITY), int(quality)]
        ok, buf = cv2.imencode(ext, img, params)
        if not ok:
            return False
        buf.tofile(str(path))
        return True
    except Exception as exc:  # noqa: BLE001
        LOG.warning("写图失败 %s：%s", path, exc)
        return False


# --------------------------------------------------------------------------- #
# 帧枚举：video 用 L2b 关键帧，photo 用原图
# --------------------------------------------------------------------------- #


@dataclass
class FrameRef:
    shot_ix: int
    sec: float
    path: Path


def frames_for_file(conn, cfg: Config, file_id: int, kind: str,
                    source_path: Optional[str] = None) -> List[FrameRef]:
    """列出该文件参与 F1 检测的帧。

    * photo：优先用 03_extract.py 写下的 NVMe 缓存图（frames/<file_id>/0000_*.jpg）。
      为什么必须优先：E: 是机械盘，照片 3.235 TB，重读原图要 6~9 小时，
      而缓存图在 NVMe 上；缓存缺失才回退原图。
      代价是检测分辨率从「原图」降到缓存短边（storage.photo_cache_short_edge，默认 768）——
      这是为绕开机械盘付出的代价；要满分辨率检脸就把该值调大后重跑 03_extract.py。
    * video：frames/<file_id>/<shot_ix:04d>_<ts_ms:09d>.jpg（03_extract.py 产出）
    """
    if kind == "photo":
        base = cfg.path("paths.frames") / str(file_id)
        cached = sorted(base.glob("0000_*.jpg")) if base.exists() else []
        if cached:
            return [FrameRef(0, 0.0, cached[0])]
        row = conn.execute("SELECT path FROM files WHERE file_id=?", (file_id,)).fetchone()
        src = Path(source_path or (row["path"] if row else ""))
        if not src or not src.exists():
            return []
        return [FrameRef(0, 0.0, src)]

    base = cfg.path("paths.frames") / str(file_id)
    if not base.exists():
        return []
    refs: List[FrameRef] = []
    for p in sorted(base.glob("*.jpg")):
        shot_s, _, ms_s = p.stem.partition("_")
        try:
            refs.append(FrameRef(int(shot_s), int(ms_s) / 1000.0, p))
        except ValueError:
            continue
    return refs


# --------------------------------------------------------------------------- #
# F2：shot 内 track 合并（correctness 关键路径，单独可测）
# --------------------------------------------------------------------------- #


@dataclass
class Obs:
    """一次检测观测：人脸 + 它所在的时间戳 + 来源帧 + 所属镜头。"""

    face: DetectedFace
    sec: float
    frame: Path
    shot_ix: int = 0


@dataclass
class Track:
    obs: List[Obs] = field(default_factory=list)

    @property
    def best(self) -> Obs:
        return max(self.obs, key=lambda o: o.face.quality)

    @property
    def sec(self) -> float:
        return self.best.sec

    def centroid(self) -> Optional[np.ndarray]:
        vecs = [np.asarray(o.face.embedding, dtype="float32")
                for o in self.obs if o.face.embedding]
        if not vecs:
            return None
        m = np.mean(vecs, axis=0)
        n = float(np.linalg.norm(m))
        return m / n if n > 0 else m


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    iw = max(0.0, min(ax2, bx2) - max(ax1, bx1))
    ih = max(0.0, min(ay2, by2) - max(ay1, by1))
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    ub = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    denom = ua + ub - inter
    return float(inter / denom) if denom > 0 else 0.0


def merge_tracks_within_shot(
    obs: List[Obs], *, iou_thr: float, cos_thr: float,
) -> List[Track]:
    """同一 shot 内按时间序遍历：IoU>0.5 或 外观 cosine>0.85 → 并入同一 track。

    规格 F2 原文：「同一 shot 内按时间序遍历：IoU>0.5 或 外观cosine>0.85 → 合并为同一 track」。
    """
    tracks: List[Track] = []
    for o in sorted(obs, key=lambda x: x.sec):
        v = (np.asarray(o.face.embedding, dtype="float32")
             if o.face.embedding else None)
        for t in tracks:
            if iou(t.best.face.bbox, o.face.bbox) > iou_thr:
                t.obs.append(o)
                break
            c = t.centroid()
            if v is not None and c is not None and float(c.dot(v)) > cos_thr:
                t.obs.append(o)
                break
        else:
            tracks.append(Track(obs=[o]))
    return tracks


def merge_adjacent_tracks(tracks: List[Track], cos_thr: float) -> List[Track]:
    """相邻 track 再做一次 cosine 合并（规格 F2 第二句）。"""
    merged: List[Track] = []
    for t in tracks:
        if merged:
            prev = merged[-1]
            cv, pv = t.centroid(), prev.centroid()
            if cv is not None and pv is not None and float(cv.dot(pv)) > cos_thr:
                prev.obs.extend(t.obs)
                continue
        merged.append(t)
    return merged


def pick_representatives(tracks: List[Track], keep_per_track: int) -> List[Obs]:
    """每条 track 只保留 1~2 张代表帧：按质量降序，且相邻代表帧间隔 ≥1s。

    规格 F2 验收：「无连续时间戳相邻的代表帧（相邻间隔<1s 视为去重失败）」。
    """
    keep = max(1, int(keep_per_track))
    reps: List[Obs] = []
    for t in tracks:
        cand = sorted(t.obs, key=lambda o: -o.face.quality)
        chosen: List[Obs] = []
        for o in cand:
            if any(abs(o.sec - c.sec) < 1.0 for c in chosen):
                continue
            chosen.append(o)
            if len(chosen) >= keep:
                break
        if not chosen:            # 极端情况：所有帧时间戳都挤在 1s 内
            chosen = [cand[0]]
        reps.extend(sorted(chosen, key=lambda o: o.sec))
    return sorted(reps, key=lambda o: o.sec)


def burst_dedup(obs: List[Obs], *, cos_thr: float, count_thr: int) -> Tuple[List[Obs], bool]:
    """同文件内 >count_thr 张 cosine>cos_thr 的脸 → 判定连拍/视频抽帧，只留代表帧。

    规格第六章展示层过滤第 3 条。实现为贪心去重：按质量降序，
    与已保留向量 cosine>cos_thr 的丢弃；若去重后数量仍 > count_thr，
    说明这些脸本来就各不相同（正常的多人素材），原样保留。
    """
    if len(obs) <= count_thr:
        return obs, False
    kept: List[Obs] = []
    kept_vecs: List[np.ndarray] = []
    for o in sorted(obs, key=lambda x: -x.face.quality):
        if o.face.embedding is None:
            continue
        arr = np.asarray(o.face.embedding, dtype="float32")
        if any(float(arr.dot(kv)) > cos_thr for kv in kept_vecs):
            continue
        kept.append(o)
        kept_vecs.append(arr)
    if len(kept) > count_thr:
        return obs, False
    kept.sort(key=lambda o: o.sec)
    LOG.info("burst 去重：%d 张 → %d 张（cosine>%.2f 且总数为 %d>%d）",
             len(obs), len(kept), cos_thr, len(obs), count_thr)
    return kept, True


# --------------------------------------------------------------------------- #
# F1+F2：单文件的候选脸
# --------------------------------------------------------------------------- #


@dataclass
class FaceCandidate:
    shot_ix: int
    sec: float
    bbox: Tuple[float, float, float, float]
    det_score: float
    quality: float
    embedding: List[float]
    aligned: np.ndarray
    frame_path: Path


@dataclass
class DetectResult:
    file_id: int
    status: str                 # done | skipped | error
    frames: int = 0
    detected: int = 0
    candidates: int = 0
    faces_written: int = 0
    group_poster: bool = False
    warning: str = ""
    error: str = ""
    elapsed: float = 0.0


def detect_file(
    conn,
    cfg: Config,
    engine: FaceEngine,
    file_id: int,
    kind: str,
    *,
    source_path: Optional[str] = None,
    dry_run: bool = False,
) -> DetectResult:
    """F1+F2：检测 → track 合并 → 落库（face_records + faces/raw）。dry_run 不写库。"""
    t0 = time.monotonic()
    res = DetectResult(file_id=file_id, status="done")

    refs = frames_for_file(conn, cfg, file_id, kind, source_path)
    res.frames = len(refs)
    if not refs:
        res.status = "skipped"
        res.warning = "没有可检测的帧（照片原图缺失 / 视频无关键帧）"
        return res

    iou_thr = float(cfg.get("faces.track_iou_threshold", 0.5))
    cos_thr = float(cfg.get("faces.track_cosine_threshold", 0.85))
    adj_thr = float(cfg.get("faces.adjacent_track_merge_cosine", 0.85))
    keep_per_track = int(cfg.get("faces.track_keep_per_track", 2))
    side_raw = int(cfg.get("storage.face_raw_px", 112))
    input_size = cfg.get("faces.input_size", [112, 112])
    side = int(input_size[0]) if isinstance(input_size, (list, tuple)) else int(input_size)

    # F1：逐帧检测 + 对齐 + 质量分，按 shot 分桶
    per_shot: Dict[int, List[Obs]] = {}
    total_detected = 0
    for ref in refs:
        img = imread_bgr(ref.path)
        if img is None:
            continue
        faces = engine.detect_and_embed(img, cfg)
        for f in faces:
            if f.embedding is None or f.short_edge < 1.0:
                continue
            total_detected += 1
            per_shot.setdefault(ref.shot_ix, []).append(
                Obs(f, ref.sec, ref.path, ref.shot_ix))
    res.detected = total_detected

    # F2：逐 shot 合并 → 相邻合并 → 代表帧优选
    reps: List[Obs] = []
    for shot_ix in sorted(per_shot):
        ts = merge_tracks_within_shot(per_shot[shot_ix], iou_thr=iou_thr, cos_thr=cos_thr)
        ts = merge_adjacent_tracks(ts, adj_thr)
        reps.extend(pick_representatives(ts, keep_per_track))

    # 展示层过滤 3：同文件内连拍/视频抽帧只留代表帧
    reps, _is_burst = burst_dedup(
        reps,
        cos_thr=float(cfg.get("faces.burst_cosine", 0.90)),
        count_thr=int(cfg.get("faces.burst_count", 50)),
    )

    # 展示层过滤 1：单图 >15 张脸 → group_poster（仅照片判定，视频无意义）
    poster_limit = int(cfg.get("faces.group_poster_face_count", 15))
    group_poster = bool(kind == "photo" and total_detected > poster_limit)
    res.group_poster = group_poster

    # 组候选（shot_ix 需回填，故在 shot 循环外按 ref 重建映射）
    cands: List[FaceCandidate] = []
    for o in reps[:MAX_FACES_PER_FILE]:
        aligned = o.face.normed_face
        if aligned is None:
            continue
        if aligned.shape[0] != side_raw or aligned.shape[1] != side_raw:
            import cv2
            aligned = cv2.resize(aligned, (side_raw, side_raw), interpolation=cv2.INTER_AREA)
        cands.append(FaceCandidate(
            shot_ix=o.shot_ix, sec=o.sec, bbox=o.face.bbox,
            det_score=float(o.face.det_score), quality=float(o.face.quality),
            embedding=list(o.face.embedding), aligned=aligned, frame_path=o.frame,
        ))
    res.candidates = len(cands)

    if dry_run:
        res.status = "skipped"
        LOG.info("[dry-run] file_id=%s kind=%s 帧=%d 检出=%d → 代表帧=%d（poster=%s）",
                 file_id, kind, len(refs), total_detected, len(cands), group_poster)
        return res

    # 幂等重跑：清掉该文件上一轮的人脸记录与磁盘产物
    old = [int(r["face_id"]) for r in conn.execute(
        "SELECT face_id FROM face_records WHERE file_id=?", (file_id,))]
    if old:
        for old_id in old:
            _unlink(cfg, old_id)
        conn.execute(
            f"DELETE FROM face_records WHERE face_id IN ({','.join('?' * len(old))})", old)

    captured = _captured_at(conn, file_id)
    for c in cands:
        face_id = D.insert_face(
            conn,
            file_id=file_id,
            shot_ix=c.shot_ix,
            sec=round(float(c.sec), 3),
            bbox=json.dumps([round(float(v), 1) for v in c.bbox]),
            det_score=round(c.det_score, 4),
            quality=round(c.quality, 4),
            thumb_path=None,          # F3 才写：保证墙上不会出现没有缩略图的卡片
            raw_path=None,
            status="alive",
            captured_at=captured,
            hidden=1 if group_poster else 0,
            has_vector=0,             # F3 完成后置 1
        )
        raw_path = cfg.path("paths.faces_raw") / f"{face_id}.webp"
        if not imwrite_unicode(raw_path, c.aligned, ".webp", 100):
            conn.execute("DELETE FROM face_records WHERE face_id=?", (face_id,))
            continue
        conn.execute("UPDATE face_records SET raw_path=? WHERE face_id=?",
                     (str(raw_path), face_id))
        res.faces_written += 1

    if group_poster:
        conn.execute("UPDATE files SET kind_hint='group_poster', updated_at=? WHERE file_id=?",
                     (local_now_iso(), file_id))

    res.elapsed = round(time.monotonic() - t0, 3)
    return res


def _captured_at(conn, file_id: int) -> Optional[str]:
    row = conn.execute("SELECT captured_at, mtime FROM files WHERE file_id=?",
                       (file_id,)).fetchone()
    if not row:
        return None
    if row["captured_at"]:
        return str(row["captured_at"])
    if row["mtime"]:
        try:
            return _dt.datetime.fromtimestamp(int(row["mtime"])).strftime("%Y-%m-%dT%H:%M:%S")
        except (OSError, OverflowError, ValueError):
            return None
    return None


def _unlink(cfg: Config, face_id: int) -> None:
    for p in (cfg.path("paths.faces_raw") / f"{face_id}.webp",
              cfg.path("paths.faces_thumbs") / f"{face_id}.webp"):
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 目标选择
# --------------------------------------------------------------------------- #


def face_targets(conn, cfg: Config, stage: str, limit: Optional[int] = None) -> List[Any]:
    """extract 已完成、files.status='done'、且该人脸阶段未 done 的文件。

    与 encode.encode_targets 同一套判定，保证断点续跑语义一致。
    """
    if stage == STAGE_FACE_DETECT:
        extra = "AND (f.kind='photo' OR EXISTS (SELECT 1 FROM shots s WHERE s.file_id=f.file_id))"
    else:
        extra = ("AND EXISTS (SELECT 1 FROM face_records fr WHERE fr.file_id=f.file_id "
                 "AND fr.has_vector=0)")
    sql = f"""
        SELECT f.file_id AS file_id, f.path AS path, f.kind AS kind
          FROM files f
          JOIN stage_status ex
                 ON ex.file_id = f.file_id AND ex.stage = ? AND ex.status = 'done'
          LEFT JOIN stage_status ss
                 ON ss.file_id = f.file_id AND ss.stage = ?
         WHERE f.status = 'done'
           AND f.kind IN ('photo','video')
           AND (ss.status IS NULL OR ss.status IN ('pending','error'))
           {extra}
         ORDER BY CASE f.kind WHEN 'photo' THEN 0 ELSE 1 END, f.file_id
    """
    rows = list(conn.execute(sql, (STAGE_EXTRACT, stage)))
    return rows[:limit] if limit else rows


# --------------------------------------------------------------------------- #
# F1+F2 入口（06_face_detect.py）
# --------------------------------------------------------------------------- #


def run_detect(args, cfg: Config) -> Dict[str, Any]:
    D.ensure_schema(cfg.db_path, cfg.root, cfg)
    conn = D.connect(cfg.db_path, cfg=cfg)
    ensure_stage_table(conn)
    conn.commit()

    rows = face_targets(conn, cfg, STAGE_FACE_DETECT, args.limit)
    from .common import shard_range

    rng = shard_range(args.shard, len(rows))
    if rng is not None:
        rows = [rows[i] for i in rng]
    LOG.info("待人脸检测文件 %d 个（dry_run=%s）", len(rows), args.dry_run)
    T.stage("face_detect", total=len(rows))

    engine: Optional[FaceEngine] = None
    if rows and not args.dry_run:
        engine = get_engine(cfg)
        if not engine.loaded:
            conn.close()
            raise RuntimeError(f"人脸引擎加载失败：\n{engine.error}")
        st = engine.status()
        LOG.info("人脸引擎：device=%s providers=%s", st["device"], st["providers"])

    stats = {"done": 0, "skipped": 0, "error": 0, "frames": 0,
             "detected": 0, "faces": 0, "poster": 0}
    t0 = time.monotonic()
    batch = max(1, int(cfg.get("scan.batch_commit_files", 500)))
    for i, r in enumerate(rows, 1):
        fid, kind, ps = int(r["file_id"]), str(r["kind"]), str(r["path"])
        T.current(ps)
        if not Path(ps).exists():
            conn.execute("UPDATE files SET status='missing', updated_at=? WHERE file_id=?",
                         (local_now_iso(), fid))
            continue
        set_stage(conn, fid, STAGE_FACE_DETECT, "pending")
        try:
            res = detect_file(conn, cfg, engine, fid, kind, source_path=ps,
                              dry_run=args.dry_run)
        except Exception as exc:  # noqa: BLE001 - 单文件失败绝不中断整批
            LOG.exception("人脸检测异常 file_id=%s", fid)
            res = DetectResult(fid, "error", error=f"未捕获异常：{exc}")

        stats["frames"] += res.frames
        stats["detected"] += res.detected
        if res.status == "done":
            stats["done"] += 1
            stats["faces"] += res.faces_written
            if res.group_poster:
                stats["poster"] += 1
            set_stage(conn, fid, STAGE_FACE_DETECT, "done", frames=res.faces_written)
        elif res.status == "skipped":
            stats["skipped"] += 1
            set_stage(conn, fid, STAGE_FACE_DETECT, "done", error=res.warning)
        else:
            stats["error"] += 1
            set_stage(conn, fid, STAGE_FACE_DETECT, "error", error=res.error)
            D.enc_error(conn, fid, D.MODEL_FACE, res.error)

        if i % batch == 0 or i % 50 == 0 or i == len(rows):
            conn.commit()
        T.tick()
        if i % 100 == 0 or i == len(rows):
            el = max(1e-6, time.monotonic() - t0)
            LOG.info("  进度 %d/%d  done=%d 空=%d 错=%d  人脸=%d  检出=%d  (%.1f 文件/秒)",
                     i, len(rows), stats["done"], stats["skipped"], stats["error"],
                     stats["faces"], stats["detected"], i / el)
    conn.commit()
    conn.close()

    elapsed = round(time.monotonic() - t0, 1)
    return {**stats, "targets": len(rows), "elapsed_sec": elapsed,
            "rate_per_sec": round(len(rows) / max(1e-6, elapsed), 2),
            "raw_dir": str(cfg.path("paths.faces_raw"))}


# --------------------------------------------------------------------------- #
# F3：单文件人脸编码入库（07_face_encode.py）
# --------------------------------------------------------------------------- #


@dataclass
class EncodeFaceResult:
    file_id: int
    status: str
    points: int = 0
    thumbs: int = 0
    error: str = ""
    elapsed: float = 0.0


def encode_file_faces(
    conn,
    cfg: Config,
    store: VectorStore,
    engine: FaceEngine,
    file_id: int,
    kind: str,
    *,
    dry_run: bool = False,
) -> EncodeFaceResult:
    t0 = time.monotonic()
    collection = face_collection(cfg)
    dim = int(cfg.get("faces.dimension", 512))
    raw_px = int(cfg.get("storage.face_raw_px", 112))
    thumb_px = int(cfg.get("storage.face_thumb_px", 128))
    thumb_q = int(cfg.get("storage.face_thumb_quality", 80))

    rows = list(conn.execute(
        "SELECT face_id, shot_ix, sec, bbox, quality, raw_path FROM face_records "
        "WHERE file_id=? AND status='alive' AND has_vector=0 ORDER BY face_id", (file_id,)))
    if not rows:
        return EncodeFaceResult(file_id, "skipped", error="没有待编码人脸")

    if dry_run:
        LOG.info("[dry-run] file_id=%s 将编码 %d 张人脸 → %s", file_id, len(rows), collection)
        return EncodeFaceResult(file_id, "skipped")

    D.enc_begin(conn, file_id, D.MODEL_FACE, n_frames=len(rows))
    conn.commit()

    ids: List[int] = []
    vectors: List[List[float]] = []
    payloads: List[Dict[str, Any]] = []
    failed: List[int] = []

    for r in rows:
        # 每个文件开头响应一次「停止任务」。没有这一行时，
        # 点停止后本阶段会一直把整批跑完（实测抽帧 1976 个文件 ≈ 几十分钟）。
        T.cancelled()   # 命中会抛 TaskCancelled，由外层脚本返回退出码 130
        face_id = int(r["face_id"])
        raw = Path(str(r["raw_path"])) if r["raw_path"] else None
        img = imread_bgr(raw) if (raw and raw.exists()) else None
        if img is None:
            failed.append(face_id)
            continue
        try:
            import cv2
            if img.shape[0] != raw_px or img.shape[1] != raw_px:
                img = cv2.resize(img, (raw_px, raw_px), interpolation=cv2.INTER_AREA)
            feat = engine.rec.get_feat(img)
            vec = np.asarray(feat, dtype="float32").ravel()
            n = float(np.linalg.norm(vec))
            if n <= 0 or vec.shape[0] != dim:
                failed.append(face_id)
                continue
            vec = vec / n
        except Exception as exc:  # noqa: BLE001
            LOG.warning("face_id=%s ArcFace 失败：%s", face_id, exc)
            failed.append(face_id)
            continue

        thumb_path = cfg.path("paths.faces_thumbs") / f"{face_id}.webp"
        tw = img
        if img.shape[0] != thumb_px:
            import cv2
            tw = cv2.resize(img, (thumb_px, thumb_px), interpolation=cv2.INTER_AREA)
        if not imwrite_unicode(thumb_path, tw, ".webp", thumb_q):
            failed.append(face_id)
            continue

        bbox = None
        if r["bbox"]:
            try:
                bbox = json.loads(str(r["bbox"]))
            except (json.JSONDecodeError, TypeError):
                bbox = None

        ids.append(face_id)
        vectors.append(vec.tolist())
        payloads.append({
            "face_id": face_id,
            "kind": kind,
            "file_id": file_id,
            "shot_ix": int(r["shot_ix"] or 0),
            "sec": round(float(r["sec"] or 0.0), 3),
            "bbox": bbox,
            "quality": round(float(r["quality"] or 0.0), 4),
            "thumb_path": str(thumb_path),
        })

    if failed:
        conn.execute(
            f"UPDATE face_records SET status='missing' "
            f"WHERE face_id IN ({','.join('?' * len(failed))})", failed)

    if not ids:
        D.enc_error(conn, file_id, D.MODEL_FACE, "全部人脸编码失败")
        set_stage(conn, file_id, STAGE_FACE_ENCODE, "error", error="全部人脸编码失败")
        conn.commit()
        return EncodeFaceResult(file_id, "error", error="全部人脸编码失败",
                                elapsed=time.monotonic() - t0)

    try:
        store.ensure_collection(collection, dim)
        written = store.upsert(collection, ids, vectors, payloads)
    except Exception as exc:  # noqa: BLE001
        D.enc_error(conn, file_id, D.MODEL_FACE, f"Qdrant upsert 失败：{exc}")
        set_stage(conn, file_id, STAGE_FACE_ENCODE, "error", error=str(exc))
        conn.commit()
        return EncodeFaceResult(file_id, "error", error=str(exc), elapsed=time.monotonic() - t0)

    # 阶段三：落 thumb_path + has_vector=1，然后 done + encoded_at
    for pid, pl in zip(ids, payloads):
        conn.execute("UPDATE face_records SET thumb_path=?, has_vector=1 WHERE face_id=?",
                     (pl["thumb_path"], pid))
    D.enc_done(conn, file_id, D.MODEL_FACE, n_frames=len(ids))
    set_stage(conn, file_id, STAGE_FACE_ENCODE, "done",
              shots=len({p["shot_ix"] for p in payloads}), frames=len(ids))

    return EncodeFaceResult(file_id, "done", points=written, thumbs=len(ids),
                            elapsed=time.monotonic() - t0)


def run_encode(args, cfg: Config) -> Dict[str, Any]:
    D.ensure_schema(cfg.db_path, cfg.root, cfg)
    conn = D.connect(cfg.db_path, cfg=cfg)
    ensure_stage_table(conn)
    conn.commit()

    rows = face_targets(conn, cfg, STAGE_FACE_ENCODE, args.limit)
    from .common import shard_range

    rng = shard_range(args.shard, len(rows))
    if rng is not None:
        rows = [rows[i] for i in rng]
    LOG.info("待人脸编码文件 %d 个（dry_run=%s）", len(rows), args.dry_run)
    T.stage("face_encode", total=len(rows))

    store: Optional[VectorStore] = None
    engine: Optional[FaceEngine] = None
    if rows and not args.dry_run:
        store = open_store(cfg)
        ok, msg = store.health()
        if not ok:
            conn.close()
            raise RuntimeError(msg)
        LOG.info("Qdrant 就绪：%s", msg)
        store.ensure_collection(face_collection(cfg), int(cfg.get("faces.dimension", 512)))
        engine = get_engine(cfg)
        if not engine.loaded:
            conn.close()
            raise RuntimeError(f"人脸引擎加载失败：\n{engine.error}")

    stats = {"done": 0, "skipped": 0, "error": 0, "points": 0, "thumbs": 0}
    t0 = time.monotonic()
    batch = max(1, int(cfg.get("scan.batch_commit_files", 500)))
    for i, r in enumerate(rows, 1):
        fid, kind = int(r["file_id"]), str(r["kind"])
        set_stage(conn, fid, STAGE_FACE_ENCODE, "pending")
        res = encode_file_faces(conn, cfg, store, engine, fid, kind, dry_run=args.dry_run)
        if res.status == "done":
            stats["done"] += 1
            stats["points"] += res.points
            stats["thumbs"] += res.thumbs
        elif res.status == "skipped":
            stats["skipped"] += 1
            set_stage(conn, fid, STAGE_FACE_ENCODE, "done", error=res.error)
        else:
            stats["error"] += 1
            LOG.error("人脸编码失败 file_id=%s：%s", fid, res.error)
        if i % batch == 0 or i % 50 == 0 or i == len(rows):
            conn.commit()
        T.tick()
        if i % 200 == 0 or i == len(rows):
            LOG.info("  进度 %d/%d  done=%d 空=%d 错=%d  点=%d  缩略图=%d",
                     i, len(rows), stats["done"], stats["skipped"], stats["error"],
                     stats["points"], stats["thumbs"])
    conn.commit()
    conn.close()
    return {**stats, "targets": len(rows),
            "elapsed_sec": round(time.monotonic() - t0, 1),
            "collection": face_collection(cfg),
            "dim": int(cfg.get("faces.dimension", 512))}


# --------------------------------------------------------------------------- #
# 维护：人脸库统计
# --------------------------------------------------------------------------- #


def face_stats(conn, cfg: Config) -> Dict[str, Any]:
    def one(sql: str, *a: Any) -> int:
        row = conn.execute(sql, a).fetchone()
        return int(row[0] or 0) if row else 0

    return {
        "alive": one("SELECT COUNT(*) FROM face_records WHERE status='alive'"),
        "with_vector": one("SELECT COUNT(*) FROM face_records WHERE status='alive' AND has_vector=1"),
        "pending_vector": one("SELECT COUNT(*) FROM face_records WHERE status='alive' AND has_vector=0"),
        "hidden": one("SELECT COUNT(*) FROM face_records WHERE hidden=1"),
        "files": one("SELECT COUNT(DISTINCT file_id) FROM face_records WHERE status='alive'"),
        "missing": one("SELECT COUNT(*) FROM face_records WHERE status='missing'"),
        "detect_done": one("SELECT COUNT(*) FROM stage_status WHERE stage=? AND status='done'",
                           STAGE_FACE_DETECT),
        "encode_done": one("SELECT COUNT(*) FROM stage_status WHERE stage=? AND status='done'",
                           STAGE_FACE_ENCODE),
    }
