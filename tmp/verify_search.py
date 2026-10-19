# -*- coding: utf-8 -*-
"""恢复之后验一次检索：文字检索能不能出结果、人脸检索能不能出结果。

不加载重排模型（那要 4~6 秒且占显存），只验「召回 + 过滤」这条主链。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import db as D  # noqa: E402
from src import search as SE  # noqa: E402
from src.common import load_config  # noqa: E402
from src.vector_store import open_store  # noqa: E402


def main() -> int:
    cfg = load_config()
    store = open_store(cfg)
    ok, msg = store.health()
    print("Qdrant:", ok, msg[:60])
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        for q in ("健身房里深蹲的人", "猫"):
            t0 = time.monotonic()
            out = SE.search(conn, cfg, store, query_mode="text",
                            payload={"text": q}, thresh=0.30, k=50)
            print(f"  文字「{q}」 → {len(out.files)} 个文件，"
                  f"{round(time.monotonic()-t0, 1)}s，reranked={out.reranked}")
        # 人脸检索：随便取一张有向量的人脸当查询
        row = conn.execute(
            "SELECT face_id FROM face_records WHERE status='alive' AND has_vector=1 "
            "ORDER BY quality DESC LIMIT 1").fetchone()
        if row:
            t0 = time.monotonic()
            out2 = SE.search_by_face_id(conn, cfg, store, int(row["face_id"]), k=20)
            print(f"  人脸 {row['face_id']} → {len(out2.files)} 个文件，"
                  f"{round(time.monotonic()-t0, 1)}s")
        r = conn.execute("SELECT COUNT(*) FROM face_records "
                         "WHERE status='alive' AND has_vector=1").fetchone()[0]
        print(f"  可检索人脸：{r:,}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
