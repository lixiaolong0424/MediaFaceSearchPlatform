# -*- coding: utf-8 -*-
"""列出 SQLite 全部表/视图/索引与行数，并核对语义/人脸向量的期望点数。

规格 v5.3 §15.2 要求「启动时自动发现 schema」。这里先把真实 schema 打出来，
供检查清单和后续配置驱动使用。
"""
from __future__ import annotations

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.common import load_config  # noqa: E402


def main() -> int:
    cfg = load_config()
    con = sqlite3.connect(str(cfg.db_path))
    con.row_factory = sqlite3.Row

    print("=" * 78)
    print(f"SQLite schema: {cfg.db_path}")
    print("=" * 78)
    objs = con.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    tables = [o for o in objs if o["type"] == "table"]
    views = [o for o in objs if o["type"] == "view"]
    idxs = [o for o in objs if o["type"] == "index"]

    for o in tables:
        n = con.execute(f"SELECT COUNT(*) FROM [{o['name']}]").fetchone()[0]
        cols = [r["name"] + (":" + r["type"] if r["type"] else "") for r in con.execute(
            f"PRAGMA table_info([{o['name']}])"
        )]
        print(f"\n[TABLE] {o['name']}  rows={n:,}")
        print(f"        cols: {', '.join(cols)}")

    for o in views:
        print(f"\n[VIEW ] {o['name']}")
    for o in idxs:
        print(f"[INDEX] {o['name']}: {o['sql']}")

    # ---- 期望点数核对 ----
    print("\n" + "=" * 78)
    print("向量点数核对（语义按 shot 分组，人脸按 face 逐条）")
    print("=" * 78)
    group = int(cfg.get("semantic.group_size", 8))

    # 每文件 shot 数
    rows = con.execute(
        """
        SELECT f.file_id, f.kind, COUNT(s.shot_ix) AS n_shots
        FROM files f LEFT JOIN shots s ON s.file_id = f.file_id
        WHERE f.status IN ('done','skipped_dup')
        GROUP BY f.file_id
        """
    ).fetchall()
    files_with_shots = sum(1 for r in rows if r["n_shots"] > 0)
    files_no_shots = sum(1 for r in rows if r["n_shots"] == 0)
    groups = sum(max(1, -(-r["n_shots"] // group)) if r["n_shots"] else 0 for r in rows)
    total_shots = sum(r["n_shots"] for r in rows)
    print(f"  参与统计的文件           : {len(rows):,}（有镜头 {files_with_shots:,} / 无镜头 {files_no_shots:,}）")
    print(f"  镜头总数                 : {total_shots:,}")
    print(f"  按 group_size={group} 期望语义点数 : {groups:,}")

    # 语义 done 的文件
    sem_files = con.execute(
        "SELECT COUNT(*) FROM file_encoding WHERE model='semantic' AND status='done'"
    ).fetchone()[0]
    print(f"  file_encoding semantic done 文件数 : {sem_files:,}")

    # 语义 done 文件的期望组数（用其自身镜头数）
    rows2 = con.execute(
        """
        SELECT f.file_id, COUNT(s.shot_ix) AS n_shots
        FROM file_encoding e
        JOIN files f ON f.file_id = e.file_id
        LEFT JOIN shots s ON s.file_id = f.file_id
        WHERE e.model='semantic' AND e.status='done'
        GROUP BY f.file_id
        """
    ).fetchall()
    exp_groups = sum(max(1, -(-r["n_shots"] // group)) if r["n_shots"] else 0 for r in rows2)
    print(f"  仅对 semantic done 文件计算的期望组数 : {exp_groups:,}")

    # 人脸：faces 表？没有的话看 shot_face 之类
    names = {r["name"] for r in tables}
    for cand in ("faces", "face_vectors", "shot_faces", "face_tracks", "persons", "person_labels"):
        if cand in names:
            n = con.execute(f"SELECT COUNT(*) FROM [{cand}]").fetchone()[0]
            print(f"  表 {cand:<20} 行数 {n:,}")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
