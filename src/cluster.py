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


def _assign_by_centroid(cfg: Config, y: np.ndarray, labels: np.ndarray,
                        fit_idx: np.ndarray, target: np.ndarray) -> int:
    """把 target 里的点按「最近簇心 + 分位数半径」并入 labels，返回归入数量。"""
    order = [c for c in sorted({int(v) for v in labels[fit_idx]}) if c >= 0]
    if not order:
        return 0
    pts = y[fit_idx]
    cents = np.stack([pts[labels[fit_idx] == c].mean(axis=0) for c in order]).astype(np.float32)
    q = float(cfg.get("cluster.assign_radius_quantile", 0.95))
    factor = float(cfg.get("cluster.assign_dist_factor", 1.2) or 1.2)
    radii = []
    for i, c in enumerate(order):
        d = np.sqrt(((pts[labels[fit_idx] == c] - cents[i]) ** 2).sum(axis=1))
        radii.append(float(np.quantile(d, q)) if len(d) else 0.0)
    radii_arr = np.asarray(radii, dtype=np.float32) * factor
    abs_cap = float(cfg.get("cluster.assign_max_dist", 0) or 0)
    if abs_cap > 0:
        radii_arr = np.minimum(radii_arr, abs_cap)
    LOG.info("归组：%d 个簇，阈值中位数 %.4f（%.0f%% 分位 × %.2f）",
             len(order), float(np.median(radii_arr)), q * 100, factor)

    assigned = 0
    order_np = np.asarray(order, dtype=np.int64)
    for st in range(0, len(target), 20000):
        chunk = target[st:st + 20000]
        d2 = ((y[chunk][:, None, :] - cents[None, :, :]) ** 2).sum(axis=2)
        nearest = d2.argmin(axis=1)
        dist = np.sqrt(d2[np.arange(len(chunk)), nearest])
        ok = dist <= radii_arr[nearest]
        labels[chunk[ok]] = order_np[nearest[ok]]
        assigned += int(ok.sum())
    return assigned


def cluster_with_sampling(cfg: Config, y: np.ndarray) -> Tuple[np.ndarray, str]:
    """人脸太多时多轮抽样拟合，避免一次性 HDBSCAN 栈溢出（0xC00000FD）。

    每轮：从「还没归组」的点里抽 max_fit 个做 HDBSCAN，其余按簇心半径并入；
    循环到剩余 <= max_fit 时，对剩余点直接拟合一次。
    这样新出现的人群（只在抽样外的点里）也能形成自己的簇，而不会全被记噪音。
    """
    n = int(y.shape[0])
    max_fit = int(cfg.get("cluster.max_fit_faces", 120_000) or 0)
    if max_fit <= 0 or n <= max_fit:
        return run_hdbscan(cfg, y), f"hdbscan(full {n:,})"

    rng = np.random.default_rng(int(cfg.get("cluster.sample_seed", 42)))
    labels = np.full(n, -1, dtype=np.int64)
    pending = np.arange(n, dtype=np.int64)
    rounds = 0
    max_rounds = int(cfg.get("cluster.max_fit_rounds", 4) or 4)
    while len(pending) > max_fit and rounds < max_rounds:
        rounds += 1
        sub = rng.choice(pending, size=max_fit, replace=False)
        sub.sort()
        LOG.info("第 %d 轮：从 %d 个未归组人脸中抽样 %d 拟合 HDBSCAN",
                 rounds, len(pending), max_fit)
        labels[sub] = run_hdbscan(cfg, y[sub])
        mask = np.ones(n, dtype=bool)
        mask[sub] = False
        target = np.where(mask & (labels < 0))[0]
        got = _assign_by_centroid(cfg, y, labels, sub, target)
        assigned_ids = target[labels[target] >= 0]
        LOG.info("第 %d 轮：拟合噪声之外的 %d 张脸里归入 %d 张，剩余 %d",
                 rounds, len(target), got, len(pending) - len(sub) - got)
        pending = np.array([i for i in pending if labels[i] < 0], dtype=np.int64)
        if got == 0:
            break

    if len(pending):
        LOG.info("最后一批：对剩余 %d 张人脸直接拟合", len(pending))
        labels[pending] = run_hdbscan(cfg, y[pending])

    n_clusters = len({int(v) for v in labels if v >= 0})
    LOG.info("多轮抽样分组完成：%d 轮，%d 个簇，噪声 %d（%.1f%%）",
             rounds, n_clusters, int((labels == -1).sum()),
             (labels == -1).sum() / n * 100)
    return labels, f"hdbscan(multi-round x{rounds}, {n:,} faces)"


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
    # Windows 默认线程栈只有 1MB，HDBSCAN 在几十万点上递归会 STATUS_STACK_OVERFLOW
    # （进程直接死，Python 抓不住）。这里显式开一个 128MB 栈的线程跑 fit。
    import threading

    result: Dict[str, Any] = {}

    def _fit() -> None:
        try:
            result["labels"] = model.fit_predict(y)
        except BaseException as exc:  # noqa: BLE001 - 包括 MemoryError
            result["error"] = exc

    try:
        old_stack = threading.stack_size(128 * 1024 * 1024)
    except (ValueError, RuntimeError):
        old_stack = None
    try:
        th = threading.Thread(target=_fit, name="hdbscan-fit")
        th.start()
        th.join()
    finally:
        if old_stack is not None:
            try:
                threading.stack_size(old_stack)
            except (ValueError, RuntimeError):
                pass

    if "error" in result:
        raise RuntimeError(f"HDBSCAN 失败：{result['error']}")
    labels = result.get("labels")
    if labels is None:
        raise RuntimeError("HDBSCAN 没有返回结果（线程异常退出）")
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
    labels, hdb_method = cluster_with_sampling(cfg, y)
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
# 人工修正：命名 / 合并 / 拆分 / 撤销（规格 §6.5「聚类一定会错，错得有出口」）
# --------------------------------------------------------------------------- #


