"""SQLite 访问层：WAL 连接、迁移执行器、入库两阶段状态机、常用 DAO。

规格铁律（4.2）：
  1. 去重跳过的文件必须在 file_encoding 里为每个模型各记一行 skipped_dup。
  2. 文件被删除只标 status=missing，绝不物理删记录；改名/移动只更新 path。
  3. 两阶段入库：pending → 写 Qdrant → done + encoded_at；检索只 join done。

并发与锁（第十章）：读连接独立只读走 WAL snapshot；扫描与 Web 不用同一 db 句柄；
入库期对单 file_id 行级锁，禁止全库写锁阻塞检索。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .common import Config, get_logger, local_now_iso, path_hash as make_path_hash

LOG = get_logger("db")

MIGRATIONS_DIRNAME = "migrations"

# 所有需要「每模型一行」的模型标识（铁律 1）
MODEL_SEMANTIC = "semantic"
MODEL_FACE = "face"

# 单库多路径：libraries 表以 root 为主键
ST_FIELD_NAMES = (
    "file_id", "path", "rel_path", "path_hash", "inode", "size", "mtime", "kind",
    "status", "phash", "captured_at", "time_source", "visible", "kind_hint",
    "duration_sec", "width", "height", "indexed_at", "updated_at",
)


# --------------------------------------------------------------------------- #
# 连接
# --------------------------------------------------------------------------- #


def _apply_pragmas(conn: sqlite3.Connection, cfg: Optional[Config] = None) -> None:
    journal = (cfg.get("db.journal_mode", "WAL") if cfg else "WAL")
    sync = (cfg.get("db.synchronous", "NORMAL") if cfg else "NORMAL")
    conn.execute(f"PRAGMA journal_mode={journal}")
    conn.execute(f"PRAGMA synchronous={sync}")
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("PRAGMA temp_store=MEMORY")
    # busy_timeout 必须足够长：照片链现在**故意**让 3 个语义编码进程与抽帧并行跑
    # （抽帧在等机械盘时 GPU 正好被编码用满，实测 3 进程 2.65x）。
    # 抽帧每 batch_commit_files 个文件才提交一次，期间持有写锁；
    # 15 秒会被超过 → 编码进程直接抛 "database is locked" 死掉。
    # 60 秒 + 下面的 retry_on_lock 一起保证并发写入不会互相打死。
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA cache_size=-65536")  # 64MB page cache


def connect(
    db_path: Path,
    *,
    cfg: Optional[Config] = None,
    readonly: bool = False,
    timeout: float = 60.0,
) -> sqlite3.Connection:
    """打开连接。readonly=True 用于检索/UI（独立句柄，不阻塞扫描写入）。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if readonly and db_path.exists():
        uri = f"file:{db_path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=timeout, check_same_thread=False)
    else:
        conn = sqlite3.connect(str(db_path), timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn, cfg)
    return conn


def retry_on_lock(fn: Any, *args: Any, attempts: int = 8, delay: float = 2.0, **kwargs: Any) -> Any:
    """并发写入专用：在 database is locked 时退避重试。

    为什么必须有它：照片链里 3 个语义编码进程与抽帧进程同时在写同一个库
    （2026-10-16 起是**设计如此**，为了把 24 小时的 GPU 编码藏进 17 小时的读盘里）。
    只靠 busy_timeout 不够健壮 —— 抽帧一次写事务最长可持有约一分钟，
    超时后编码进程会直接抛错退出，丢掉整轮工作。这里再退避重试，
    把「偶发的锁竞争」变成「等一会儿就好」。
    """
    last: Optional[Exception] = None
    for i in range(max(1, attempts)):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            last = exc
            if i < attempts - 1:
                LOG.warning("数据库被占用（第 %d/%d 次重试，%.1fs 后继续）：%s",
                            i + 1, attempts, delay, exc)
                time.sleep(delay)
    assert last is not None
    raise last


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式写事务。批量提交由调用方按 batch_commit_files 控制。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@contextmanager
def row_lock(lock: threading.Lock) -> Iterator[None]:
    """入库期对单 file_id 行级串行化（进程内），避免全库写锁阻塞检索。"""
    with lock:
        yield


# --------------------------------------------------------------------------- #
# 迁移
# --------------------------------------------------------------------------- #


