"""端到端接线自检：照片墙 / 检索 / 统计 在真实（进行中的）库上是否可用。

不依赖 Streamlit，直接调 UI 层与检索层的函数，能在流水线还在跑的时候随时跑。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import db as D  # noqa: E402
from src import ui_wall  # noqa: E402
from src.common import load_config  # noqa: E402
from src.face_pipeline import face_stats  # noqa: E402

cfg = load_config(None, None)
conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)

print("=== library_stats ===")
st = D.library_stats(conn, str(cfg.media_root))
for k in ("files_total", "files_done", "files_pending", "files_skipped_dup",
          "photos", "videos", "shots", "faces", "faces_alive",
          "faces_pending_vector", "files_missing", "files_error"):
    print(f"  {k:<22} {st.get(k)}")
print(f"  next_step              {st.get('next_step')}")

print("\n=== face_stats ===")
for k, v in face_stats(conn, cfg).items():
    print(f"  {k:<22} {v}")

print("\n=== wall_page（平铺，抽 5 条） ===")
try:
    page = ui_wall.wall_page(conn, cfg, limit=5, mode="tile", min_quality=0.0)
    print(f"  total={page['total']} has_more={page['has_more']} items={len(page['items'])}")
    for it in page["items"][:3]:
        print("   ", {k: it[k] for k in ("face_id", "file_id", "kind", "quality", "timecode")})
except Exception as exc:  # noqa: BLE001
    print("  ERR", type(exc).__name__, exc)

print("\n=== wall_facets ===")
try:
    fac = ui_wall.wall_facets(conn, cfg)
    print(f"  files={len(fac['files'])} faces_total={fac['faces_total']} range={fac['date_range']}")
except Exception as exc:  # noqa: BLE001
    print("  ERR", type(exc).__name__, exc)

print("\n=== 检索路由（空向量库时必须是可读 warning，不是崩溃） ===")
try:
    from src import search as SE
    from src.vector_store import open_store

    store = open_store(cfg)
    ok, msg = store.health()
    print(f"  qdrant health: {ok} {msg}")
    out = SE.search(conn, cfg, store, query_mode="text", payload={"text": "猫"})
    print(f"  mode={out.mode} badge={out.badge} files={len(out.files)} warning={out.warning[:80]!r}")
except Exception as exc:  # noqa: BLE001
    print("  ERR", type(exc).__name__, exc)

conn.close()
print("\nOK")
