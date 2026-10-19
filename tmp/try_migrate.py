# -*- coding: utf-8 -*-
"""在**副本**上演练 migrations/003，量耗时并验证幂等，绝不碰生产库。

用法：
    .venv\\Scripts\\python.exe tmp\try_migrate.py
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(ROOT))

from src.common import load_config  # noqa: E402
from src import db as D  # noqa: E402


def main() -> int:
    cfg = load_config()
    src = cfg.db_path
    dst = ROOT / "tmp" / "test_migrate.db"
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(dst) + suffix)
        if p.exists():
            p.unlink()
    t0 = time.monotonic()
    shutil.copy2(src, dst)
    print(f"copy {src.name} -> {dst.name}  {time.monotonic()-t0:.1f}s  "
          f"{dst.stat().st_size/2**20:.0f} MB")

    conn = D.connect(dst)
    print(f"from_version = {D.current_version(conn)}")
    conn.close()

    t0 = time.monotonic()
    res = D.migrate(dst, ROOT)
    print(f"migrate -> v{res['to_version']}  applied={res['applied']}  "
          f"用时 {time.monotonic()-t0:.1f}s")

    conn = D.connect(dst)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print("tables:", tables)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(files)")]
    print("files has content_hash:", "content_hash" in cols, "| lease_until:", "lease_until" in cols)
    fcols = [r[1] for r in conn.execute("PRAGMA table_info(face_records)")]
    print("face_records has face_ix:", "face_ix" in fcols)
    n_null = conn.execute("SELECT COUNT(*) FROM face_records WHERE face_ix IS NULL").fetchone()[0]
    print(f"face_ix NULL 行数: {n_null}")
    dup = conn.execute(
        "SELECT COUNT(*) FROM (SELECT file_id, shot_ix, face_ix, COUNT(*) c "
        "FROM face_records GROUP BY 1,2,3 HAVING c>1)"
    ).fetchone()[0]
    print(f"重复 (file_id,shot_ix,face_ix) 组数: {dup}")
    ix = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'uk_%'")]
    print("unique indexes:", ix)
    print("sample:", conn.execute(
        "SELECT face_id, file_id, shot_ix, face_ix FROM face_records ORDER BY face_id LIMIT 5"
    ).fetchall())
    conn.close()

    # 幂等：再跑一次 ensure_schema 不应重复执行
    t0 = time.monotonic()
    v = D.ensure_schema(dst, ROOT)
    print(f"ensure_schema 二次调用 -> v{v}  {time.monotonic()-t0:.2f}s（应 <= 0.5s）")

    # 回滚脚本可用性：模拟「迁移失败」
    print("\n演练完成；生产库未被改动。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