def discover_migrations(root: Path) -> List[Tuple[int, Path]]:
    """返回按版本号升序的 (version, path)。文件名形如 001_init.sql。"""
    mdir = root / MIGRATIONS_DIRNAME
    if not mdir.exists():
        return []
    out: List[Tuple[int, Path]] = []
    for p in sorted(mdir.glob("*.sql")):
        stem = p.stem.split("_", 1)[0]
        if stem.isdigit():
            out.append((int(stem), p))
    out.sort(key=lambda t: t[0])
    return out


def current_version(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT MAX(v) AS v FROM db_version").fetchone()
        return int(row["v"]) if row and row["v"] is not None else 0
    except sqlite3.OperationalError:
        return 0


def migrate(db_path: Path, root: Path, cfg: Optional[Config] = None, *, dry_run: bool = False) -> Dict[str, Any]:
    """顺序执行版本号大于当前的脚本，每个脚本包在一个事务里，失败回滚。"""
    migrations = discover_migrations(root)
    conn = connect(db_path, cfg=cfg)
    try:
        ver = current_version(conn)
        applied: List[int] = []
        skipped: List[int] = []
        for version, path in migrations:
            if version <= ver:
                skipped.append(version)
                continue
            sql = path.read_text(encoding="utf-8")
            if dry_run:
                LOG.info("[dry-run] 将执行迁移 %03d（%s，%d 字节）", version, path.name, len(sql))
                applied.append(version)
                continue
            try:
                conn.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
                # executescript 会隐式提交，db_version 单独写入并立即提交
                conn.execute("INSERT OR REPLACE INTO db_version(v) VALUES (?)", (version,))
                conn.commit()
                applied.append(version)
                LOG.info("迁移 %03d 完成（%s）", version, path.name)
            except sqlite3.Error as exc:
                conn.rollback()
                LOG.error("迁移 %03d 失败并已回滚：%s", version, exc)
                raise
        result = {
            "db_path": str(db_path),
            "from_version": ver,
            "to_version": current_version(conn),
            "applied": applied,
            "already_applied": skipped,
            "dry_run": dry_run,
        }
        return result
    finally:
        conn.close()


def ensure_schema(db_path: Path, root: Path, cfg: Optional[Config] = None) -> int:
    """幂等入口：保证库存在且 schema 是最新。返回当前版本。"""
    conn = connect(db_path, cfg=cfg)
    try:
        ver = current_version(conn)
    finally:
        conn.close()
    migrations = discover_migrations(root)
    latest = max((v for v, _ in migrations), default=0)
    if ver < latest:
        migrate(db_path, root, cfg)
        conn = connect(db_path, cfg=cfg)
        try:
            return current_version(conn)
        finally:
            conn.close()
    return ver


# --------------------------------------------------------------------------- #
# libraries
# --------------------------------------------------------------------------- #


def upsert_library(conn: sqlite3.Connection, root: str, **fields: Any) -> None:
    conn.execute(
        "INSERT INTO libraries(root) VALUES (?) ON CONFLICT(root) DO NOTHING", (root,)
    )
    if fields:
        sets = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE libraries SET {sets} WHERE root=?", (*fields.values(), root))


def get_library(conn: sqlite3.Connection, root: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM libraries WHERE root=?", (root,)).fetchone()


def library_stats(conn: sqlite3.Connection, root: str, *,
                  with_top_dirs: bool = False) -> Dict[str, Any]:
    """给 UI 用的库统计。

    `root` 参数只是「config 里配置的 media_root」，仅作展示；
    **实际索引的根**以 libraries 表为准（indexed_roots），
    两者不一致时 UI 会明确提示——否则很容易误以为「扫了但没进库」。

    with_top_dirs：是否统计「每个索引根下的顶层目录分布」。
    这个统计要遍历该根下所有 path（实测 43.5 万行约 1.3s），
    所以默认关闭、只在用户展开详情面板时才付出这个代价。
    """
    def one(sql: str, *a: Any) -> Any:
        row = conn.execute(sql, a).fetchone()
        return row[0] if row else 0

    def safe(sql: str, arg: Any = None) -> List[sqlite3.Row]:
        try:
            return list(conn.execute(sql) if arg is None else conn.execute(sql, (arg,)))
        except sqlite3.Error:
            return []

    by_status = {str(r["status"]): int(r["n"]) for r in safe(
        "SELECT status, COUNT(*) n FROM files GROUP BY status")}

    # 一次 GROUP BY 拿到「类型 × 状态」全部计数。
    # 原实现是 7 条独立 COUNT（photos / videos / photos_done / videos_done /
    # videos_pending / videos_error / …），每一条都在 51 万行上做一次全表扫描，
    # 合计 4~5 秒 —— 而 Streamlit 每次重跑都要付这个代价（界面就卡住变白）。
    # 配合 migration 004 的 idx_files_kind_status，这条现在走索引。
    by_kind: Dict[str, Dict[str, int]] = {}
    for r in safe("SELECT kind, status, COUNT(*) n FROM files GROUP BY kind, status"):
        by_kind.setdefault(str(r["kind"]), {})[str(r["status"])] = int(r["n"])

    def kcount(kind: str, *statuses: str) -> int:
        d = by_kind.get(kind, {})
        if not statuses:
            return sum(d.values())
        return sum(int(d.get(s, 0)) for s in statuses)

    # 实际索引的根 + 各自文件数（libraries 记录 + path 前缀计数）
    indexed_roots: List[Dict[str, Any]] = []
    top_dirs: List[Dict[str, Any]] = []
    for r in safe("SELECT root, last_scan, status FROM libraries ORDER BY last_scan DESC"):
        root_s = str(r["root"])
        like = root_s.rstrip("\\/") + "%"
        n_files = one("SELECT COUNT(*) FROM files WHERE path LIKE ?", like)
        indexed_roots.append({
            "indexed_root": root_s,
            "files": int(n_files),
            "last_scan": r["last_scan"],
            "scan_status": r["status"],
        })
        if not with_top_dirs:
            continue
        # 顶层目录用 rel_path 的第一段（比按 path 推层级可靠，见 12_maintain.cmd_status 注释）
        counter: Dict[str, int] = {}
        for row in safe("SELECT rel_path, path FROM files WHERE path LIKE ?", like):
            text = str(row["rel_path"] or row["path"])
            seg = text.replace("/", "\\").lstrip("\\").split("\\", 1)[0]
            if seg:
                counter[seg] = counter.get(seg, 0) + 1
        for name, n in sorted(counter.items(), key=lambda kv: -kv[1])[:12]:
            top_dirs.append({"root": root_s, "dir": name, "files": n})

    # 下一步建议
    total = int(one("SELECT COUNT(*) FROM files"))
    pending = by_status.get("pending", 0)
    done = by_status.get("done", 0)
    enc_done = int(one(
        "SELECT COUNT(*) FROM file_encoding WHERE model='semantic' AND status='done'"))
    # 人脸统计同样合并成一条：原来 3 条 COUNT 每次各扫 29 万行。
    # 「已检脸但 F3 未编码」的数量必须与「墙上可见」分开报，
    # 否则用户会看到「人脸总数 3 万但墙是空的」。
    faces_by_vec = {int(r["has_vector"] or 0): int(r["n"]) for r in safe(
        "SELECT has_vector, COUNT(*) n FROM face_records "
        "WHERE status='alive' GROUP BY has_vector")}
    faces = sum(faces_by_vec.values())
    faces_pending = faces_by_vec.get(0, 0)
    faces_ready = faces_by_vec.get(1, 0)
    if total == 0:
        nxt = f'库是空的。先扫描：01_manifest.py --root "{root}"'
    elif pending > 0:
        # 照片侧默认把 L2a 折进 03_extract 的同趟读盘（config scan.fuse_photo_pipeline），
        # 所以推荐顺序是 03 → 06 → 07 → 04，而不是旧的 02 → 03 → 04。
        nxt = (f"还有 {pending:,} 个 pending。推荐顺序："
               f"03_extract.py → 06_face_detect.py → 07_face_encode.py → 04_encode.py"
               f"（无人值守一条命令：pwsh -File tmp\\chain_rounds.ps1 -Stage all）")
    elif enc_done < done:
        nxt = "文件已抽帧但未编码，下一阶段：04_encode.py"
    elif faces_pending > 0:
        nxt = (f"已检出 {faces_pending:,} 张人脸但还没有向量与缩略图，"
               f"下一阶段：07_face_encode.py")
    elif faces_ready == 0:
        nxt = "素材已编码，但人脸库为空 —— 下一阶段：06_face_detect.py → 07_face_encode.py"
    else:
        nxt = "索引就绪，可在 Tab1/Tab2 浏览与检索。"

    return {
        "root": root,                       # config 里的值（展示用）
        "configured_root": root,
        "indexed_roots": indexed_roots,     # 实际索引的根（权威）
        "indexed_top_dirs": top_dirs,
        "next_step": nxt,
        "by_status": by_status,
        "files_total": total,
        "files_done": done,
        "files_pending": pending,
        "files_error": by_status.get("error", 0),
        "files_missing": by_status.get("missing", 0),
        "files_skipped_dup": by_status.get("skipped_dup", 0),
        "photos": kcount("photo"),
        "videos": kcount("video"),
        # 视频进度（2026-10-18 加）：界面侧边栏原来只显示视频**总数**，
        # 看不出「已处理多少」——视频阶段要跑十几小时，这个数字很关键。
        "photos_done": kcount("photo", "done"),
        "videos_done": kcount("video", "done"),
        "videos_pending": kcount("video", "pending", "error"),
        "videos_error": kcount("video", "error"),
        "shots": int(one("SELECT COUNT(*) FROM shots")),
        "faces": faces_ready,             # 照片墙唯一数据源 = 已完成 F3 的记录
        "faces_alive": faces,             # F1 已检出（含尚未编码的中间态）
        "faces_pending_vector": faces_pending,
        "faces_ready": faces_ready,
        "last_scan": one("SELECT MAX(last_scan) FROM libraries"),
        "by_kind_status": by_kind,
    }


# --------------------------------------------------------------------------- #
# files
# --------------------------------------------------------------------------- #

_UPSERT_FILE_SQL = """
INSERT INTO files (path, rel_path, path_hash, inode, size, mtime, kind, status,
                   captured_at, time_source, visible, duration_sec, width, height,
                   root, updated_at)
VALUES (:path, :rel_path, :path_hash, :inode, :size, :mtime, :kind, :status,
        :captured_at, :time_source, :visible, :duration_sec, :width, :height,
        :root, :updated_at)
ON CONFLICT(path) DO UPDATE SET
    rel_path     = excluded.rel_path,
    path_hash    = excluded.path_hash,
    inode        = excluded.inode,
    size         = excluded.size,
    mtime        = excluded.mtime,
    kind         = excluded.kind,
    root         = COALESCE(excluded.root, files.root),
    updated_at   = excluded.updated_at
"""


def upsert_file(conn: sqlite3.Connection, **fields: Any) -> int:
    """按 path upsert，返回 file_id。已存在则只更新身份字段（保留 status/向量/人脸）。"""
    fields.setdefault("rel_path", None)
    fields.setdefault("path_hash", make_path_hash(fields["path"]))
    fields.setdefault("root", None)
    fields.setdefault("inode", None)
    fields.setdefault("size", None)
    fields.setdefault("mtime", None)
    fields.setdefault("kind", "unknown")
    fields.setdefault("status", "pending")
    fields.setdefault("captured_at", None)
    fields.setdefault("time_source", None)
    fields.setdefault("visible", 1)
    fields.setdefault("duration_sec", None)
    fields.setdefault("width", None)
    fields.setdefault("height", None)
    fields["updated_at"] = local_now_iso()
    conn.execute(_UPSERT_FILE_SQL, fields)
    row = conn.execute("SELECT file_id FROM files WHERE path=?", (fields["path"],)).fetchone()
    return int(row["file_id"])


def set_file_status(conn: sqlite3.Connection, file_id: int, status: str, **fields: Any) -> None:
    fields["status"] = status
    fields["updated_at"] = local_now_iso()
    if status == "done" and "indexed_at" not in fields:
        fields["indexed_at"] = local_now_iso()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE files SET {sets} WHERE file_id=?", (*fields.values(), file_id))


def update_file_path(conn: sqlite3.Connection, file_id: int, new_path: str,
                     rel_path: Optional[str] = None) -> None:
    """改名/移动：只更新 path，保留全部向量与人脸记录（铁律 2）。"""
    conn.execute(
        "UPDATE files SET path=?, rel_path=?, path_hash=?, updated_at=? WHERE file_id=?",
        (new_path, rel_path, make_path_hash(rel_path or new_path), local_now_iso(), file_id),
    )


def mark_missing(conn: sqlite3.Connection, file_ids: Sequence[int]) -> int:
    if not file_ids:
        return 0
    q = ",".join("?" * len(file_ids))
    cur = conn.execute(
        f"UPDATE files SET status='missing', updated_at=? WHERE file_id IN ({q})",
        (local_now_iso(), *file_ids),
    )
    return cur.rowcount


def touch_hits(conn: sqlite3.Connection, file_ids: Sequence[int]) -> int:
    """记录「文件被检索命中」的时刻，供 frames/ 的 LRU 清理判断是否 regenerate。

    规格第三章：frames LRU 淘汰「最近 N 天未被命中的关键帧」。没有这张时间戳
    就只能猜，因此检索层每次返回结果都应调用本函数。
    """
    ids = sorted({int(i) for i in file_ids})
    if not ids:
        return 0
    now = local_now_iso()
    total = 0
    for chunk in _chunks(ids, 900):
        q = ",".join("?" * len(chunk))
        total += conn.execute(
            f"UPDATE files SET last_hit_at=?, hit_count=COALESCE(hit_count,0)+1 WHERE file_id IN ({q})",
            (now, *chunk),
        ).rowcount
    return total


def files_by_ids(conn: sqlite3.Connection, ids: Sequence[int]) -> Dict[int, sqlite3.Row]:
    if not ids:
        return {}
    out: Dict[int, sqlite3.Row] = {}
    for chunk in _chunks(list(ids), 900):
        q = ",".join("?" * len(chunk))
        for row in conn.execute(f"SELECT * FROM files WHERE file_id IN ({q})", chunk):
            out[int(row["file_id"])] = row
    return out


# --------------------------------------------------------------------------- #
# file_encoding 两阶段状态机
# --------------------------------------------------------------------------- #


def enc_begin(conn: sqlite3.Connection, file_id: int, model: str, n_frames: Optional[int] = None) -> None:
    """阶段一：pending。让崩溃后能被 repair 捞回来。"""
    conn.execute(
        """INSERT INTO file_encoding(file_id, model, status, n_frames, error, encoded_at)
           VALUES (?,?, 'pending', ?, NULL, NULL)
           ON CONFLICT(file_id, model) DO UPDATE SET
               status='pending', n_frames=excluded.n_frames, error=NULL, encoded_at=NULL""",
        (file_id, model, n_frames),
    )


def enc_done(conn: sqlite3.Connection, file_id: int, model: str, n_frames: Optional[int] = None) -> None:
    """阶段三：done + encoded_at。检索侧只 join done。"""
    conn.execute(
        """UPDATE file_encoding SET status='done', n_frames=?, error=NULL, encoded_at=?
           WHERE file_id=? AND model=?""",
        (n_frames, local_now_iso(), file_id, model),
    )


def enc_error(conn: sqlite3.Connection, file_id: int, model: str, error: str) -> None:
    conn.execute(
        """INSERT INTO file_encoding(file_id, model, status, error, encoded_at)
           VALUES (?,?, 'error', ?, ?)
           ON CONFLICT(file_id, model) DO UPDATE SET
               status='error', error=excluded.error, encoded_at=excluded.encoded_at""",
        (file_id, model, error[:2000], local_now_iso()),
    )


def enc_skipped_dup(conn: sqlite3.Connection, file_id: int, model: str, note: str = "duplicate") -> None:
    """铁律 1：去重跳过的文件必须为每个模型各记一行 skipped_dup，否则统计数对不上。"""
    conn.execute(
        """INSERT INTO file_encoding(file_id, model, status, error, encoded_at)
           VALUES (?,?, 'skipped_dup', ?, ?)
           ON CONFLICT(file_id, model) DO UPDATE SET
               status='skipped_dup', error=excluded.error, encoded_at=excluded.encoded_at""",
        (file_id, model, note[:2000], local_now_iso()),
    )


def all_skipped_dup(conn: sqlite3.Connection, file_id: int, note: str = "duplicate") -> None:
    """为语义与人脸两个模型都写 skipped_dup。"""
    for model in (MODEL_SEMANTIC, MODEL_FACE):
        enc_skipped_dup(conn, file_id, model, note)


def enc_status_map(conn: sqlite3.Connection, model: str) -> Dict[int, str]:
    return {
        int(r["file_id"]): (r["status"] or "")
        for r in conn.execute("SELECT file_id, status FROM file_encoding WHERE model=?", (model,))
    }


def enc_counters(conn: sqlite3.Connection) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in conn.execute(
        "SELECT model, status, COUNT(*) AS n FROM file_encoding GROUP BY model, status"
    ):
        out[f"{row['model']}:{row['status']}"] = int(row["n"])
    return out


def pending_or_error(
    conn: sqlite3.Connection, model: str, max_attempts: int = 3
) -> List[int]:
    """repair 用：status 为 pending/error 且死信尝试次数未超限的 file_id。"""
    rows = conn.execute(
        """SELECT fe.file_id AS file_id,
                  (SELECT COUNT(*) FROM dead_letter dl
                    WHERE dl.file_id = fe.file_id AND dl.stage = fe.model) AS attempts
             FROM file_encoding fe
            WHERE fe.model=? AND fe.status IN ('pending','error')""",
        (model,),
    ).fetchall()
    return [int(r["file_id"]) for r in rows if int(r["attempts"] or 0) < max_attempts]


# --------------------------------------------------------------------------- #
# shots
# --------------------------------------------------------------------------- #


def upsert_shot(conn: sqlite3.Connection, file_id: int, shot_ix: int,
                shot_start: float, shot_end: float, n_frames: Optional[int] = None) -> None:
    conn.execute(
        """INSERT INTO shots(file_id, shot_ix, shot_start, shot_end, n_frames)
           VALUES (?,?,?,?,?)
           ON CONFLICT(file_id, shot_ix) DO UPDATE SET
               shot_start=excluded.shot_start, shot_end=excluded.shot_end,
               n_frames=excluded.n_frames""",
        (file_id, shot_ix, shot_start, shot_end, n_frames),
    )


def delete_shots(conn: sqlite3.Connection, file_id: int) -> None:
    conn.execute("DELETE FROM shots WHERE file_id=?", (file_id,))


def shots_for_file(conn: sqlite3.Connection, file_id: int) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM shots WHERE file_id=? ORDER BY shot_ix", (file_id,)
    ))


