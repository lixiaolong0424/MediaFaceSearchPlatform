"""只读导览：library.db 的表结构 + 行数 + 关键列（给用户看的"数据库里有什么"）。"""

import sqlite3
import sys

sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')

from src import db as D
from src.common import load_config

cfg = load_config(None, None)
print(f"库文件：{cfg.db_path}")
print(f"只读连接：file:{cfg.db_path.as_posix()}?mode=ro\n")

conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
print("journal_mode =", conn.execute("PRAGMA journal_mode").fetchone()[0],
      "| page_size =", conn.execute("PRAGMA page_size").fetchone()[0],
      "| schema 版本 =", D.current_version(conn))
conn.close()

conn = sqlite3.connect(f"file:{cfg.db_path.as_posix()}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row
tables = [r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]

print(f"\n{'表名':<20}{'行数':>12}   说明")
print("-" * 78)
desc = {
    "files": "素材主表：每个媒体文件一行（路径/状态/分辨率/时间）",
    "shots": "镜头表：视频切分出的镜头（照片是 shot_ix=0）",
    "face_records": "人脸表：一张脸一行（bbox/质量/缩略图/向量已入库标记）",
    "file_encoding": "两阶段入库状态：每文件×每模型一行 done/pending/error/skipped_dup",
    "stage_status": "各阶段断点续跑标记：dedup/extract/face_detect/face_encode/semantic_encode",
    "path_index": "路径哈希索引：增量扫描秒级比对用",
    "libraries": "索引根：扫过哪些 media_root、最近扫描时间",
    "dead_letter": "死信表：反复失败的文件（含失败次数与原因）",
    "audit_log": "审计日志：移动/改名等操作留痕",
    "judgments": "标注表：TREC 风格，调阈值的依据",
    "query_sessions": "检索会话缓存",
    "person_labels": "人脸聚类命名（P3，未启用）",
    "db_version": "迁移版本",
}
for t in tables:
    try:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    except sqlite3.Error:
        n = -1
    print(f"{t:<20}{n:>12,}   {desc.get(t, '')}")

print("\n=== 关键表的列 ===")
for t in ("files", "face_records", "file_encoding", "stage_status", "shots"):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
    print(f"\n{t} ({len(cols)} 列):")
    print("  " + ", ".join(cols))

print("\n=== 索引 ===")
for r in conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type='index' "
                      "AND name NOT LIKE 'sqlite_%' ORDER BY tbl_name, name"):
    print(f"  {r['tbl_name']:<16} {r['name']}")

print("\n=== files 状态分布 ===")
for r in conn.execute("SELECT kind, status, COUNT(*) n FROM files GROUP BY kind,status ORDER BY n DESC"):
    print(f"  {r['kind']:<7} {r['status']:<12} {r['n']:>9,}")
conn.close()
