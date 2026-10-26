"""12_maintain.py · 运维：repair / reconcile / LRU 清理 / query_cache 清理
/ 孤儿回收 / 配额告警 / 日志轮转 / migrate。

规格第三章 + 第十章：
  * repair：重试 pending/error <= 3 次，超限入死信表
  * reconcile：比对 sqlite 与 Qdrant point 数，输出差异报告（不自动删除）
  * frames/ 每日 LRU 清理（淘汰最近 N 天未被命中的关键帧）；thumbs 与 faces 永不淘汰
  * query_cache/ 启动时清理超过 24h 的文件
  * 孤儿回收：status=missing 超 30 天 → 删其 thumb 后再物理删记录，
    必须手动触发 + 二次确认 + 先输出 dry-run 报告
  * 配额告警：NVMe 使用 > 80% 时告警并暂停增量入库

用法：
    .venv\\Scripts\\python.exe 12_maintain.py migrate
    .venv\\Scripts\\python.exe 12_maintain.py reconcile --json
    .venv\\Scripts\\python.exe 12_maintain.py repair --dry-run
    .venv\\Scripts\\python.exe 12_maintain.py clean-frames --days 30 --dry-run
    .venv\\Scripts\\python.exe 12_maintain.py clean-query-cache
    .venv\\Scripts\\python.exe 12_maintain.py prune-orphans --dry-run
    .venv\\Scripts\\python.exe 12_maintain.py prune-orphans --confirm
    .venv\\Scripts\\python.exe 12_maintain.py quota
"""

from __future__ import annotations

import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import db as D  # noqa: E402
from src.common import (  # noqa: E402
    Config, base_argparser, dir_size_bytes, emit_summary, get_logger, human_bytes,
    local_now_iso, make_point_id,
)
from src.config_loader import cfg_from_args  # noqa: E402
from src.vector_store import face_collection, open_store, semantic_collection  # noqa: E402

LOG = get_logger("maintain")

MAX_REPAIR_ATTEMPTS = 3


# --------------------------------------------------------------------------- #
# migrate
# --------------------------------------------------------------------------- #


def cmd_migrate(cfg: Config, args) -> Dict[str, Any]:
    return D.migrate(cfg.db_path, cfg.root, cfg, dry_run=args.dry_run)


# --------------------------------------------------------------------------- #
# repair
# --------------------------------------------------------------------------- #


def cmd_repair(cfg: Config, args) -> Dict[str, Any]:
    """重试 pending/error <= 3 次，超限入死信表。

    ⚠️ 2026-10-16 修：「重置为 pending」必须**同时清掉该阶段的 stage_status 行**，
    否则等于没重试。原因：各阶段的取数条件是
        JOIN/LEFT JOIN stage_status ... AND (ss.status IS NULL OR ss.status IN ('pending','error'))
    也就是说 stage_status='done' 的文件永不入选。早期实现只改 file_encoding，
    那些「Qdrant 里已经有向量、但 file_encoding 停在 pending」的文件（进程被中断留下的）
    会永远修不回来，reconcile 每次都要报一批孤儿向量。
    现在把 stage_status 一起删掉，阶段自然重新拾取它。
    """
    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        out: Dict[str, Any] = {}
        stage_of = {D.MODEL_SEMANTIC: "semantic_encode", D.MODEL_FACE: "face_encode"}
        for model in (D.MODEL_SEMANTIC, D.MODEL_FACE):
            candidates = D.pending_or_error(conn, model, MAX_REPAIR_ATTEMPTS)
            over = [
                int(r["file_id"]) for r in conn.execute(
                    """SELECT fe.file_id AS file_id,
                              (SELECT COUNT(*) FROM dead_letter dl
                                WHERE dl.file_id=fe.file_id AND dl.stage=fe.model) AS n
                         FROM file_encoding fe
                        WHERE fe.model=? AND fe.status IN ('pending','error')""", (model,))
                if int(r["n"] or 0) >= MAX_REPAIR_ATTEMPTS
            ]
            out[f"{model}_retryable"] = len(candidates)
            out[f"{model}_dead_lettered"] = len(over)
            if args.dry_run:
                LOG.info("[dry-run] %s：可重试 %d 个，超限 %d 个", model, len(candidates), len(over))
                continue
            stage = stage_of.get(model, "")
            for fid in candidates:
                # 重置为 pending，让对应阶段重新拾取
                conn.execute(
                    "UPDATE file_encoding SET status='pending', error=NULL WHERE file_id=? AND model=?",
                    (fid, model))
                if stage:
                    # 关键：不清 stage_status，04/07 的取数条件会把该文件排除掉
                    conn.execute("DELETE FROM stage_status WHERE file_id=? AND stage=?",
                                 (fid, stage))
                # ⚠️ files.status 不能无脑重置成 pending（2026-10-16 修的真实 bug）：
                #   * 03_extract 的取数条件是 `f.status IN ('pending','error')`
                #     且 `stage_status(extract)` 未 done；
                #   * 04_encode / 07_face_encode 的取数条件是
                #     `stage_status(extract)='done'` **且 `f.status='done'`**。
                #   所以对一个「抽帧早就成功、只是编码失败」的文件，
                #   把 status 改成 pending 会让它三条路全进不去 ——
                #   抽帧因 stage 已 done 而跳过它，编码因 status 不是 done 而跳过它，
                #   于是永远编码不了（实测有文件这样卡住）。
                #   正确做法：抽帧已完成就保持 done（让编码阶段拾取），
                #   否则才置 pending（让抽帧重来）。
                ex = conn.execute(
                    "SELECT status FROM stage_status WHERE file_id=? AND stage='extract'",
                    (fid,)).fetchone()
                if ex and str(ex["status"]) == "done":
                    # 抽帧成功 → files.status 必须是 done，否则编码阶段看不见它。
                    # 这里要**显式恢复**（不能只是「不改成 pending」）：
                    # 之前那版 bug 已经把一批文件的 status 改成了 pending，
                    # 只避免再次改坏是修不回来的（实测剩 25 个一直卡着）。
                    # 只在 pending/error 时恢复，绝不覆盖 missing / skipped_dup。
                    conn.execute(
                        "UPDATE files SET status='done', updated_at=? "
                        "WHERE file_id=? AND status IN ('pending','error')",
                        (local_now_iso(), fid))
                else:
                    conn.execute("UPDATE files SET status='pending', updated_at=? WHERE file_id=?",
                                 (local_now_iso(), fid))
            for fid in over:
                D.add_dead_letter(conn, fid, model, MAX_REPAIR_ATTEMPTS,
                                  "超过最大重试次数，转入死信表")
            conn.commit()
            out[f"{model}_requeued"] = len(candidates)
        return out
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# reconcile
# --------------------------------------------------------------------------- #