def shot_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM shots").fetchone()
    return int(row["n"]) if row else 0


# --------------------------------------------------------------------------- #
# path_index（增量比对秒级完成）
# --------------------------------------------------------------------------- #


def load_path_index(conn: sqlite3.Connection,
                    root: Optional[str] = None) -> Dict[str, sqlite3.Row]:
    """path_hash → row。**按 root 过滤**：两个素材根下可能有同名相对路径，
    用全局字典会互相覆盖（这是多素材目录支持的前提）。"""
    if root is None:
        return {str(r["path_hash"]): r for r in conn.execute("SELECT * FROM path_index")}
    return {
        str(r["path_hash"]): r
        for r in conn.execute("SELECT * FROM path_index WHERE root=?", (root,))
    }


def upsert_path_index(conn: sqlite3.Connection, root: str, ph: str, file_id: int,
                      inode: Optional[int], mtime: Optional[int], size: Optional[int]) -> None:
    conn.execute(
        """INSERT INTO path_index(root, path_hash, file_id, inode, mtime, size, updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(root, path_hash) DO UPDATE SET
               file_id=excluded.file_id, inode=excluded.inode,
               mtime=excluded.mtime, size=excluded.size, updated_at=excluded.updated_at""",
        (root, ph, file_id, inode, mtime, size, local_now_iso()),
    )


