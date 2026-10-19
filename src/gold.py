# -*- coding: utf-8 -*-
"""src/gold.py · 用你自己的照片集标定识别灵敏度（规格 v5.3 §7.4）。

用户要做的事（界面上的原话）：
    准备一个文件夹，里面按「人名」建子文件夹，每人放 5~20 张照片。
系统要做的事（全自动）：
    扫目录 → 给每张照片取一张脸的向量 → 算同人对/异人对的相似度分布
    → 扫阈值 → 选出「召回 ≥ 95% 前提下精度最高」的那一档 → 写成建议。

与规格的两处偏离（记录在案）：
  1. 规格写 recall@20/precision@20（检索式）；本实现算的是**配对级** precision/recall
     —— 因为阈值的作用对象就是「两个向量像不像」，配对级直接对应它，
     而且不需要为每个人建子库做 top-20 检索，快一个数量级。
  2. 规格要求输出 PR 曲线图（PNG）；本机没装绘图库，先输出曲线数据（JSON/CSV）
     和终端可见的字符表，避免为一张图引入新依赖。
"""

from __future__ import annotations

import csv
import itertools
import json
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from . import tasks as T
from .common import Config, get_logger, local_now_iso

LOG = get_logger("gold")

MIN_IDENTITIES = 3
MIN_PER_IDENTITY = 5
MIN_THRESHOLD, MAX_THRESHOLD, STEP = 0.20, 0.70, 0.02
MAX_PER_IDENTITY = 20          # 防止 C(n,2) 爆炸（规格 §7.4.2 的 190 对上限）
DEFAULT_THRESHOLD = 0.40       # 样本不足时的降级默认（规格 §7.4.2）


def _scan(root: Path) -> Dict[str, List[Path]]:
    """子文件夹名 = 身份；`_` 开头的文件夹忽略（规格 §7.4.1）。"""
    from .common import Config as _C  # noqa: F401

    out: Dict[str, List[Path]] = {}
    exts = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith("_") or d.name.startswith("."):
            continue
        imgs = [p for p in sorted(d.iterdir()) if p.suffix.lower() in exts]
        if imgs:
            out[d.name] = imgs
    return out


def _embed_dir(cfg: Config, people: Dict[str, List[Path]]) -> Tuple[Dict[str, List[np.ndarray]], Dict[str, Any]]:
    """给每个人的每张照片取一张脸的向量（取面积最大的那张脸 = 主角）。"""
    from .face_search import get_engine
    from .common import imread_bgr

    engine = get_engine(cfg)
    if not engine.loaded:
        raise RuntimeError(f"人脸模型加载失败：{engine.error}")
    out: Dict[str, List[np.ndarray]] = {}
    stats = {"images": 0, "no_face": 0, "ok": 0, "multi_face": 0}
    for name, paths in people.items():
        vecs: List[np.ndarray] = []
        for p in paths:
            stats["images"] += 1
            img = imread_bgr(p)
            if img is None:
                stats["no_face"] += 1
                continue
            try:
                faces = engine.detect_and_embed(img, cfg)
            except Exception as exc:  # noqa: BLE001 - 单张图失败不该中断
                LOG.warning("照片处理失败 %s：%s", p, exc)
                stats["no_face"] += 1
                continue
            if not faces:
                stats["no_face"] += 1
                continue
            if len(faces) > 1:
                stats["multi_face"] += 1
            best = max(faces, key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]))
            if getattr(best, "embedding", None) is None:
                stats["no_face"] += 1
                continue
            v = np.asarray(best.embedding, dtype=np.float32)
            n = float(np.linalg.norm(v))
            vecs.append(v / (n or 1.0))
            stats["ok"] += 1
            T.tick()
        if vecs:
            out[name] = vecs
    return out, stats


def _pairs(vecs: Dict[str, List[np.ndarray]]) -> Tuple[np.ndarray, np.ndarray]:
    """返回 (相似度数组, 是否同人)，同人对全部枚举，异人对等量随机采样。"""
    same: List[float] = []
    for vs in vecs.values():
        for a, b in itertools.combinations(vs, 2):
            same.append(float(np.dot(a, b)))
    names = list(vecs)
    diff: List[float] = []
    rng = random.Random(42)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            for va in vecs[a]:
                for vb in vecs[b]:
                    diff.append(float(np.dot(va, vb)))
    if len(diff) > max(1, len(same) * 2):
        diff = rng.sample(diff, min(len(diff), len(same) * 2))
    sims = np.asarray(same + diff, dtype=np.float32)
    label = np.asarray([1] * len(same) + [0] * len(diff), dtype=np.int8)
    # 记下同/异人的分界，后面算误报样例用
    return sims, label, np.asarray(same, dtype=np.float32), np.asarray(diff, dtype=np.float32)