def expected_point_ids(conn, cfg: Config, collection: str) -> List[int]:
    """按 sqlite 计算「理论上应该存在的 point_id 集合」。

    铁律 3：检索只 join done 的文件。因此语义侧必须同时满足
    files.status='done' AND file_encoding(semantic).status='done'，
    否则「已抽帧但未编码」的文件会被误判为 Qdrant 缺失。
    """
    if collection == semantic_collection(cfg):
        rows = conn.execute("""
            SELECT f.file_id AS file_id, s.shot_ix AS shot_ix
              FROM files f
              JOIN shots s ON s.file_id = f.file_id
              JOIN file_encoding fe
                    ON fe.file_id = f.file_id
                   AND fe.model = 'semantic'
                   AND fe.status = 'done'
             WHERE f.status = 'done'
        """)
        return [make_point_id(int(r["file_id"]), int(r["shot_ix"])) for r in rows]
    if collection == face_collection(cfg):
        rows = conn.execute("SELECT face_id FROM face_records WHERE status='alive' AND has_vector=1")
        return [int(r["face_id"]) for r in rows]
    return []


def cmd_reconcile(cfg: Config, args) -> Dict[str, Any]:
    store = open_store(cfg)
    ok, msg = store.health()
    if not ok:
        raise RuntimeError(msg)
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        report: Dict[str, Any] = {}
        for coll in (semantic_collection(cfg), face_collection(cfg)):
            expected = expected_point_ids(conn, cfg, coll)
            report[coll] = store.reconcile(coll, expected)
        total_diff = sum((r.get("diff") or 0) for r in report.values())
        report["total_diff"] = total_diff
        report["verdict"] = "零差异" if total_diff == 0 else f"存在 {total_diff} 处差异"
        return report
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# clean-frames（LRU）
# --------------------------------------------------------------------------- #