def drop_path_index(conn: sqlite3.Connection, root: str, ph: str) -> None:
    conn.execute("DELETE FROM path_index WHERE root=? AND path_hash=?", (root, ph))



def insert_face(conn: sqlite3.Connection, **fields: Any) -> int:
    fields.setdefault("shot_ix", 0)
    fields.setdefault("sec", None)
    fields.setdefault("bbox", None)
    fields.setdefault("det_score", None)
    fields.setdefault("quality", None)
    fields.setdefault("track_id", None)
    fields.setdefault("thumb_path", None)
    fields.setdefault("raw_path", None)
    fields.setdefault("status", "alive")
    fields.setdefault("captured_at", None)
    fields.setdefault("hidden", 0)
    fields.setdefault("has_vector", 1)
    cols = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    cur = conn.execute(f"INSERT INTO face_records({cols}) VALUES ({marks})", tuple(fields.values()))
    return int(cur.lastrowid)


def face_wall_count(conn: sqlite3.Connection, *, min_quality: float = 0.0,
                    t_start: Optional[str] = None, t_end: Optional[str] = None,
                    file_id: Optional[int] = None, only_new: bool = False) -> int:
    sql = ["SELECT COUNT(*) AS n FROM face_records fr JOIN files f ON f.file_id = fr.file_id WHERE 1=1"]
    args: List[Any] = []
    sql.append("AND fr.status='alive' AND fr.has_vector=1 AND fr.hidden=0 AND f.visible=1")
    if min_quality > 0:
        sql.append("AND COALESCE(fr.quality,0) >= ?")
        args.append(min_quality)
    if t_start:
        sql.append("AND COALESCE(fr.captured_at, f.captured_at, f.mtime) >= ?")
        args.append(t_start)
    if t_end:
        sql.append("AND COALESCE(fr.captured_at, f.captured_at, f.mtime) <= ?")
        args.append(t_end)
    if file_id:
        sql.append("AND fr.file_id = ?")
        args.append(file_id)
    if only_new:
        sql.append("AND f.indexed_at >= datetime('now', '-1 day')")
    row = conn.execute(" ".join(sql), args).fetchone()
    return int(row["n"]) if row else 0