def _v(cfg: Config, version: Optional[str]) -> str:
    v = version or current_version(cfg)
    if not v:
        raise RuntimeError("还没有任何分组结果，先跑一次自动分组。")
    return v


def _op(cfg: Config, version: str, action: str, payload: Dict[str, Any]) -> int:
    from . import db as D

    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        cur = conn.execute(
            "INSERT INTO cluster_ops(at, version, action, payload, undone) "
            "VALUES (?,?,?,?,0)",
            (local_now_iso(), version, action, json.dumps(payload, ensure_ascii=False)))
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def refresh_meta_for_root(cfg: Config, root_prefix: str,
                          version: Optional[str] = None) -> int:
    """素材根被停用/启用后，重算「包含它的人脸」的所有组的人数与封面。

    停用时如果只是把 files.visible 置 0 而不重算，cluster_meta 里的人数与封面
    还是旧的，照片墙会继续显示这些组（2026-10-26 实测：2,155 组全部成员已不可见、
    2,432 组封面失效，卡片却照旧）。
    人数按「可见成员」重算；可见成员为 0 的组会被 `_refresh_meta` 从 meta 里删掉，
    卡片随之消失。返回重算的组数。
    """
    from . import db as D

    v = version or current_version(cfg)
    if not v:
        return 0
    pref = str(root_prefix)
    like = pref + "%"
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        ids = [int(r[0]) for r in conn.execute(
            "SELECT DISTINCT fc.cluster_id FROM face_clusters fc "
            "JOIN face_records fr ON fr.face_id = fc.face_id "
            "JOIN files f ON f.file_id = fr.file_id "
            "WHERE fc.version=? AND fc.cluster_id >= 0 "
            "  AND (f.root=? OR f.path LIKE ?)", (v, pref, like))]
    finally:
        conn.close()
    if not ids:
        return 0
    for i in range(0, len(ids), 200):
        _refresh_meta(cfg, v, ids[i:i + 200])
    LOG.info("素材根 %s 变更：重算了 %d 个分组的人数与封面", pref, len(ids))
    return len(ids)