def cmd_clean_frames(cfg: Config, args) -> Dict[str, Any]:
    """frames/ LRU 清理：淘汰最近 N 天未被命中的关键帧。thumbs 与 faces 永不淘汰。

    「未被命中」的判定依据是 files.last_hit_at（由检索层 D.touch_hits 写入），
    这正是规格第三章要求 LRU 时隐含需要的那张时间戳。
      * last_hit_at IS NULL   → 从未被任何检索命中过
      * last_hit_at < cutoff  → 最近 N 天未被命中
    另保留一条安全阀：仍是「检索可用」的文件（file_encoding semanic=done）
    且 indexed_at 在 N 天内的，不动（刚建完索引还没被搜到的不该立刻删）。
    """
    days = int(getattr(args, "days", 30) or 30)
    cutoff = datetime.now() - timedelta(days=days)
    cutoff_s = cutoff.strftime("%Y-%m-%dT%H:%M:%S")
    frames_root = cfg.path("paths.frames")
    if not frames_root.exists():
        return {"frames_root": str(frames_root), "removed_dirs": 0, "freed": "0 B",
                "days": days, "dry_run": bool(args.dry_run)}

    keep_ids: set[int] = set()
    recent_ids: set[int] = set()
    try:
        conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    except Exception as exc:  # noqa: BLE001
        LOG.error("无法读取库，拒绝清理（安全优先）：%s", exc)
        return {"frames_root": str(frames_root), "removed_dirs": 0, "freed": "0 B",
                "error": str(exc)}
    try:
        # 最近 N 天被命中的 → 保留
        for r in conn.execute(
            "SELECT file_id FROM files WHERE last_hit_at IS NOT NULL AND last_hit_at >= ?",
            (cutoff_s,),
        ):
            keep_ids.add(int(r["file_id"]))
        # 尚未被任何检索命中、但索引很新的 → 暂缓（给「刚建完库还没搜」留缓冲期）
        for r in conn.execute(
            "SELECT file_id FROM files WHERE last_hit_at IS NULL AND indexed_at >= ?",
            (cutoff_s,),
        ):
            recent_ids.add(int(r["file_id"]))
    finally:
        conn.close()

    removed = 0
    freed = 0
    skipped_recent = 0
    victims: List[Path] = []
    for d in sorted(frames_root.iterdir()):
        if not d.is_dir() or not d.name.isdigit():
            continue
        fid = int(d.name)
        if fid in keep_ids:
            continue
        if fid in recent_ids:
            skipped_recent += 1
            continue
        victims.append(d)

    for d in victims:
        try:
            size = dir_size_bytes(d)
            if args.dry_run:
                LOG.info("[dry-run] 将删除关键帧目录 %s（%s）", d.name, human_bytes(size))
            else:
                shutil.rmtree(d, ignore_errors=True)
            removed += 1
            freed += size
        except OSError as exc:
            LOG.warning("删除失败 %s：%s", d, exc)

    if not args.dry_run:
        wconn = D.connect(cfg.db_path, cfg=cfg)
        try:
            D.audit(wconn, "maintain", f"clean-frames days={days} removed={removed}")
            wconn.commit()
        finally:
            wconn.close()
    return {
        "frames_root": str(frames_root),
        "days": days,
        "removed_dirs": removed,
        "kept_recently_indexed": skipped_recent,
        "kept_hit_files": len(keep_ids),
        "freed": human_bytes(freed),
        "dry_run": bool(args.dry_run),
    }


# --------------------------------------------------------------------------- #
# clean-query-cache
# --------------------------------------------------------------------------- #


def cmd_clean_query_cache(cfg: Config, args) -> Dict[str, Any]:
    ttl_hours = float(cfg.get("storage.query_cache_ttl_hours", 24))
    cutoff = time.time() - ttl_hours * 3600
    root = cfg.path("paths.query_cache")
    removed = 0
    freed = 0
    if root.exists():
        for p in root.rglob("*"):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    sz = p.stat().st_size
                    if not args.dry_run:
                        p.unlink()
                    removed += 1
                    freed += sz
            except OSError:
                continue
        # 清理空目录
        if not args.dry_run:
            for d in sorted((x for x in root.rglob("*") if x.is_dir()), reverse=True):
                try:
                    if not any(d.iterdir()):
                        d.rmdir()
                except OSError:
                    pass
    return {"query_cache": str(root), "removed_files": removed,
            "freed": human_bytes(freed), "ttl_hours": ttl_hours,
            "dry_run": bool(args.dry_run)}


# --------------------------------------------------------------------------- #
# prune-orphans（必须手动触发 + 二次确认 + 先 dry-run）
# --------------------------------------------------------------------------- #


