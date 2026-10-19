"""诊断：召回结果的 payload 里到底有没有可用的文档媒体路径。"""

import sys
from pathlib import Path

sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')

from src import db as D
from src.common import load_config
from src.encode_model import encode_query
from src.search import enrich, semantic_search
from src.vector_store import open_store

cfg = load_config(None, None)
store = open_store(cfg)
conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
try:
    vec, _ = encode_query(cfg, text="一只猫")
    shots, cut = semantic_search(conn, cfg, store, query_vec=vec, recall_k=20)
    enrich(conn, cfg, shots)
    print("recalled:", len(shots), "cut:", cut.note)
    ok_thumb = ok_path = 0
    for h in shots[:8]:
        tp = h.thumb_path
        exists = Path(str(tp)).exists() if tp else False
        pexists = Path(str(h.path)).exists() if h.path else False
        print(f"  fid={h.file_id} shot={h.shot_ix} kind={h.kind} score={h.score:.4f}")
        print(f"      thumb_path={tp!r} exists={exists}")
        print(f"      path      ={h.path!r} exists={pexists}")
        ok_thumb += bool(exists)
        ok_path += bool(pexists)
    print("first 8: thumb exists =", ok_thumb, " path exists =", ok_path)
    # 再看原始 payload
    hits = store.search_knn("materials_semantic", vec, 3)
    for hh in hits:
        print("  raw payload:", {k: hh.payload.get(k) for k in
                                 ("kind", "file_id", "shot_ix", "sec", "thumb_path", "quality", "face_id")})
finally:
    conn.close()
