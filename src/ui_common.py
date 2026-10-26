# -*- coding: utf-8 -*-
"""src/ui_common.py · 界面通用小工具（界面各页共享，避免 app.py 越来越厚）。

这里只放**无状态**或**全局唯一**的东西：
  * conn()      只读连接（界面绝不持有写连接，规格第十章）
  * flash()     一次性提示（写 session_state["toast"]，与 app.py 的 _show_toast 约定一致）
  * invalidate()清 cache_data（内容变更事件到达时调用）
  * fmt_eta()   把秒数说成人话
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import streamlit as st

from . import db as D
from .common import Config


def conn(cfg: Config):
    """界面一律用只读句柄。WAL 下只读连接不会挡住后台写入进程。"""
    return D.connect(cfg.db_path, cfg=cfg, readonly=True)


def flash(msg: str, level: str = "info") -> None:
    """记一条提示，下一次 rerun 时由 app.py::_show_toast 显示。"""
    st.session_state["toast"] = {"msg": msg, "level": level}


def take_toast() -> Optional[Dict[str, Any]]:
    t = st.session_state.get("toast")
    if t:
        st.session_state["toast"] = None
    return t


def invalidate() -> None:
    """内容变了：把所有 cache_data 清掉（下次访问重查）。

    规格 §3.4 的缓存失效规则就落在这里 —— 宁可多查一次，也不给用户看旧数字。
    """
    try:
        st.cache_data.clear()
    except Exception:  # noqa: BLE001
        pass


def fmt_eta(sec: Optional[float]) -> str:
    if sec is None:
        return "估算中…"
    sec = max(0, int(sec))
    if sec < 60:
        return f"{sec} 秒"
    if sec < 3600:
        return f"{sec // 60} 分钟"
    if sec < 86400:
        return f"{sec // 3600} 小时 {sec % 3600 // 60} 分"
    return f"{sec // 86400} 天 {sec % 86400 // 3600} 小时"


def fmt_sec(sec: Optional[float]) -> str:
    if sec is None:
        return "—"
    sec = max(0.0, float(sec))
    if sec < 1:
        return f"{sec*1000:.0f} 毫秒"
    if sec < 60:
        return f"{sec:.1f} 秒"
    if sec < 3600:
        return f"{int(sec // 60)} 分 {int(sec % 60)} 秒"
    return f"{int(sec // 3600)} 时 {int(sec % 3600 // 60)} 分"


def fmt_rate(rate: Optional[float], unit: str = "个文件") -> str:
    if not rate:
        return "—"
    if rate >= 1:
        return f"{rate:.1f} {unit}/秒"
    return f"{rate*60:.1f} {unit}/分钟"


def open_folder(abs_path: str) -> str:
    """在资源管理器里定位文件。成功返回 ''，失败返回文案。

    坑（2026-10-19 修）：`Popen(["explorer", "/select,"+p], shell=True)` 会把路径
    拼进命令行，空格/中文/&() 一旦破坏解析，explorer 就退回默认目录（文档）。
    这里不用 shell，整条命令行自己拼，路径用双引号包住。
    """
    import os
    import subprocess
    import sys
    from pathlib import Path

    p_ = Path(abs_path)
    if not p_.exists():
        return f"文件不存在：{abs_path}"
    if os.name != "nt":
        try:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open",
                              str(p_.parent)])
            return ""
        except Exception as exc:  # noqa: BLE001
            return f"打开失败：{exc}"
    try:
        subprocess.Popen(f'explorer /select,"{p_}"', shell=False)
        return ""
    except Exception as exc:  # noqa: BLE001
        try:
            os.startfile(str(p_.parent))  # type: ignore[attr-defined]
            return ""
        except Exception as exc2:  # noqa: BLE001
            return f"打开失败：{exc} / {exc2}"