def _refresh_meta(cfg: Config, version: str, cluster_ids: List[int]) -> None:
    """重算这些组的人数与封面（封面取清晰度最高的一张）。"""
    from . import db as D

    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        for cid in cluster_ids:
            # 只统计**还能显示**的人脸：源文件被删/移走后 face_records 会被标 missing，
            # 它们不该继续算进「这一组多少张」。原来没加这个条件，
            # 于是重算完人数还是原值（2026-10-22 实测：747 一直不变）。
            row = conn.execute(
                "SELECT COUNT(*) n FROM face_clusters fc "
                "JOIN face_records fr ON fr.face_id = fc.face_id "
                "LEFT JOIN files f2 ON f2.file_id = fr.file_id "
                "WHERE fc.version=? AND fc.cluster_id=? AND fr.status='alive' "
                "  AND (f2.file_id IS NULL OR f2.visible=1)",
                (version, cid)).fetchone()
            n = int(row["n"] or 0)
            if n == 0:
                conn.execute("DELETE FROM cluster_meta WHERE version=? AND cluster_id=?",
                             (version, cid))
                continue
            # 封面必须来自「源文件还在」的人脸，否则点开就报文件不存在。
            # 先按这个条件挑；一个都没有才退回原来的排序。
            cover = conn.execute(
                "SELECT fc.face_id, fc.confidence FROM face_clusters fc "
                "JOIN face_records fr ON fr.face_id=fc.face_id "
                "LEFT JOIN files f2 ON f2.file_id=fr.file_id "
                "WHERE fc.version=? AND fc.cluster_id=? AND fr.status='alive' "
                "  AND (f2.status IS NULL OR f2.status != 'missing') "
                "  AND (f2.visible IS NULL OR f2.visible=1) "
                "ORDER BY fr.quality DESC, fc.face_id LIMIT 1",
                (version, cid)).fetchone()
            if cover is None:
                cover = conn.execute(
                    "SELECT fc.face_id, fc.confidence FROM face_clusters fc "
                    "JOIN face_records fr ON fr.face_id=fc.face_id "
                    "WHERE fc.version=? AND fc.cluster_id=? "
                    "ORDER BY fr.quality DESC, fc.face_id LIMIT 1",
                    (version, cid)).fetchone()
            old = conn.execute("SELECT name, confidence FROM cluster_meta "
                               "WHERE version=? AND cluster_id=?", (version, cid)).fetchone()
            name = old["name"] if old else None
            conf = float(old["confidence"]) if old and old["confidence"] is not None else 0.0
            conn.execute(
                "INSERT INTO cluster_meta(version, cluster_id, size, confidence, "
                "cover_face_id, name, reviewed) VALUES (?,?,?,?,?,?,COALESCE("
                "(SELECT reviewed FROM cluster_meta WHERE version=? AND cluster_id=?),0)) "
                "ON CONFLICT(version, cluster_id) DO UPDATE SET "
                "size=excluded.size, cover_face_id=excluded.cover_face_id, "
                "name=COALESCE(cluster_meta.name, excluded.name)",
                (version, cid, n, conf, int(cover["face_id"]) if cover else None,
                 name, version, cid))
        conn.commit()
    finally:
        conn.close()


def set_name(cfg: Config, cluster_id: int, name: str,
             version: Optional[str] = None) -> Dict[str, Any]:
    """给某个组起名字（用户指定当前组人员姓名）。"""
    from . import db as D

    v = _v(cfg, version)
    nm = str(name or "").strip()[:64]
    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        row = conn.execute("SELECT size FROM cluster_meta WHERE version=? AND cluster_id=?",
                           (v, cluster_id)).fetchone()
        if row is None:
            raise RuntimeError(f"第 {cluster_id} 组不存在（版本 {v}）。")
        conn.execute("UPDATE cluster_meta SET name=? WHERE version=? AND cluster_id=?",
                     (nm or None, v, cluster_id))
        conn.commit()
    finally:
        conn.close()
    _op(cfg, v, "rename", {"cluster_id": cluster_id, "name": nm})
    T.init(cfg)
    T.emit(cfg, "clusters_changed", {"version": v, "action": "rename"})
    LOG.info("第 %s 组命名为 %r", cluster_id, nm)
    return {"cluster_id": cluster_id, "name": nm}


