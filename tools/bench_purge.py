# -*- coding: utf-8 -*-
"""真·端到端基准：在**隔离的临时 collection** 里复现批量删除，测真实耗时。

做法：
  1. 用临时 collection 名（bench_faces / bench_semantic）覆盖配置；
  2. 造 1,500 个文件 + 每个 3 张人脸 / 2 个镜头，写进临时 SQLite 与 Qdrant；
  3. 用 `_purge_rows` 真删，计时；
  4. 删掉临时 collection 与临时库。

这样既不碰真实数据，又能测到「删真实存在的点」的耗时。
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import db as D  # noqa: E402
from src import migrate as MG  # noqa: E402
from src.common import load_config  # noqa: E402
from src.vector_store import open_store  # noqa: E402

N = 1500
FACES_PER_FILE = 3
SHOTS_PER_FILE = 2


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bench_"))
    cfg = load_config()
    cfg.root = tmp
    cfg.override("db.path", str(tmp / "library.db"))
    cfg.override("faces.collection", "bench_faces")
    cfg.override("semantic.collection", "bench_semantic")
    D.ensure_schema(cfg.db_path, ROOT, cfg)
    store = open_store(cfg)
    print("Qdrant:", store.health()[0])

    # 清理可能残留的临时 collection
    for c in ("bench_faces", "bench_semantic"):
        try:
            store.client.delete_collection(c)
        except Exception:  # noqa: BLE001
            pass
    store.ensure_collection("bench_faces", dim=8)
    store.ensure_collection("bench_semantic", dim=8)

    con = D.connect(cfg.db_path, cfg=cfg)
    conn = con
    ids, face_ids, shot_ids, fpts, spts = [], [], [], [], []
    now = "2026-10-23T00:00:00"
    for i in range(1, N + 1):
        fid = i
        ids.append(fid)
        conn.execute(
            "INSERT INTO files(file_id,path,rel_path,path_hash,kind,size,mtime,status,"
            "visible,root) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (fid, f"C:\\x\\f{fid}.jpg", f"f{fid}.jpg", f"h{fid}", "photo",
             1000, 1, "missing", 1, "C:\\x\\"))
        for k in range(FACES_PER_FILE):
            faced = fid * 10 + k
            face_ids.append(faced)
            conn.execute(
                "INSERT INTO face_records(face_id,file_id,status,has_vector,quality,"
                "bbox,thumb_path,raw_path) VALUES (?,?,?,?,?,?,?,?)",
                (faced, fid, "alive", 1, 0.5, "0,0,1,1", "t", "r"))
            fpts.append((faced, [0.0] * 8, {"file_id": fid, "face_id": faced}))
        for k in range(SHOTS_PER_FILE):
            sid = fid << 20 | k
            shot_ids.append(sid)
            conn.execute(
                "INSERT INTO shots(file_id,shot_ix,shot_start,shot_end,n_frames) "
                "VALUES (?,?,?,?,?)", (fid, k, 0.0, 1.0, 1))
            spts.append((sid, [0.0] * 8, {"file_id": fid, "shot_ix": k}))
    conn.commit()

    t0 = time.monotonic()
    store.upsert("bench_faces", [p for p, _v, _x in fpts], [v for _p, v, _x in fpts],
                 [x for _p, _v, x in fpts])
    store.upsert("bench_semantic", [p for p, _v, _x in spts], [v for _p, v, _x in spts],
                 [x for _p, _v, x in spts])
    print(f"造数据：{N} 文件 / {len(fpts)} 人脸 / {len(spts)} 镜头，写入用时 "
          f"{time.monotonic()-t0:.1f}s")
    print(f"  点数：faces={store.count('bench_faces')} semantic={store.count('bench_semantic')}")

    gone = [(i, f"C:\\x\\f{i}.jpg", "photo") for i in ids]
    t0 = time.monotonic()
    n = MG._purge_rows(cfg, conn, gone)   # noqa: SLF001
    dt = time.monotonic() - t0
    print(f"\n删除 {n} 条：{dt:.2f} 秒 → {n/dt:.0f} 条/秒")
    print(f"  剩余点数 faces={store.count('bench_faces')} "
          f"semantic={store.count('bench_semantic')}")
    left = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    left_faces = conn.execute("SELECT COUNT(*) FROM face_records").fetchone()[0]
    print(f"  剩余 SQLite：files={left} face_records={left_faces}")
    conn.close()

    for c in ("bench_faces", "bench_semantic"):
        try:
            store.client.delete_collection(c)
        except Exception:  # noqa: BLE001
            pass
    shutil.rmtree(tmp, ignore_errors=True)
    print("\n临时 collection 与临时库已清理")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