# --------------------------------------------------------------------------- #
# dead_letter / audit
# --------------------------------------------------------------------------- #


def add_dead_letter(conn: sqlite3.Connection, file_id: int, stage: str,
                    attempt: int, error: str) -> None:
    conn.execute(
        "INSERT INTO dead_letter(file_id, stage, attempt, error, at) VALUES (?,?,?,?,?)",
        (file_id, stage, attempt, error[:2000], local_now_iso()),
    )


def dead_letter_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM dead_letter").fetchone()
    return int(row["n"]) if row else 0


def audit(conn: sqlite3.Connection, who: str, what: str,
          before: Any = None, after: Any = None) -> None:
    conn.execute(
        "INSERT INTO audit_log(who, at, what, before, after) VALUES (?,?,?,?,?)",
        (who, local_now_iso(), what,
         json.dumps(before, ensure_ascii=False) if before is not None else None,
         json.dumps(after, ensure_ascii=False) if after is not None else None),
    )


# --------------------------------------------------------------------------- #
# query_sessions / judgments
# --------------------------------------------------------------------------- #


def save_query_session(conn: sqlite3.Connection, **fields: Any) -> None:
    cols = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    conn.execute(f"INSERT OR REPLACE INTO query_sessions({cols}) VALUES ({marks})",
                 tuple(fields.values()))


