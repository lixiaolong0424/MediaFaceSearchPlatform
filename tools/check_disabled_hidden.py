# -*- coding: utf-8 -*-
"""验证停用后：照片墙分组、分组弹窗、搜索 三处都不再出现停用目录的内容。"""
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import cluster as C  # noqa: E402
from src.common import load_config  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


cfg = load_config()
con = sqlite3.connect(str(cfg.db_path))
con.row_factory = sqlite3.Row


def one(sql, *a):
    return int(con.execute(sql, a).fetchone()[0] or 0)


v = C.current_version(cfg)
print("当前版本:", v)

print("\n=== 1) 照片墙分组 ===")
n_groups = one("SELECT COUNT(*) FROM cluster_meta WHERE version=?", v)
bad_groups = one("""SELECT COUNT(*) FROM cluster_meta m WHERE m.version=? AND NOT EXISTS (
    SELECT 1 FROM face_clusters fc
    JOIN face_records fr ON fr.face_id=fc.face_id
    JOIN files f ON f.file_id=fr.file_id
    WHERE fc.version=m.version AND fc.cluster_id=m.cluster_id
      AND fr.status='alive' AND f.visible=1 AND f.status!='missing')""", v)
print(f"  组数 {n_groups}，其中「全部成员不可见」的组 {bad_groups}")
check("照片墙上没有被停用目录撑起来的空组", bad_groups == 0, bad_groups)

bad_cover = one("""SELECT COUNT(*) FROM cluster_meta m
    JOIN face_records fr ON fr.face_id=m.cover_face_id
    JOIN files f ON f.file_id=fr.file_id
    WHERE m.version=? AND f.visible=0""", v)
print(f"  封面来自停用目录的组 {bad_cover}")
check("封面不再指向停用目录", bad_cover == 0, bad_cover)

print("\n=== 2) 分组弹窗成员 ===")
row = con.execute("SELECT cluster_id FROM cluster_meta WHERE version=? LIMIT 1", (v,)).fetchone()
if row:
    from src.ui_groups import _members

    mem = _members(cfg, v, int(row["cluster_id"]), limit=60)
    hidden = [m for m in mem if m.get("file_status") == "missing"]
    print(f"  第 {row['cluster_id']} 组成员 {len(mem)} 张，其中 missing {len(hidden)}")
    # 直接查这些 face_id 的文件可见性
    if mem:
        ids = [int(m["face_id"]) for m in mem]
        q = ",".join("?" * len(ids))
        vis = con.execute(
            f"SELECT COUNT(*) FROM face_records fr JOIN files f ON f.file_id=fr.file_id "
            f"WHERE fr.face_id IN ({q}) AND f.visible=1", ids).fetchone()[0]
        check("弹窗成员全部来自可见目录", int(vis) == len(ids), f"{vis}/{len(ids)}")

print("\n=== 3) 搜索 ===")
allowed = one("SELECT COUNT(*) FROM files WHERE status='done' AND visible=1")
total = one("SELECT COUNT(*) FROM files WHERE status='done'")
print(f"  可检索文件 {allowed:,} / 全部 {total:,}")
check("搜索候选集只含可见文件", allowed < total and allowed > 0, f"{allowed}/{total}")

# 真跑一次检索，检查命中是否都在可见集合内
from src import db as D  # noqa: E402
from src import search as S  # noqa: E402

conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
try:
    t0 = time.monotonic()
    out = S.search(conn, cfg, text="海边", topk=30)
    dt = time.monotonic() - t0
    hits = out.get("files") or []
    ids = [int(h["file_id"]) for h in hits]
    q = ",".join("?" * len(ids)) if ids else "0"
    hidden_hits = int(conn.execute(
        f"SELECT COUNT(*) FROM files WHERE file_id IN ({q}) AND visible=0", ids).fetchone()[0]) if ids else 0
    print(f"  检索「海边」耗时 {dt:.1f}s，命中 {len(hits)} 个文件，其中不可见的 {hidden_hits}")
    check("检索结果里没有停用目录的文件", hidden_hits == 0, hidden_hits)
except Exception as exc:  # noqa: BLE001
    check("检索可执行", False, f"{type(exc).__name__}: {exc}")

con.close()
print("\n" + "=" * 50)
print(f"PASS={PASS} FAIL={FAIL}")
print("=" * 50)
raise SystemExit(0 if FAIL == 0 else 1)
