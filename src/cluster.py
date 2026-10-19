# -*- coding: utf-8 -*-
"""src/cluster.py · 人脸自动分组（规格 v5.3 §6 / §15.6 / §15.7）。

流程（与规格一致）：
    取人脸向量 → 过滤质量门 → 降维（UMAP→PCA 兜底）→ HDBSCAN 密度聚类
    → 写入带版本号的快照 → 原子切换「当前版本」指针

版本与回滚（§15.6）：
    产物落 data/clusters/v{yyyyMMdd_HHmm}/，含 assignments.parquet / centroid.npy /
    config.json；当前版本指针存在 app_settings.cluster_version。
    切换 = 一次单行 UPDATE，秒级完成，搜索与照片墙下一请求自动用新版本；
    回滚同理。保留最近 keep_versions 个版本，超出的自动删最旧的。

为什么降维要允许 PCA 兜底：
    UMAP 要装 umap-learn（含 numba），在 Windows 上不一定装得动。
    PCA（TruncatedSVD）在 ArcFace 512 维上质量略差但**一定能跑**，
    对「先把功能跑起来、再看质量」这个顺序更友好。用哪种会写进报告。
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np

from . import tasks as T
from .common import Config, get_logger, local_now_iso

LOG = get_logger("cluster")

CLUSTER_VERSION_KEY = "cluster_version"


# --------------------------------------------------------------------------- #
# 取向量
# --------------------------------------------------------------------------- #


def load_face_vectors(cfg: Config, *, quality_gate: float,
                      limit: Optional[int] = None,
                      progress=None) -> Tuple[np.ndarray, np.ndarray, List[int]]:
    """从向量库读人脸向量 + 从 SQLite 读质量分。返回 (ids, vecs, qualities)。

    为什么向量从 Qdrant 读而不是从人脸原图重算：
      重算 29 万张脸要 ~3 小时 GPU；向量本来就在库里，读一遍只要几十秒。
    """
    from . import db as D
    from .vector_store import face_collection, open_store

    store = open_store(cfg)
    ok, msg = store.health()
    if not ok:
        raise RuntimeError(f"向量库不可用：{msg}")
    coll = face_collection(cfg)

    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        q = ("SELECT face_id, quality FROM face_records "
             "WHERE status='alive' AND has_vector=1")
        rows = conn.execute(q).fetchall()
    finally:
        conn.close()
    qmap = {int(r["face_id"]): float(r["quality"] or 0.0) for r in rows}
    keep = {fid for fid, qv in qmap.items() if qv >= quality_gate}
    LOG.info("人脸总数 %d，过质量门（>=%.2f）后 %d", len(qmap), quality_gate, len(keep))

    client = store.client
    ids: List[int] = []
    vecs: List[List[float]] = []
    offset = None
    scanned = 0
    while True:
        points, offset = client.scroll(
            collection_name=coll, limit=8192, offset=offset,
            with_payload=False, with_vectors=True,
        )
        for p in points:
            pid = int(p.id)
            if pid not in keep:
                continue
            v = p.vector
            if isinstance(v, dict):            # 命名向量
                v = next(iter(v.values()))
            ids.append(pid)
            vecs.append(v if isinstance(v, list) else list(v))
        scanned += len(points)
        if progress:
            progress(scanned)
        if offset is None:
            break
        if limit and len(ids) >= limit:
            break

    arr = np.asarray(vecs, dtype=np.float32)
    if arr.size:
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        arr /= norms
    qs = np.asarray([qmap[i] for i in ids], dtype=np.float32)
    if limit and len(ids) > limit:
        idx = np.random.default_rng(42).choice(len(ids), limit, replace=False)
        idx.sort()
        return arr[idx], qs[idx], [ids[i] for i in idx]
    return arr, qs, ids


# --------------------------------------------------------------------------- #
# 降维 / 聚类
# --------------------------------------------------------------------------- #


def reduce_dims(cfg: Config, x: np.ndarray) -> Tuple[np.ndarray, str]:
    n_comp = int(cfg.get("cluster.umap_n_components", 20))
    n_comp = max(2, min(n_comp, x.shape[1], x.shape[0] - 1))
    try:
        import umap  # noqa: WPS433

        t0 = time.monotonic()
        reducer = umap.UMAP(
            n_neighbors=int(cfg.get("cluster.umap_n_neighbors", 15)),
            n_components=n_comp,
            min_dist=float(cfg.get("cluster.umap_min_dist", 0.0)),
            metric="cosine",
            verbose=False,
        )
        y = reducer.fit_transform(x)
        LOG.info("UMAP 降维完成：%s → %s，%.1fs", x.shape, y.shape, time.monotonic() - t0)
        return np.asarray(y, dtype=np.float32), "umap"
    except Exception as exc:  # noqa: BLE001 - 装不上就降级，不让功能不可用
        LOG.warning("UMAP 不可用（%s），降级为 PCA。质量略差但一定能跑。", exc)
    from sklearn.decomposition import PCA

    t0 = time.monotonic()
    y = PCA(n_components=n_comp, random_state=42).fit_transform(x)
    LOG.info("PCA 降维完成：%s → %s，%.1fs", x.shape, y.shape, time.monotonic() - t0)
    return np.asarray(y, dtype=np.float32), "pca"


def run_hdbscan(cfg: Config, y: np.ndarray) -> np.ndarray:
    try:
        from sklearn.cluster import HDBSCAN  # sklearn >= 1.3 自带，无需额外依赖
    except ImportError:  # pragma: no cover
        import hdbscan as _h  # type: ignore

        return _h.HDBSCAN(
            min_cluster_size=int(cfg.get("cluster.hdbscan_min_cluster_size", 3)),
            min_samples=int(cfg.get("cluster.hdbscan_min_samples", 2)),
            cluster_selection_epsilon=float(
                cfg.get("cluster.hdbscan_cluster_selection_epsilon", 0.15)),
            cluster_selection_method=str(
                cfg.get("cluster.hdbscan_cluster_selection_method", "leaf")),
            metric="euclidean",
        ).fit_predict(y)

    model = HDBSCAN(
        min_cluster_size=int(cfg.get("cluster.hdbscan_min_cluster_size", 3)),
        min_samples=int(cfg.get("cluster.hdbscan_min_samples", 2)),
        cluster_selection_epsilon=float(
            cfg.get("cluster.hdbscan_cluster_selection_epsilon", 0.15)),
        cluster_selection_method=str(
            cfg.get("cluster.hdbscan_cluster_selection_method", "leaf")),
        metric="euclidean",
        allow_single_cluster=False,
    )
    t0 = time.monotonic()
    labels = model.fit_predict(y)
    LOG.info("HDBSCAN 完成：%.1fs，簇数 %d，噪声 %d",
             time.monotonic() - t0, len(set(labels)) - (1 if -1 in labels else 0),
             int((labels == -1).sum()))
    return labels


def _cluster_confidence(x: np.ndarray, labels: np.ndarray, cap: int = 30) -> Dict[int, float]:
    """每个簇的平均相似度（抽样到 cap 个成员两两算，避免 O(n²) 爆掉）。"""
    out: Dict[int, float] = {}
    rng = np.random.default_rng(7)
    for cid in set(int(v) for v in labels):
        if cid < 0:
            continue
        idx = np.where(labels == cid)[0]
        if len(idx) > cap:
            idx = rng.choice(idx, cap, replace=False)
        sub = x[idx]
        if len(sub) < 2:
            out[cid] = 1.0
            continue
        sim = sub @ sub.T
        n = len(sub)
        out[cid] = float((sim.sum() - n) / (n * (n - 1)))
    return out


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def _atomically_update_pointer(cfg: Config, version: str) -> None:
    T.init(cfg)
    T.setting_set(cfg, CLUSTER_VERSION_KEY, version)
    LOG.info("当前分组版本指针 → %s（单行更新，秒级生效）", version)


def current_version(cfg: Config) -> Optional[str]:
    T.init(cfg)
    return T.setting_get(cfg, CLUSTER_VERSION_KEY)


def _cleanup_old(cfg: Config, keep: int) -> List[str]:
    root = Path(cfg.get("paths.data")) / "clusters"
    if not root.exists():
        return []
    dirs = sorted([d for d in root.iterdir() if d.is_dir() and d.name.startswith("v")],
                  key=lambda d: d.name)
    removed: List[str] = []
    while len(dirs) > keep:
        old = dirs.pop(0)
        try:
            shutil.rmtree(old)
            removed.append(old.name)
        except OSError as exc:
            LOG.warning("删除旧分组版本失败 %s：%s", old.name, exc)
    if removed:
        LOG.info("已清理 %d 个旧分组版本：%s", len(removed), removed)
    return removed


def run(cfg: Config, *, trial: bool = False,
        limit: Optional[int] = None) -> Dict[str, Any]:
    from . import db as D

    if not bool(cfg.get("cluster.enabled", True)):
        raise RuntimeError("自动分组在设置里被关掉了（cluster.enabled=false）")

    t0 = time.monotonic()
    gate = float(cfg.get("cluster.quality_gate", 0.3))
    if trial and limit is None:
        limit = int(cfg.get("cluster.trial_sample", 100_000))

    T.stage("cluster", total=None, ix=1, n=1)
    T.say(f"正在读取人脸向量（{'试跑 ' + format(limit, ',') + ' 个' if limit else '全量'}）…")

    def _p(n: int) -> None:
        T.say(f"正在读取人脸向量：已读 {n:,} 个")
        T.tick(0)

    x, q, face_ids = load_face_vectors(cfg, quality_gate=gate, limit=limit, progress=_p)
    if len(face_ids) < 10:
        raise RuntimeError(
            f"参与分组的人脸只有 {len(face_ids)} 个，太少。"
            "先跑完人脸检测与编码，或把设置里的「清晰度归一化基准」调大一些。")
    T.say(f"已读 {len(face_ids):,} 个人脸向量，正在降维…")

    y, backend = reduce_dims(cfg, x)
    T.tick()
    T.say(f"降维完成（{backend}），正在自动分组…")
    labels = run_hdbscan(cfg, y)
    T.tick()

    conf = _cluster_confidence(x, labels)
    low_thr = float(cfg.get("cluster.low_confidence_threshold", 0.45))
    n_clusters = len({int(v) for v in labels if v >= 0})
    n_noise = int((labels == -1).sum())

    version = f"v{datetime.now():%Y%m%d_%H%M}"
    out_dir = Path(cfg.get("paths.data")) / "clusters" / version
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- 落盘（版本化，可回退）----
    T.say("正在保存分组结果…")
    import pandas as pd

    df = pd.DataFrame({
        "face_id": face_ids,
        "cluster_id": labels.astype(np.int32),
        "quality": q,
        "confidence": [conf.get(int(l), 0.0) for l in labels],
    })
    df.to_parquet(out_dir / "assignments.parquet", index=False)

    centroid = np.zeros((max(1, n_clusters), x.shape[1]), dtype=np.float32)
    order = [c for c in sorted(set(int(v) for v in labels)) if c >= 0]
    for i, cid in enumerate(order):
        centroid[i] = x[labels == cid].mean(axis=0)
    np.save(out_dir / "centroid.npy", centroid)
    meta = {
        "version": version, "created_at": local_now_iso(), "backend": backend,
        "params": {k: cfg.get(f"cluster.{k}") for k in (
            "quality_gate", "umap_n_neighbors", "umap_n_components", "umap_min_dist",
            "hdbscan_min_cluster_size", "hdbscan_min_samples",
            "hdbscan_cluster_selection_epsilon", "hdbscan_cluster_selection_method")},
        "n_faces": len(face_ids), "n_clusters": n_clusters, "n_noise": n_noise,
        "trial": bool(trial), "cluster_ids": order,
    }
    (out_dir / "config.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 写库 ----
    T.say("正在写入分组结果…")
    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        conn.execute("DELETE FROM face_clusters WHERE version=?", (version,))
        conn.execute("DELETE FROM cluster_meta WHERE version=?", (version,))
        conn.executemany(
            "INSERT OR REPLACE INTO face_clusters(version, face_id, cluster_id, confidence) "
            "VALUES (?,?,?,?)",
            [(version, int(f), int(l), float(conf.get(int(l), 0.0)))
             for f, l in zip(face_ids, labels)],
        )
        # 代表脸：簇内 quality 最高者
        best: Dict[int, Tuple[float, int]] = {}
        for f, l, qv in zip(face_ids, labels, q):
            if l < 0:
                continue
            if int(l) not in best or qv > best[int(l)][0]:
                best[int(l)] = (float(qv), int(f))
        conn.executemany(
            "INSERT OR REPLACE INTO cluster_meta(version, cluster_id, size, confidence, "
            "cover_face_id, reviewed) VALUES (?,?,?,?,?,0)",
            [(version, cid, int((labels == cid).sum()), float(conf.get(cid, 0.0)),
              best.get(cid, (0.0, None))[1]) for cid in order],
        )
        conn.execute(
            "INSERT OR REPLACE INTO cluster_runs(version, algo, params, n_faces, "
            "n_faces_all, n_clusters, n_noise, quality, artifact_dir, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (version, f"{backend}+hdbscan", json.dumps(meta["params"], ensure_ascii=False),
             len(face_ids), _count_all_faces(cfg, conn), n_clusters, n_noise,
             json.dumps({
                 "n_low_confidence": sum(1 for c in order if conf.get(c, 0) < low_thr),
                 "low_threshold": low_thr,
                 "mean_cluster_similarity": float(np.mean(list(conf.values()))) if conf else 0.0,
             }, ensure_ascii=False),
             str(out_dir), local_now_iso()),
        )
        conn.commit()
    finally:
        conn.close()

    # ---- 原子切换 + 清理旧版本 ----
    _atomically_update_pointer(cfg, version)
    removed = _cleanup_old(cfg, int(cfg.get("cluster.keep_versions", 3)))

    elapsed = time.monotonic() - t0
    result = {
        "version": version, "backend": backend,
        "n_faces": len(face_ids), "n_faces_all": _count_all_faces(cfg),
        "n_clusters": n_clusters, "n_noise": n_noise,
        "low_confidence": sum(1 for c in order if conf.get(c, 0) < low_thr),
        "low_threshold": low_thr,
        "elapsed_sec": round(elapsed, 1),
        "artifact_dir": str(out_dir),
        "removed_versions": removed,
        "trial": bool(trial),
    }
    LOG.info("自动分组完成：%s", result)
    T.say(f"自动分组完成：{n_clusters:,} 组，{n_noise:,} 张未分组")
    T.emit(cfg, "clusters_changed", {"version": version})
    return result


def _count_all_faces(cfg: Config, conn=None) -> int:
    from . import db as D

    own = conn is None
    if own:
        conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        return int(conn.execute(
            "SELECT COUNT(*) FROM face_records WHERE status='alive'").fetchone()[0] or 0)
    finally:
        if own:
            conn.close()


# --------------------------------------------------------------------------- #
# 读取（给界面用）
# --------------------------------------------------------------------------- #


def groups_overview(cfg: Config, version: Optional[str] = None,
                    *, min_size: int = 1, only_low: bool = False,
                    limit: int = 500) -> List[Dict[str, Any]]:
    from . import db as D

    version = version or current_version(cfg)
    if not version:
        return []
    low = float(cfg.get("cluster.low_confidence_threshold", 0.45))
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        sql = ("SELECT m.cluster_id, m.size, m.confidence, m.cover_face_id, m.name, "
               "m.reviewed, fr.file_id, fr.shot_ix, fr.sec, fr.thumb_path "
               "FROM cluster_meta m LEFT JOIN face_records fr ON fr.face_id=m.cover_face_id "
               "WHERE m.version=? AND m.size>=?")
        args: List[Any] = [version, min_size]
        if only_low:
            sql += " AND m.confidence < ?"
            args.append(low)
        sql += " ORDER BY m.size DESC LIMIT ?"
        args.append(limit)
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def version_history(cfg: Config, limit: int = 10) -> List[Dict[str, Any]]:
    from . import db as D

    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        rows = conn.execute(
            "SELECT version, algo, n_faces, n_faces_all, n_clusters, n_noise, quality, "
            "created_at FROM cluster_runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def switch_version(cfg: Config, version: str) -> None:
    """一键回滚/切换：只改一行指针。"""
    T.init(cfg)
    T.setting_set(cfg, CLUSTER_VERSION_KEY, version)
    T.emit(cfg, "clusters_changed", {"version": version, "switched": True})
    LOG.info("分组版本已切换到 %s", version)
