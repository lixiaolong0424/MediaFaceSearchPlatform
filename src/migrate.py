# -*- coding: utf-8 -*-
"""src/migrate.py · 素材路径变更（规格 v5.3 §4）。

三条铁律（规格原文）：
  1. 改路径 ≠ 清空库。默认保留所有向量、人脸、身份关系。
  2. 原图文件永远由用户管理，系统绝不删除用户文件。
  3. 破坏性操作必须「先出报告、后人工确认」。

本项目的路径模型让重映射非常便宜：
  files.path    = 绝对路径（E:\\迅雷下载\\a.jpg）
  files.rel_path= 相对素材根的路径（迅雷下载\\a.jpg），**不含根**
  files.path_hash = md5(rel_path.lower())

所以换根时 rel_path / path_hash / inode / size / mtime 全都不用动，
只需要把 path 的根前缀换掉 —— 一次 UPDATE 就够，向量一条都不用重算。
这也是「策略①保留全部」能做到几分钟完成的原因。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import db as D
from . import tasks as T
from .common import CONFIG_LOCAL_FILENAME, Config, get_logger, human_bytes, local_now_iso

LOG = get_logger("migrate")

STRATEGY_KEEP = "keep"        # ① 保留全部（默认）：旧数据一条不删，缺失的只标 missing
STRATEGY_HIDE = "hide"        # ② 隐藏旧数据：缺失的标 missing + visible=0，向量保留
STRATEGY_PURGE = "purge"      # ③ 彻底清理：缺失的物理删除向量与行（需二次确认）

STRATEGY_LABELS = {
    STRATEGY_KEEP: "保留全部（推荐）",
    STRATEGY_HIDE: "隐藏旧数据（向量保留，搜索不可见）",
    STRATEGY_PURGE: "彻底清理（物理删除向量与记录，不可恢复）",
}


def _prefix(root: str | Path) -> str:
    """把根目录规范成「带结尾分隔符」的前缀，和 files.path 的存储格式一致。"""
    s = str(Path(str(root)))
    return s if s.endswith(("\\", "/")) else s + os.sep


def _bare(root: str | Path) -> str:
    return str(Path(str(root))).rstrip("\\/")


# --------------------------------------------------------------------------- #
# Step 1：校验
# --------------------------------------------------------------------------- #


def validate_new_root(cfg: Config, new_root: str) -> Tuple[bool, str]:
    p = Path(str(new_root))
    if not str(new_root).strip():
        return False, "请填写新的素材文件夹路径。"
    if not p.exists():
        return False, f"这个路径不存在：{p}"
    if not p.is_dir():
        return False, f"这不是一个文件夹：{p}"
    try:
        os.scandir(p).close()
    except OSError as exc:
        return False, f"这个文件夹读不了（权限或磁盘问题）：{exc}"
    old = _bare(cfg.media_root)
    if _bare(p) == old:
        return False, "新路径和当前路径相同，不需要变更。"
    if _bare(p).lower().startswith(old.lower() + os.sep):
        return False, "新路径在旧路径里面：这会让相对路径算错，请选一个不在旧目录里的位置。"
    return True, ""


# --------------------------------------------------------------------------- #
# Step 2：预览差异
# --------------------------------------------------------------------------- #


@dataclass
class Preview:
    old_root: str
    new_root: str
    total: int = 0
    outside: int = 0            # 不在旧根下的行（本次不处理）
    ok: int = 0                 # 新路径存在 → 直接改路径
    missing: int = 0            # 新路径不存在 → 会被标为「缺失」
    n_photo: int = 0
    n_video: int = 0
    bytes: int = 0
    checked: int = 0
    eta_sec: float = 0.0
    samples: List[Dict[str, Any]] = field(default_factory=list)
    elapsed_sec: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "old_root": self.old_root, "new_root": self.new_root,
            "total": self.total, "outside": self.outside,
            "will_update": self.ok + self.missing, "ok": self.ok,
            "missing": self.missing, "photo": self.n_photo, "video": self.n_video,
            "bytes": self.bytes, "bytes_human": human_bytes(self.bytes),
            "eta_sec": round(self.eta_sec, 1), "samples": self.samples,
            "elapsed_sec": round(self.elapsed_sec, 2),
        }


def preview(cfg: Config, new_root: str, *, old_root: Optional[str] = None,
            check_exists: bool = True,
            limit: Optional[int] = None,
            progress: Optional[Callable[[int, int], None]] = None) -> Preview:
    """只读。统计「改路径能对上多少、对不上多少」。

    check_exists=False 时不做逐文件 stat（秒级），但无法给出 missing 数。
    """
    t0 = time.monotonic()
    old_prefix = _prefix(old_root if old_root is not None else cfg.media_root)
    new_prefix = _prefix(new_root)
    pv = Preview(old_root=old_prefix, new_root=new_prefix)

    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        pv.outside = int(conn.execute(
            "SELECT COUNT(*) FROM files WHERE path NOT LIKE ? || '%'", (old_prefix,)
        ).fetchone()[0] or 0)
        rows = conn.execute(
            "SELECT file_id, path, rel_path, kind, size FROM files "
            "WHERE path LIKE ? || '%' ORDER BY file_id", (old_prefix,)
        ).fetchall()
    finally:
        conn.close()

    if limit:
        rows = rows[:limit]
    pv.total = len(rows)
    for i, r in enumerate(rows, 1):
        rel = str(r["rel_path"] or "")
        if not rel:
            # 理论上不会发生（rel_path 由扫描写入）；保守地按「对不上」处理
            pv.missing += 1
            continue
        pv.n_photo += 1 if r["kind"] == "photo" else 0
        pv.n_video += 1 if r["kind"] == "video" else 0
        pv.bytes += int(r["size"] or 0)
        if not check_exists:
            continue
        if os.path.exists(new_prefix + rel):
            pv.ok += 1
        else:
            pv.missing += 1
            if len(pv.samples) < 20:
                pv.samples.append({"file_id": int(r["file_id"]),
                                   "old": str(r["path"]), "new": new_prefix + rel})
        pv.checked = i
        if progress and (i % 2000 == 0 or i == len(rows)):
            progress(i, len(rows))

    pv.elapsed_sec = time.monotonic() - t0
    # 粗估执行耗时：一次 UPDATE + 每文件一次 stat，实测约 3000 文件/秒（机械盘）
    pv.eta_sec = max(5.0, pv.total / 3000.0)
    return pv


# --------------------------------------------------------------------------- #
# Step 4：执行
# --------------------------------------------------------------------------- #


def apply(cfg: Config, new_root: str, *, old_root: Optional[str] = None,
          strategy: str = STRATEGY_KEEP,
          dry_run: bool = False,
          progress: Optional[Callable[[str, int, int], None]] = None) -> Dict[str, Any]:
    """执行重映射。strategy 决定「新路径上找不到的文件」怎么处理。

    progress(stage, done, total) 供任务进度条使用。
    """
    if strategy not in STRATEGY_LABELS:
        raise ValueError(f"未知策略：{strategy}")
    ok, msg = validate_new_root(cfg, new_root)
    if not ok and not dry_run:
        raise ValueError(msg)

    old_prefix = _prefix(old_root if old_root is not None else cfg.media_root)
    new_prefix = _prefix(new_root)
    t0 = time.monotonic()
    stats: Dict[str, Any] = {"strategy": strategy, "dry_run": dry_run,
                             "remapped": 0, "missing": 0, "purged": 0,
                             "outside": 0, "root_changed": False}

    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        stats["outside"] = int(conn.execute(
            "SELECT COUNT(*) FROM files WHERE path NOT LIKE ? || '%'", (old_prefix,)
        ).fetchone()[0] or 0)

        # ---- 第一步：批量改路径（一条 UPDATE，向量完全不动）----
        total = int(conn.execute(
            "SELECT COUNT(*) FROM files WHERE path LIKE ? || '%'", (old_prefix,)
        ).fetchone()[0] or 0)
        if progress:
            progress("更新路径", 0, total)
        if dry_run:
            stats["remapped"] = total
            LOG.info("[dry-run] 将更新 %d 条路径：%s → %s", total, old_prefix, new_prefix)
        else:
            cur = conn.execute(
                "UPDATE files SET path = ? || rel_path, root = ?, updated_at=? "
                "WHERE path LIKE ? || '%'",
                (new_prefix, new_prefix, local_now_iso(), old_prefix),
            )
            stats["remapped"] = cur.rowcount
            conn.commit()
        if progress:
            progress("更新路径", total, total)
        LOG.info("路径前缀已更新 %d 条：%s → %s", stats["remapped"], old_prefix, new_prefix)

        # ---- 第二步：按策略处理「新路径上不存在」的文件 ----
        if strategy in (STRATEGY_HIDE, STRATEGY_PURGE) and not dry_run:
            rows = conn.execute(
                "SELECT file_id, path, kind FROM files WHERE path LIKE ? || '%' "
                "AND status != 'missing'", (new_prefix,)
            ).fetchall()
            gone: List[Tuple[int, str, str]] = []
            for i, r in enumerate(rows, 1):
                if not os.path.exists(str(r["path"])):
                    gone.append((int(r["file_id"]), str(r["path"]), str(r["kind"])))
                if progress and (i % 2000 == 0 or i == len(rows)):
                    progress("核对文件", i, len(rows))
            stats["missing"] = len(gone)
            if gone and strategy == STRATEGY_HIDE:
                now = local_now_iso()
                for fid, _p, _k in gone:
                    conn.execute("UPDATE files SET status='missing', visible=0, "
                                 "updated_at=? WHERE file_id=?", (now, fid))
                conn.commit()
                LOG.info("已隐藏 %d 个在新位置上不存在的文件（向量保留，可恢复）", len(gone))
            elif gone:
                stats["purged"] = _purge_rows(cfg, conn, gone)
                LOG.info("已物理清理 %d 个文件的记录与向量", stats["purged"])

        # ---- 第三步：库根登记 ----
        if not dry_run:
            now = local_now_iso()
            conn.execute("DELETE FROM libraries WHERE root=?", (old_prefix,))
            conn.execute(
                "INSERT INTO libraries(root, last_scan, status) VALUES (?,?,?) "
                "ON CONFLICT(root) DO UPDATE SET last_scan=excluded.last_scan, "
                "status=excluded.status",
                (new_prefix, now, "idle"),
            )
            conn.commit()
            stats["root_changed"] = True
    finally:
        conn.close()

    # ---- 第四步：把新根写进设置叠加层（否则下次扫描会用旧根）----
    if not dry_run:
        _update_registry(cfg, old_prefix, new_root)
        stats["config_written"] = True
        T.init(cfg)
        T.emit(cfg, "library_changed", {"reason": "media_root_changed",
                                        "old": old_prefix, "new": new_prefix})
    stats["elapsed_sec"] = round(time.monotonic() - t0, 1)
    return stats


def _purge_rows(cfg: Config, conn, gone: List[Tuple[int, str, str]]) -> int:
    """物理删除这些文件的 SQLite 行与向量（绝不碰磁盘上的用户文件）。"""
    try:
        from .vector_store import open_store, face_collection, semantic_collection

        store = open_store(cfg)
        sem = semantic_collection(cfg)
        fac = face_collection(cfg)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("Qdrant 不可用，只清 SQLite：%s", exc)
        store = None
        sem = fac = ""
    n = 0
    for fid, _path, _kind in gone:
        if store is not None:
            for coll in (sem, fac):
                try:
                    store.delete_by_file(coll, fid)
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("删除向量失败 file_id=%s coll=%s：%s", fid, coll, exc)
        conn.execute("DELETE FROM face_records WHERE file_id=?", (fid,))
        conn.execute("DELETE FROM shots WHERE file_id=?", (fid,))
        conn.execute("DELETE FROM file_encoding WHERE file_id=?", (fid,))
        conn.execute("DELETE FROM stage_status WHERE file_id=?", (fid,))
        conn.execute("DELETE FROM dead_letter WHERE file_id=?", (fid,))
        conn.execute("DELETE FROM path_index WHERE file_id=?", (fid,))
        conn.execute("DELETE FROM files WHERE file_id=?", (fid,))
        n += 1
        if n % 200 == 0:
            conn.commit()
    conn.commit()
    return n


def _write_media_root(cfg: Config, new_root: str) -> None:
    import yaml

    path = Path(cfg.root) / CONFIG_LOCAL_FILENAME
    data: Dict[str, Any] = {}
    if path.exists():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            data = {}
    data["media_root"] = str(new_root).replace("\\", "/")
    header = (
        "# 由界面自动生成，请勿手工编辑（改了会被下一次保存覆盖）。\n"
        f"# 最后修改：{local_now_iso()}\n"
    )
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# 维护页：清理「已删除」文件的残留
# --------------------------------------------------------------------------- #


def missing_report(cfg: Config, *, limit: int = 500) -> Dict[str, Any]:
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        total = int(conn.execute(
            "SELECT COUNT(*) FROM files WHERE status='missing'").fetchone()[0] or 0)
        by_kind = dict(conn.execute(
            "SELECT kind, COUNT(*) FROM files WHERE status='missing' GROUP BY kind"
        ).fetchall())
        bytes_ = int(conn.execute(
            "SELECT COALESCE(SUM(size),0) FROM files WHERE status='missing'").fetchone()[0] or 0)
        rows = conn.execute(
            "SELECT file_id, path, kind, size, updated_at FROM files "
            "WHERE status='missing' ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
    finally:
        conn.close()
    return {"total": total, "by_kind": by_kind, "bytes": bytes_,
            "bytes_human": human_bytes(bytes_),
            "items": [dict(r) for r in rows]}


def restore_missing(cfg: Config, file_ids: List[int]) -> Dict[str, int]:
    """把「缺失」改回待处理并重新排队。文件真在磁盘上时下一次扫描就能复活。"""
    if not file_ids:
        return {"restored": 0}
    now = local_now_iso()
    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        n = 0
        for fid in file_ids:
            row = conn.execute("SELECT path, status FROM files WHERE file_id=?",
                               (fid,)).fetchone()
            if row is None or row["status"] != "missing":
                continue
            conn.execute("UPDATE files SET status='pending', visible=1, updated_at=? "
                         "WHERE file_id=?", (now, fid))
            conn.execute("DELETE FROM dead_letter WHERE file_id=? AND stage='extract'",
                         (fid,))
            n += 1
        conn.commit()
    finally:
        conn.close()
    T.init(cfg)
    T.emit(cfg, "library_changed", {"reason": "restore_missing", "n": n})
    return {"restored": n}


def purge_missing(cfg: Config, file_ids: Optional[List[int]] = None, *,
                  dry_run: bool = True) -> Dict[str, Any]:
    """物理删除「缺失」文件的记录与向量。**必须人工确认后才允许 dry_run=False。**"""
    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        if file_ids:
            placeholders = ",".join("?" * len(file_ids))
            rows = conn.execute(
                f"SELECT file_id, path, kind FROM files WHERE status='missing' "
                f"AND file_id IN ({placeholders})", file_ids).fetchall()
        else:
            rows = conn.execute(
                "SELECT file_id, path, kind FROM files WHERE status='missing'").fetchall()
        gone = [(int(r["file_id"]), str(r["path"]), str(r["kind"])) for r in rows]
        if dry_run:
            return {"dry_run": True, "would_purge": len(gone),
                    "samples": [g[1] for g in gone[:20]]}
        n = _purge_rows(cfg, conn, gone)
    finally:
        conn.close()
    T.init(cfg)
    T.emit(cfg, "library_changed", {"reason": "purge_missing", "n": n})
    return {"dry_run": False, "purged": n}


def _update_registry(cfg: Config, old_prefix: str, new_root: str) -> None:
    """把「改了路径」这件事同步到素材目录登记表（多目录之后 media_root 只是第一项）。"""
    try:
        from . import media_roots as MR

        MR.update(cfg, old_prefix, new_path=new_root, who="界面/路径变更")
        return
    except Exception as exc:  # noqa: BLE001 - 登记表不在（老配置）时退回写 media_root
        LOG.warning("更新素材目录登记失败，改回只写 media_root：%s", exc)
    _write_media_root(cfg, new_root)