def merge_groups(cfg: Config, src_id: int, dst_id: int,
                 version: Optional[str] = None) -> Dict[str, Any]:
    """把 src_id 组整个并入 dst_id 组。"""
    from . import db as D

    v = _v(cfg, version)
    if int(src_id) == int(dst_id):
        raise ValueError("同一个组不能合并到自己。")
    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        faces = [int(r["face_id"]) for r in conn.execute(
            "SELECT face_id FROM face_clusters WHERE version=? AND cluster_id=?",
            (v, int(src_id)))]
        if not faces:
            raise RuntimeError(f"第 {src_id} 组里没有人脸。")
        conn.executemany(
            "UPDATE face_clusters SET cluster_id=? WHERE version=? AND face_id=?",
            [(int(dst_id), v, f) for f in faces])
        conn.commit()
    finally:
        conn.close()
    _refresh_meta(cfg, v, [int(src_id), int(dst_id)])
    _op(cfg, v, "merge", {"src": int(src_id), "dst": int(dst_id), "faces": faces})
    T.init(cfg)
    T.emit(cfg, "clusters_changed", {"version": v, "action": "merge"})
    LOG.info("合并：第 %s 组 %d 张 → 第 %s 组", src_id, len(faces), dst_id)
    return {"moved": len(faces), "src": src_id, "dst": dst_id}


def split_group(cfg: Config, cluster_id: int, face_ids: List[int],
                version: Optional[str] = None) -> Dict[str, Any]:
    """把选中的若干张脸从组里拆出去，成立一个新组。"""
    from . import db as D

    v = _v(cfg, version)
    if not face_ids:
        raise ValueError("没有选中任何人脸。")
    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        mx = conn.execute("SELECT COALESCE(MAX(cluster_id),-1) m FROM cluster_meta "
                          "WHERE version=?", (v,)).fetchone()
        new_id = int(mx["m"]) + 1
        conn.executemany(
            "UPDATE face_clusters SET cluster_id=? WHERE version=? AND face_id=?",
            [(new_id, v, int(f)) for f in face_ids])
        conn.commit()
    finally:
        conn.close()
    _refresh_meta(cfg, v, [int(cluster_id), new_id])
    _op(cfg, v, "split", {"src": int(cluster_id), "new": new_id,
                          "faces": [int(f) for f in face_ids]})
    T.init(cfg)
    T.emit(cfg, "clusters_changed", {"version": v, "action": "split"})
    LOG.info("拆分：从第 %s 组取出 %d 张 → 新第 %s 组", cluster_id, len(face_ids), new_id)
    return {"moved": len(face_ids), "src": cluster_id, "new": new_id}


def undo_last(cfg: Config, version: Optional[str] = None) -> Dict[str, Any]:
    """撤销最近一次合并/拆分（命名不可撤销，改回来即可）。"""
    from . import db as D

    v = _v(cfg, version)
    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        row = conn.execute(
            "SELECT op_id, action, payload FROM cluster_ops "
            "WHERE version=? AND undone=0 AND action IN ('merge','split') "
            "ORDER BY op_id DESC LIMIT 1", (v,)).fetchone()
        if row is None:
            raise RuntimeError("没有可撤销的合并/拆分操作。")
        op_id = int(row["op_id"])
        payload = json.loads(row["payload"] or "{}")
        action = str(row["action"])
        if action == "merge":
            back = int(payload["src"])
        else:
            back = int(payload["src"])
        moves = payload.get("faces") or []
        # 撤销：把这些人脸放回原来的组
        if action == "split":
            # 拆出去的人脸原来都在 src 组
            conn.executemany(
                "UPDATE face_clusters SET cluster_id=? WHERE version=? AND face_id=?",
                [(int(payload["src"]), v, int(f)) for f in moves])
            conn.execute("DELETE FROM cluster_meta WHERE version=? AND cluster_id=?",
                         (v, int(payload["new"])))
            touched = [int(payload["src"])]
        else:
            conn.executemany(
                "UPDATE face_clusters SET cluster_id=? WHERE version=? AND face_id=?",
                [(int(payload["src"]), v, int(f)) for f in moves])
            touched = [int(payload["src"]), int(payload["dst"])]
        conn.execute("UPDATE cluster_ops SET undone=1 WHERE op_id=?", (op_id,))
        conn.commit()
    finally:
        conn.close()
    _refresh_meta(cfg, v, touched)
    T.init(cfg)
    T.emit(cfg, "clusters_changed", {"version": v, "action": "undo"})
    LOG.info("已撤销操作 #%s（%s），影响 %d 张脸", op_id, action, len(moves))
    return {"undone": action, "faces": len(moves)}




