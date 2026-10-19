# -*- coding: utf-8 -*-
"""规格 v5.3 第十六章「执行前检查清单」的落地版（适配本项目真实 schema）。

对应关系（规格里的名字 -> 本项目真实名字）：
    manifest            -> files(status)
    semantic_vectors    -> Qdrant collection materials_semantic
    face_vectors        -> Qdrant collection materials_faces
    本项目没有本地 semantic_vectors / face_vectors 表：向量只在 Qdrant，
    SQLite 只存 files / file_encoding / shots / faces。

用法：
    .venv\\Scripts\\python.exe tmp\\precheck_v5.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.common import load_config, get_logger  # noqa: E402
from src.db import connect  # noqa: E402

OK = "[OK]  "
WARN = "[WARN]"
BAD = "[BAD] "


def q1(con, sql, args=()):
    row = con.execute(sql, args).fetchone()
    return row[0] if row else None


def has_table(con, name):
    return (
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (name,)
        ).fetchone()
        is not None
    )


def columns(con, table):
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]


def main() -> int:
    cfg = load_config()
    log = get_logger("precheck", cfg, console=False)
    db_path = cfg.db_path
    base = str(cfg.get("qdrant.url")).rstrip("/")
    problems, notes = [], []

    print("=" * 78)
    print("规格 v5.3 §16 执行前检查清单")
    print("=" * 78)

    # ---------------- 16.2 环境 ----------------
    print("\n[16.2 环境检查]")
    print(f"  Python      : {sys.version.split()[0]}")
    ok = sys.version_info >= (3, 8)
    print(f"                {'>= 3.8' if ok else 'TOO OLD'}  {OK if ok else BAD}")
    if not ok:
        problems.append("Python < 3.8")
    print(f"  SQLite      : {sqlite3.sqlite_version}")
    ok = tuple(int(x) for x in sqlite3.sqlite_version.split(".")[:2]) >= (3, 30)
    print(f"                {'>= 3.30' if ok else 'TOO OLD (need >= 3.30)'}  {OK if ok else BAD}")
    if not ok:
        problems.append("SQLite < 3.30")

    import requests

    try:
        r = requests.get(base + "/", timeout=5)
        r.raise_for_status()
        print(f"  Qdrant      : HTTP {r.status_code}  {base}  {OK}")
        qdrant_ok = True
    except Exception as exc:
        print(f"  Qdrant      : 不可达 {base} -> {exc}  {BAD}")
        problems.append(f"Qdrant unreachable: {exc}")
        qdrant_ok = False

    for label, p in (("库/缓存盘", db_path.parent),):
        try:
            du = shutil.disk_usage(p)
            pct = du.free / du.total * 100
            flag = OK if pct > 20 else WARN
            print(
                f"  {label:<10}: 剩余 {du.free/2**30:.1f} GiB / {du.total/2**30:.1f} GiB "
                f"({pct:.1f}%)  {flag}"
            )
            if pct <= 20:
                problems.append(f"{label} 余量 {pct:.1f}% <= 20%")
        except Exception as exc:
            print(f"  {label:<10}: 读盘信息失败 {exc}")

    # ---------------- 16.1 数据一致性 ----------------
    print("\n[16.1 数据一致性]")
    with connect(db_path) as con:
        files_total = q1(con, "SELECT COUNT(*) FROM files")
        by_status = dict(con.execute("SELECT status, COUNT(*) FROM files GROUP BY status"))
        by_kind = {
            f"{k}:{s}": c
            for k, s, c in con.execute(
                "SELECT kind, status, COUNT(*) FROM files GROUP BY kind, status"
            )
        }
        enc = {
            (m, s): c
            for m, s, c in con.execute(
                "SELECT model, status, COUNT(*) FROM file_encoding GROUP BY model, status"
            )
        }
        zombie = con.execute(
            "SELECT status, COUNT(*) FROM files WHERE status='running' GROUP BY status"
        ).fetchall()
        n_shots = q1(con, "SELECT COUNT(*) FROM shots") if has_table(con, "shots") else None
        n_faces = q1(con, "SELECT COUNT(*) FROM faces") if has_table(con, "faces") else None
        fcols = columns(con, "files")
        shot_cols = columns(con, "shots") if has_table(con, "shots") else []

    sem_done = enc.get(("semantic", "done"), 0)
    sem_pend = enc.get(("semantic", "pending"), 0)
    sem_err = enc.get(("semantic", "error"), 0)
    face_done = enc.get(("face", "done"), 0)
    files_done = by_status.get("done", 0)

    print(f"  files 总数           : {files_total:,}")
    print(f"  files by status      : {by_status}")
    print(f"  files by kind:status : {by_kind}")
    print(f"  encoding semantic    : done {sem_done:,} | pending {sem_pend:,} | error {sem_err:,}")
    print(f"  encoding face        : done {face_done:,}")
    print(f"  shots                : {n_shots:,}" if n_shots is not None else "  shots                : (无表)")
    print(f"  faces                : {n_faces:,}" if n_faces is not None else "  faces                : (无表)")

    # ① 三个数必须相等（语义 done == files done == Qdrant points）
    qd_counts = {}
    if qdrant_ok:
        for name, expect, label in (
            (cfg.get("semantic.collection"), sem_done, "semantic"),
            (cfg.get("faces.collection"), face_done, "face"),
        ):
            try:
                rr = requests.get(f"{base}/collections/{name}", timeout=15)
                rr.raise_for_status()
                cnt = rr.json()["result"]["points_count"]
                qd_counts[name] = cnt
                delta = cnt - (expect or 0)
                pct = (delta / expect * 100) if expect else 0.0
                flag = OK if abs(pct) <= 2 else WARN
                if pct <= -5:
                    flag = BAD
                print(
                    f"  {label:<8} Qdrant {name:<20} {cnt:>9,}  "
                    f"SQLite-enc {expect:>9,}  diff {delta:+,} ({pct:+.2f}%)  {flag}"
                )
                if abs(pct) > 2:
                    notes.append(f"{label} Qdrant 与 SQLite 差 {pct:+.2f}%")
            except Exception as exc:
                print(f"  {label:<8} Qdrant {name:<20} 查询失败 -> {exc}  {BAD}")
                problems.append(f"qdrant count fail {name}: {exc}")

    # files done vs 语义 done（照片 + 视频 shot 组的差异要能解释）
    delta = sem_done - files_done
    print(
        f"  files.done {files_done:,} vs semantic.done {sem_done:,} ："
        f"差 {delta:+,}（含 0 镜头的视频/照片不入语义，属正常，需人工确认口径）"
    )

    # ② 僵尸 running
    if zombie:
        print(f"  僵尸 running 状态    : {zombie}  {BAD}")
        problems.append(f"zombie running rows: {zombie}")
    else:
        print(f"  僵尸 running 状态    : 无  {OK}")

    # ③ 单调性快照
    with connect(db_path) as con:
        snap = con.execute(
            "SELECT MAX(encoded_at), COUNT(*) FROM file_encoding WHERE model='semantic' AND status='done'"
        ).fetchone()
    print(f"  单调性快照（明天同期再跑对比）: max(encoded_at)={snap[0]}  count={snap[1]:,}  ({OK})")
    notes.append(f"MONOTONIC_SNAPSHOT semantic_done={snap[1]} at={snap[0]}")

    # ④ NVMe 余量 + 缓存目录实际大小
    nvme = cfg.nvme_root
    try:
        du = shutil.disk_usage(nvme)
        print(
            f"  缓存盘 {nvme}         剩余 {du.free/2**30:.1f} GiB / {du.total/2**30:.1f} GiB "
            f"({du.free/du.total*100:.1f}%)"
        )
    except Exception as exc:
        print(f"  缓存盘读取失败: {exc}")

    # ---------------- 16.3 仓库检查 ----------------
    print("\n[16.3 代码仓库与管线]")
    root = str(cfg.root)
    entries = [
        "01_manifest.py", "02_dedup.py", "03_extract.py", "04_encode.py",
        "06_face_detect.py", "07_face_encode.py", "08_search.py", "12_maintain.py", "app.py",
    ]
    missing = [f for f in entries if not os.path.exists(os.path.join(root, f))]
    print(f"  入口脚本齐全         : {'是' if not missing else '缺 ' + str(missing)}  {OK if not missing else BAD}")
    if missing:
        problems.append(f"missing entry scripts: {missing}")

    print(f"  files 列             : {', '.join(fcols)}")
    has_lease = any("lease" in c for c in fcols)
    print(f"  lease 字段(§8.3)     : {'有' if has_lease else '无 -> 需补'}  {OK if has_lease else WARN}")
    if not has_lease:
        problems.append("files 无 lease 字段：running 态无法超时回收（§8.3 要求）")

    has_hash = any(c == "content_hash" for c in fcols)
    print(f"  content_hash(§7.2)   : {'有' if has_hash else '无 -> 需补'}  {OK if has_hash else WARN}")
    if not has_hash:
        problems.append("files 无 content_hash 字段：搬家去重无法实现（§7.2/M4 要求）")

    print(f"  shots 列             : {', '.join(shot_cols)}")

    # 关键索引
    with connect(db_path) as con:
        idx = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
    need = ["uk_face_vector", "uk_semantic"]
    have = [n for n in need if any(n in i for i in idx)]
    print(f"  幂等唯一索引(§8.3)   : 已有 {have or '无'} / 需要 {need}  {OK if len(have)==len(need) else WARN}")
    if len(have) != len(need):
        problems.append(f"缺幂等唯一索引：{[n for n in need if n not in have]}")

    # ---------------- M7 聚类依赖 ----------------
    print("\n[M7 聚类依赖]")
    for mod in ("numpy", "sklearn", "umap", "hdbscan", "pyarrow"):
        try:
            m = __import__(mod)
            v = getattr(m, "__version__", "?")
            print(f"  {mod:<10}: {v}  {OK}")
        except Exception:
            print(f"  {mod:<10}: 未装（M7 前需装）  {WARN}")
            notes.append(f"missing module: {mod}")
    try:
        import torch

        print(f"  torch     : {torch.__version__}  cuda={torch.cuda.is_available()}  {OK}")
    except Exception as exc:
        print(f"  torch     : 不可用 {exc}  {BAD}")

    print("\n" + "=" * 78)
    if problems:
        print(f"结论：{len(problems)} 项需要处理（阻塞 M0 之后的部分里程碑，不阻塞 M0/M1/M2/M3）")
        for p in problems:
            print(f"  - {p}")
    else:
        print("结论：全部通过")
    if notes:
        print(f"备注 {len(notes)} 条：")
        for n in notes:
            print(f"  · {n}")
    print("=" * 78)
    log.info("PRE-CHECK %s", json.dumps({"problems": problems, "notes": notes}, ensure_ascii=False))
    return 0


def _walk(root):
    return []


def _walk(root):
    return []


if __name__ == "__main__":
    raise SystemExit(main())
