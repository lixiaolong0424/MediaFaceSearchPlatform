"""11_export.py · TSV 导出 + DEAD 校验 + 幂等 + --relative（规格 9.3）。

硬要求：
  * tab 分隔（文件名可能含逗号，CSV 会列错位）
  * 首行写 UTF-8 BOM（\\ufeff）—— TSV 唯一需要偏离纯 UTF-8 的地方
  * 列：kind \\t abs_path \\t shot_start_ms \\t shot_end_ms \\t face_id \\t score \\t thumb_path \\t path_status
  * 按 path 排序保证幂等（同输入同输出）
  * 导出前 os.path.exists 校验，失效标 DEAD，尾部汇总 # DEAD_PATHS
  * 首行注释写 # scope=... 与 # exported_at=ISO8601 UTC
  * --relative 开关输出相对路径
  * 三入口（全库清单 / 当前照片墙筛选结果 / 命中清单）不得混成一个按钮

同时提供结构化 TXT（供下游 agent 剪辑归档 + 用户快捷操作）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import db as D
from .common import Config, get_logger, utc_now_iso

LOG = get_logger("export")

SCOPE_LIBRARY = "library"
SCOPE_WALL = "wall_filtered"
SCOPE_HITS = "search_hits"

DEFAULT_COLUMNS = ["kind", "abs_path", "shot_start_ms", "shot_end_ms",
                   "face_id", "score", "thumb_path", "path_status"]

ALIVE = "ALIVE"
DEAD = "DEAD"


@dataclass
class ExportRow:
    kind: str
    abs_path: str
    shot_start_ms: Optional[int] = None
    shot_end_ms: Optional[int] = None
    face_id: Optional[int] = None
    score: Optional[float] = None
    thumb_path: Optional[str] = None
    path_status: str = ALIVE

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "abs_path": self.abs_path,
            "shot_start_ms": self.shot_start_ms,
            "shot_end_ms": self.shot_end_ms,
            "face_id": self.face_id,
            "score": None if self.score is None else round(float(self.score), 4),
            "thumb_path": self.thumb_path,
            "path_status": self.path_status,
        }


# --------------------------------------------------------------------------- #
# 行的构造
# --------------------------------------------------------------------------- #


def shots_to_rows(
    files: Sequence[Any],
    *,
    max_rows_per_file: Optional[int] = None,
) -> List[ExportRow]:
    """命中清单（search.SearchOutcome.files）→ 导出行。

    每个命中镜头一行，带时间段毫秒；同一文件多镜头展开为多行。
    """
    rows: List[ExportRow] = []
    for f in files:
        shots = getattr(f, "shots", []) or []
        if max_rows_per_file:
            shots = shots[:max_rows_per_file]
        for s in shots:
            rows.append(ExportRow(
                kind=str(getattr(f, "kind", "") or ""),
                abs_path=str(getattr(f, "path", "") or ""),
                shot_start_ms=_ms(getattr(s, "shot_start", None)),
                shot_end_ms=_ms(getattr(s, "shot_end", None)),
                face_id=getattr(s, "face_id", None),
                score=getattr(s, "score", None),
                thumb_path=getattr(s, "thumb_path", None),
            ))
        if not shots:  # 没有镜头信息时至少导出文件本身
            rows.append(ExportRow(
                kind=str(getattr(f, "kind", "") or ""),
                abs_path=str(getattr(f, "path", "") or ""),
                score=getattr(f, "best_score", None),
            ))
    return rows


def _ms(seconds: Optional[float]) -> Optional[int]:
    if seconds is None:
        return None
    return int(round(float(seconds) * 1000.0))


def library_to_rows(conn, cfg: Config) -> List[ExportRow]:
    """全库清单：每个文件的每个镜头一行；照片给一行。"""
    rows: List[ExportRow] = []
    sql = """
        SELECT f.file_id, f.path, f.kind, f.captured_at, s.shot_ix, s.shot_start, s.shot_end
          FROM files f
          LEFT JOIN shots s ON s.file_id = f.file_id
         WHERE f.status != 'missing'
         ORDER BY f.path, s.shot_ix
    """
    for r in conn.execute(sql):
        rows.append(ExportRow(
            kind=str(r["kind"] or ""),
            abs_path=str(r["path"] or ""),
            shot_start_ms=_ms(r["shot_start"]),
            shot_end_ms=_ms(r["shot_end"]),
        ))
    return rows


def wall_to_rows(conn, cfg: Config, *, min_quality: float = 0.0,
                 t_start: Optional[str] = None, t_end: Optional[str] = None,
                 file_id: Optional[int] = None) -> List[ExportRow]:
    """当前照片墙筛选结果：每张人脸一行。"""
    sql = ["""
        SELECT fr.face_id, fr.file_id, fr.sec, fr.quality, fr.thumb_path, fr.hidden,
               f.path, f.kind, f.status
          FROM face_records fr JOIN files f ON f.file_id = fr.file_id
         WHERE fr.status='alive' AND f.status != 'missing'
    """]
    args: List[Any] = []
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
    sql.append("ORDER BY f.path, fr.sec, fr.face_id")

    rows: List[ExportRow] = []
    for r in conn.execute(" ".join(sql), args):
        ms = _ms(r["sec"]) if r["kind"] == "video" else None
        rows.append(ExportRow(
            kind=str(r["kind"] or ""),
            abs_path=str(r["path"] or ""),
            shot_start_ms=ms,
            face_id=int(r["face_id"]),
            score=r["quality"],
            thumb_path=str(r["thumb_path"]) if r["thumb_path"] else None,
        ))
    return rows


# --------------------------------------------------------------------------- #
# 写盘
# --------------------------------------------------------------------------- #


@dataclass
class ExportResult:
    path: str
    scope: str
    written: int
    dead: List[str] = field(default_factory=list)
    relative: bool = False
    total_bytes: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path, "scope": self.scope, "written": self.written,
            "dead": len(self.dead), "dead_samples": self.dead[:5],
            "relative": self.relative, "bytes": self.total_bytes,
        }


def _relativize(abs_path: str, base: Path) -> str:
    try:
        return os.path.relpath(abs_path, str(base))
    except ValueError:
        return abs_path


def export_tsv(
    rows: Sequence[ExportRow] | Sequence[Dict[str, Any]],
    scope_label: str,
    out_path: Path,
    relative: bool = False,
    *,
    cfg: Optional[Config] = None,
    columns: Optional[Sequence[str]] = None,
    extra_notes: Sequence[str] = (),
    verify_exists: bool = True,
) -> ExportResult:
    """签名与规格第十三章一致，返回 {written, dead:[...]} 的可扩展结构。"""
    cols = list(columns or (cfg.get("export.columns") if cfg else None) or DEFAULT_COLUMNS)
    delim = str(cfg.get("export.delimiter", "\t")) if cfg else "\t"
    encoding = str(cfg.get("export.encoding", "utf-8-sig")) if cfg else "utf-8-sig"
    base = cfg.media_root if (cfg and relative) else Path(".")

    norm: List[ExportRow] = []
    for r in rows:
        if isinstance(r, ExportRow):
            norm.append(r)
        else:
            norm.append(ExportRow(
                kind=str(r.get("kind") or ""),
                abs_path=str(r.get("abs_path") or r.get("path") or ""),
                shot_start_ms=r.get("shot_start_ms"),
                shot_end_ms=r.get("shot_end_ms"),
                face_id=r.get("face_id"),
                score=r.get("score"),
                thumb_path=r.get("thumb_path"),
                path_status=str(r.get("path_status") or ALIVE),
            ))

    # 存在性校验 → DEAD 标记
    dead: List[str] = []
    if verify_exists:
        for r in norm:
            if r.abs_path and not os.path.exists(r.abs_path):
                r.path_status = DEAD
                dead.append(r.abs_path)

    # 幂等排序：按绝对路径 → 时间 → face_id
    norm.sort(key=lambda r: (r.abs_path, r.shot_start_ms if r.shot_start_ms is not None else -1,
                             r.face_id if r.face_id is not None else -1))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: List[str] = []
    lines.append(f"# scope={scope_label}")
    lines.append(f"# exported_at={utc_now_iso()}")
    lines.append(f"# rows={len(norm)}")
    lines.append(f"# dead={len(dead)}")
    lines.append(f"# relative={bool(relative)}")
    lines.append(f"# columns={delim.join(cols)}")
    for n in extra_notes:
        lines.append(f"# note={n}")
    lines.append("#" + "-" * 78)
    lines.append(delim.join(cols))

    for r in norm:
        d = r.as_dict()
        if relative and d.get("abs_path"):
            d["abs_path"] = _relativize(str(d["abs_path"]), base)
        if relative and d.get("thumb_path"):
            d["thumb_path"] = _relativize(str(d["thumb_path"]), base)
        lines.append(delim.join("" if d.get(c) is None else str(d.get(c)) for c in cols))

    if dead:
        lines.append("# DEAD_PATHS")
        for p in sorted(dead):
            lines.append(f"# DEAD\t{p}")

    text = "\n".join(lines) + "\n"
    with open(out_path, "w", encoding=encoding, newline="\n") as fh:
        fh.write(text)

    size = out_path.stat().st_size if out_path.exists() else 0
    LOG.info("导出完成 scope=%s rows=%d dead=%d → %s", scope_label, len(norm), len(dead), out_path)
    return ExportResult(str(out_path), scope_label, len(norm), dead, bool(relative), size)


def default_out_path(cfg: Config, scope: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return cfg.export_dir / f"{scope}_{ts}.tsv"


# --------------------------------------------------------------------------- #
# 结构化 TXT（下游 agent 剪辑归档 + 用户快捷操作）
# --------------------------------------------------------------------------- #

TXT_COLUMNS = ["kind", "abs_path", "timecode", "shot_start_ms", "shot_end_ms",
               "face_id", "score", "open_folder_cmd"]


def export_structured_txt(
    rows: Sequence[ExportRow],
    scope_label: str,
    out_path: Path,
    *,
    cfg: Optional[Config] = None,
    relative: bool = False,
) -> ExportResult:
    """人类可读 + 机器可解析的 TXT：带时间码、可直接复制的 explorer 命令。"""
    base = cfg.media_root if (cfg and relative) else Path(".")
    norm = list(rows)
    dead: List[str] = []
    for r in norm:
        if r.abs_path and not os.path.exists(r.abs_path):
            r.path_status = DEAD
            dead.append(r.abs_path)
    norm.sort(key=lambda r: (r.abs_path, r.shot_start_ms or -1, r.face_id or -1))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="\n") as fh:
        fh.write("# 素材检索结果（供下游 agent 剪辑 / 归档，也供人工快捷操作）\n")
        fh.write(f"# scope={scope_label}\n")
        fh.write(f"# exported_at={utc_now_iso()}\n")
        fh.write(f"# rows={len(norm)}\n")
        fh.write(f"# dead={len(dead)}\n")
        fh.write(f"# columns={chr(9).join(TXT_COLUMNS)}\n")
        fh.write("# 说明：timecode 为视频内时间点（照片为空）；open_folder_cmd 可直接粘到 cmd 执行\n")
        fh.write("#" + "-" * 78 + "\n")
        fh.write("\t".join(TXT_COLUMNS) + "\n")
        for r in norm:
            p = _relativize(r.abs_path, base) if relative else r.abs_path
            tc = ""
            if r.shot_start_ms is not None:
                tc = _fmt_timecode(r.shot_start_ms)
            fh.write("\t".join([
                str(r.kind or ""),
                p,
                tc,
                "" if r.shot_start_ms is None else str(r.shot_start_ms),
                "" if r.shot_end_ms is None else str(r.shot_end_ms),
                "" if r.face_id is None else str(r.face_id),
                "" if r.score is None else f"{float(r.score):.4f}",
                f'explorer /select,"{r.abs_path}"' if r.abs_path else "",
            ]) + "\n")
        if dead:
            fh.write("# DEAD_PATHS\n")
            for p in sorted(dead):
                fh.write(f"# DEAD\t{p}\n")
    size = out_path.stat().st_size if out_path.exists() else 0
    return ExportResult(str(out_path), scope_label, len(norm), dead, bool(relative), size)


def _fmt_timecode(ms: int) -> str:
    total = max(0, int(ms)) // 1000
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
