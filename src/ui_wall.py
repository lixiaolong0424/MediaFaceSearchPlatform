"""Tab1 照片墙（browse）· 数据源必须是 sqlite 分页，Qdrant 只在点 [找同款] 时才调用。

规格 8.2：
  * 默认「概览模式」（贪心局部向量去重：顺序扫描，与已选代表脸 cosine>0.85 则跳过；
    38k→~300 张，O(n)，内存只存代表脸向量）；二级「平铺模式」全量
  * 每张卡片：128 人脸缩略图 + 角标（视频显 mm:ss，照片显日期）+ hover 浮层
    （完整绝对路径 + quality + bbox）+ 三按钮 [▶ 预览] [找同款] [📂]
  * 筛选：时间范围 / 来源文件 / 质量≥ / 排序 / 仅看本次新增
  * 分页必须 keyset cursor：游标=(captured_at,face_id) 复合键，
    WHERE (captured_at,face_id)<(?,?) ORDER BY ... LIMIT 61。禁用 offset 深层翻页。
  * 数据源必须是 sqlite 分页，一次 ≤60 条

概览模式的向量来源：优先读 faces collection（Qdrant）；若 Qdrant 不可用，
退回 sqlite 的 quality 排序 + 同文件秒级时间窗去重（保证墙永远能刷出来）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import db as D
from .common import Config, get_logger

LOG = get_logger("ui_wall")

SORT_CAPTURED_DESC = "captured_at_desc"
SORT_CAPTURED_ASC = "captured_at_asc"
SORT_QUALITY_DESC = "quality_desc"
SORT_RANDOM = "random"

ORDER_SQL = {
    SORT_CAPTURED_DESC: "COALESCE(fr.captured_at, f.captured_at, f.mtime) DESC, fr.face_id DESC",
    SORT_CAPTURED_ASC: "COALESCE(fr.captured_at, f.captured_at, f.mtime) ASC, fr.face_id ASC",
    SORT_QUALITY_DESC: "COALESCE(fr.quality,0) DESC, fr.face_id DESC",
}


@dataclass
class Cursor:
    """keyset 游标：复合键 (time_key, face_id)。"""

    time_key: Optional[str] = None
    face_id: Optional[int] = None

    @property
    def valid(self) -> bool:
        return self.time_key is not None and self.face_id is not None

    def encode(self) -> str:
        return json.dumps({"t": self.time_key, "f": self.face_id}, ensure_ascii=False)

    @staticmethod
    def decode(raw: Optional[str]) -> Optional["Cursor"]:
        if not raw:
            return None
        try:
            d = json.loads(raw)
            return Cursor(d.get("t"), int(d["f"]) if d.get("f") is not None else None)
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            return None


@dataclass
class WallItem:
    face_id: int
    file_id: int
    path: str
    kind: str
    captured_at: Optional[str]
    sec: Optional[float]
    quality: float
    bbox: Optional[List[float]]
    thumb_path: Optional[str]
    raw_path: Optional[str]
    hidden: int = 0
    time_source: Optional[str] = None
    badge: str = ""

    @property
    def timecode(self) -> str:
        if self.kind == "video" and self.sec is not None:
            total = int(self.sec)
            h, rem = divmod(total, 3600)
            m, s = divmod(rem, 60)
            return f"{h:02d}:{m:02d}:{s:02d}"
        if self.captured_at:
            return self.captured_at[:10].replace("-", "/")
        return ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "face_id": self.face_id, "file_id": self.file_id, "path": self.path,
            "kind": self.kind, "captured_at": self.captured_at, "sec": self.sec,
            "quality": self.quality, "bbox": self.bbox, "thumb_path": self.thumb_path,
            "raw_path": self.raw_path, "timecode": self.timecode,
            "time_source": self.time_source, "badge": self.badge,
        }


def _build_where(
    *,
    t_start: Optional[str] = None,
    t_end: Optional[str] = None,
    file_id: Optional[int] = None,
    min_quality: float = 0.0,
    only_new: bool = False,
    include_hidden: bool = False,
) -> Tuple[List[str], List[Any]]:
    # has_vector=1 是「F3 已完成」的锚点：06_face_detect 先写记录、07_face_encode 才补
    # thumb_path 与向量。若不过滤，06 跑完但 07 没跑时墙上会出现一堆没有缩略图的空卡片。
    where = ["fr.status='alive'", "fr.has_vector=1", "f.visible=1", "f.status != 'missing'"]
    args: List[Any] = []
    if not include_hidden:
        where.append("fr.hidden=0")
    if min_quality > 0:
        where.append("COALESCE(fr.quality,0) >= ?")
        args.append(float(min_quality))
    time_expr = "COALESCE(fr.captured_at, f.captured_at, f.mtime)"
    if t_start:
        where.append(f"{time_expr} >= ?")
        args.append(t_start)
    if t_end:
        where.append(f"{time_expr} <= ?")
        args.append(t_end)
    if file_id:
        where.append("fr.file_id = ?")
        args.append(int(file_id))
    if only_new:
        where.append("f.indexed_at >= datetime('now','-1 day')")
    return where, args


def _row_to_item(r: Any) -> WallItem:
    bbox = None
    if r["bbox"]:
        try:
            bbox = json.loads(r["bbox"]) if isinstance(r["bbox"], str) else list(r["bbox"])
        except (json.JSONDecodeError, TypeError):
            bbox = None
    return WallItem(
        face_id=int(r["face_id"]),
        file_id=int(r["file_id"]),
        path=str(r["path"]),
        kind=str(r["kind"] or ""),
        captured_at=r["captured_at"],
        sec=r["sec"],
        quality=float(r["quality"] or 0.0),
        bbox=bbox,
        thumb_path=r["thumb_path"],
        raw_path=r["raw_path"] if "raw_path" in r.keys() else None,
        hidden=int(r["hidden"] or 0),
        time_source=r["time_source"] if "time_source" in r.keys() else None,
    )


def wall_page(
    conn,
    cfg: Config,
    *,
    limit: int = 60,
    sort: str = SORT_CAPTURED_DESC,
    t_start: Optional[str] = None,
    t_end: Optional[str] = None,
    file_id: Optional[int] = None,
    min_quality: float = 0.3,
    only_new: bool = False,
    cursor: Optional[Cursor | str] = None,
    mode: str = "overview",
) -> Dict[str, Any]:
    """keyset 分页的照片墙。返回 {total, items, facets, next_cursor, has_more, mode}。

    mode: "overview"（默认，局部向量去重）| "tile"（全量平铺）
    """
    if isinstance(cursor, str):
        cursor = Cursor.decode(cursor)

    where, base_args = _build_where(t_start=t_start, t_end=t_end, file_id=file_id,
                                    min_quality=min_quality, only_new=only_new)
    args: List[Any] = list(base_args)
    time_expr = "COALESCE(fr.captured_at, f.captured_at, f.mtime)"

    # 概览模式：先用 sqlite 做「同文件同秒」粗去重（便宜的近似），再按 keyset 翻页。
    # 真正的向量去重由 overview_rows() 在拿到候选后做（Qdrant 不可用时也能出墙）。
    sql = [
        "SELECT fr.face_id, fr.file_id, fr.sec, fr.quality, fr.bbox, fr.thumb_path,",
        "       fr.raw_path, fr.hidden, fr.captured_at, f.path, f.kind, f.time_source,",
        f"       {time_expr} AS time_key",
        "  FROM face_records fr JOIN files f ON f.file_id = fr.file_id",
        " WHERE " + " AND ".join(where),
    ]

    if cursor and cursor.valid and sort in (SORT_CAPTURED_DESC, SORT_CAPTURED_ASC):
        if sort == SORT_CAPTURED_DESC:
            sql.append(f"   AND ({time_expr} < ? OR ({time_expr} = ? AND fr.face_id < ?))")
        else:
            sql.append(f"   AND ({time_expr} > ? OR ({time_expr} = ? AND fr.face_id > ?))")
        args.extend([cursor.time_key, cursor.time_key, cursor.face_id])

    order = ORDER_SQL.get(sort, ORDER_SQL[SORT_CAPTURED_DESC])
    if sort == SORT_RANDOM:
        order = "RANDOM()"
    sql.append(f" ORDER BY {order}")
    sql.append(" LIMIT ?")
    # 多取 1 条探测是否有下一页（规格：LIMIT 61）
    fetch = max(limit + 1, int(cfg.get("ui.fetch_limit", 61)) if limit >= 60 else limit + 1)
    page_args = list(args) + [fetch]

    rows = list(conn.execute(" ".join(sql), page_args))
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [_row_to_item(r) for r in rows]

    if mode == "overview" and items:
        items = overview_filter(conn, cfg, items)

    next_cursor = None
    if items and sort in (SORT_CAPTURED_DESC, SORT_CAPTURED_ASC):
        last = items[-1]
        # keyset 游标的 time_key 必须取「数据库里的排序键原值」，不能用裁剪后的值
        next_cursor = Cursor(_row_time_key(rows[-1]) if rows else (last.captured_at or ""),
                             last.face_id).encode()

    total = D.face_wall_count(conn, min_quality=min_quality, t_start=t_start,
                              t_end=t_end, file_id=file_id, only_new=only_new)

    # facets 只吃 WHERE 条件（不含游标与 LIMIT）
    facets = _facets(conn, where, base_args)

    return {
        "total": total,
        "items": [i.as_dict() for i in items],
        "facets": facets,
        "next_cursor": next_cursor,
        "has_more": has_more,
        "mode": mode,
        "sort": sort,
        "limit": limit,
    }


def _row_time_key(r: Any) -> str:
    """取排序键在数据库中的原值，保证 keyset 游标与 ORDER BY 完全一致。"""
    for key in ("time_key", "captured_at", "f_mtime"):
        try:
            if key in r.keys() and r[key] is not None:
                return str(r[key])
        except (AttributeError, IndexError):
            continue
    return ""


def _facets(conn, where: List[str], args: List[Any]) -> Dict[str, Any]:
    """概览信息：文件数、时间范围、质量分布。"""
    base = " FROM face_records fr JOIN files f ON f.file_id = fr.file_id WHERE " + " AND ".join(where)
    out: Dict[str, Any] = {}
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT fr.file_id) AS files,"
            " MIN(COALESCE(fr.captured_at, f.captured_at, f.mtime)) AS t_min,"
            " MAX(COALESCE(fr.captured_at, f.captured_at, f.mtime)) AS t_max" + base, args
        ).fetchone()
        if row:
            out["files_count"] = int(row["files"] or 0)
            out["date_range"] = [row["t_min"], row["t_max"]]
    except Exception as exc:  # noqa: BLE001
        LOG.warning("facets 计算失败：%s", exc)
    return out


def overview_filter(conn, cfg: Config, items: Sequence[WallItem]) -> List[WallItem]:
    """概览模式（贪心局部向量去重）：顺序扫描，与已选代表脸 cosine>0.85 则跳过。

    O(n)，内存只存代表脸向量。向量优先从本地 embeddings 缓存读取；
    读不到时退回「同文件 + 时间窗内只留质量最高的一张」的等价近似，
    保证墙在任何情况下都能刷出来（规格 8.6：空库与降级要有明确行为）。
    """
    threshold = float(cfg.get("ui.overview_cosine", 0.85))
    vecs = _load_face_vectors(cfg, [i.face_id for i in items])
    if not vecs:
        return _overview_by_time_window(items)

    reps: List[WallItem] = []
    rep_vecs: List[np.ndarray] = []
    for it in items:
        v = vecs.get(it.face_id)
        if v is None:
            reps.append(it)
            continue
        dup = False
        for rv in rep_vecs:
            if float(v.dot(rv)) > threshold:
                dup = True
                break
        if not dup:
            reps.append(it)
            rep_vecs.append(v)
    return reps


def _overview_by_time_window(items: Sequence[WallItem]) -> List[WallItem]:
    """无向量时的降级去重：同一文件 + 同 3 秒窗内只保留质量最高的一张。"""
    best: Dict[Tuple[int, int], WallItem] = {}
    order: List[Tuple[int, int]] = []
    for it in items:
        bucket = (it.file_id, int((it.sec or 0.0) // 3))
        cur = best.get(bucket)
        if cur is None:
            best[bucket] = it
            order.append(bucket)
        elif it.quality > cur.quality:
            best[bucket] = it
    return [best[k] for k in order]


def _load_face_vectors(cfg: Config, face_ids: Sequence[int]) -> Dict[int, np.ndarray]:
    """从 Qdrant 批量取人脸向量（只用于概览去重，不参与检索排序）。

    规格 8.2：Qdrant 只在点 [找同款] 时才调用 —— 因此这里默认关闭，
    需要概览向量去重时由 UI 显式打开（config ui.overview_use_qdrant）。
    未开启或失败时返回 {}，退回时间窗去重。
    """
    if not cfg.get("ui.overview_use_qdrant", False):
        return {}
    try:
        from .vector_store import face_collection, open_store

        store = open_store(cfg)
        client = getattr(store, "client", None)
        if client is None:
            return {}
        recs = client.retrieve(collection_name=face_collection(cfg),
                               ids=[int(i) for i in face_ids], with_vectors=True)
        out: Dict[int, np.ndarray] = {}
        for r in recs:
            v = r.vector
            if isinstance(v, dict):
                v = next(iter(v.values()))
            arr = np.asarray(v, dtype="float32")
            n = float(np.linalg.norm(arr))
            out[int(r.id)] = arr / n if n else arr
        return out
    except Exception as exc:  # noqa: BLE001
        LOG.warning("读取人脸向量失败（退回时间窗去重）：%s", exc)
        return {}


def wall_facets(conn, cfg: Config) -> Dict[str, Any]:
    """筛选栏需要的下拉数据：来源文件 top-N、时间范围、质量分位。"""
    try:
        files = [
            {"file_id": int(r["file_id"]), "path": str(r["path"]),
             "n": int(r["n"]), "kind": str(r["kind"] or "")}
            for r in conn.execute("""
                SELECT fr.file_id, f.path, f.kind, COUNT(*) AS n
                  FROM face_records fr JOIN files f ON f.file_id = fr.file_id
                 WHERE fr.status='alive'
                 GROUP BY fr.file_id ORDER BY n DESC LIMIT 200
            """)
        ]
        row = conn.execute("""
            SELECT MIN(COALESCE(fr.captured_at, f.captured_at, f.mtime)) AS t_min,
                   MAX(COALESCE(fr.captured_at, f.captured_at, f.mtime)) AS t_max,
                   COUNT(*) AS n
              FROM face_records fr JOIN files f ON f.file_id = fr.file_id
             WHERE fr.status='alive'
        """).fetchone()
    except Exception as exc:  # noqa: BLE001 - 表不存在/只读库异常都退化为空
        LOG.warning("读取筛选栏数据失败（按空库处理）：%s", exc)
        return {"files": [], "date_range": [None, None], "faces_total": 0}
    return {
        "files": files,
        "date_range": [row["t_min"], row["t_max"]] if row else [None, None],
        "faces_total": int(row["n"] or 0) if row else 0,
    }