def cmd_prune_orphans(cfg: Config, args) -> Dict[str, Any]:
    days = int(cfg.get("storage.orphan_missing_days", 30))
    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        orphans = D.mark_orphans_for_reconcile(conn, days=days)
        rows = [{"face_id": fid, "thumb_path": tp} for fid, tp in orphans]
        report = {
            "missing_days": days,
            "orphan_faces": len(rows),
            "sample": rows[:20],
            "dry_run": not bool(getattr(args, "confirm", False)),
        }
        if not getattr(args, "confirm", False):
            LOG.warning("这是 dry-run 报告。加 --confirm 才会真正删除（规格要求二次确认）。")
            return report

        deleted_thumbs = 0
        for fid, tp in orphans:
            if tp:
                try:
                    p = Path(tp)
                    if p.exists():
                        p.unlink()
                        deleted_thumbs += 1
                except OSError as exc:
                    LOG.warning("删除 thumb 失败 %s：%s", tp, exc)
        ids = [fid for fid, _ in orphans]
        if ids:
            q = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM face_records WHERE face_id IN ({q})", ids)
        conn.commit()
        D.audit(conn, "maintain", f"prune-orphans days={days}",
                {"orphans": len(rows)}, {"deleted": len(ids), "thumbs": deleted_thumbs})
        conn.commit()
        report.update({"deleted_thumbs": deleted_thumbs, "deleted_records": len(ids)})
        return report
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# status：一眼看清「索引建到哪了」
# --------------------------------------------------------------------------- #


