# -*- coding: utf-8 -*-
"""视频检索检查（修正版）：
  V1 视频/镜头/人脸规模与「已编码覆盖率」
  V2 取一个**真的有向量**的视频镜头，做自检索（top1 应命中自己）
  V3 静态服务：关键帧 + 原视频可访问
  V4 视频人脸向量可检索
  V5 enrich（结果渲染所需字段）正确
"""
from __future__ import annotations

import sqlite3
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.common import load_config  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def http_code(url: str) -> int:
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=25) as r:
            return r.status
    except Exception as exc:  # noqa: BLE001
        return getattr(exc, "code", 0) or 0


def main() -> int:
    cfg = load_config()
    con = sqlite3.connect(str(cfg.db_path))
    con.row_factory = sqlite3.Row

    from src.vector_store import open_store, semantic_collection

    store = open_store(cfg)
    ok, msg = store.health()
    check("Qdrant 可连", bool(ok), msg)
    col = semantic_collection(cfg)

    n_video = int(con.execute(
        "SELECT COUNT(*) FROM files WHERE kind='video' AND status='done'").fetchone()[0])
    n_shot = int(con.execute(
        "SELECT COUNT(*) FROM shots s JOIN files f ON f.file_id=s.file_id "
        "WHERE f.kind='video'").fetchone()[0])
    n_face = int(con.execute(
        "SELECT COUNT(*) FROM face_records fr JOIN files f ON f.file_id=fr.file_id "
        "WHERE f.kind='video' AND fr.status='alive'").fetchone()[0])
    total_pts = store.count(col)
    print("=== V1 规模与覆盖率 ===")
    print(f"  已完成视频 {n_video:,} · 视频镜头 {n_shot:,} · 视频人脸 {n_face:,}")
    print(f"  语义集合总点数 {total_pts:,}")
    try:
        from qdrant_client import models as _qm
        vpts = store.count(col, filter=_qm.Filter(must=[_qm.FieldCondition(
            key="kind", match=_qm.MatchValue(value="video"))])) if False else None
        _c = store.client.count(collection_name=col, count_filter=_qm.Filter(must=[
            _qm.FieldCondition(key="kind", match=_qm.MatchValue(value="video"))]),
            exact=True).count
        print(f"  已编码视频镜头 {_c:,} / 视频镜头总数 {n_shot:,} "
              f"→ 覆盖率 {(_c/n_shot*100 if n_shot else 0):.1f}%")
    except Exception as _e:
        print("  覆盖率统计跳过:", _e)
    check("有视频与镜头", n_video > 0 and n_shot > 0, f"{n_video}/{n_shot}")

    print("\n=== V2 取「有向量」的视频镜头做自检索 ===")
    # 从 Qdrant 里直接找一个 kind=video 的点，再回查 shots
    from qdrant_client import models as qm

    pts, _ = store.client.scroll(
        collection_name=col, limit=20, with_vectors=True, with_payload=True,
        scroll_filter=qm.Filter(must=[qm.FieldCondition(
            key="kind", match=qm.MatchValue(value="video"))]))
    check("Qdrant 里有视频向量点", bool(pts), len(pts))
    if not pts:
        print("  （视频还没有编码过，先跑一次「画面向量编码」再检查）")
        return 1
    p0 = pts[0]
    fid = int(p0.payload.get("file_id"))
    shot_ix = int(p0.payload.get("shot_ix", 0))
    print(f"  取样: file_id={fid} shot_ix={shot_ix} point_id={p0.id}")

    shot_row = con.execute("SELECT * FROM shots WHERE file_id=? AND shot_ix=?",
                           (fid, shot_ix)).fetchone()
    check("shots 表能查到该镜头", shot_row is not None)
    hits = store.search_knn(col, p0.vector, 5)
    ids = [int(h.point_id) for h in hits]
    print(f"  自检索 top5: {ids}")
    check("自检索 top1 命中自己", ids and ids[0] == int(p0.id), ids)
    check("payload.kind 是 video", p0.payload.get("kind") == "video", p0.payload.get("kind"))
    check("payload 带 thumb_path（结果缩略图）", bool(p0.payload.get("thumb_path")))

    print("\n=== V3 静态服务 ===")
    base = f"http://{cfg.get('ui.host', '127.0.0.1')}:{int(cfg.get('ui.static_port', 18501))}"
    thumb = Path(str(p0.payload.get("thumb_path") or ""))
    if thumb.exists():
        rel = thumb.relative_to(Path(cfg.path("paths.frames")).parent)
        url = f"{base}/nvme/" + str(rel).replace("\\", "/")
        code = http_code(url)
        print(f"  关键帧 {url[-60:]} → {code}")
        check("关键帧缩略图可访问", code == 200, code)
        check("关键帧文件在磁盘上", True)
    else:
        check("关键帧文件在磁盘上", False, str(thumb))
    src = con.execute("SELECT path FROM files WHERE file_id=?", (fid,)).fetchone()
    if src:
        q = urllib.parse.quote(str(src["path"]).replace("\\", "/").split(":", 1)[1])
        code = http_code(f"{base}/media/{q}")
        print(f"  原视频 /media… → {code}")
        check("原视频可访问（播放/预览）", code in (200, 206), code)

    print("\n=== V4 视频人脸可检索 ===")
    fr = con.execute(
        "SELECT fr.face_id FROM face_records fr JOIN files f ON f.file_id=fr.file_id "
        "WHERE f.kind='video' AND fr.status='alive' AND fr.has_vector=1 LIMIT 1").fetchone()
    check("存在带向量的视频人脸", fr is not None)
    if fr:
        from src.vector_store import face_collection

        fpts = store.client.retrieve(collection_name=face_collection(cfg),
                                     ids=[int(fr["face_id"])], with_vectors=True)
        check("该人脸在 Qdrant 里有向量", bool(fpts), fr["face_id"])

    print("\n=== V5 enrich 渲染字段 ===")
    from src import db as D
    from src import search as S

    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        hit = S.ShotHit(file_id=fid, shot_ix=shot_ix, score=0.99, kind="video", sec=0.0,
                        shot_start=float(shot_row["shot_start"]) if shot_row else 0.0,
                        shot_end=float(shot_row["shot_end"]) if shot_row else 0.0)
        S.enrich(conn, cfg, [hit])
        print(f"  enrich 后: file_id={hit.file_id} path={str(getattr(hit,'path',''))[-50:]} "
              f"kind={hit.kind} seg={hit.shot_start}-{hit.shot_end}")
        check("enrich 补上了 path", bool(getattr(hit, "path", "")))
        check("enrich 保留 kind=video", hit.kind == "video", hit.kind)
    except Exception as exc:  # noqa: BLE001
        check("enrich 调用成功", False, f"{type(exc).__name__}: {exc}")
    finally:
        conn.close()

    con.close()
    print("\n" + "=" * 55)
    print(f"PASS={PASS} FAIL={FAIL}")
    print("=" * 55)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
