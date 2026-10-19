# -*- coding: utf-8 -*-
"""src/reconcile.py · 库完整性检查（规格 v5.3 §8.2 / §7.5）。

只做**检测与出报告**，绝不自动物理删除（规格 §8.2 自动化边界、§7.5 已拍板）。

三类异常（规格 §7.5 原文编号）：
  A. 孤儿向量：向量库有，元数据里已经没有（文件被删/重建过）
  B. 悬空引用：元数据说「有向量」，向量库里查不到（表现为漏召回）
  C. 内容漂移：向量里存的路径/状态与元数据不一致

本项目的数据结构让这件事很便宜：
  * 语义向量的 point_id = (file_id << 20) | shot_ix，可以直接反解码出 file_id；
  * 人脸向量的 point_id 就是 face_id，直接查 face_records。
所以 A/B 都能精确定位到文件，不用模糊匹配。
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import tasks as T
from .common import Config, get_logger, human_bytes, local_now_iso

LOG = get_logger("reconcile")


def _reports_dir(cfg: Config) -> Path:
    d = Path(cfg.get("paths.data")) / "reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _scroll_meta(store, collection: str, limit: int = 2_000_000) -> Dict[int, Dict[str, Any]]:
    """把向量库里的 (point_id → payload) 拉一遍。60 万点约 3~6 秒。"""
    client = store.client
    out: Dict[int, Dict[str, Any]] = {}
    offset = None
    page = 8192
    while True:
        points, offset = client.scroll(
            collection_name=collection, limit=page, offset=offset,
            with_payload=True, with_vectors=False,
        )
        for p in points:
            out[int(p.id)] = dict(p.payload or {})
        if offset is None:
            break
        if len(out) >= limit:
            LOG.warning("向量库超过 %d 点，本次只核对前 %d 个", limit, len(out))
            break
    return out


def run(cfg: Config, *, progress=None) -> Dict[str, Any]:
    from . import db as D
    from .vector_store import face_collection, open_store, semantic_collection

    t0 = time.monotonic()
    store = open_store(cfg)
    ok, msg = store.health()
    if not ok:
        raise RuntimeError(f"向量库不可用：{msg}")
    sem = semantic_collection(cfg)
    fac = face_collection(cfg)

    T.stage("reconcile", total=None, ix=1, n=1)
    T.say("读取向量库索引…")
    sem_points = _scroll_meta(store, sem)
    fac_points = _scroll_meta(store, fac)

    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        files = {
            int(r["file_id"]): (str(r["path"]), str(r["status"]))
            for r in conn.execute("SELECT file_id, path, status FROM files")
        }
        faces = {
            int(r["face_id"]): (int(r["file_id"]), str(r["status"]))
            for r in conn.execute("SELECT face_id, file_id, status FROM face_records")
        }
        sem_done = {
            int(r["file_id"]) for r in conn.execute(
                "SELECT file_id FROM file_encoding WHERE model='semantic' AND status='done'")
        }
        shots_by_file = {
            int(r["file_id"]): int(r["n"]) for r in conn.execute(
                "SELECT file_id, COUNT(*) AS n FROM shots GROUP BY file_id")
        }
    finally:
        conn.close()
    group = max(1, int(cfg.get("semantic.group_size", 8)))

    # ---------------- A. 孤儿向量 ----------------
    def split_pid(pid: int) -> int:
        return pid >> 20

    orphan_sem: List[Dict[str, Any]] = []
    for pid, payload in sem_points.items():
        fid = int(payload.get("file_id") or split_pid(pid))
        if fid not in files or files[fid][1] in ("missing",):
            orphan_sem.append({"point_id": pid, "file_id": fid,
                               "reason": "文件已不在库" if fid not in files else "文件已标记缺失"})
    orphan_fac: List[Dict[str, Any]] = []
    for pid, payload in fac_points.items():
        fid = int(payload.get("file_id") or 0)
        if pid not in faces:
            orphan_fac.append({"point_id": pid, "face_id": pid, "file_id": fid,
                               "reason": "人脸记录已删除"})
        elif faces[pid][1] == "missing":
            orphan_fac.append({"point_id": pid, "face_id": pid, "file_id": fid,
                               "reason": "人脸记录已标记缺失"})

    # ---------------- B. 悬空引用 ----------------
    # 只有「语义编码标记为 done 且真的有镜头」的文件才应该有点。
    # 0 镜头的文件（绝大多数是抽帧时判为空/全黑的视频）本来就没有可编码内容，
    # 把它们的 status 计成 done 是既有实现的语义，不算悬空 —— 否则每天都会误报。
    dangling_sem: List[Dict[str, Any]] = []
    have = set(sem_points)
    n_no_shot = 0
    for fid in sem_done:
        n_shots = shots_by_file.get(fid, 0)
        if n_shots == 0:
            n_no_shot += 1
            continue
        base = fid << 20
        span = min(n_shots + 1, 1 << 20)
        if not any((base | i) in have for i in range(span)):
            dangling_sem.append({"file_id": fid, "path": files.get(fid, ("?",))[0]})
    dangling_fac = [{"face_id": fid, "file_id": v[0]}
                    for fid, v in faces.items()
                    if v[1] == "alive" and fid not in fac_points]

    # ---------------- C. 内容漂移 ----------------
    drift_sem: List[Dict[str, Any]] = []
    drift_fac: List[Dict[str, Any]] = []
    for pid, payload in fac_points.items():
        if pid in faces and int(payload.get("file_id") or -1) != faces[pid][0]:
            drift_fac.append({"face_id": pid, "payload_file_id": payload.get("file_id"),
                              "db_file_id": faces[pid][0]})
            if len(drift_fac) >= 200:
                break

    # ---------------- 点数账目 ----------------
    # 回答一个必须能回答的问题：向量库里的点数，和「镜头数」对得上吗？
    #
    # 实测结论（2026-10-19 首次核对，记录在案）：语义向量是**一个镜头一个点**，
    # point_id 的低 20 位就是 shot_ix；config 里的 semantic.group_size 是
    # 「一次送多少个文件进模型」的批大小，不是「多少个镜头合成一个向量」。
    # 所以期望点数 = 该文件镜头数，而不是 ceil(镜头数/group_size)。
    # （一开始按 group_size 算，凭空算出 21,689 个「多余点」，纯属口径错。）
    from collections import Counter

    expected = sum(shots_by_file[fid] for fid in sem_done if shots_by_file.get(fid))
    per_file = Counter(pid >> 20 for pid in sem_points)
    over: List[Dict[str, Any]] = []
    for fid, n in per_file.items():
        ns = shots_by_file.get(fid, 0)
        if n > ns:
            over.append({"file_id": fid, "points": n, "expected": ns,
                         "note": "点比镜头多：很可能是镜头重新切分后旧点没清",
                         "path": files.get(fid, ("?",))[0]})
    over.sort(key=lambda d: -(d["points"] - d["expected"]))
    missing_pts = [{"file_id": fid, "shots": ns, "points": per_file.get(fid, 0),
                    "path": files.get(fid, ("?",))[0]}
                   for fid in sem_done
                   if (ns := shots_by_file.get(fid, 0)) and per_file.get(fid, 0) < ns][:10]

    report = {
        "generated_at": local_now_iso(),
        "elapsed_sec": round(time.monotonic() - t0, 1),
        "collections": {
            sem: {"points": len(sem_points)},
            fac: {"points": len(fac_points)},
        },
        "meta": {"files": len(files), "faces": len(faces)},
        "A_orphan": {
            "semantic": len(orphan_sem), "face": len(orphan_fac),
            "samples": (orphan_sem + orphan_fac)[:20],
        },
        "B_dangling": {
            "semantic": len(dangling_sem), "face": len(dangling_fac),
            "samples": (dangling_sem + dangling_fac)[:20],
        },
        "C_drift": {
            "semantic": len(drift_sem), "face": len(drift_fac),
            "samples": (drift_sem + drift_fac)[:20],
        },
        "point_accounting": {
            "expected_points": expected,
            "actual_points": len(sem_points),
            "delta": len(sem_points) - expected,
            "files_with_zero_shots": n_no_shot,
            "files_with_extra_points": len(over),
            "extra_samples": over[:10],
            "missing_samples": missing_pts,
        },
        "verdict": "",
    }
    a = report["A_orphan"]["semantic"] + report["A_orphan"]["face"]
    b = report["B_dangling"]["semantic"] + report["B_dangling"]["face"]
    c = report["C_drift"]["semantic"] + report["C_drift"]["face"]
    if a == 0 and b == 0 and c == 0:
        gap = expected - len(sem_points)
        note = ""
        if gap > 0:
            note = (f"另有 {gap:,} 个镜头没有对应向量 —— 抽帧阶段判定为无效画面"
                    "（全黑/极小帧）的镜头本来就不会写向量，属正常。")
        report["verdict"] = "元数据与向量库一致，没有发现异常。" + note
    else:
        report["verdict"] = (f"发现 {a:,} 个孤儿向量、{b:,} 个悬空引用、{c:,} 个内容漂移。"
                             "物理清理必须人工确认（维护页 → 清理残留数据）。")

    out = _reports_dir(cfg) / f"reconcile_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    LOG.info("完整性检查完成：A=%d B=%d C=%d → %s", a, b, c, out)
    report["report_path"] = str(out)
    T.say(report["verdict"])
    return report