def cmd_status(cfg: Config, args) -> Dict[str, Any]:
    """只读汇报索引状态：索引根 / 文件状态分布 / 类型分布 / 时间来源 / 产物占用 / 下一步。

    回答三个最常被问的问题：
      1) 我扫描的目录到底进库了没有？
      2) 现在进行到哪一步（pending / done / error / missing 各多少）？
      3) 下一步该跑哪个脚本？
    """
    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        libs: List[Dict[str, Any]] = []
        for r in conn.execute(
            "SELECT root, last_scan, status, indexed_faces FROM libraries ORDER BY last_scan DESC"
        ):
            root = str(r["root"])
            # 该根下真实索引到的文件数：用 path 前缀统计，这是唯一可靠的口径
            like = root.rstrip("\\/") + "%"
            n_files = conn.execute(
                "SELECT COUNT(*) c FROM files WHERE path LIKE ?", (like,)).fetchone()["c"]
            libs.append({
                "indexed_root": root,
                "files": int(n_files),
                "last_scan": r["last_scan"],
                "scan_status": r["status"],
            })

        total = conn.execute("SELECT COUNT(*) c FROM files").fetchone()["c"]
        by_status = {r["status"]: int(r["n"]) for r in conn.execute(
            "SELECT status, COUNT(*) n FROM files GROUP BY status ORDER BY n DESC")}
        by_kind = {r["kind"]: int(r["n"]) for r in conn.execute(
            "SELECT kind, COUNT(*) n FROM files GROUP BY kind ORDER BY n DESC")}
        time_src = {(r["time_source"] or "(未解析)"): int(r["n"]) for r in conn.execute(
            "SELECT time_source, COUNT(*) n FROM files GROUP BY time_source ORDER BY n DESC")}

        def optional(sql: str, key: str = "n") -> Dict[str, int]:
            """统计可选表；表还不存在（对应阶段没跑过）时返回空 dict，不报错。"""
            try:
                return {f"{r[0]}:{r[1]}": int(r[2]) for r in conn.execute(sql)}
            except Exception:  # noqa: BLE001 - 表不存在属于正常状态
                return {}

        def scalar(sql: str, default: int = 0) -> int:
            try:
                return int(conn.execute(sql).fetchone()[0])
            except Exception:  # noqa: BLE001
                return default

        enc = optional(
            "SELECT model, status, COUNT(*) FROM file_encoding GROUP BY model, status")
        stage = optional(
            "SELECT stage, status, COUNT(*) FROM stage_status GROUP BY stage, status")
        shots = scalar("SELECT COUNT(*) FROM shots")
        faces = scalar("SELECT COUNT(*) FROM face_records WHERE status='alive'")
        rng = conn.execute(
            "SELECT MIN(captured_at) a, MAX(captured_at) b FROM files WHERE captured_at IS NOT NULL"
        ).fetchone()

        # 按 kind × status 分开统计（2026-10-18 新增，用户反馈「看不出视频进度」）：
        # 原来的 by_status 是跨 photo/video 的扁平计数，只看到 pending=21670，
        # 没法判断这些 pending 到底是照片还是视频，也就没法回答
        # 「视频跑到哪了」。这里把两类拆开，并附上视频的关键帧/镜头规模。
        by_kind_status: Dict[str, int] = {}
        for r in conn.execute(
            "SELECT kind, status, COUNT(*) n FROM files GROUP BY kind, status ORDER BY kind, n DESC"
        ):
            by_kind_status[f"{r['kind']}:{r['status']}"] = int(r["n"])

        # 视频侧规模：已完成多少、抽到多少镜头/关键帧、还剩多少体积要读盘
        video = {
            "done": by_kind_status.get("video:done", 0),
            "pending": by_kind_status.get("video:pending", 0),
            "error": by_kind_status.get("video:error", 0),
            "shots": scalar("SELECT COUNT(*) FROM shots s JOIN files f "
                            "ON f.file_id=s.file_id WHERE f.kind='video'"),
            "bytes_remaining": scalar(
                "SELECT COALESCE(SUM(size),0) FROM files "
                "WHERE kind='video' AND status IN ('pending','error')"),
            "bytes_done": scalar(
                "SELECT COALESCE(SUM(size),0) FROM files "
                "WHERE kind='video' AND status='done'"),
        }
        video["total"] = video["done"] + video["pending"] + video["error"]
        video["pct"] = round(100.0 * video["done"] / video["total"], 2) if video["total"] else 0.0
        video["shots_per_file"] = round(
            video["shots"] / video["done"], 2) if video["done"] else 0.0

        # 「实际索引了哪些顶层目录」——判断「我扫的目录到底进库没有」最直观的口径。
        #
        # 不能自己按 '\\' 切 path 去推层级：对 "E:\\" 这种盘根，
        # parts 是 ['E:', '晚上', ...]，层级不是从盘符算得出来的
        # （实测把 depth 算成 2，结果把 E:\\晚上\\ 的下一层当成了顶层）。
        # 库里已经存了 rel_path（相对索引根），直接取它的第一段即可。
        #
        # ⚠️ 2026-10-18：原来是在 Python 里逐行 split，50.3 万行要 **134 秒**
        #   （status 这条命令本身只该几秒）。改成 SQL 聚合 —— 同样的口径，
        #   一次 GROUP BY 就够，实测回到秒级。
        top_dirs: List[Dict[str, Any]] = []
        for lib in libs:
            like = str(lib["indexed_root"]).rstrip("\\/") + "%"
            try:
                rows = conn.execute(
                    """SELECT CASE WHEN instr(rel_path, '\\') > 0
                                   THEN substr(rel_path, 1, instr(rel_path, '\\') - 1)
                                   ELSE rel_path END AS seg,
                              COUNT(*) AS n
                         FROM files
                        WHERE path LIKE ? AND rel_path IS NOT NULL AND rel_path <> ''
                        GROUP BY seg ORDER BY n DESC LIMIT 12""", (like,)).fetchall()
            except Exception as exc:  # noqa: BLE001 - 统计失败不该让 status 整体失败
                LOG.warning("统计顶层目录失败：%s", exc)
                rows = []
            for r in rows:
                seg = str(r["seg"] or "").replace("/", "\\").lstrip("\\")
                if seg:
                    top_dirs.append({"root": lib["indexed_root"], "dir": seg,
                                     "files": int(r["n"])})

        pending = by_status.get("pending", 0)
        done = by_status.get("done", 0)
        if total == 0:
            nxt = f'库是空的。先扫描：01_manifest.py --root "{cfg.media_root}"'
        elif pending > 0:
            # 照片侧默认把 L2a 去重折进 03_extract 的同一趟读盘（scan.fuse_photo_pipeline），
            # 所以推荐顺序是 03 → 06 → 07 → 04，而不是旧的 02 → 03 → 04。
            nxt = (f"还有 {pending:,} 个 pending。推荐顺序："
                   f"03_extract.py → 06_face_detect.py → 07_face_encode.py → 04_encode.py"
                   f"（无人值守一条命令：pwsh -File tmp\\chain_rounds.ps1 -Stage all）")
        elif enc.get("semantic:done", 0) < done and done > 0:
            nxt = "文件已抽帧但未编码，下一阶段：04_encode.py"
        elif faces == 0:
            nxt = ("素材已编码，但人脸库为空 —— 照片墙/精确找人需要 "
                   "06_face_detect.py → 07_face_encode.py")
        else:
            nxt = "索引就绪，可启动界面：start.bat"

        # 人可读的两行摘要（JSON 里也有同样的数据，这里只是别让它挤成一长串）
        LOG.info("按类型×状态：%s", by_kind_status)
        LOG.info(
            "视频进度：%s/%s（%.1f%%）  镜头 %s（%.2f/文件）  已完成 %s  剩余待读 %s  失败 %s",
            f"{video['done']:,}", f"{video['total']:,}", video["pct"],
            f"{video['shots']:,}", video["shots_per_file"],
            human_bytes(video["bytes_done"]), human_bytes(video["bytes_remaining"]),
            f"{video['error']:,}",
        )

        return {
            "config_media_root": str(cfg.media_root),
            "db": str(cfg.db_path),
            "db_size": human_bytes(cfg.db_path.stat().st_size) if cfg.db_path.exists() else "0 B",
            "indexed_roots": libs,
            "indexed_top_dirs": top_dirs,
            "files_total": int(total),
            "by_status": by_status,
            "by_kind": by_kind,
            "by_kind_status": by_kind_status,
            "video": video,
            "time_source": time_src,
            "captured_range": [rng["a"], rng["b"]] if rng else [None, None],
            "shots": int(shots),
            "faces": int(faces),
            "encoding": enc,
            "pipeline_stage": stage,
            "paths_index": conn.execute("SELECT COUNT(*) c FROM path_index").fetchone()["c"],
            # --no-disk 时跳过 NVMe 目录统计：那是 35 万个文件的真实 I/O，
            # 单次要 21 秒左右。只想看「跑到哪一步了」的时候没必要付这个代价。
            "nvme_used": ("skipped（--no-disk）" if getattr(args, "no_disk", False)
                          else human_bytes(dir_size_bytes(cfg.path("paths.nvme_root")))),
            "next_step": nxt,
        }
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# quota
# --------------------------------------------------------------------------- #


