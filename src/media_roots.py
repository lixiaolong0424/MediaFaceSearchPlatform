# -*- coding: utf-8 -*-
"""src/media_roots.py · 素材目录的增 / 删 / 改（v5.3 §4 扩展：多目录）。

设计取舍（记录在案）：
  * 目录**清单**写在 config.local.yaml 的 media_roots 里（人可读、可备份、可随项目拷走）；
    `media_root` 保留为「第一个启用的目录」，兼容所有还只认单根的旧代码。
  * `libraries` 表里存每个目录的扫描状态（last_scan / status / enabled），
    它不再等于「用户登记了哪些目录」，而是「这些目录各自扫到哪了」。
  * 删除有两种语义，必须让用户选：
      ① 只从列表移除（默认）：记录、向量、人脸全留，仍可搜索；只是以后不再扫它。
      ② 同时清理数据：删掉这个目录下的记录与向量（磁盘上的原文件绝不碰）。
  * 同一个文件被两个目录同时覆盖（一个根在另一个根里面）是禁止的：
    会重复入库、重复计人脸，且删哪个都说不清。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from . import db as D
from .common import CONFIG_LOCAL_FILENAME, Config, local_now_iso

KEY = "media_roots"


# --------------------------------------------------------------------------- #
# 规范化
# --------------------------------------------------------------------------- #


def normalize(path: str | Path) -> str:
    """统一成「绝对路径 + 正斜杠 + 无结尾斜杠」，用于比较与存盘。"""
    p = Path(str(path).strip().strip('"')).expanduser()
    try:
        p = p.resolve()
    except OSError:
        pass
    out = str(p).replace("\\", "/").rstrip("/")
    # 盘符根（E:）必须写成 E:/：否则 Path("E:") 会变成「当前目录在 E 盘上」
    if re.fullmatch(r"[A-Za-z]:", out):
        out += "/"
    return out or "/"


def prefix_of(path: str | Path) -> str:
    """数据库里用的前缀：Windows 风格、带结尾分隔符（path 列就是这种格式）。"""
    s = str(Path(str(path)))
    return s if s.endswith(("\\", "/")) else s + os.sep


def _same(a: str, b: str) -> bool:
    return normalize(a).lower() == normalize(b).lower()


def _nested(a: str, b: str) -> bool:
    """a 是否在 b 里面（含相等）。"""
    na, nb = normalize(a).lower(), normalize(b).lower()
    return na == nb or na.startswith(nb + "/")


# --------------------------------------------------------------------------- #
# 读写配置
# --------------------------------------------------------------------------- #


def _overlay_path(cfg: Config) -> Path:
    return Path(cfg.root) / CONFIG_LOCAL_FILENAME


def _read_overlay(cfg: Config) -> Dict[str, Any]:
    p = _overlay_path(cfg)
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}


def _entries(cfg: Config) -> List[Dict[str, Any]]:
    """当前登记的目录列表（来自 config.local.yaml；没有就退回 cfg.media_root）。"""
    data = _read_overlay(cfg)
    raw = data.get(KEY)
    out: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                out.append({"path": item, "enabled": True, "label": ""})
            elif isinstance(item, dict) and item.get("path"):
                out.append({
                    "path": str(item["path"]),
                    "enabled": bool(item.get("enabled", True)),
                    "label": str(item.get("label") or ""),
                })
    if not out:
        # 兼容：只有 media_root 的老配置
        out = [{"path": str(cfg.get("media_root") or "E:/"), "enabled": True, "label": ""}]
    return out


def _write(cfg: Config, entries: List[Dict[str, Any]]) -> None:
    data = _read_overlay(cfg)
    data[KEY] = [{"path": normalize(e["path"]), "enabled": bool(e.get("enabled", True)),
                  **({"label": e["label"]} if e.get("label") else {})}
                 for e in entries]
    enabled = [e["path"] for e in entries if e.get("enabled", True)] or \
              ([entries[0]["path"]] if entries else [])
    # 兼容字段：旧代码只认 media_root
    data["media_root"] = normalize(enabled[0]) if enabled else ""
    header = ("# 由界面「设置」自动生成，请勿手工编辑。\n"
              f"# 最后修改：{local_now_iso()}\n")
    p = _overlay_path(cfg)
    tmp = p.with_suffix(".yaml.tmp")
    tmp.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    tmp.replace(p)
    # 同步内存里的 cfg，界面下一轮就不用等重载
    try:
        cfg.override(KEY, data[KEY])
        cfg.override("media_root", data["media_root"])
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #


def validate(cfg: Config, path: str, *, exclude: Optional[str] = None) -> Tuple[bool, str]:
    """检查「加/改」这个目录是否合理。exclude=被修改的那一项（改路径时）。"""
    if not str(path or "").strip():
        return False, "请填写文件夹路径。"
    p = Path(str(path).strip().strip('"'))
    if not p.exists():
        return False, f"这个路径不存在：{p}"
    if not p.is_dir():
        return False, f"这不是一个文件夹：{p}"
    try:
        with os.scandir(p) as it:
            next(it, None)
    except OSError as exc:
        return False, f"这个文件夹读不了（权限或磁盘问题）：{exc}"
    for e in _entries(cfg):
        if exclude and _same(e["path"], exclude):
            continue
        if _same(e["path"], str(path)):
            return False, "这个文件夹已经在列表里了。"
        if _nested(str(path), e["path"]):
            return False, f"这个文件夹在已登记的 `{e['path']}` 里面，会造成重复入库。"
        if _nested(e["path"], str(path)):
            return False, f"已登记的 `{e['path']}` 在这个文件夹里面，会造成重复入库。"
    return True, ""


# --------------------------------------------------------------------------- #
# 增 / 删 / 改
# --------------------------------------------------------------------------- #


def add(cfg: Config, path: str, *, label: str = "", who: str = "界面") -> Dict[str, Any]:
    ok, msg = validate(cfg, path)
    if not ok:
        raise ValueError(msg)
    entries = _entries(cfg)
    entries.append({"path": normalize(path), "enabled": True, "label": label})
    _write(cfg, entries)
    _register(cfg, path, who=who, action="add")
    _emit(cfg)
    return {"added": normalize(path), "count": len(entries)}


def update(cfg: Config, old_path: str, *, new_path: Optional[str] = None,
           enabled: Optional[bool] = None, label: Optional[str] = None,
           who: str = "界面") -> Dict[str, Any]:
    """改路径 / 启用停用 / 改备注。改路径只改登记，**不动已有记录**
    （真要搬数据请走向导 `migrate.apply`，这里只处理「这一项指向哪」）。"""
    entries = _entries(cfg)
    idx = next((i for i, e in enumerate(entries) if _same(e["path"], old_path)), None)
    if idx is None:
        raise ValueError("要修改的文件夹不在列表里。")
    if new_path and not _same(new_path, old_path):
        ok, msg = validate(cfg, new_path, exclude=old_path)
        if not ok:
            raise ValueError(msg)
        entries[idx]["path"] = normalize(new_path)
    if enabled is not None:
        entries[idx]["enabled"] = bool(enabled)
    if label is not None:
        entries[idx]["label"] = label
    _write(cfg, entries)
    _register(cfg, entries[idx]["path"], who=who, action="update")
    if new_path and not _same(new_path, old_path):
        _unregister(cfg, old_path)
    _emit(cfg)
    return {"updated": entries[idx]}


def remove(cfg: Config, path: str, *, purge: bool = False,
           who: str = "界面") -> Dict[str, Any]:
    """从列表移除。purge=False（默认）只移除登记，数据一条不删。"""
    entries = _entries(cfg)
    keep = [e for e in entries if not _same(e["path"], path)]
    if len(keep) == len(entries):
        raise ValueError("要移除的文件夹不在列表里。")
    if not keep:
        raise ValueError("至少要保留一个素材文件夹。")
    result: Dict[str, Any] = {"removed": normalize(path)}
    if purge:
        result["purged"] = purge_root(cfg, path)
    _write(cfg, keep)
    if not purge:
        # 只移除登记：把 libraries 里的状态行也拿掉，避免统计里还显示它
        _unregister(cfg, path)
    _emit(cfg)
    return result


def purge_root(cfg: Config, path: str) -> int:
    """物理删除某目录下的**记录与向量**。磁盘上的原文件绝不碰（铁律 2）。"""
    from .migrate import _purge_rows

    pref = prefix_of(path)
    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        rows = conn.execute(
            "SELECT file_id, path, kind FROM files WHERE root=? OR path LIKE ?",
            (pref, pref + "%")).fetchall()
        gone = [(int(r["file_id"]), str(r["path"]), str(r["kind"])) for r in rows]
        n = _purge_rows(cfg, conn, gone) if gone else 0
        conn.execute("DELETE FROM path_index WHERE root=?", (pref,))
        conn.commit()
    finally:
        conn.close()
    _unregister(cfg, path)
    return n


def set_enabled(cfg: Config, path: str, enabled: bool) -> Dict[str, Any]:
    return update(cfg, path, enabled=enabled)


# --------------------------------------------------------------------------- #
# libraries 同步
# --------------------------------------------------------------------------- #


def _register(cfg: Config, path: str, *, who: str, action: str) -> None:
    pref = prefix_of(path)
    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        conn.execute(
            "INSERT INTO libraries(root, enabled, added_at, status) VALUES (?,1,?,'idle') "
            "ON CONFLICT(root) DO UPDATE SET enabled=1",
            (pref, local_now_iso()))
        D.audit(conn, who, f"media_root {action}", None, {"path": pref})
        conn.commit()
    finally:
        conn.close()


def _unregister(cfg: Config, path: str) -> None:
    pref = prefix_of(path)
    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        conn.execute("DELETE FROM libraries WHERE root=?", (pref,))
        conn.commit()
    finally:
        conn.close()


def _emit(cfg: Config) -> None:
    from . import tasks as T

    T.init(cfg)
    T.emit(cfg, "media_root_changed", {"roots": [e["path"] for e in _entries(cfg)]})


# --------------------------------------------------------------------------- #
# 查询
# --------------------------------------------------------------------------- #


def enabled_roots(cfg: Config) -> List[str]:
    return [e["path"] for e in _entries(cfg) if e.get("enabled", True)]


def list_roots(cfg: Config) -> List[Dict[str, Any]]:
    """给界面用的合并视图：登记信息 + 扫描状态 + 每个根的文件计数。"""
    entries = _entries(cfg)
    stats: Dict[str, Dict[str, Any]] = {}
    try:
        conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
        try:
            for r in conn.execute(
                    "SELECT root, COUNT(*) n FROM files GROUP BY root"):
                stats[str(r["root"] or "")] = {"files": int(r["n"])}
            for r in conn.execute(
                    "SELECT root, COUNT(*) n FROM files WHERE status='done' GROUP BY root"):
                stats.setdefault(str(r["root"] or ""), {})["done"] = int(r["n"])
            libs = {
                str(r["root"]): dict(r) for r in conn.execute(
                    "SELECT root, last_scan, status, enabled FROM libraries")
            }
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        stats, libs = {}, {}
    out: List[Dict[str, Any]] = []
    for e in entries:
        pref = prefix_of(e["path"])
        st = stats.get(pref, {})
        lib = libs.get(pref, {})
        out.append({
            "path": e["path"],
            "enabled": bool(e.get("enabled", True)),
            "label": e.get("label") or "",
            "files": int(st.get("files") or 0),
            "done": int(st.get("done") or 0),
            "last_scan": lib.get("last_scan"),
            "status": lib.get("status") or ("未扫描" if not lib else "idle"),
            "exists": Path(str(e["path"])).exists(),
        })
    return out


def total_files(cfg: Config) -> int:
    return sum(r["files"] for r in list_roots(cfg))
