"""查清「全库变 missing」的成因：是不是 media_root 被改了之后重扫过。"""

import sqlite3
import sys

sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')
from src.common import load_config  # noqa: E402

cfg = load_config(None, None)
print("=== 当前 config.media_root ===")
print("  ", cfg.get("media_root"))

conn = sqlite3.connect(r'data\library.db')
conn.row_factory = sqlite3.Row

print("\n=== libraries 表（每个索引根一行）===")
for r in conn.execute("SELECT root, last_scan, status FROM libraries ORDER BY last_scan DESC"):
    like = str(r["root"]).rstrip("\\/") + "%"
    n = conn.execute("SELECT COUNT(*) FROM files WHERE path LIKE ?", (like,)).fetchone()[0]
    print(f"  root={r['root']!r}  last_scan={r['last_scan']}  status={r['status']}  该根下文件数={n:,}")

print("\n=== 文件状态总和 ===")
tot = 0
for r in conn.execute("SELECT kind, status, COUNT(*) n FROM files GROUP BY kind,status ORDER BY kind,n DESC"):
    print(f"  {r['kind']:<6} {r['status']:<12} {r['n']:>8,}")
    tot += r["n"]
print(f"  合计 {tot:,}")

print("\n=== 路径前缀分布（前 8）===")
import collections
cnt = collections.Counter()
for (p,) in conn.execute("SELECT path FROM files"):
    seg = str(p).replace("/", "\\").split("\\")
    cnt["\\".join(seg[:3])] += 1
for k, v in cnt.most_common(8):
    print(f"  {v:>8,}  {k}")

print("\n=== skipped_dup 是否被抹掉 ===")
print("  files.status='skipped_dup':",
      conn.execute("SELECT COUNT(*) FROM files WHERE status='skipped_dup'").fetchone()[0])
print("  file_encoding skipped_dup (semantic):",
      conn.execute("SELECT COUNT(*) FROM file_encoding WHERE model='semantic' AND status='skipped_dup'").fetchone()[0])

print("\n=== 最近入库的一批（说明新扫进来的东西）===")
for r in conn.execute(
    "SELECT file_id, path, kind, status, indexed_at FROM files "
    "WHERE status='pending' ORDER BY file_id DESC LIMIT 5"):
    print(f"  fid={r['file_id']} {r['kind']:<6} {r['status']:<8} {str(r['path'])[:78]}")
conn.close()