def add_judgment(conn: sqlite3.Connection, query_id: str, doc_id: str, rank: int,
                 score: float, label: int) -> None:
    conn.execute(
        "INSERT INTO judgments(query_id, doc_id, rank, score, label, at) VALUES (?,?,?,?,?,?)",
        (query_id, doc_id, rank, score, label, local_now_iso()),
    )


# --------------------------------------------------------------------------- #
# 杂项
# --------------------------------------------------------------------------- #


def _chunks(seq: Sequence[Any], n: int) -> Iterator[Sequence[Any]]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def integrity_check(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return row[0] if row else "unknown"


def wal_ok(db_path: Path, cfg: Optional[Config] = None) -> bool:
    """启动自检：确认 WAL 可用（可能需要能创建的目录）。"""
    try:
        conn = connect(db_path, cfg=cfg)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        return str(mode).lower() == "wal"
    except sqlite3.Error:
        return False


def mark_orphans_for_reconcile(
    conn: sqlite3.Connection, days: int = 30, model: str = MODEL_FACE
) -> List[Tuple[int, str]]:
    """reconcile 第二阶段：status=missing 超 N 天的记录 → (face_id, thumb_path) 列表。

    必须手动触发 + 二次确认 + 先输出 dry-run 报告（规格第三章）。
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    rows = conn.execute(
        """SELECT fr.face_id AS face_id, fr.thumb_path AS thumb_path
             FROM face_records fr JOIN files f ON f.file_id = fr.file_id
            WHERE f.status='missing' AND fr.status='alive'
              AND COALESCE(f.updated_at, f.indexed_at, f.mtime) < ?""",
        (cutoff,),
    ).fetchall()
    return [(int(r["face_id"]), r["thumb_path"] or "") for r in rows]