def cmd_quota(cfg: Config, args) -> Dict[str, Any]:
    root = cfg.path("paths.nvme_root")
    used = dir_size_bytes(root)
    budget_gb = float(cfg.get("storage.nvme_budget_gb", 300))
    warn_at = float(cfg.get("storage.budget_warn_ratio", 0.80))
    ratio = used / (budget_gb * 1024 ** 3) if budget_gb else 0.0
    free_gb = 0.0
    try:
        free_gb = shutil.disk_usage(str(Path(root).anchor or root)).free / 1024 ** 3
    except OSError:
        pass
    paused = ratio >= warn_at
    if paused:
        LOG.error("NVMe 使用 %.1f%% 超阈值 %.0f%%：按规格第三章要求暂停增量入库",
                  ratio * 100, warn_at * 100)
    return {
        "nvme_root": str(root),
        "used": human_bytes(used),
        "budget_gb": budget_gb,
        "used_ratio": round(ratio, 4),
        "warn_ratio": warn_at,
        "should_pause_indexing": paused,
        "disk_free_gb": round(free_gb, 1),
        "breakdown": {
            "frames": human_bytes(dir_size_bytes(cfg.path("paths.frames"))),
            "thumbs": human_bytes(dir_size_bytes(cfg.path("paths.thumbs"))),
            "faces_raw": human_bytes(dir_size_bytes(cfg.path("paths.faces_raw"))),
            "faces_thumbs": human_bytes(dir_size_bytes(cfg.path("paths.faces_thumbs"))),
            "embeddings": human_bytes(dir_size_bytes(cfg.path("paths.embeddings"))),
        },
    }


# --------------------------------------------------------------------------- #
# rotate-logs
# --------------------------------------------------------------------------- #


def cmd_rotate_logs(cfg: Config, args) -> Dict[str, Any]:
    keep_days = int(getattr(args, "days", 30) or 30)
    cutoff = datetime.now() - timedelta(days=keep_days)
    removed, freed = 0, 0
    for p in cfg.log_dir.glob("*.log"):
        try:
            if datetime.fromtimestamp(p.stat().st_mtime) < cutoff:
                sz = p.stat().st_size
                if not args.dry_run:
                    p.unlink()
                removed += 1
                freed += sz
        except OSError:
            continue
    return {"log_dir": str(cfg.log_dir), "removed": removed,
            "freed": human_bytes(freed), "keep_days": keep_days,
            "dry_run": bool(args.dry_run)}


# --------------------------------------------------------------------------- #
# fix-skipped-dup：修复铁律 1 的记账完整性
# --------------------------------------------------------------------------- #


