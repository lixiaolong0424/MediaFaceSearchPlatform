# -*- coding: utf-8 -*-
"""全面体检（精简版）：文件被增/删/改/移动后，还有哪些地方会「显示文件不存在」或漏检。

上一版 H 段对每个 size 单独查一次（没有 size 索引）→ 2000 次全表扫描，跑了 10 分钟没完。
这一版改成「一次性把 (size, 文件名) 装进内存再分组」，全流程应在 1~2 分钟内完成。

用法：.venv\\Scripts\\python.exe tmp\\audit_paths2.py
"""
from __future__ import annotations

import os
import random
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.common import load_config  # noqa: E402


def p(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    t0 = time.monotonic()
    cfg = load_config()
    con = sqlite3.connect(str(cfg.db_path))
    con.row_factory = sqlite3.Row
    findings: list[str] = []

    p("A. 人脸 alive 但源文件 missing")
    a = con.execute(
        "SELECT COUNT(*) FROM face_records fr JOIN files f ON f.file_id=fr.file_id "
        "WHERE fr.status='alive' AND f.status='missing'").fetchone()[0]
    p(f"   {a:,}")
    if a:
        findings.append(f"A: {a} 张脸的源文件已 missing 但仍 alive")

    # ---- 一次性把文件清单装进内存（559k 行，约 1~2 秒），后面都基于它 ----
    t = time.monotonic()
    files = con.execute(
        "SELECT file_id, path, status, kind, size, mtime, root FROM files").fetchall()
    p(f"\n[载入] files {len(files):,} 行，{time.monotonic()-t:.1f}s")

    alive_files = [r for r in files if r["status"] != "missing"]
    random.seed(42)
    sample = random.sample(alive_files, min(300, len(alive_files)))

    p("\nB. 抽样 300 个有效文件：磁盘上还在不在")
    gone = [r for r in sample if not os.path.exists(str(r["path"]))]
    p(f"   不存在 {len(gone)}/300")
    if gone:
        findings.append(f"B: 抽样 {len(gone)}/300 个有效文件磁盘上已不存在（漏标 missing）")
        for r in gone[:5]:
            p(f"      {r['file_id']} {r['kind']} {r['path'][:100]}")

    p("\nC. 人脸缩略图存在性（抽样 500）")
    rows = con.execute("SELECT face_id, thumb_path, raw_path FROM face_records "
                       "WHERE status='alive' ORDER BY face_id LIMIT 500").fetchall()
    mt = sum(1 for r in rows if r["thumb_path"] and not os.path.exists(str(r["thumb_path"])))
    mr = sum(1 for r in rows if r["raw_path"] and not os.path.exists(str(r["raw_path"])))
    p(f"   thumb 缺失 {mt}/{len(rows)}   raw 缺失 {mr}/{len(rows)}")
    if mt or mr:
        findings.append(f"C: 人脸缓存文件缺失 thumb={mt} raw={mr}（抽样 500）")

    p("\nD. Qdrant 点 → file_id / thumb_path 检查")
    try:
        from src.vector_store import open_store, semantic_collection, face_collection

        store = open_store(cfg)
        ok, _ = store.health()
        if not ok:
            raise RuntimeError("Qdrant 不可用")
        alive_ids = {r["file_id"] for r in alive_files}
        all_ids = {r["file_id"] for r in files}
        for coll, label in ((semantic_collection(cfg), "semantic"),
                            (face_collection(cfg), "faces")):
            pts, _ = store.client.scroll(collection_name=coll, limit=3000,
                                        with_payload=True, with_vectors=False)
            orphan_gone = sum(1 for x in pts
                              if int((x.payload or {}).get("file_id") or 0) not in all_ids)
            orphan_missing = sum(1 for x in pts
                                 if int((x.payload or {}).get("file_id") or 0) in all_ids
                                 and int((x.payload or {}).get("file_id") or 0) not in alive_ids)
            thumb_miss = sum(1 for x in pts
                             if (x.payload or {}).get("thumb_path")
                             and not os.path.exists(str((x.payload or {}).get("thumb_path"))))
            p(f"   {label}: 抽样 {len(pts)} 点 → 记录已删 {orphan_gone}、"
              f"记录 missing {orphan_missing}、缩略图缺失 {thumb_miss}")
            if orphan_gone:
                findings.append(f"D: {label} 有 {orphan_gone}/{len(pts)} 个点指向已删除的记录")
            if thumb_miss:
                findings.append(f"D: {label} 有 {thumb_miss}/{len(pts)} 个点的缩略图不存在")
    except Exception as exc:  # noqa: BLE001
        p(f"   跳过：{exc}")

    p("\nE. 素材根三处一致性（media_roots / libraries / files.root）")
    try:
        from src import media_roots as MR

        reg = MR.list_roots(cfg)
        libs = [r[0] for r in con.execute("SELECT root FROM libraries")]
        froots = defaultdict(int)
        for r in files:
            froots[str(r["root"] or "")] += 1
        p("   登记:", [r["path"] for r in reg])
        p("   libraries:", libs)
        p("   files.root:", dict(froots))
        for r in reg:
            pref = MR.prefix_of(r["path"])
            if pref not in froots:
                findings.append(f"E: 登记根 {r['path']} 在 files.root 里没有记录（{pref}）")
        for pref, n in froots.items():
            if pref and not any(MR.prefix_of(x["path"]) == pref for x in reg):
                findings.append(f"E: files.root={pref}（{n} 条）未在 media_roots 登记")
    except Exception as exc:  # noqa: BLE001
        p(f"   跳过：{exc}")

    p("\nF. 疑似「移动被当成新增」：同 size + 同文件名 + 同 mtime，且两条都有效")
    groups = defaultdict(list)
    for r in alive_files:
        key = (r["size"], r["mtime"], os.path.basename(str(r["path"])).lower())
        groups[key].append(r)
    dup_groups = [(k, v) for k, v in groups.items() if len(v) > 1]
    p(f"   这样的一组共 {len(dup_groups):,} 组")
    from collections import Counter as _C
    kinds = _C(str(v[0]["kind"]) for _, v in dup_groups)
    states = _C(tuple(sorted({str(x["status"]) for x in v})) for _, v in dup_groups)
    p(f"   按类型: {dict(kinds)}")
    p(f"   按状态组合: {dict(states)}")
    if dup_groups:
        for k, v in dup_groups[:5]:
            p(f"     size={k[0]} name={k[2]} × {len(v)}")
            for r in v[:3]:
                p(f"        #{r['file_id']} {r['status']:>11} {str(r['path'])[:95]}")

    p("\nG. shots / file_encoding 属于 missing 文件的计数（保留数据，不影响显示）")
    s1 = con.execute("SELECT COUNT(*) FROM shots s JOIN files f ON f.file_id=s.file_id "
                     "WHERE f.status='missing'").fetchone()[0]
    s2 = con.execute("SELECT COUNT(*) FROM file_encoding e JOIN files f "
                     "ON f.file_id=e.file_id WHERE f.status='missing'").fetchone()[0]
    p(f"   shots={s1:,}  file_encoding={s2:,}")

    p("\n" + "=" * 70)
    if findings:
        p(f"发现 {len(findings)} 类问题：")
        for f in findings:
            p("  - " + f)
    else:
        p("未发现异常")
    p(f"总耗时 {time.monotonic()-t0:.1f}s")
    p("=" * 70)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
