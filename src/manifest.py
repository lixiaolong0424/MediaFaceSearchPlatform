"""01_manifest.py · L1 清单扫描（五类判定 + path_hash/inode 改名识别 + 反向比对索引表）。

规格第五章 L1：
  * 后缀白名单 .jpg .jpeg .png .webp .mp4 .mov .mkv .avi .m4v
  * stat 取 mtime/inode/size，path_hash = md5(相对路径)
  * 维护轻量索引表 path_hash → (inode,mtime,size) 使增量比对秒级完成
  * 输出五类 new / modified / moved / unchanged / missing
  * 不解码、不读内容

第十章 inode 不稳定降级：FAT/exFAT/移动硬盘上 inode 不稳定 →
唯一键降级为 (path_hash, size, mtime_rounded_to_1s)，并行 UI banner 提示。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Set, Tuple

from . import db as D
from . import tasks as T
from .common import (
    Config, get_logger, human_bytes, local_now_iso, path_hash as make_path_hash,
)

LOG = get_logger("manifest")

NEW, MODIFIED, MOVED, UNCHANGED, MISSING, SKIPPED = (
    "new", "modified", "moved", "unchanged", "missing", "skipped"
)


# --------------------------------------------------------------------------- #
# inode 稳定性探测（第十章）
# --------------------------------------------------------------------------- #


@dataclass
class InodeProbe:
    stable: bool
    reason: str
    sample_path: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"inode_stable": self.stable, "reason": self.reason, "sample": self.sample_path}


def _root_key(root) -> str:
    """素材根的规范写法：绝对路径 + 结尾分隔符。

    ⚠️ 必须统一：files.root 只允许这一种写法。
    2026-10-19 踩过——迁移回填与界面登记写的是带结尾分隔符的形式，
    而 01_manifest 写的是 str(Path) 形式（不带结尾分隔符），
    于是按前缀统计文件数时一条都对不上，界面上显示「未扫描 / 0 个文件」。
    """
    s = str(Path(str(root)))
    return s if s.endswith(("\\", "/")) else s + os.sep


def probe_inode_stability(root: Path, cfg: Config) -> InodeProbe:
    """在不写盘的前提下探测 inode 是否可用且稳定。

    新库尚无已索引文件，无法做跨次 stat 比对，因此这里只判断「文件系统是否提供
    有意义的 st_ino」；真正的跨次比对在 classify() 里完成，若发现 inode 大面积
    不匹配则自动降级并在 summary 中标记。
    """
    if os.name == "nt":
        # 网络盘 / 不支持的 FS：探测 st_ino 是否为 0
        probe_target = root if root.exists() else Path(root.anchor or "C:/")
        try:
            st = probe_target.stat()
        except OSError as exc:
            return InodeProbe(False, f"无法 stat {probe_target}：{exc}")
        if not getattr(st, "st_ino", 0):
            # 根目录本身 st_ino 为 0 的情况少见；更常见的是 **文件** 的
            # DirEntry.stat().st_ino 为 0 而 os.stat() 有值 —— 那种情况
            # walk_media 会补一次 os.stat，所以这里仍算「可用」。
            return InodeProbe(True, "根目录 st_ino 为 0；文件侧改用 os.stat 取 inode",
                              str(probe_target))
        return InodeProbe(True, "st_ino 可用", str(probe_target))
    st = root.stat()
    return InodeProbe(bool(getattr(st, "st_ino", 0)), "posix st_ino")


def inode_unstable_by_mismatch(mismatch_ratio: float, threshold: float = 0.5) -> bool:
    """跨次扫描时 inode 完全不匹配（> 阈值比例）→ 判定该盘 inode 不可用。"""
    return mismatch_ratio >= threshold


# --------------------------------------------------------------------------- #
# 遍历
# --------------------------------------------------------------------------- #


@dataclass
class FoundFile:
    path: str
    rel_path: str
    size: int
    mtime: int
    inode: int
    ext: str
    kind: str


def is_blacklisted_dir(name: str, cfg: Config) -> bool:
    return name.lower() in cfg.dir_blacklist


def walk_media(root: Path, cfg: Config, *, limit: Optional[int] = None,
               on_error: Optional[List[Tuple[str, str]]] = None) -> Iterator[FoundFile]:
    """递归遍历素材根目录。不解码、不读内容，只 stat。"""
    whitelist = cfg.ext_whitelist
    photo_ext = cfg.photo_ext
    video_ext = cfg.video_ext
    min_photo = int(cfg.get("library.min_photo_bytes", 0))
    min_video = int(cfg.get("library.min_video_bytes", 0))
    follow = bool(cfg.get("scan.follow_symlinks", False))
    emitted = 0
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except OSError as exc:
            if on_error is not None:
                on_error.append((str(cur), str(exc)))
            LOG.warning("无法读取目录，跳过：%s", exc)
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=follow):
                    if not is_blacklisted_dir(entry.name, cfg):
                        stack.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=follow):
                    continue
            except OSError as exc:
                if on_error is not None:
                    on_error.append((entry.path, str(exc)))
                continue

            ext = os.path.splitext(entry.name)[1].lower().lstrip(".")
            if ext not in whitelist:
                continue
            kind = "photo" if ext in photo_ext else ("video" if ext in video_ext else "unknown")
            if kind == "unknown":
                continue
            try:
                st = entry.stat(follow_symlinks=follow)
            except OSError as exc:
                if on_error is not None:
                    on_error.append((entry.path, str(exc)))
                continue
            if min_photo and kind == "photo" and st.st_size < min_photo:
                continue
            if min_video and kind == "video" and st.st_size < min_video:
                continue

            abs_path = os.path.abspath(entry.path)
            try:
                rel = os.path.relpath(abs_path, str(root))
            except ValueError:
                rel = entry.name
            ino = int(getattr(st, "st_ino", 0) or 0)
            if ino == 0:
                # 本机实测：Windows 上 DirEntry.stat() 的 st_ino 恒为 0，
                # 而 os.stat() 能拿到真 inode（NTFS 文件索引）。移动/改名识别
                # 全靠它，所以必须补这一次 stat —— 只对 st_ino=0 的文件付代价。
                try:
                    ino = int(os.stat(entry.path, follow_symlinks=follow).st_ino or 0)
                except OSError:
                    ino = 0
            yield FoundFile(
                path=str(Path(abs_path)),
                rel_path=rel,
                size=int(st.st_size),
                mtime=int(st.st_mtime),
                inode=ino,
                ext=ext,
                kind=kind,
            )
            emitted += 1
            if limit and emitted >= limit:
                return


# --------------------------------------------------------------------------- #
# 五类判定
# --------------------------------------------------------------------------- #


@dataclass
class Classification:
    new: List[FoundFile] = field(default_factory=list)
    modified: List[FoundFile] = field(default_factory=list)
    moved: List[Tuple[FoundFile, int]] = field(default_factory=list)   # (found, file_id)
    unchanged: List[Tuple[FoundFile, int]] = field(default_factory=list)
    seen_file_ids: Set[int] = field(default_factory=set)

    def counts(self) -> Dict[str, int]:
        return {
            NEW: len(self.new),
            MODIFIED: len(self.modified),
            MOVED: len(self.moved),
            UNCHANGED: len(self.unchanged),
        }


def _match_other_root(conn, f, root_s: str) -> Optional[int]:
    """在**其他素材根**里找「同一份文件」的旧记录（移动/改名 + 跨根）。

    判定条件（三者同时满足才认）：
      * size、mtime 完全相同，文件名相同（忽略大小写）；
      * 旧路径在磁盘上已经不存在 —— 没有这一条就会把「真副本」误当移动，
        导致原件记录被抢走，副本反而指向不存在的路径。

    走 idx_files_size 索引；每个「新增」文件最多扫 50 个同尺寸候选。
    """
    name = os.path.basename(f.path).lower()
    try:
        cands = conn.execute(
            "SELECT file_id, path, mtime FROM files "
            "WHERE size=? AND (root IS NULL OR root <> ?) LIMIT 50",
            (f.size, root_s)).fetchall()
    except Exception:  # noqa: BLE001 - 匹配失败最多是「少认一次移动」，不该让扫描失败
        return None
    for c in cands:
        try:
            if int(c[2] or 0) != int(f.mtime):
                continue
            old_path = str(c[1] or "")
            if os.path.basename(old_path).lower() != name:
                continue
            if not old_path or os.path.exists(old_path):
                continue
            return int(c[0])
        except Exception:  # noqa: BLE001
            continue
    return None


def classify(
    files: Iterable[FoundFile],
    root: Path,
    conn,
    *,
    inode_stable: bool = True,
    inode_mismatch: Optional[List[int]] = None,
) -> Classification:
    """把磁盘上的文件与库中记录做反向比对，产出五类判定。

    判定顺序（严格按规格第十章）：
      1) path_hash 命中且 inode+mtime+size 全同            → unchanged
      2) path_hash 命中但 mtime/size 变                     → modified
      3) path_hash 未命中但 inode 命中（inode 稳定时）      → moved（只更新 path）
      4) 以上都不是                                          → new

    inode 不稳定时：唯一键降级为 (path_hash, size, mtime_rounded_to_1s)，
    只做 1/2/4，不做 moved（改名会被视为 new，由 UI banner 提示）。
    """
    root_s = _root_key(root)
    path_index = D.load_path_index(conn, root_s)          # path_hash -> row（本根内）
    like_scope = root_s.rstrip("\\/") + "%"
    # 只在本根内比对：两个素材根下可能有同名相对路径，混在一起会互相覆盖。
    # root IS NULL 的行（迁移前后手工插入的）按路径前缀归到本根。
    files_table = {
        int(r["file_id"]): r
        for r in conn.execute(
            "SELECT file_id, path, path_hash, inode, size, mtime, status FROM files "
            "WHERE root = ? OR (root IS NULL AND path LIKE ?)", (root_s, like_scope))
    }
    by_inode: Dict[int, List[int]] = {}
    for fid, row in files_table.items():
        ino = row["inode"]
        if ino:
            by_inode.setdefault(int(ino), []).append(fid)
    by_size_mtime: Dict[Tuple[int, int], List[int]] = {}
    # 指纹索引：size + mtime + 文件名。用于 **inode 不可用**时识别移动/改名。
    # 只认「唯一命中，且该记录的原路径在磁盘上已经不存在」——否则复制出来的
    # 副本会被误认成「移动」，把原记录挪到新位置。
    by_fingerprint: Dict[Tuple[int, int, str], List[int]] = {}
    for fid, row in files_table.items():
        if row["size"] is None or row["mtime"] is None or not row["path"]:
            continue
        key = (int(row["size"]), int(row["mtime"]),
               os.path.basename(str(row["path"])).lower())
        by_fingerprint.setdefault(key, []).append(fid)
        if row["size"] is not None and row["mtime"] is not None:
            by_size_mtime.setdefault((int(row["size"]), int(row["mtime"])), []).append(fid)

    result = Classification()
    claimed: Set[int] = set()
    mismatch = 0

    for f in files:
        ph = make_path_hash(f.rel_path)
        idx = path_index.get(ph)
        if idx is not None:
            fid = int(idx["file_id"]) if idx["file_id"] is not None else None
            rec = files_table.get(fid) if fid is not None else None
            if rec is not None:
                same = (int(idx["size"] or -1) == f.size
                        and int(idx["mtime"] or -1) == f.mtime)
                if inode_stable and rec["inode"]:
                    same = same and int(rec["inode"]) == f.inode
                    if not same and int(rec["inode"]) != f.inode:
                        mismatch += 1
                claimed.add(fid)
                result.seen_file_ids.add(fid)
                if same:
                    result.unchanged.append((f, fid))
                else:
                    result.modified.append(f)
                continue

        if f.inode:
            cands = [c for c in by_inode.get(f.inode, []) if c not in claimed]
            if len(cands) == 1:
                fid = cands[0]
                claimed.add(fid)
                result.seen_file_ids.add(fid)
                result.moved.append((f, fid))
                continue

        # inode 拿不到（本机老数据全是 0）或用不上时，退到指纹匹配。
        # 判据：size+mtime+同名 唯一命中，且那条记录的原路径已经不在磁盘上
        # → 就是这个文件被移动/改名了。
        fkey = (f.size, f.mtime, os.path.basename(f.path).lower())
        fp_cands = [c for c in by_fingerprint.get(fkey, []) if c not in claimed]
        if len(fp_cands) == 1:
            fid = fp_cands[0]
            old_path = str(files_table[fid]["path"] or "")
            if old_path and not os.path.exists(old_path):
                claimed.add(fid)
                result.seen_file_ids.add(fid)
                result.moved.append((f, fid))
                continue
        if not inode_stable:
            cands = [c for c in by_size_mtime.get((f.size, f.mtime), []) if c not in claimed]
            # 降级键碰撞概率高，只在唯一命中时才认为「不变」，绝不判 moved
            if len(cands) == 1:
                fid = cands[0]
                claimed.add(fid)
                result.seen_file_ids.add(fid)
                result.unchanged.append((f, fid))
                continue

        result.new.append(f)

    # 跨素材根：new 里可能其实是「从别的根挪过来的」。老路径已不存在才算移动。
    if result.new:
        kept: List[FoundFile] = []
        for f in result.new:
            other = _match_other_root(conn, f, root_s)
            if other is not None and other not in claimed:
                claimed.add(other)
                result.seen_file_ids.add(other)
                result.moved.append((f, other))
            else:
                kept.append(f)
        result.new = kept

    if inode_mismatch is not None:
        inode_mismatch.append(mismatch)
    return result


# --------------------------------------------------------------------------- #
# 落库
# --------------------------------------------------------------------------- #


def apply_classification(
    conn,
    root: Path,
    cls: Classification,
    cfg: Config,
    *,
    dry_run: bool = False,
) -> Dict[str, int]:
    """把分类结果写入 files / path_index / libraries。missing 只软删（铁律 2）。"""
    batch = int(cfg.get("scan.batch_commit_files", 500))
    now = local_now_iso()
    stats = {"new": 0, "modified": 0, "moved": 0, "unchanged": 0, "missing": 0,
             "revived": 0, "errors": 0}
    root_s = _root_key(root)

    # --dry-run 必须**不写任何东西**。早期版本这里无条件 upsert_library，
    # 于是 dry-run 也会去拿写锁：一旦有别的进程在写库（例如 03_extract 正在跑），
    # 就报 "database is locked" —— dry-run 本该是最安全的操作，却在此时失败。
    if not dry_run:
        D.upsert_library(conn, root_s, status="scanning")

    pending = 0

    def maybe_commit(force: bool = False) -> None:
        nonlocal pending
        if dry_run:
            pending = 0
            return
        if force or pending >= batch:
            conn.commit()
            pending = 0

    # 新增：正常走后续管线
    for f in cls.new:
        stats["new"] += 1
        if dry_run:
            continue
        try:
            fid = D.upsert_file(
                conn, path=f.path, rel_path=f.rel_path, path_hash=make_path_hash(f.rel_path),
                inode=f.inode, size=f.size, mtime=f.mtime, kind=f.kind, status="pending",
                root=root_s,
            )
            D.upsert_path_index(conn, root_s, make_path_hash(f.rel_path), fid, f.inode, f.mtime, f.size)
            cls.seen_file_ids.add(fid)   # 关键：新入库的文件也算「磁盘上见到了」
            pending += 1
            maybe_commit()
        except Exception as exc:  # noqa: BLE001 - 单文件失败绝不中断整批
            stats["errors"] += 1
            LOG.error("新增入库失败 %s：%s", f.path, exc)

    # 修改：原记录过期，重跑管线
    for f in cls.modified:
        stats["modified"] += 1
        if dry_run:
            continue
        try:
            fid = D.upsert_file(
                conn, path=f.path, rel_path=f.rel_path, path_hash=make_path_hash(f.rel_path),
                inode=f.inode, size=f.size, mtime=f.mtime, kind=f.kind, status="pending",
                root=root_s,
            )
            conn.execute(
                "UPDATE files SET status='pending', captured_at=NULL, phash=NULL, indexed_at=NULL "
                "WHERE file_id=?", (fid,))
            D.upsert_path_index(conn, root_s, make_path_hash(f.rel_path), fid, f.inode, f.mtime, f.size)
            cls.seen_file_ids.add(fid)
            pending += 1
            maybe_commit()
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            LOG.error("修改入库失败 %s：%s", f.path, exc)

    # 移动/改名：只更新 path，保留所有向量与人脸记录
    for f, fid in cls.moved:
        stats["moved"] += 1
        if dry_run:
            continue
        try:
            old = conn.execute("SELECT path, path_hash, status, root FROM files WHERE file_id=?", (fid,)).fetchone()
            if old and old["path_hash"] and old["path_hash"] != make_path_hash(f.rel_path):
                D.drop_path_index(conn, str(old["root"] or root_s), old["path_hash"])
            D.update_file_path(conn, fid, f.path, f.rel_path)
            conn.execute("UPDATE files SET inode=?, size=?, mtime=?, root=? WHERE file_id=?",
                         (f.inode, f.size, f.mtime, root_s, fid))
            # 软删复活：文件重新出现（或被移回）时必须撤销 status=missing
            if old is not None and old["status"] == "missing":
                conn.execute("UPDATE files SET status='pending', updated_at=? WHERE file_id=?",
                             (local_now_iso(), fid))
            D.upsert_path_index(conn, root_s, make_path_hash(f.rel_path), fid, f.inode, f.mtime, f.size)
            D.audit(conn, "manifest", f"move file_id={fid}",
                    {"path": old["path"] if old else None}, {"path": f.path})
            pending += 1
            maybe_commit()
        except Exception as exc:  # noqa: BLE001
            stats["errors"] += 1
            LOG.error("移动更新失败 %s：%s", f.path, exc)

    for f, fid in cls.unchanged:
        stats["unchanged"] += 1
        if dry_run:
            continue
        # 刷新 path_index 的 mtime/size（可能因 FAT 取整而变化，但内容未变）
        D.upsert_path_index(conn, root_s, make_path_hash(f.rel_path), fid, f.inode, f.mtime, f.size)
        # 历史遗留：早期扫描把 inode 全存成 0（DirEntry.stat 的坑），
        # 这里遇到就顺手补上，之后同一文件的移动/改名才能被认出来。
        if f.inode:
            conn.execute(
                "UPDATE files SET inode=? WHERE file_id=? AND (inode IS NULL OR inode=0)",
                (f.inode, fid))
        # 软删复活：上次扫描时文件不在，这次又在了 → 撤销 missing，重新排队走管线
        revived = conn.execute(
            "UPDATE files SET status='pending', updated_at=? WHERE file_id=? AND status='missing'",
            (local_now_iso(), fid),
        ).rowcount
        if revived:
            stats["revived"] = stats.get("revived", 0) + 1
            # 文件回来了，之前跟着软删的人脸也一起恢复（记录与向量一直都在）
            conn.execute(
                "UPDATE face_records SET status='alive' "
                "WHERE file_id=? AND status='missing'", (fid,))

    # 删除：库有但磁盘无 → status=missing，软删
    # 注意：必须用「本次扫描确实在磁盘上见到的 file_id」集合做差集。
    # seen_file_ids 在真实写入时被逐个补齐；--dry-run 下不写库，
    # 因此用「库中原本就存在、且属于 new/moved 目标的记录」补全，保证 dry-run 的报告正确。
    disk_ids = set(cls.seen_file_ids)
    if dry_run:
        known = {
            (str(r["path"]), int(r["file_id"])) for r in
            conn.execute("SELECT path, file_id FROM files")
        }
        by_path = {p: fid for p, fid in known}
        for f in cls.new:
            if f.path in by_path:
                disk_ids.add(by_path[f.path])
    now = local_now_iso()
    # ------------------------------------------------------------------ #
    # 「消失」判定必须**限定在本次扫描的根之内**（2026-10-19 血的教训）
    #
    # 原实现是全局差集：`WHERE status != 'missing'` 减去本次扫到的集合。
    # 它完全不看本次 --root 是什么，于是只要扫描根不是库当前索引的那个根
    # （换盘、改目录名、手抖填错路径、界面拿着旧配置），就会把**另外整个库**
    # 标成 missing —— 一天之内踩了两次，两次都是 50 万行瞬间「消失」。
    #
    # 正确语义：扫 E:\ 只能判定「E:\ 下面的东西还在不在」，
    # 对 F:\ 或别的根下的记录一个字都不该动。
    # ------------------------------------------------------------------ #
    scope_like = root_s.rstrip("\\/") + "%"
    n_in_scope = int(conn.execute(
        "SELECT COUNT(*) FROM files WHERE status != 'missing' "
        "AND (root = ? OR (root IS NULL AND path LIKE ?))",
        (root_s, scope_like)).fetchone()[0] or 0)
    if not cls.new and not cls.modified and not cls.moved and not cls.unchanged and n_in_scope:
        stats["missing_skipped"] = n_in_scope
        LOG.error(
            "扫描根 %s 下一个文件都没找到，但库里有 %d 条记录在这个根下 —— "
            "判定为路径填错/盘未就绪，本次**不**标记任何文件为缺失（防止把整库清空）。"
            "请确认 --root 是否正确。", root_s, n_in_scope)
        if not dry_run:
            D.upsert_library(conn, root_s, last_scan=now, status="idle")
        maybe_commit(force=True)
        return stats

    for row in conn.execute(
            "SELECT file_id FROM files WHERE status != 'missing' "
            "AND (root = ? OR (root IS NULL AND path LIKE ?))",
            (root_s, scope_like)):
        fid = int(row["file_id"])
        if fid not in disk_ids:
            stats["missing"] += 1
            if not dry_run:
                conn.execute("UPDATE files SET status='missing', updated_at=? WHERE file_id=?",
                             (now, fid))
                # 同步软删它的人脸记录：否则缩略图还在 nvme 里，墙上照旧显示，
                # 用户点「打开所在文件夹」才发现原文件没了 —— 2026-10-22 的真实反馈。
                conn.execute(
                    "UPDATE face_records SET status='missing' "
                    "WHERE file_id=? AND status='alive'", (fid,))
    if not dry_run:
        D.upsert_library(conn, root_s, last_scan=now, status="idle")

    maybe_commit(force=True)
    return stats


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def reset_for_full_reprocess(conn, cfg: Config, *, dry_run: bool = False) -> Dict[str, int]:
    """「重新扫描全部」的重置语义：把已完成的文件重新排队。

    只重置 status='done' 的文件：
      * `missing` 保持不动（文件真的不在磁盘上，重排也没有意义）；
      * `skipped_dup` 保持不动（那是去重结论，重跑只会得出同样结论，白读一遍盘）；
      * `pending` 本来就是待处理。

    只删 `file_encoding` 的 done 行与 `stage_status` 行：这两张表是「某阶段做过没有」
    的唯一依据，删掉才会真的重做。向量本身（Qdrant）不删，重做时按同一个 point_id
    upsert 覆盖，不会产生重复点。
    """
    counts: Dict[str, int] = {}
    counts["files_done"] = int(conn.execute(
        "SELECT COUNT(*) FROM files WHERE status='done'").fetchone()[0] or 0)
    counts["encoding_done"] = int(conn.execute(
        "SELECT COUNT(*) FROM file_encoding WHERE status='done'").fetchone()[0] or 0)
    counts["stage_rows"] = 0
    if _table_exists(conn, "stage_status"):
        counts["stage_rows"] = int(conn.execute(
            "SELECT COUNT(*) FROM stage_status").fetchone()[0] or 0)
    if dry_run:
        return counts
    conn.execute("DELETE FROM file_encoding WHERE status='done'")
    if _table_exists(conn, "stage_status"):
        # stage_status 由抽帧阶段懒建（首次运行 03_extract 时才出现），
        # 所以空库上可能还没有这张表 —— 不能假设它一定在。
        conn.execute("DELETE FROM stage_status")
    conn.execute("UPDATE files SET status='pending', updated_at=? WHERE status='done'",
                 (local_now_iso(),))
    conn.commit()
    LOG.info("重新扫描全部：%d 个文件重新排队（清掉 %d 条编码结论、%d 条阶段记录）",
             counts["files_done"], counts["encoding_done"], counts["stage_rows"])
    return counts


def run(args, cfg: Config) -> Dict[str, Any]:
    root = Path(args.root).expanduser().resolve() if getattr(args, "root", None) else Path(str(cfg.media_root)).resolve()
    if not root.exists():
        raise FileNotFoundError(f"素材根目录不存在：{root}")

    db_path = cfg.db_path
    # 即使 --dry-run 也必须先建/迁移 schema：classify() 要读 files/path_index 做反向比对。
    # dry-run 的语义是「只报告将要做什么，不写业务数据」，建表不属于业务数据。
    D.ensure_schema(db_path, cfg.root, cfg)
    conn = D.connect(db_path, cfg=cfg)

    t0 = time.monotonic()
    errors: List[Tuple[str, str]] = []
    probe = probe_inode_stability(root, cfg)

    LOG.info("开始扫描 %s（inode_stable=%s, dry_run=%s）", root, probe.stable, args.dry_run)
    T.stage("scan")
    files: List[FoundFile] = []
    next_tick = time.monotonic() + 10.0
    next_say = time.monotonic() + 3.0
    for f in walk_media(root, cfg, limit=args.limit, on_error=errors):
        files.append(f)
        T.tick()          # 进度条上的「已发现 N 个文件」（总数未知，只报已发现数）
        if time.monotonic() >= next_say:
            # 扫描阶段总数未知（要遍历完才知道），所以只报「已发现多少」。
            T.say(f"扫描文件清单：已发现 {len(files):,} 个可处理文件")
            next_say = time.monotonic() + 3.0
        if time.monotonic() >= next_tick:
            LOG.info("  已发现 %d 个候选文件…", len(files))
            next_tick = time.monotonic() + 10.0

    scan_sec = time.monotonic() - t0
    LOG.info("遍历完成：%d 个候选文件，%.1fs", len(files), scan_sec)

    mismatch: List[int] = []
    cls = classify(files, root, conn, inode_stable=probe.stable, inode_mismatch=mismatch)
    if probe.stable and mismatch and mismatch[0] > 0:
        # 跨次比对发现 inode 大面积变化 → 该盘 inode 不可用，降级唯一键
        ratio = mismatch[0] / max(1, len(files))
        if inode_unstable_by_mismatch(ratio):
            LOG.warning("检测到 inode 不稳定（%d/%d 不匹配，比率 %.2f）："
                        "降级唯一键为 (path_hash,size,mtime)，文件移动会被视为新增。",
                        mismatch[0], len(files), ratio)
            probe = InodeProbe(False, f"跨次比对 inode 不匹配 {mismatch[0]}/{len(files)}")

    if args.shard:
        # 清单扫描必须全量：只扫一半会导致另一半被误判 missing。
        # --shard 是留给 02/03/04 的，这里只提示，不做分片。
        LOG.info("--shard %s 仅作用于后续处理阶段；清单扫描必须全量才能正确判定 missing", args.shard)

    stats = apply_classification(conn, root, cls, cfg, dry_run=args.dry_run)
    if getattr(args, "reprocess_all", False):
        stats["reprocess_reset"] = reset_for_full_reprocess(
            conn, cfg, dry_run=args.dry_run)
    # 扫描改变了「文件路径 / 状态」，必须让界面缓存失效。
    # 原来只有走任务队列（run_task）时才写事件，命令行直接跑 01_manifest.py
    # 不写事件 → 界面的照片墙/搜索/人脸分组缓存最多陈旧 3 分钟（ttl），
    # 用户看到的就是「明明扫过了，地址还是旧的」。
    if not args.dry_run:
        try:
            T.emit(cfg, "library_changed",
                   {"reason": "manifest", "root": str(root),
                    "new": stats.get("new", 0), "moved": stats.get("moved", 0),
                    "modified": stats.get("modified", 0),
                    "missing": stats.get("missing", 0)})
        except Exception as exc:  # noqa: BLE001 - 事件写失败不该让扫描失败
            LOG.warning("写变更事件失败（界面缓存可能延迟刷新）：%s", exc)
    conn.close()

    total_bytes = sum(f.size for f in files)
    summary = {
        "root": str(root),
        "candidates": len(files),
        "total_size": human_bytes(total_bytes),
        **stats,
        "scan_seconds": round(scan_sec, 2),
        "dir_errors": len(errors),
        "inode_stable": probe.stable,
        "inode_note": probe.reason,
        "dry_run": bool(args.dry_run),
        "db": str(db_path),
    }
    if errors:
        summary["dir_error_samples"] = errors[:5]
    return summary