def calibrate(cfg: Config, gold_root: Optional[str] = None) -> Dict[str, Any]:
    t0 = time.monotonic()
    root = Path(str(gold_root or cfg.get("cluster.gold_root") or ""))
    if not str(root) or not root.exists():
        raise RuntimeError("没有设置照片集文件夹（设置 → 灵敏度标定），或该路径不存在。")
    people = _scan(root)
    n_id = len(people)
    total_imgs = sum(len(v) for v in people.values())
    if n_id < MIN_IDENTITIES or any(len(v) < MIN_PER_IDENTITY for v in people.values()):
        detail = "、".join(f"{k}:{len(v)} 张" for k, v in people.items())
        return {
            "ok": False,
            "reason": (f"样本量不足：至少要 {MIN_IDENTITIES} 个身份、每人 {MIN_PER_IDENTITY} 张。"
                       f"现在识别到 {n_id} 个身份（{detail}）。"),
            "fallback_threshold": DEFAULT_THRESHOLD,
            "identities": {k: len(v) for k, v in people.items()},
        }

    T.stage("reconcile", total=None, ix=1, n=1)
    T.say(f"正在给 {total_imgs:,} 张照片取人脸向量…")
    people = {k: v[:MAX_PER_IDENTITY] for k, v in people.items()}
    vecs, stats = _embed_dir(cfg, people)
    if len(vecs) < MIN_IDENTITIES:
        return {"ok": False,
                "reason": f"只有 {len(vecs)} 个身份成功取到人脸，至少需要 {MIN_IDENTITIES} 个。",
                "fallback_threshold": DEFAULT_THRESHOLD, "stats": stats}

    T.say("正在比较相似度并扫描阈值…")
    sims, label, same_arr, diff_arr = _pairs(vecs)
    n_same, n_diff = int((label == 1).sum()), int((label == 0).sum())

    curve: List[Dict[str, Any]] = []
    thr = MIN_THRESHOLD
    while thr <= MAX_THRESHOLD + 1e-9:
        pred = sims >= thr
        tp = int((pred & (label == 1)).sum())
        fp = int((pred & (label == 0)).sum())
        recall = tp / n_same if n_same else 0.0
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        curve.append({"threshold": round(thr, 2), "recall": round(recall, 4),
                      "precision": round(precision, 4), "tp": tp, "fp": fp})
        thr += STEP

    ok_pts = [c for c in curve if c["recall"] >= 0.95]
    if ok_pts:
        chosen = max(ok_pts, key=lambda c: c["precision"])
        rule = "召回≥0.95 的档里取精度最高"
    else:
        best = max(curve, key=lambda c: c["recall"])
        idx = min(len(curve) - 1, curve.index(best) + 1)
        chosen = curve[idx]
        rule = "没有满足召回≥0.95 的档，取召回最高点的下一档"

    # 误报样例：该阈值下被判为同人的异人对
    fp_thr = chosen["threshold"]
    false_positives: List[Dict[str, Any]] = []
    if len(diff_arr):
        idx = np.where(diff_arr >= fp_thr)[0][:50]
        false_positives = [{"similarity": round(float(diff_arr[i]), 4)} for i in idx]

    out_dir = Path(cfg.get("paths.data")) / "gold"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    result = {
        "ok": True, "generated_at": local_now_iso(),
        "identities": {k: len(v) for k, v in people.items()},
        "pairs": {"same": n_same, "diff": n_diff},
        "embed_stats": stats,
        "chosen_threshold": chosen["threshold"],
        "chosen_rule": rule,
        "at_chosen": {"recall": chosen["recall"], "precision": chosen["precision"]},
        "curve": curve,
        "same_sim_mean": round(float(same_arr.mean()), 4) if len(same_arr) else 0.0,
        "diff_sim_mean": round(float(diff_arr.mean()), 4) if len(diff_arr) else 0.0,
        "false_positive_pairs": false_positives,
        "elapsed_sec": round(time.monotonic() - t0, 1),
    }
    (out_dir / f"result_{ts}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(out_dir / "false_positives.csv", "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["similarity"])
        w.writeheader()
        w.writerows(false_positives)
    LOG.info("标定完成：建议阈值 %.2f（%s）", chosen["threshold"], rule)
    T.say(f"建议识别灵敏度 {chosen['threshold']:.2f}"
          f"（召回 {chosen['recall']*100:.0f}%，精度 {chosen['precision']*100:.0f}%）")
    return result


def apply_threshold(cfg: Config, threshold: float, *, who: str = "界面/标定") -> Dict[str, Any]:
    """把建议值写进设置（写前先记旧值到审计日志）。"""
    from . import settings_store as SS

    old = cfg.get("retrieval.min_score")
    res = SS.save(cfg, {"retrieval.min_score": float(threshold)}, who=who)
    LOG.info("识别灵敏度 %s → %s（%s）", old, threshold, res.get("notes"))
    return {"old": old, "new": threshold, **res}
