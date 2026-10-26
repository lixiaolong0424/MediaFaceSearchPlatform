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
import os
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


IMG_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def _identity_of(img: Path, root: Path) -> str:
    """图片属于谁：相对根的第一段；直接放在根下就用根目录名。"""
    try:
        rel = img.relative_to(root)
    except ValueError:
        return root.name
    parts = rel.parts
    return parts[0] if len(parts) > 1 else root.name


def _scan(root: Path) -> Dict[str, List[Path]]:
    """单个目录 → {身份: [图片]}。容器目录与身份目录都支持（见模块注释）。"""
    out: Dict[str, List[Path]] = {}
    if not root.exists() or not root.is_dir():
        return out
    for dirpath, _dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        for fn in filenames:
            if Path(fn).suffix.lower() not in IMG_EXTS:
                continue
            if fn.startswith("."):
                continue
            ident = _identity_of(dp / fn, root)
            if ident.startswith(("_", ".")):
                continue      # `_` 开头的文件夹按约定忽略（放负样本用）
            out.setdefault(ident, []).append(dp / fn)
    return out


def _iter_images(root: Path) -> List[Path]:
    """root 下所有图片，跳过 `_`/`.` 开头的子目录与文件。"""
    out: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(("_", "."))]
        dp = Path(dirpath)
        for fn in filenames:
            if fn.startswith(("_", ".")):
                continue
            if Path(fn).suffix.lower() in IMG_EXTS:
                out.append(dp / fn)
    return out


def scan_roots(roots: List[str], layout: str = "per_path") -> Tuple[Dict[str, List[Path]], List[str]]:
    """多个目录 → 合并后的 {身份: [图片]}，以及提示信息。

    layout:
      * per_path   （默认，规格 §7.4.2「一个路径下是一个人」）
                    每个加进来的目录**本身就是一个人**，里面所有图片
                    （含子文件夹里的不同作品）都算这个人。身份 = 目录名。
      * subfolders （一个总目录下每人一个子文件夹）身份 = 相对路径第一段。

    两种布局都**按身份名合并**（大小写不敏感）并按绝对路径去重：
    同一个人分散在两个目录时必须合并，否则「同一人的对」会被当成
    「不同人的对」，阈值会系统性偏掉。
    """
    merged: Dict[str, List[Path]] = {}
    canon: Dict[str, str] = {}
    notes: List[str] = []
    seen: set = set()
    for raw in roots:
        if not str(raw).strip():
            continue
        rp = Path(str(raw).strip().strip('"')).expanduser()
        if not rp.exists():
            notes.append(f"跳过不存在的目录：{rp}")
            continue
        if not rp.is_dir():
            notes.append(f"不是一个文件夹，已跳过：{rp}")
            continue
        if layout == "subfolders":
            got = _scan(rp)
        else:
            imgs = _iter_images(rp)
            got = {rp.name: imgs} if imgs else {}
        if not got:
            notes.append(f"没找到图片（或全是 `_` 开头的目录）：{rp}")
        for ident, paths in got.items():
            key = ident.strip().lower()
            name = canon.setdefault(key, ident.strip())
            bucket = merged.setdefault(name, [])
            for pth in paths:
                k = str(pth).lower()
                if k in seen:
                    continue
                seen.add(k)
                bucket.append(pth)
            if name != ident.strip():
                notes.append(f"「{ident}」与「{name}」视为同一个人（同名合并）")
    return merged, notes


def _embed_dir(cfg: Config, people: Dict[str, List[Path]]) -> Tuple[Dict[str, List[np.ndarray]], Dict[str, Any]]:
    """给每个人的每张照片取一张脸的向量（取面积最大的那张脸 = 主角）。"""
    from .face_pipeline import imread_bgr
    from .face_search import get_engine

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
    # 优先用多目录清单；命令行 --root 或单个 gold_root 作为补充
    roots: List[str] = []
    for r in (cfg.get("cluster.gold_roots") or []):
        if str(r).strip():
            roots.append(str(r))
    if gold_root:
        roots.insert(0, str(gold_root))
    elif cfg.get("cluster.gold_root"):
        roots.append(str(cfg.get("cluster.gold_root")))
    roots = list(dict.fromkeys(roots))
    if not roots:
        raise RuntimeError("没有设置照片集文件夹（维护 → 灵敏度标定）。")
    layout = str(cfg.get("cluster.gold_layout") or "per_path")
    people, notes = scan_roots(roots, layout=layout)
    for n in notes:
        LOG.info("标定照片集：%s", n)
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
