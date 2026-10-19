"""定位 reconcile 差异的具体来源：打印孤儿/缺失点的 file_id 及其库内状态。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import db as D  # noqa: E402
from src.common import load_config, split_point_id  # noqa: E402
from src.vector_store import open_store, semantic_collection  # noqa: E402

CFG = load_config(None, None, overlay=[str(ROOT / "tests" / "config_smoke.yaml")])


def main() -> int:
    coll = semantic_collection(CFG)
    store = open_store(CFG)
    conn = D.connect(CFG.db_path, cfg=CFG, readonly=True)

    q_ids = set(store.scroll_ids(coll))
    print(f"collection={coll}  qdrant_points={len(q_ids)}")

    rows = list(conn.execute("""
        SELECT f.file_id, f.kind, f.status, f.path,
               (SELECT COUNT(*) FROM shots s WHERE s.file_id=f.file_id) AS n_shots,
               (SELECT status FROM file_encoding fe
                 WHERE fe.file_id=f.file_id AND fe.model='semantic') AS enc
          FROM files f ORDER BY f.file_id
    """))
    print(f"{'file_id':>7} {'kind':<7} {'status':<12} {'enc':<10} {'shots':>5}  in_qdrant")
    expected = set()
    for r in rows:
        fid = int(r["file_id"])
        pts = {p for p in q_ids if split_point_id(p)[0] == fid}
        if r["enc"] == "done" and r["status"] == "done":
            for s in conn.execute("SELECT shot_ix FROM shots WHERE file_id=?", (fid,)):
                expected.add((fid, int(s["shot_ix"])))
        mark = "YES" if pts else "-"
        print(f"{fid:>7} {str(r['kind']):<7} {str(r['status']):<12} {str(r['enc']):<10} "
              f"{int(r['n_shots']):>5}  {mark} ({len(pts)})  {Path(str(r['path'])).name}")

    want_ids = {((f << 20) | s) for f, s in expected}
    orphan = q_ids - want_ids
    miss = want_ids - q_ids
    print(f"\nexpected_points={len(want_ids)}  qdrant_points={len(q_ids)}")
    print(f"orphan_in_qdrant ({len(orphan)}): {sorted(orphan)}")
    print(f"  → (file_id, shot_ix) = {sorted(split_point_id(p) for p in orphan)}")
    print(f"missing_in_qdrant ({len(miss)}): {sorted(miss)}")
    print(f"  → (file_id, shot_ix) = {sorted(split_point_id(p) for p in miss)}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