def cmd_fix_skipped_dup(cfg: Config, args) -> Dict[str, Any]:
    """为每个 `files.status='skipped_dup'` 的文件补齐缺失的 skipped_dup 行。

    铁律 1（规格 4.2）要求：**每个模型各记一行** skipped_dup。
    这里做的是幂等修补 —— 只 INSERT 缺失的那些，不碰已有的行。

    什么时候会缺（2026-10-16 实际踩到）：
    维护时按 model 单独清理 `file_encoding`（例如换语义编码的像素预算后要重编码，
    于是 `DELETE FROM file_encoding WHERE model='semantic'`），会把
    semantic 侧的 skipped_dup 一起删掉，而 face 侧还在 —— 两个模型的
    skipped_dup 计数就对不上了，审计会报「去重文件数不等于已入库+已跳过」。
    """
    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        rows = list(conn.execute(
            "SELECT f.file_id AS file_id, f.kind_hint AS kind_hint FROM files f "
            "WHERE f.status='skipped_dup'"))
        missing = {m: [] for m in (D.MODEL_SEMANTIC, D.MODEL_FACE)}
        for r in rows:
            fid = int(r["file_id"])
            have = {str(x["model"]) for x in conn.execute(
                "SELECT model FROM file_encoding WHERE file_id=?", (fid,))}
            for model in missing:
                if model not in have:
                    missing[model].append(fid)
        if args.dry_run:
            return {"skipped_dup_files": len(rows),
                    "missing_semantic": len(missing[D.MODEL_SEMANTIC]),
                    "missing_face": len(missing[D.MODEL_FACE]),
                    "dry_run": True}
        note = "backfilled by 12_maintain fix-skipped-dup"
        for model, ids in missing.items():
            for fid in ids:
                D.enc_skipped_dup(conn, fid, model, note)
        D.retry_on_lock(conn.commit)
        return {"skipped_dup_files": len(rows),
                "backfilled_semantic": len(missing[D.MODEL_SEMANTIC]),
                "backfilled_face": len(missing[D.MODEL_FACE]),
                "dry_run": False}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# fix-status：修复 files.status 与阶段状态不一致
# --------------------------------------------------------------------------- #


def cmd_fix_status(cfg: Config, args) -> Dict[str, Any]:
    """把 `files.status` 与「阶段/编码的真实状态」对齐。

    为什么需要它（2026-10-18 / 10-19 两次真实事故）：
      ① 一次早期版本的 `repair` 把「抽帧早已成功、只是编码失败」的文件
         `status` 改成了 `pending`；后来编码补成功了，但**没有任何代码路径会把
         status 改回 done** —— 于是 `semantic_search`（只认 done）搜不到它们、
         `reconcile` 把它们报成孤儿向量、链的 `pending>0` 永远成立（实测空转 291 轮）。
      ② 用不同的 `--root` 重扫一次，`manifest.apply_classification()` 的
         missing 判定是**全库差集**（`SELECT file_id FROM files WHERE status != 'missing'`
         不按扫描根过滤），于是把旧根下**所有**非 missing 的文件——**包括
         status='skipped_dup' 的去重记录**——统统改成了 missing。
         去重信息本身还完整地留在 `file_encoding`（两个模型各一行 skipped_dup），
         但 `files.status` 已经丢了，重扫回来时这些文件会被当成 `pending` 重新
         走一遍 L2a/L2b/L2c/F 系列（实测会白白重做 18 万张照片）。

    本命令是幂等的对账，按以下优先级恢复（**优先级从上到下**）：
      1) 两个模型的 file_encoding 都是 skipped_dup → `status='skipped_dup'`（恢复去重记账）
      2) extract 阶段 done                          → `status='done'`（让检索/照片墙看得见）
      3) extract 阶段 error                         → `status='error'`
    绝不覆盖 `missing`（那是磁盘上确实没有的文件，要由重扫来复活）。
    """
    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        # 1) 去重记账恢复：两模型都是 skipped_dup
        to_dup = [int(r["file_id"]) for r in conn.execute(
            """SELECT f.file_id AS file_id FROM files f
                WHERE f.status <> 'missing'
                  AND (SELECT COUNT(*) FROM file_encoding fe
                        WHERE fe.file_id=f.file_id AND fe.model='semantic'
                          AND fe.status='skipped_dup') > 0
                  AND (SELECT COUNT(*) FROM file_encoding fe
                        WHERE fe.file_id=f.file_id AND fe.model='face'
                          AND fe.status='skipped_dup') > 0
                  AND f.status <> 'skipped_dup'""")]
        dup_set = set(to_dup)

        # 2) 抽帧成功 → done（排除刚判定为去重的）
        to_done = [int(r["file_id"]) for r in conn.execute(
            """SELECT f.file_id AS file_id FROM files f
                 JOIN stage_status ex ON ex.file_id=f.file_id
                  AND ex.stage='extract' AND ex.status='done'
                WHERE f.status IN ('pending','error')""")
            if int(r["file_id"]) not in dup_set]

        # 3) 抽帧失败 → error
        to_error = [int(r["file_id"]) for r in conn.execute(
            """SELECT f.file_id AS file_id FROM files f
                 JOIN stage_status ex ON ex.file_id=f.file_id
                  AND ex.stage='extract' AND ex.status='error'
                WHERE f.status='done'""")]

        if args.dry_run:
            return {"would_restore_skipped_dup": len(to_dup),
                    "would_set_done": len(to_done),
                    "would_set_error": len(to_error),
                    "sample_skipped_dup": to_dup[:8], "sample_done": to_done[:8],
                    "dry_run": True}

        now = local_now_iso()

        def batch_update(ids: List[int], sql: str, extra: tuple = ()) -> None:
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                q = ",".join("?" * len(chunk))
                conn.execute(sql.format(q=q), (*extra, now, *chunk))

        if to_dup:
            # 去重文件：不可见、kind_hint 标 duplicate，与 02_dedup/fused 的口径一致
            for start in range(0, len(to_dup), 500):
                chunk = to_dup[start:start + 500]
                q = ",".join("?" * len(chunk))
                conn.execute(
                    f"UPDATE files SET status='skipped_dup', visible=0, "
                    f"kind_hint='duplicate', updated_at=? WHERE file_id IN ({q})",
                    (now, *chunk))
        if to_done:
            batch_update(to_done, "UPDATE files SET status='done', updated_at=? "
                                  "WHERE file_id IN ({q})")
        if to_error:
            batch_update(to_error, "UPDATE files SET status='error', updated_at=? "
                                   "WHERE file_id IN ({q})")
        D.retry_on_lock(conn.commit)
        return {"restored_skipped_dup": len(to_dup),
                "set_done": len(to_done), "set_error": len(to_error),
                "dry_run": False}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def cmd_purge_missing(cfg, args) -> dict:
    """清理「缺失」文件的记录与向量。默认只报告；--apply 才真删。

    界面上的「删除记录与向量」如果因为某些原因失效，可以用它兜底：
        .venv\\Scripts\\python.exe 12_maintain.py purge-missing            # 只报告
        .venv\\Scripts\\python.exe 12_maintain.py purge-missing --apply    # 真删
        .venv\\Scripts\\python.exe 12_maintain.py purge-missing --apply --limit 100
    """
    from src import migrate as MG

    rep = MG.purge_missing(cfg, None, dry_run=True)
    would = int(rep.get("would_purge") or 0)
    if not getattr(args, "apply", False):
        return {"dry_run": True, "would_purge": would,
                "samples": rep.get("samples", [])[:5]}
    ids = None
    limit = int(getattr(args, "purge_max", 0) or 0)
    if limit > 0:
        import sqlite3

        from src import db as D

        conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
        try:
            ids = [int(r[0]) for r in conn.execute(
                "SELECT file_id FROM files WHERE status='missing' "
                "ORDER BY file_id LIMIT ?", (limit,))]
        finally:
            conn.close()
    res = MG.purge_missing(cfg, ids, dry_run=False)
    left = int(MG.missing_report(cfg, limit=1)["total"] or 0)
    return {"dry_run": False, "purged": int(res.get("purged") or 0),
            "missing_before": would, "missing_left": left}


