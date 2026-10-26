"""04_encode.py · L2c 多模态编码（Qwen3-VL-Embedding-2B，int8 upsert）+ L3 入库。

规格第五章 L2c / 4.2 铁律 3：
  两阶段入库保一致性：先写 file_encoding.status=pending → 写 Qdrant → 最后 done + encoded_at。
  检索 L4 只 join done 的文件。

粒度决策：镜头级（shot 级）向量。一个 shot 的所有关键帧向量先各自编码，
再平均并重新归一化 —— 与 L4「时序一致性聚类 / shot 级去重」的颗粒度对齐，
同时把 60 万关键帧量级压到镜头量级。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np

from . import db as D
from . import tasks as T
from .common import perf_value, Config, get_logger, human_bytes, local_now_iso, make_point_id
from .encode_model import SemanticEncoder, get_encoder
from .extract import STAGE_EXTRACT, ensure_stage_table, set_stage
from .vector_store import VectorStore, open_store, semantic_collection

LOG = get_logger("encode_run")

# 编码阶段的 stage_status 键（表由 extract.ensure_stage_table 建）
STAGE_SEMANTIC = "semantic_encode"


def stage_done(conn, file_id: int, stage: str) -> bool:
    """该文件在该阶段是否已完成（断点续跑的判定依据）。"""
    row = conn.execute(
        "SELECT status FROM stage_status WHERE file_id=? AND stage=?", (file_id, stage)
    ).fetchone()
    return bool(row and row["status"] == "done")


# --------------------------------------------------------------------------- #
# 关键帧收集
# --------------------------------------------------------------------------- #


@dataclass
class ShotFrames:
    shot_ix: int
    shot_start: float
    shot_end: float
    frames: List[Path] = field(default_factory=list)


def frames_dir(cfg: Config, file_id: int) -> Path:
    return cfg.path("paths.frames") / str(file_id)


def collect_shot_frames(conn, cfg: Config, file_id: int,
                        kind: Optional[str] = None,
                        source_path: Optional[str] = None) -> List[ShotFrames]:
    """从 shots 表 + frames/<file_id>/ 目录重建镜头→关键帧映射。

    文件名格式：<shot_ix:04d>_<ts_ms:09d>.jpg（03_extract.py 产出）

    photo 与 video 的关键帧来源不同，必须分开处理：
      * video：frames/<file_id>/<shot>_<ms>.jpg —— 由 L2b 抽帧产生
      * photo：**优先用 03_extract.py 写下的 NVMe 缓存图**（frames/<file_id>/0000_*.jpg）。
        为什么必须优先：E: 是机械盘，照片共 3.235 TB，重读原图要 6~9 小时；
        缓存图在 NVMe 上，读一遍只要几分钟。缓存缺失（老数据 / 关掉缓存）才回退原图。
    """
    rows = D.shots_for_file(conn, file_id)
    out: List[ShotFrames] = []
    base = frames_dir(cfg, file_id)

    if kind == "photo":
        shot_ix = int(rows[0]["shot_ix"]) if rows else 0
        shot_start = float(rows[0]["shot_start"] or 0.0) if rows else 0.0
        shot_end = float(rows[0]["shot_end"] or 0.0) if rows else 0.0
        cached = sorted(base.glob("0000_*.jpg")) if base.exists() else []
        if cached:
            return [ShotFrames(shot_ix, shot_start, shot_end, cached)]
        src = Path(source_path) if source_path else None
        if src is None:
            row = conn.execute("SELECT path FROM files WHERE file_id=?", (file_id,)).fetchone()
            src = Path(str(row["path"])) if row else None
        frames = [src] if (src is not None and src.exists()) else []
        return [ShotFrames(shot_ix, shot_start, shot_end, frames)]

    for r in rows:
        # 每个文件开头响应一次「停止任务」。没有这一行时，
        # 点停止后本阶段会一直把整批跑完（实测抽帧 1976 个文件 ≈ 几十分钟）。
        T.cancelled()   # 命中会抛 TaskCancelled，由外层脚本返回退出码 130
        sf = ShotFrames(int(r["shot_ix"]), float(r["shot_start"] or 0.0), float(r["shot_end"] or 0.0))
        if base.exists():
            sf.frames = sorted(base.glob(f"{sf.shot_ix:04d}_*.jpg"))
        out.append(sf)
    return out


def encode_targets(conn, cfg: Config, stage: str, limit: Optional[int] = None) -> List[Any]:
    """选出需要跑该阶段的文件：extract 已完成、files.status='done'、该阶段未 done。"""
    sql = """
        SELECT f.file_id AS file_id, f.path AS path, f.kind AS kind, f.status AS status
          FROM files f
          JOIN stage_status ex
                 ON ex.file_id = f.file_id AND ex.stage = ? AND ex.status = 'done'
          LEFT JOIN stage_status ss
                 ON ss.file_id = f.file_id AND ss.stage = ?
         WHERE f.status = 'done'
           AND f.kind IN ('photo','video')
           AND (ss.status IS NULL OR ss.status IN ('pending','error'))
         ORDER BY f.file_id
    """
    rows = list(conn.execute(sql, (STAGE_EXTRACT, stage)))
    if limit:
        rows = rows[:limit]
    return rows


# --------------------------------------------------------------------------- #
# 单个文件的语义编码
# --------------------------------------------------------------------------- #


@dataclass
class EncodeResult:
    file_id: int
    status: str                 # done | error | skipped
    shots: int = 0
    points: int = 0
    frames: int = 0
    error: str = ""
    elapsed: float = 0.0


def encode_file(
    conn,
    cfg: Config,
    store: VectorStore,
    encoder: SemanticEncoder,
    file_id: int,
    kind: str,
    *,
    source_path: Optional[str] = None,
    dry_run: bool = False,
) -> EncodeResult:
    t0 = time.monotonic()
    collection = semantic_collection(cfg)
    dim = int(cfg.get("semantic.dimension", 2048))
    model_key = D.MODEL_SEMANTIC

    shots = collect_shot_frames(conn, cfg, file_id, kind=kind, source_path=source_path)
    if not shots:
        return EncodeResult(file_id, "error", error="没有 shots 记录（请先跑 03_extract.py）")

    all_frames = [p for s in shots for p in s.frames]
    if not all_frames:
        return EncodeResult(file_id, "error", error="没有可用关键帧")

    if dry_run:
        LOG.info("[dry-run] file_id=%s 将编码 %d 个镜头 / %d 帧 → %s",
                 file_id, len(shots), len(all_frames), collection)
        return EncodeResult(file_id, "skipped", shots=len(shots), frames=len(all_frames))

    # 阶段一：pending
    # 并发写库（3 个编码进程 + 抽帧进程同时跑）时会遇到锁竞争，用 retry_on_lock 兜住
    D.retry_on_lock(D.enc_begin, conn, file_id, model_key, n_frames=len(all_frames))
    D.retry_on_lock(conn.commit)

    try:
        vecs = encoder.encode_images(all_frames)
    except Exception as exc:  # noqa: BLE001 - 单文件失败绝不中断整批
        D.enc_error(conn, file_id, model_key, f"编码失败：{exc}")
        set_stage(conn, file_id, "semantic_encode", "error", error=str(exc))
        conn.commit()
        return EncodeResult(file_id, "error", error=str(exc),
                            elapsed=time.monotonic() - t0)

    if len(vecs) != len(all_frames):
        msg = f"向量数 {len(vecs)} 与帧数 {len(all_frames)} 不一致"
        D.enc_error(conn, file_id, model_key, msg)
        set_stage(conn, file_id, "semantic_encode", "error", error=msg)
        conn.commit()
        return EncodeResult(file_id, "error", error=msg, elapsed=time.monotonic() - t0)

    arr = np.asarray(vecs, dtype="float32")

    # 按镜头聚合：平均后重新归一化
    ids: List[int] = []
    payloads: List[Dict[str, Any]] = []
    vectors: List[List[float]] = []
    cursor = 0
    for sf in shots:
        n = len(sf.frames)
        if n == 0:
            continue
        chunk = arr[cursor:cursor + n]
        cursor += n
        mean = chunk.mean(axis=0)
        norm = float(np.linalg.norm(mean))
        if norm > 0:
            mean = mean / norm
        mean = mean.astype("float16").astype("float32")  # int8 量化前先压精度，省 IO

        sec = float((sf.shot_start + sf.shot_end) / 2.0)
        thumb_rel = str(sf.frames[0]) if sf.frames else None
        ids.append(make_point_id(file_id, sf.shot_ix))
        vectors.append(mean.tolist())
        payloads.append({
            "kind": kind,
            "file_id": file_id,
            "shot_ix": sf.shot_ix,
            "sec": round(sec, 3),
            "shot_start": round(sf.shot_start, 3),
            "shot_end": round(sf.shot_end, 3),
            "path": None,          # 由入库后统一从 files 表取（payload 只放短字段）
            "thumb_path": thumb_rel,
        })

    # 先删该文件旧点，保证「修改后重跑」不产生重复向量
    try:
        store.delete_by_file(collection, file_id)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("清理旧向量失败（继续）file_id=%s：%s", file_id, exc)

    # 阶段二：写 Qdrant
    try:
        written = store.upsert(collection, ids, vectors, payloads)
    except Exception as exc:  # noqa: BLE001
        D.enc_error(conn, file_id, model_key, f"Qdrant upsert 失败：{exc}")
        set_stage(conn, file_id, "semantic_encode", "error", error=str(exc))
        conn.commit()
        return EncodeResult(file_id, "error", error=str(exc), elapsed=time.monotonic() - t0)

    # 阶段三：done + encoded_at
    D.retry_on_lock(D.enc_done, conn, file_id, model_key, n_frames=len(all_frames))
    D.retry_on_lock(set_stage, conn, file_id, "semantic_encode", "done",
                    shots=len(vectors), frames=len(all_frames))

    # 顺手写 embeddings 缓存（L2 输出的落盘形态，便于审计）
    cache = cfg.path("paths.embeddings") / f"{file_id}.json"
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump({"file_id": file_id, "dim": dim, "shots": [
                {"shot_ix": p["shot_ix"], "sec": p["sec"]} for p in payloads]}, fh,
                ensure_ascii=False)
    except OSError:
        pass

    return EncodeResult(file_id, "done", shots=len(vectors), points=written,
                        frames=len(all_frames), elapsed=time.monotonic() - t0)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def group_encode(
    conn,
    cfg: Config,
    store: VectorStore,
    encoder: SemanticEncoder,
    items: List[Any],
    *,
    dry_run: bool = False,
) -> Dict[str, int]:
    """**跨文件成组编码**：把一整组的帧合成一次 encode_images 调用。

    为什么必须成组（2026-10-16 实测，这是全集 ETA 的关键）
    ----------------------------------------------------
    `encode_file` 是「一个文件一次 encode_images」，而照片只有 1 帧 →
    实际是 **batch=1** 前向，config 里的 `semantic.batch_size: 16` 对照片**从未生效**。
    batch=1 时 GPU 是延迟瓶颈（单进程利用率仅 65%，3 个进程互相切上下文
    也只有 3.59 张/秒，GPU 却已 99% —— 典型的「忙而无效」）。
    成组后（实测同一批图）：单进程 batch=16 就能到 9.55 张/秒量级。

    铁律 3 仍然成立：整组先全部 `enc_begin`（pending）→ 逐文件写 Qdrant → 逐文件
    `enc_done`。中途崩溃时未完成的文件停在 pending，由 12_maintain repair 捞回。
    """
    collection = semantic_collection(cfg)
    dim = int(cfg.get("semantic.dimension", 2048))
    model_key = D.MODEL_SEMANTIC
    st_done = {"done": 0, "error": 0, "shots": 0, "points": 0, "frames": 0}

    # ---- 收集各文件的帧 ----
    plan: List[Dict[str, Any]] = []
    for r in items:
        fid, kind, ps = int(r["file_id"]), str(r["kind"]), str(r["path"])
        shots = collect_shot_frames(conn, cfg, fid, kind=kind, source_path=ps)
        frames = [p for s in shots for p in s.frames]
        if not shots or not frames:
            st_done["error"] += 1
            msg = "没有 shots 记录（请先跑 03_extract.py）" if not shots else "没有可用关键帧"
            D.retry_on_lock(D.enc_error, conn, fid, model_key, msg)
            LOG.error("语义编码失败 file_id=%s：%s", fid, msg)
            continue
        plan.append({"file_id": fid, "kind": kind, "shots": shots, "frames": frames})

    if not plan:
        return st_done

    if dry_run:
        for p in plan:
            LOG.info("[dry-run] file_id=%s 将编码 %d 个镜头 / %d 帧 → %s",
                     p["file_id"], len(p["shots"]), len(p["frames"]), collection)
        st_done["shots"] = sum(len(p["shots"]) for p in plan)
        st_done["frames"] = sum(len(p["frames"]) for p in plan)
        return st_done

    # ---- 阶段一：整组置 pending ----
    for p in plan:
        D.retry_on_lock(D.enc_begin, conn, p["file_id"], model_key, n_frames=len(p["frames"]))
    D.retry_on_lock(conn.commit)

    # ---- 阶段二：一次编码整组的帧 ----
    all_frames = [fr for p in plan for fr in p["frames"]]
    try:
        vecs = encoder.encode_images(all_frames)
    except Exception as exc:  # noqa: BLE001 - 整组失败不能拖垮整批，退化为逐文件
        LOG.warning("成组编码失败（%d 帧），退回逐文件编码：%s", len(all_frames), exc)
        for p in plan:
            res = encode_file(conn, cfg, store, encoder, p["file_id"], p["kind"],
                              source_path=str(p["frames"][0]))
            if res.status == "done":
                st_done["done"] += 1
                st_done["shots"] += res.shots
                st_done["points"] += res.points
                st_done["frames"] += res.frames
            else:
                st_done["error"] += 1
        D.retry_on_lock(conn.commit)
        return st_done

    if len(vecs) != len(all_frames):
        msg = f"向量数 {len(vecs)} 与帧数 {len(all_frames)} 不一致"
        for p in plan:
            D.retry_on_lock(D.enc_error, conn, p["file_id"], model_key, msg)
        st_done["error"] += len(plan)
        D.retry_on_lock(conn.commit)
        return st_done

    arr = np.asarray(vecs, dtype="float32")

    # ---- 阶段三：逐文件聚合成镜头级向量 → 写 Qdrant → done ----
    cursor = 0
    for p in plan:
        fid, kind, shots = p["file_id"], p["kind"], p["shots"]
        n = len(p["frames"])
        chunk = arr[cursor:cursor + n]
        cursor += n

        ids: List[int] = []
        vectors: List[List[float]] = []
        payloads: List[Dict[str, Any]] = []
        off = 0
        for sf in shots:
            m = len(sf.frames)
            if m == 0:
                continue
            sub = chunk[off:off + m]
            off += m
            mean = sub.mean(axis=0)
            norm = float(np.linalg.norm(mean))
            if norm > 0:
                mean = mean / norm
            mean = mean.astype("float16").astype("float32")
            sec = float((sf.shot_start + sf.shot_end) / 2.0)
            ids.append(make_point_id(fid, sf.shot_ix))
            vectors.append(mean.tolist())
            payloads.append({
                "kind": kind, "file_id": fid, "shot_ix": sf.shot_ix,
                "sec": round(sec, 3),
                "shot_start": round(sf.shot_start, 3),
                "shot_end": round(sf.shot_end, 3),
                "path": None,
                "thumb_path": str(sf.frames[0]) if sf.frames else None,
            })

        try:
            store.delete_by_file(collection, fid)
            written = store.upsert(collection, ids, vectors, payloads)
        except Exception as exc:  # noqa: BLE001
            D.retry_on_lock(D.enc_error, conn, fid, model_key, f"Qdrant upsert 失败：{exc}")
            set_stage(conn, fid, "semantic_encode", "error", error=str(exc))
            st_done["error"] += 1
            continue

        D.retry_on_lock(D.enc_done, conn, fid, model_key, n_frames=n)
        set_stage(conn, fid, "semantic_encode", "done", shots=len(vectors), frames=n)
        conn.execute("UPDATE files SET indexed_at=?, updated_at=? WHERE file_id=?",
                     (local_now_iso(), local_now_iso(), fid))
        cache = cfg.path("paths.embeddings") / f"{fid}.json"
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump({"file_id": fid, "dim": dim, "shots": [
                    {"shot_ix": pl["shot_ix"], "sec": pl["sec"]} for pl in payloads]}, fh,
                    ensure_ascii=False)
        except OSError:
            pass
        st_done["done"] += 1
        st_done["shots"] += len(vectors)
        st_done["points"] += written
        st_done["frames"] += n

    D.retry_on_lock(conn.commit)
    return st_done


def run(args, cfg: Config) -> Dict[str, Any]:
    D.ensure_schema(cfg.db_path, cfg.root, cfg)
    conn = D.connect(cfg.db_path, cfg=cfg)
    ensure_stage_table(conn)
    conn.commit()

    rows = encode_targets(conn, cfg, STAGE_SEMANTIC, args.limit)
    from .common import shard_range

    rng = shard_range(args.shard, len(rows))
    if rng is not None:
        rows = [rows[i] for i in rng]
    LOG.info("待语义编码文件 %d 个（dry_run=%s）", len(rows), args.dry_run)
    T.stage("encode", total=len(rows))

    store: Optional[VectorStore] = None
    encoder: Optional[SemanticEncoder] = None
    if not args.dry_run and rows:
        store = open_store(cfg)
        ok, msg = store.health()
        if not ok:
            conn.close()
            raise RuntimeError(msg)
        LOG.info("Qdrant 就绪：%s", msg)
        collection = semantic_collection(cfg)
        store.ensure_collection(collection, int(cfg.get("semantic.dimension", 2048)))
        encoder = get_encoder(cfg)
        st = encoder.load()
        if not st.loaded:
            conn.close()
            raise RuntimeError(f"语义模型加载失败：\n{st.error}")
        LOG.info("编码维度 %d（backend=%s）", encoder.dimension, st.backend)
        # config 维度与模型维度对齐检查
        if encoder.dimension != int(cfg.get("semantic.dimension", 2048)) and not cfg.get("semantic.mrl_dim"):
            LOG.warning("config semantic.dimension=%s 与模型 %d 不一致：以模型为准（collection 维数按模型建）",
                        cfg.get("semantic.dimension"), encoder.dimension)

    stats = {"done": 0, "error": 0, "shots": 0, "points": 0, "frames": 0}
    t0 = time.monotonic()
    batch = int(cfg.get("scan.batch_commit_files", 500))
    # 成组大小：把整个组的所有帧合成一次 encode_images，照片才不会退化成 batch=1。
    # 8 个文件 × 平均 1.2 帧 ≈ 10 帧；视频文件帧多，成组后由 encode_images 内部
    # 按 semantic.batch_size 再切（并有 OOM 自动折半），不会撑爆显存。
    # 方案 B3：分组大小可被 performance.encode_group 覆盖（0=用 semantic.group_size）
    _pg = perf_value(cfg, "encode_group")
    group_size = max(1, int(_pg or cfg.get("semantic.group_size", 8)))
    last_commit = time.monotonic()
    for i in range(0, len(rows), group_size):
        group = rows[i:i + group_size]
        # 跳过已经不存在的路径
        alive: List[Any] = []
        for r in group:
            if Path(str(r["path"])).exists():
                alive.append(r)
                T.current(str(r["path"]))
            else:
                conn.execute(
                    "UPDATE files SET status='missing', updated_at=? WHERE file_id=?",
                    (local_now_iso(), int(r["file_id"])))
        if alive:
            got = group_encode(conn, cfg, store, encoder, alive, dry_run=args.dry_run)
            for k in ("done", "error", "shots", "points", "frames"):
                stats[k] += got.get(k, 0)
        done_n = min(i + group_size, len(rows))
        if done_n % max(1, batch) < group_size:
            D.retry_on_lock(conn.commit)
            last_commit = time.monotonic()
        if (done_n % 100 < group_size or done_n == len(rows)
                or (time.monotonic() - last_commit) >= 2.0):
            D.retry_on_lock(conn.commit)
            last_commit = time.monotonic()
        T.tick(len(group), total=len(rows))
        if done_n % 100 < group_size or done_n == len(rows):
            LOG.info("  进度 %d/%d  done=%d error=%d 镜头=%d 点=%d",
                     done_n, len(rows), stats["done"], stats["error"],
                     stats["shots"], stats["points"])
    D.retry_on_lock(conn.commit)

    conn.close()

    return {
        **stats,
        "targets": len(rows),
        "collection": semantic_collection(cfg),
        "dim": int(cfg.get("semantic.dimension", 2048)),
        "elapsed_sec": round(time.monotonic() - t0, 1),
        "frames_dir_size": human_bytes(_dir_size(cfg.path("paths.frames"))),
    }


def _dir_size(p: Path) -> int:
    total = 0
    if not p.exists():
        return 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total