def match_names(cfg: Config, text: str, version: Optional[str] = None,
                limit: int = 12) -> List[Dict[str, Any]]:
    """按人名匹配分组（检索词里出现「给这组起的名字」时优先展示该组）。

    为什么要这个：用户给人起过名字之后，最自然的检索就是直接打名字。
    纯向量检索对「名字」无能为力（名字不在画面上），只能靠这张表兜。
    """
    from . import db as D

    t = str(text or "").strip()
    if len(t) < 2:
        return []
    v = version or current_version(cfg)
    if not v:
        return []
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        rows = conn.execute(
            "SELECT m.cluster_id, m.name, m.size, m.confidence, m.cover_face_id, "
            "fr.thumb_path, fr.file_id, f.path AS file_path "
            "FROM cluster_meta m "
            "LEFT JOIN face_records fr ON fr.face_id = m.cover_face_id "
            "LEFT JOIN files f ON f.file_id = fr.file_id "
            "WHERE m.version=? AND m.name IS NOT NULL AND m.name != '' "
            "  AND (m.name LIKE ? OR ? LIKE '%' || m.name || '%') "
            "ORDER BY m.size DESC LIMIT ?",
            (v, f"%{t}%", t, limit)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]




def group_of_face(cfg: Config, face_id: int,
                  version: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """这张脸属于哪一组（当前版本）。未分组返回 None。"""
    from . import db as D

    v = version or current_version(cfg)
    if not v:
        return None
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        row = conn.execute(
            "SELECT fc.cluster_id, fc.confidence, m.name, m.size, m.cover_face_id "
            "FROM face_clusters fc LEFT JOIN cluster_meta m "
            "  ON m.version = fc.version AND m.cluster_id = fc.cluster_id "
            "WHERE fc.version=? AND fc.face_id=?", (v, int(face_id))).fetchone()
    finally:
        conn.close()
    if row is None or int(row["cluster_id"]) < 0:
        return None
    return dict(row)


# --------------------------------------------------------------------------- #
# 读取（给界面用）
# --------------------------------------------------------------------------- #


def live_meta_stats(cfg: Config, version: Optional[str] = None) -> Dict[str, int]:
    """当前**可见**的分组统计（按 cluster_meta 现算，不受停用/删除影响）。

    集群完成时会把当时的组数/人数写进 cluster_runs，那是**历史快照**；
    停用某个素材根后实际可见的组会变少，界面必须显示实时值，
    否则用户会以为「停用没生效」（2026-10-26 实测就是这样）。
    """
    from . import db as D

    v = version or current_version(cfg)
    if not v:
        return {"groups": 0, "faces": 0}
    try:
        conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    except Exception:  # noqa: BLE001
        return {"groups": 0, "faces": 0}
    try:
        row = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(size), 0) faces FROM cluster_meta "
            "WHERE version=?", (v,)).fetchone()
        return {"groups": int(row["n"] or 0), "faces": int(row["faces"] or 0)}
    except Exception:  # noqa: BLE001
        return {"groups": 0, "faces": 0}
    finally:
        conn.close()


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