COMMANDS = {
    "status": cmd_status,
    "migrate": cmd_migrate,
    "repair": cmd_repair,
    "reconcile": cmd_reconcile,
    "clean-frames": cmd_clean_frames,
    "clean-query-cache": cmd_clean_query_cache,
    "prune-orphans": cmd_prune_orphans,
    "quota": cmd_quota,
    "rotate-logs": cmd_rotate_logs,
    "fix-skipped-dup": cmd_fix_skipped_dup,
    "fix-status": cmd_fix_status,
    "purge-missing": cmd_purge_missing,
}


def main(argv=None) -> int:
    p = base_argparser("运维：migrate / repair / reconcile / LRU 清理 / 孤儿回收 / 配额 / 日志轮转",
                       needs_root=False)
    p.add_argument("command", choices=sorted(COMMANDS), help="子命令")
    p.add_argument("--days", type=int, default=30, help="clean-frames / rotate-logs 的天数阈值")
    p.add_argument("--no-disk", action="store_true",
                   help="status 时跳过 NVMe 目录统计（省约 21 秒，看进度时用）")
    p.add_argument("--apply", action="store_true",
                   help="purge-missing 的二次确认（不加则只报告）")
    p.add_argument("--max", type=int, default=0, dest="purge_max",
                   help="purge-missing 只处理前 N 条（0=全部）")
    p.add_argument("--confirm", action="store_true",
                   help="prune-orphans 的二次确认（不加则只输出 dry-run 报告）")
    args = p.parse_args(argv)

    cfg = cfg_from_args(args)
    log = get_logger("12_maintain", cfg, args.log_level)
    D.ensure_schema(cfg.db_path, cfg.root, cfg)
    try:
        summary = COMMANDS[args.command](cfg, args)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2
    emit_summary(summary, args.json, log, f"{args.command.upper()} SUMMARY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
