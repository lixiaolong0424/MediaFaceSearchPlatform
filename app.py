"""app.py · Streamlit 双 Tab 主界面（规格第八章 UI 契约）。

布局：
  侧边栏：库路径[切换] / 增量扫描 / 全量扫描 / 状态数字 / NVMe 用量
  Tab1【照片墙】browse      Tab2【检索】ask
  筛选栏 + 网格 + 分页       输入三态 + 精确找人 + 阈值 + 返回上限
                            命中清单：关键帧+时间码+badge+操作栏
  Banner 常驻：模式 · 人脸库 N 张 · NVMe 已用 X/Y

互跳靠 URL 参数（8.4）：
  墙上点 [找同款] → st.query_params["face_id"]=12847 → 自动切 Tab2 跑人脸检索
  结果里点头像   → st.query_params["highlight_face_id"]=12847 → 切回 Tab1 高亮

硬规则（7.1）：
  1. 文字输入时「精确找人」开关必须物理隐藏，不能只是灰色。
  2. 未检出人脸时明确提示并真的降级，不能报错卡死也不能静默切换。
  3. 两种模式的分数绝不混排：语义标 sim=，人脸标 face=，不同颜色 badge。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# 保证以 `streamlit run app.py` 启动时也能 import src
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import streamlit as st  # noqa: E402

from src import db as D  # noqa: E402
from src.common import (  # noqa: E402
    CONFIG_ENV, CONFIG_FILENAME, CONFIG_LOCAL_FILENAME, Config, get_logger, human_bytes,
    load_config, new_session_id, project_root,
)
from src import export as EX  # noqa: E402
from src import ui_wall  # noqa: E402
from src import ui_text as U  # noqa: E402
from src import ui_pages as _pages  # noqa: E402
from src.ui_actions import (  # noqa: E402
    active_task, live_task_fragment, start_task, watch_events,
)
from src import ui_actions as _lines  # noqa: E402

LOG = get_logger("app")

st.set_page_config(page_title="素材人脸检索系统", page_icon="🔍", layout="wide")

# --------------------------------------------------------------------------- #
# 跨平台打开所在文件夹（规格 9.1，Windows 版）
# --------------------------------------------------------------------------- #


def open_containing_folder(abs_path: str) -> str:
    """成功返回 ''，失败返回错误文案。

    explorer /select, 逗号后不能有空格；路径含 & , () 时必须整体双引号；
    整体 shell=True 兼容中文与特殊字符。绝不用 f-string 拼命令行给 shell。
    """
    p = Path(abs_path)
    if not p.exists():
        return f"文件不存在：{abs_path}"
    try:
        subprocess.Popen(["explorer", "/select," + str(p)], shell=True)
        return ""
    except Exception as e:  # noqa: BLE001
        return f"打开文件夹失败：{e}"


def open_with_player(abs_path: str) -> str:
    """用本地默认播放器打开（os.startfile 对中文路径安全）。"""
    p = Path(abs_path)
    if not p.exists():
        return f"文件不存在：{abs_path}"
    try:
        if os.name == "nt":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return ""
    except Exception as e:  # noqa: BLE001
        return f"打开失败：{e}"


# --------------------------------------------------------------------------- #
# 会话状态 / 资源
# --------------------------------------------------------------------------- #


@st.cache_resource(show_spinner=False)
def _cfg() -> Config:
    """加载配置。

    生产：读项目根目录的 config.yaml。
    测试/多环境：用 MMS_CONFIG 指向基座、MMS_CONFIG_OVERLAY 指向叠加文件
    （例如 tests/config_smoke.yaml 只覆盖路径，其余阈值继承 config.yaml）。
    """
    base = os.environ.get(CONFIG_ENV)
    overlay = os.environ.get("MMS_CONFIG_OVERLAY")
    if overlay:
        # 显式给出基座，确保叠加文件里的相对键（media_root 等）在正确根下展开
        return load_config(base or (project_root() / CONFIG_FILENAME),
                           overlay=[overlay])
    if base:
        return load_config(base)
    return load_config()


@st.cache_resource(show_spinner=False)
def _static_server():
    from src.static_serve import get_server

    cfg = _cfg()
    srv = get_server(cfg)
    ok, msg = srv.start()
    if not ok:
        LOG.warning("静态服务未启动：%s", msg)
    return srv, ok, msg


@st.cache_resource(show_spinner=False)
def _ensure_schema_once(db_path_str: str) -> str:
    """空库/未迁移时也要能打开界面：启动时幂等建表。

    WAL 允许多进程并发，UI 建表不会与扫描冲突；已是最新版本时是一次只读查询。
    """
    try:
        from src import db as _db

        cfg = _cfg()
        v = _db.ensure_schema(cfg.db_path, cfg.root, cfg)
        return f"schema v{v}"
    except Exception as exc:  # noqa: BLE001 - 建表失败不应让界面白屏
        LOG.warning("初始化 schema 失败：%s", exc)
        return f"schema init failed: {exc}"


def _conn(cfg: Config):
    """UI 用独立只读句柄（规格第十章：扫描与 Web 不用同一 db 句柄）。"""
    return D.connect(cfg.db_path, cfg=cfg, readonly=True)


# --------------------------------------------------------------------------- #
# 查询缓存 —— 「屏幕变白、点不动」的主要成因就是这些查询每次都重跑
# --------------------------------------------------------------------------- #
# 实测（51 万文件 / 29 万脸，2026-10-19）：
#   library_stats  4880 ms → 合并成一条 GROUP BY + migration 004 的复合索引后 359 ms
#   wall_page      2540 ms
#   wall_facets    1995 ms
#   dir_size_bytes 47 000 ms（已改成后台线程，见 _start_nvme_walk）
# 单次交互把上面几个相加 ≈ 5~9 秒的整页置灰，这就是「变白不能点」。
# 现在：① 查询本身瘦身；② 结果按「数据版本」缓存，重复交互 0 成本。


def _data_version(cfg: Config) -> int:
    """数据版本号 = 最近一条变更事件 id。

    任何写库动作（扫描/抽帧/编码/人脸/分组/设置/路径变更）都会写一条事件，
    所以把它放进缓存键，数据一变缓存自动失效，不用手动清、也不会显示旧数字。
    """
    try:
        from src import tasks as _T

        _T.init(cfg)
        return int(_T.latest_event_id(cfg))
    except Exception:  # noqa: BLE001 - 事件表不可用也不该让界面打不开
        return 0


@st.cache_data(ttl=30, show_spinner=False)
def _stats_cached(db_path: str, media_root: str, version: int) -> Dict[str, Any]:
    cfg = _cfg()
    conn = _conn(cfg)
    try:
        return D.library_stats(conn, media_root)
    finally:
        conn.close()


@st.cache_data(ttl=180, show_spinner=False)
def _facets_cached(db_path: str, version: int) -> Dict[str, Any]:
    cfg = _cfg()
    conn = _conn(cfg)
    try:
        return ui_wall.wall_facets(conn, cfg)
    finally:
        conn.close()


@st.cache_data(ttl=180, show_spinner=False)
def _wall_page_cached(db_path: str, version: int, limit: int, sort: str,
                      t_start: Optional[str], t_end: Optional[str],
                      file_id: Optional[int], min_quality: float, only_new: bool,
                      cursor: Optional[str], mode: str) -> Dict[str, Any]:
    cfg = _cfg()
    conn = _conn(cfg)
    try:
        return ui_wall.wall_page(
            conn, cfg, limit=limit, sort=sort, t_start=t_start, t_end=t_end,
            file_id=file_id, min_quality=min_quality, only_new=only_new,
            cursor=cursor, mode=mode,
        )
    finally:
        conn.close()


def _config_fingerprint(cfg: Config) -> tuple:
    try:
        root = Path(str(cfg.root))
        a = root / CONFIG_FILENAME
        b = root / CONFIG_LOCAL_FILENAME
        return (a.stat().st_mtime_ns if a.exists() else 0,
                b.stat().st_mtime_ns if b.exists() else 0)
    except OSError:
        return (0, 0)


def _check_config_fresh(cfg: Config) -> None:
    """配置文件被改过（界面向导、手工编辑、别的脚本）就立刻重载。

    为什么必须有：`Config` 是 `@st.cache_resource`，进程启动后就不再重读。
    2026-10-19 就是因为界面一直拿着「media_root=F:」的旧配置，
    用户点一次扫描就把整库标成了缺失 —— 配置不新鲜会直接变成数据事故。
    """
    fp = _config_fingerprint(cfg)
    old = st.session_state.get("_cfg_fp")
    if old is None:
        st.session_state["_cfg_fp"] = fp
        return
    if old != fp:
        st.session_state["_cfg_fp"] = fp
        try:
            _cfg.clear()
        except Exception:  # noqa: BLE001
            pass
        st.cache_data.clear()
        st.rerun()


def _ensure_state(cfg: Config) -> None:
    ss = st.session_state
    ss.setdefault("session_id", new_session_id())
    ss.setdefault("page", "wall")           # v5.3：六页路由，一次只渲染一页
    ss.setdefault("media_root", str(cfg.media_root))
    ss.setdefault("wall_mode", "overview")
    # 默认质量下限取 config（faces.quality_min_show）。
    # 原实现写死 0.3，而 quality 的清晰度项用 lap_var/500 归一化，
    # 在真实 384px 关键帧上中位数只有 0.22 —— 于是默认就把 2/3 的人脸挡在墙外。
    # 归一化尺度已修（faces.quality_lap_norm），默认值也改为与 config 一致。
    ss.setdefault("min_quality", float(cfg.get("faces.quality_min_show", 0.3)))
    ss.setdefault("sort", ui_wall.SORT_CAPTURED_DESC)
    ss.setdefault("cursors", [])          # keyset 游标栈
    ss.setdefault("page_ix", 0)           # 照片墙第几页（与上面的路由页区分开）
    ss.setdefault("t_start", None)
    ss.setdefault("t_end", None)
    ss.setdefault("file_id", None)
    ss.setdefault("only_new", False)
    ss.setdefault("outcome", None)        # 最近一次检索的 SearchOutcome(dict)
    ss.setdefault("query_mode", "text")
    ss.setdefault("exact_person", False)
    # 识别灵敏度的默认值取 config，不再写死 0.55（0.55 在实测分数区间 0.31~0.50 里
    # 永远不可能命中，曾导致「所有文字搜索都是 0 结果」）
    ss.setdefault("thresh", float(cfg.get("retrieval.min_score", 0.30)))
    ss.setdefault("rerank_input_k", int(cfg.get("retrieval.rerank_input_k", 60)))
    ss.setdefault("topk", 200)
    ss.setdefault("toast", None)
    ss.setdefault("picked_faces", [])
    ss.setdefault("highlight_face_id", None)
    ss.setdefault("_live_state", None)
    ss.setdefault("_event_cursor", None)


def _flash(msg: str, level: str = "info") -> None:
    st.session_state["toast"] = {"msg": msg, "level": level}


def _show_toast() -> None:
    t = st.session_state.get("toast")
    if not t:
        return
    st.session_state["toast"] = None
    fn = {"info": st.info, "success": st.success, "warning": st.warning,
          "error": st.error}.get(t["level"], st.info)
    fn(t["msg"])


# --------------------------------------------------------------------------- #
# NVMe 预算与告警（规格第三章）
# --------------------------------------------------------------------------- #


def _nvme_used_bytes(nvme_root: str) -> int:
    """统计 data/nvme 的占用字节数（**只在后台线程里调用**，不在页面渲染路径上）。

    ⚠️ 2026-10-16 修（「打开界面长时间白屏」的根因）：原来用 `Path.rglob("*")`
    逐层开目录再逐项 stat；`frames/` 是「一张照片一个目录」的布局，5 万多个目录、
    14 万个文件，实测 47~51 秒。
    ⚠️ 2026-10-19 再修：换成 scandir 仍要 47 秒，而它原来挂在 `@st.cache_data(ttl=120)`
    上 —— 也就是说**每 2 分钟就会有一次约 50 秒的整页置灰**，用户感受是
    「屏幕突然变白、什么都点不动」。现在改成：值存库、后台线程算、界面只读库，
    渲染路径上**永远不遍历目录**。
    """
    total = 0
    root = Path(nvme_root)
    if not root.exists():
        return 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat().st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


_NVME_KEY = "nvme_usage"
_NVME_PENDING_KEY = "nvme_pending_at"          # app_settings 里的键：{"bytes": int, "at": iso}
_NVME_WALKING = {"on": False}     # 后台线程去重（同一进程只跑一个）
_NVME_STALE_SEC = 6 * 3600        # 超过 6 小时就在后台重算一次


def _read_nvme_usage(db_path: str) -> Dict[str, Any]:
    import json
    import sqlite3

    try:
        con = sqlite3.connect(db_path, timeout=3.0)
        try:
            row = con.execute("SELECT value FROM app_settings WHERE key=?",
                              (_NVME_KEY,)).fetchone()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return {}
    if not row or not row[0]:
        return {}
    try:
        return json.loads(row[0])
    except Exception:  # noqa: BLE001
        return {}


def _read_setting_raw(db_path: str, key: str) -> str:
    import sqlite3

    try:
        con = sqlite3.connect(db_path, timeout=3.0)
        try:
            row = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        finally:
            con.close()
        return str(row[0]) if row and row[0] else ""
    except Exception:  # noqa: BLE001
        return ""


def _write_nvme_usage(db_path: str, used: int) -> None:
    import json
    import sqlite3
    from datetime import datetime

    try:
        con = sqlite3.connect(db_path, timeout=10.0)
        try:
            con.execute(
                "INSERT INTO app_settings(key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (_NVME_KEY, json.dumps({"bytes": int(used), "at": datetime.now().isoformat(
                    timespec="seconds")}), datetime.now().isoformat(timespec="seconds")),
            )
            con.execute("DELETE FROM app_settings WHERE key=?", (_NVME_PENDING_KEY,))
            con.commit()
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        LOG.warning("写入中间文件占用失败：%s", exc)


def _mark_nvme_pending(db_path: str) -> None:
    import sqlite3
    from datetime import datetime

    try:
        con = sqlite3.connect(db_path, timeout=5.0)
        try:
            con.execute(
                "INSERT INTO app_settings(key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (_NVME_PENDING_KEY, datetime.now().isoformat(timespec="seconds"),
                 datetime.now().isoformat(timespec="seconds")))
            con.commit()
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        pass


def _start_nvme_walk(db_path: str, nvme_root: str) -> None:
    """后台线程统计占用。**绝不在渲染路径上做**，界面先显示上次的值。"""
    if _NVME_WALKING["on"]:
        return
    _NVME_WALKING["on"] = True
    _mark_nvme_pending(db_path)

    def work() -> None:
        try:
            used = _nvme_used_bytes(nvme_root)
            _write_nvme_usage(db_path, used)
            LOG.info("中间文件占用统计完成：%s", human_bytes(used))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("统计中间文件占用失败：%s", exc)
        finally:
            _NVME_WALKING["on"] = False

    import threading

    threading.Thread(target=work, name="nvme-walk", daemon=True).start()


def _nvme_usage(cfg: Config, *, stale_sec: int = _NVME_STALE_SEC,
                start_walk: bool = True) -> Dict[str, Any]:
    """读库里的占用值，必要时在后台触发一次重算。**渲染路径上不做 IO 遍历。**"""
    from datetime import datetime

    root = cfg.path("paths.nvme_root")
    budget_gb = float(cfg.get("storage.nvme_budget_gb", 300))
    warn_at = float(cfg.get("storage.budget_warn_ratio", 0.80))
    data = _read_nvme_usage(str(cfg.db_path))
    used = int(data.get("bytes") or 0)
    at = str(data.get("at") or "")
    age = None
    if at:
        try:
            age = (datetime.now() - datetime.fromisoformat(at)).total_seconds()
        except ValueError:
            age = None
    stale = age is None or age > stale_sec
    if stale and start_walk:
        _start_nvme_walk(str(cfg.db_path), str(root))
    ratio = used / (budget_gb * 1024 ** 3) if budget_gb else 0.0
    free_gb = 0.0
    try:
        import shutil as _sh

        free_gb = _sh.disk_usage(str(root.anchor or root)).free / 1024 ** 3
    except OSError:
        pass
    pending_raw = _read_setting_raw(str(cfg.db_path), _NVME_PENDING_KEY)
    pending = bool(pending_raw) and (not at or str(pending_raw) >= str(at))
    return {
        "used": used, "budget_gb": budget_gb, "ratio": ratio,
        "warn": ratio >= warn_at, "alert": ratio >= warn_at,
        "free_gb": free_gb, "measured_at": at, "known": bool(at), "stale": stale,
        "pending": pending,
    }


# --------------------------------------------------------------------------- #
# 卡片渲染
# --------------------------------------------------------------------------- #


def _badge_html(badge: str, score: float) -> str:
    if badge == "face":
        color, label = "#d9480f", f"face={score:.2f}"
    else:
        color, label = "#1971c2", f"sim={score:.2f}"
    return (f"<span style='background:{color};color:#fff;padding:1px 6px;"
            f"border-radius:9px;font-size:11px'>{label}</span>")


def _render_face_card(cfg: Config, srv, item: Dict[str, Any], *, idx: int,
                      highlight: Optional[int] = None) -> None:
    face_id = int(item["face_id"])
    thumb = item.get("thumb_path")
    url = srv.thumb_url(thumb) if thumb else None
    border = "2px solid #f59f00" if highlight and face_id == highlight else "1px solid #2b2b2b"

    with st.container(border=True):
        if url:
            st.markdown(
                f"<div style='border:{border};border-radius:8px;overflow:hidden'>"
                f"<img src='{url}' style='width:100%;display:block'/></div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                "<div style='height:112px;display:flex;align-items:center;"
                "justify-content:center;background:#1b1b1b;border-radius:8px;"
                "color:#888;font-size:12px'>无缩略图</div>",
                unsafe_allow_html=True,
            )
        st.caption(f"#{face_id} · {item.get('timecode') or ''}")

        with st.expander("ℹ️ 详情", expanded=False):
            st.markdown(
                f"**完整路径**  \n`{item.get('path')}`  \n"
                f"**quality** {float(item.get('quality') or 0):.3f}  \n"
                f"**bbox** `{item.get('bbox')}`  \n"
                f"**kind** {item.get('kind')}  \n"
                f"**time_source** {item.get('time_source')}"
            )

        c1, c2, c3 = st.columns(3)
        key = f"w{idx}_{face_id}"
        with c1:
            if st.button("▶ 预览", key=f"{key}_pv", use_container_width=True):
                st.session_state[f"preview_{key}"] = not st.session_state.get(f"preview_{key}", False)
        with c2:
            if st.button("找同款", key=f"{key}_sim", use_container_width=True):
                st.query_params["face_id"] = str(face_id)
                st.session_state["tab"] = "ask"
                st.session_state["pending_face_id"] = face_id
                st.rerun()
        with c3:
            if st.button("📂", key=f"{key}_of", use_container_width=True, help="打开所在文件夹"):
                err = open_containing_folder(str(item.get("path") or ""))
                _flash(err or f"已在资源管理器中定位：{item.get('path')}",
                       "error" if err else "success")
                st.rerun()

        if st.session_state.get(f"preview_{key}"):
            _render_preview(cfg, srv, item.get("path"), item.get("kind"), item.get("sec"))


def _render_preview(cfg: Config, srv, path: Optional[str], kind: Optional[str],
                    sec: Optional[float]) -> None:
    if not path:
        st.warning("该记录没有关联文件路径")
        return
    p = Path(path)
    if not p.exists():
        st.error(f"文件已不存在：{path}")
        return
    if kind == "video":
        url = srv.media_url(p, start=(sec or 0.0), end=((sec or 0.0) + 6.0))
        if url:
            st.markdown(
                f"<video controls preload='metadata' style='width:100%;max-height:420px' "
                f"src='{url}'></video>",
                unsafe_allow_html=True,
            )
            st.caption(f"从 {sec:.2f}s 开始播放该镜头片段（+6s）")
        else:
            st.warning("静态服务未启动或路径不在白名单内，无法内嵌预览。")
        if st.button("用本地播放器打开", key=f"pl_{abs(hash(path))}", use_container_width=True):
            err = open_with_player(path)
            _flash(err or "已用本地播放器打开", "error" if err else "success")
    else:
        st.image(path, width=int(cfg.get("ui.preview_max_px", 1280)))
    if st.button("📂 打开所在文件夹", key=f"pf_{abs(hash(path))}", use_container_width=True):
        err = open_containing_folder(path)
        _flash(err or "已定位", "error" if err else "success")


def _render_hit_card(cfg: Config, srv, fh: Dict[str, Any], *, idx: int) -> None:
    kind = fh.get("kind")
    path = fh.get("path")
    badge = fh.get("badge", "sim")
    best = float(fh.get("best_score") or 0)
    n_shots = int(fh.get("n_shots") or 0)
    segs = fh.get("segments") or []

    with st.container(border=True):
        head = st.columns([3, 2, 2])
        with head[0]:
            st.markdown(f"**`{Path(path).name if path else '未知文件'}`**")
            st.caption(f"{path}")
        with head[1]:
            st.markdown(_badge_html(badge, best), unsafe_allow_html=True)
            st.caption(f"命中 {n_shots} 个镜头 / {len(segs)} 个时间段")
        with head[2]:
            st.caption(f"拍摄时间 {fh.get('captured_at') or '未知'}")

        # 命中镜头代表帧
        thumbs = [s for s in (fh.get("shots") or []) if s.get("thumb_path")][:4]
        if thumbs:
            cols = st.columns(len(thumbs))
            for c, s in zip(cols, thumbs):
                u = srv.frame_url(s["thumb_path"])
                with c:
                    if u:
                        st.markdown(
                            f"<img src='{u}' style='width:100%;border-radius:6px'/>",
                            unsafe_allow_html=True,
                        )
                    tc = _tc(s.get("sec"))
                    st.caption(f"{tc} · {badge}={float(s.get('score') or 0):.2f}"
                               + (f" · 该镜头内出现 {s['faces_in_shot']} 次"
                                  if int(s.get("faces_in_shot") or 1) > 1 else ""))

        # 时间段列表 + 一键片段预览
        if segs:
            st.markdown("**命中时间段**")
            for seg in segs[:6]:
                a, b = _tc(seg.get("start")), _tc(seg.get("end"))
                st.markdown(f"- `{a}` → `{b}` （{seg.get('n')} 个镜头，最高 {badge}="
                            f"{float(seg.get('best_score') or 0):.2f}）")

        act = st.columns([1, 1, 1, 3])
        with act[0]:
            if st.button("▶ 预览", key=f"h{idx}_pv", use_container_width=True):
                st.session_state[f"hprev_{idx}"] = not st.session_state.get(f"hprev_{idx}", False)
        with act[1]:
            fid = (fh.get("shots") or [{}])[0].get("face_id")
            if st.button("找同款", key=f"h{idx}_sim", use_container_width=True,
                         disabled=fid is None):
                if fid is not None:
                    st.query_params["face_id"] = str(int(fid))
                    st.session_state["tab"] = "ask"
                    st.session_state["pending_face_id"] = int(fid)
                    st.rerun()
        with act[2]:
            if st.button("📂", key=f"h{idx}_of", use_container_width=True, help="打开所在文件夹"):
                err = open_containing_folder(str(path or ""))
                _flash(err or "已定位", "error" if err else "success")
                st.rerun()

        if st.session_state.get(f"hprev_{idx}"):
            start = (segs[0]["start"] if segs else 0.0)
            end = (segs[0]["end"] if segs else None)
            _render_preview(cfg, srv, path, kind, start)


def _tc(sec: Optional[float]) -> str:
    if sec is None:
        return "--:--:--"
    total = int(float(sec))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# --------------------------------------------------------------------------- #
# Tab1 照片墙
# --------------------------------------------------------------------------- #


def tab_wall(cfg: Config, srv, stats: Dict[str, Any]) -> None:
    ss = st.session_state
    st.markdown(f"## {U.t('wall.title')}")
    if stats["faces"] == 0:
        _pending = int(stats.get("faces_pending_vector") or 0)
        busy = active_task(cfg) is not None
        if _pending:
            st.warning(
                f"**照片墙还是空的：已经检出 {_pending:,} 张人脸，但还没转成可比较的数据。**\n\n"
                f"点下面的按钮把它跑完（{U.t('act.face_encode.help')}）。"
            )
            if st.button(U.t("act.face_encode"), type="primary", disabled=busy):
                if start_task(cfg, "face_encode"):
                    st.rerun()
        else:
            st.warning(
                "**照片墙还是空的。** 最可能的原因是没有处理过：\n\n"
                "1. 点左侧「扫描新文件」，把素材先入库；\n"
                "2. 然后点「人脸检测与编码」，人脸才会出现在这里；\n"
                "3. 如果两条都跑过了，检查上面的「清晰度 ≥」是不是调太高、"
                "或者日期筛选把结果挡住了。"
            )

    with st.container():
        f1, f2, f3, f4 = st.columns([1.2, 1.2, 1, 1])
        with f1:
            mode = st.radio("视图", ["overview", "tile"], horizontal=True,
                            format_func=lambda v: "概览（去重）" if v == "overview" else "平铺（全量）",
                            index=0 if ss["wall_mode"] == "overview" else 1,
                            key="wall_mode_radio")
            if mode != ss["wall_mode"]:
                ss["wall_mode"] = mode
                ss["cursors"] = []
                ss["page_ix"] = 0
        with f2:
            sort = st.selectbox(
                "排序", [ui_wall.SORT_CAPTURED_DESC, ui_wall.SORT_CAPTURED_ASC,
                         ui_wall.SORT_QUALITY_DESC, ui_wall.SORT_RANDOM],
                format_func=lambda v: {
                    ui_wall.SORT_CAPTURED_DESC: "拍摄时间 ↓",
                    ui_wall.SORT_CAPTURED_ASC: "拍摄时间 ↑",
                    ui_wall.SORT_QUALITY_DESC: "质量 ↓",
                    ui_wall.SORT_RANDOM: "随机",
                }[v], key="sort_sel")
            if sort != ss["sort"]:
                ss["sort"] = sort
                ss["cursors"] = []
                ss["page_ix"] = 0
        with f3:
            q = st.slider("质量 ≥", 0.0, 1.0, float(ss["min_quality"]), 0.05, key="q_slider")
            if abs(q - ss["min_quality"]) > 1e-9:
                ss["min_quality"] = q
                ss["cursors"] = []
                ss["page_ix"] = 0
        with f4:
            only_new = st.checkbox("仅看本次新增", value=bool(ss["only_new"]), key="only_new_cb")
            ss["only_new"] = only_new

        g1, g2, g3 = st.columns([1, 1, 2])
        with g1:
            d1 = st.text_input("起始日期", value=ss["t_start"] or "", placeholder="2024-01-01", key="t1")
        with g2:
            d2 = st.text_input("结束日期", value=ss["t_end"] or "", placeholder="2024-12-31", key="t2")
        with g3:
            fac = _facets_cached(str(cfg.db_path), _data_version(cfg))
            opts = [None] + [f["file_id"] for f in fac["files"]]
            labels = {None: "全部来源文件"} | {
                f["file_id"]: f"{Path(f['path']).name} ({f['n']})" for f in fac["files"]}
            sel = st.selectbox("来源文件", opts, format_func=lambda v: labels.get(v, str(v)),
                               key="file_sel")
        if d1 != (ss["t_start"] or "") or d2 != (ss["t_end"] or "") or sel != ss["file_id"]:
            ss["t_start"] = d1 or None
            ss["t_end"] = d2 or None
            ss["file_id"] = sel
            ss["cursors"] = []
            ss["page_ix"] = 0

    page_ix = int(ss["page_ix"])
    cursors: List[str] = ss["cursors"]
    cursor = cursors[page_ix - 1] if page_ix > 0 and page_ix - 1 < len(cursors) else None

    page_size = int(cfg.get("ui.page_size", 60))
    t0 = time.monotonic()
    page = _wall_page_cached(
        str(cfg.db_path), _data_version(cfg), page_size, ss["sort"],
        ss["t_start"], ss["t_end"], ss["file_id"],
        float(ss["min_quality"]), bool(ss["only_new"]),
        cursor, ss["wall_mode"],
    )
    elapsed_ms = (time.monotonic() - t0) * 1000.0

    items = page["items"]
    if page.get("next_cursor"):
        need = page_ix
        if len(cursors) <= need:
            cursors.append(page["next_cursor"])
        else:
            cursors[need] = page["next_cursor"]

    st.caption(
        f"共 {page['total']:,} 张人脸 · 本页 {len(items)} 张 · "
        f"文件 {page['facets'].get('files_count', 0):,} 个 · "
        f"查询 {elapsed_ms:.0f}ms · 视图={ss['wall_mode']}"
    )
    if st.session_state.get("highlight_face_id"):
        st.info(f"已从检索结果跳转，高亮 face_id={st.session_state['highlight_face_id']}")

    if not items:
        st.info("当前筛选条件下没有可展示的人脸。")
        return

    ncols = 8
    hi = st.session_state.get("highlight_face_id")
    for start in range(0, len(items), ncols):
        cols = st.columns(ncols)
        for j, item in enumerate(items[start:start + ncols]):
            with cols[j]:
                _render_face_card(cfg, srv, item, idx=start + j, highlight=hi)

    nav = st.columns([1, 1, 2, 1, 1, 1])
    with nav[0]:
        if st.button("⏮ 首页", disabled=page_ix == 0, use_container_width=True):
            ss["page_ix"] = 0
            st.rerun()
    with nav[1]:
        if st.button("◀ 上一页", disabled=page_ix == 0, use_container_width=True):
            ss["page_ix"] = max(0, page_ix - 1)
            st.rerun()
    with nav[2]:
        st.markdown(
            f"<div style='text-align:center;padding-top:6px'>第 <b>{page_ix + 1}</b> 页"
            f" · 本页 {len(items)} 张 · 共 {page['total']:,} 张"
            f"<br/><span style='font-size:11px;color:#888'>keyset 游标分页，一次 {page_size} 条"
            f"（禁用 offset 深层翻页）</span></div>", unsafe_allow_html=True)
    with nav[3]:
        if st.button("下一页 ▶", disabled=not page["has_more"], use_container_width=True):
            ss["page_ix"] = page_ix + 1
            st.rerun()
    with nav[4]:
        jump = st.number_input("跳页", min_value=1, value=page_ix + 1, step=1,
                               key="jump_page", label_visibility="collapsed")
    with nav[5]:
        if st.button("跳转", use_container_width=True) and int(jump) != page_ix + 1:
            ss["page_ix"] = max(0, int(jump) - 1)
            st.rerun()
    if page_ix > 0:
        st.caption("提示：keyset 分页只能顺序向后推进，跳页会沿游标链逐段推进（首次跳页可能较慢）。")

    # 导出当前照片墙筛选结果（入口 2/3）
    _export_bar(cfg, scope="wall_filtered", wall_items=items)


def _export_bar(cfg: Config, *, scope: str, wall_items: Optional[List[Dict]] = None,
                outcome: Optional[Dict] = None) -> None:
    c1, c2, c3 = st.columns([1, 1, 3])
    with c1:
        fmt = st.selectbox("导出格式", ["tsv", "txt"], key=f"fmt_{scope}",
                           format_func=lambda v: "TSV（Excel 用）" if v == "tsv" else "TXT（下游 agent）")
    with c2:
        relative = st.checkbox("相对路径", value=False, key=f"rel_{scope}")
    with c3:
        if st.button(f"⬇ 导出当前{('照片墙筛选结果' if scope == 'wall_filtered' else '命中清单')}",
                     key=f"exp_{scope}", use_container_width=True):
            out = EX.default_out_path(cfg, scope)
            if scope == "wall_filtered":
                rows = [
                    EX.ExportRow(
                        kind=str(i.get("kind") or ""), abs_path=str(i.get("path") or ""),
                        shot_start_ms=int(i["sec"] * 1000) if i.get("sec") is not None else None,
                        face_id=int(i["face_id"]), score=i.get("quality"),
                        thumb_path=i.get("thumb_path"),
                    ) for i in (wall_items or [])
                ]
            else:
                rows = _outcome_rows(outcome or {})
            if fmt == "tsv":
                res = EX.export_tsv(rows, scope, out, relative=relative, cfg=cfg)
                _flash(f"已导出 {res.written} 行，DEAD {len(res.dead)} 个 → {res.path}", "success")
            else:
                out = out.with_suffix(".txt")
                res = EX.export_structured_txt(rows, scope, out, cfg=cfg, relative=relative)
                _flash(f"已导出 {res.written} 行 TXT，DEAD {len(res.dead)} 个 → {res.path}", "success")
            st.rerun()


def _outcome_rows(outcome: Dict[str, Any]) -> List[EX.ExportRow]:
    rows: List[EX.ExportRow] = []
    for f in outcome.get("files") or []:
        shots = f.get("shots") or []
        for s in shots:
            rows.append(EX.ExportRow(
                kind=str(f.get("kind") or ""), abs_path=str(f.get("path") or ""),
                shot_start_ms=int(float(s["shot_start"]) * 1000) if s.get("shot_start") is not None else None,
                shot_end_ms=int(float(s["shot_end"]) * 1000) if s.get("shot_end") is not None else None,
                face_id=s.get("face_id"), score=s.get("score"),
                thumb_path=s.get("thumb_path"),
            ))
        if not shots:
            rows.append(EX.ExportRow(kind=str(f.get("kind") or ""),
                                     abs_path=str(f.get("path") or ""),
                                     score=f.get("best_score")))
    return rows


# --------------------------------------------------------------------------- #
# Tab2 检索
# --------------------------------------------------------------------------- #


def tab_ask(cfg: Config, srv, stats: Dict[str, Any]) -> None:
    ss = st.session_state
    st.markdown(f"## {U.t('search.title')}")
    st.caption("三种找法：用一句话描述画面、传一张图找相似的画面、传一张人脸找同一个人。")
    face_ready = stats["faces"] > 0

    # URL param 驱动的「找同款」自动检索
    pending = ss.pop("pending_face_id", None)
    if pending is None and "face_id" in st.query_params:
        try:
            pending = int(st.query_params["face_id"])
        except (TypeError, ValueError):
            pending = None

    st.markdown("#### 输入检索素材")
    mode = st.radio("输入类型", ["text", "image", "video"], horizontal=True,
                    format_func=lambda v: {"text": "文字描述", "image": "图片",
                                           "video": "视频"}[v],
                    index=["text", "image", "video"].index(ss["query_mode"]),
                    key="qm_radio")
    ss["query_mode"] = mode

    payload: Dict[str, Any] = {}
    text_val = ""
    uploaded = None
    if mode == "text":
        text_val = st.text_input("描述你要找的画面", value=st.session_state.get("q_text", ""),
                                 placeholder="例如：小孩在草地上跑", key="q_text")
        payload["text"] = text_val
        # 硬规则 1：文字输入时开关必须物理隐藏
        exact = False
        st.caption("文字检索不支持人脸匹配（该开关在文字模式下已隐藏）。")
    else:
        uploaded = st.file_uploader(
            "拖入图片或视频" if mode == "image" else "拖入视频",
            type=["jpg", "jpeg", "png", "webp"] if mode == "image" else ["mp4", "mov", "mkv", "avi", "m4v"],
            key=f"up_{mode}")
        exact = st.checkbox(
            "🎯 精确找人（人脸匹配）",
            value=bool(ss["exact_person"]) and face_ready,
            disabled=not face_ready,
            help="按人脸特征匹配，而不是画面内容。"
                 + ("" if face_ready else
                    "\n人脸库当前为空：先点左侧「人脸检测与编码」。"),
            key="exact_cb",
        )
        if not face_ready:
            st.warning("人脸库还是空的，所以「按人脸找」暂时不可用。"
                       "点左侧「人脸检测与编码」把这一步跑完就能用了。")
        ss["exact_person"] = exact

        if uploaded is not None:
            qdir = cfg.path("paths.query_cache") / ss["session_id"]
            qdir.mkdir(parents=True, exist_ok=True)
            qpath = qdir / getattr(uploaded, "name", f"query_{int(time.time())}")
            qpath.write_bytes(uploaded.getbuffer())
            payload["image" if mode == "image" else "video"] = qpath
            ss["query_path"] = str(qpath)

    c1, c2, c3 = st.columns(3)
    with c1:
        thresh = st.slider(U.t("search.sensitivity"), 0.0, 1.0, float(ss["thresh"]), 0.01,
                           key="th_slider",
                           help="相似度门槛的精确值。调低=多召回但可能混入不相关的画面；"
                                "调高=只留最像的。默认 0.30 是按本机实测标定的："
                                "这套模型的相似度整体挤在 0.31~0.50，填 0.55 会把结果全砍空。")
        ss["thresh"] = thresh
    with c2:
        topk = st.number_input("最多返回几条", 10, 2000, int(ss["topk"]), 10, key="k_num")
        ss["topk"] = int(topk)
    with c3:
        st.write("")
        run = st.button("🔍 开始检索", type="primary", use_container_width=True)

    # 重排序（L2d）：召回余弦在这个模型上区分度很低（实测连无关查询词都能拿 0.405），
    # 真正把「相关的排到前面」的是交叉编码重排序。代价是每条候选约 0.2 秒，
    # 所以这里让用户自己选重排多少条，并把预估耗时写在选项上。
    rr_col1, rr_col2 = st.columns([2, 1])
    with rr_col1:
        RERANK_CHOICES = [0, 20, 40, 60, 120, 400]
        rr_k = st.select_slider(
            "重排序候选数（越大越准、越慢）", options=RERANK_CHOICES,
            value=int(ss.get("rerank_input_k", 60)), key="rr_slider",
            format_func=lambda v: ("关闭（只按召回余弦排序）" if v == 0
                                   else f"前 {v} 条 ≈ {v * 0.2:.0f} 秒"),
            help="实测 0.2 秒/条（本机显卡，默认像素预算）。\n"
                 "降像素预算能快 2 倍，但实测 Top-10 只有 6~7/10 与全分辨率一致，"
                 "会真实改变排序结果，所以默认不动它。\n"
                 "选 0 = 完全关闭重排，只按初筛相似度排（最快）。")
        ss["rerank_input_k"] = int(rr_k)
    with rr_col2:
        st.caption(f"最终保留 Top-{int(cfg.get('retrieval.rerank_topk', 50))}"
                   if rr_k else "最终保留按截断规则")
        st.caption(f"模型：{'已加载' if ss.get('reranker_loaded') else '首次检索时加载'}"
                   f"（2B，约 4~6 秒）")

    # 找同款（face_id 驱动）
    if pending is not None:
        st.info(f"来自照片墙的「找同款」请求：face_id={pending}")
        if st.button("▶ 执行找同款", key="run_findsame"):
            run = True
            mode = "findsame"

    if not run:
        _render_last_outcome(cfg, srv)
        return

    if not face_ready and (ss.get("exact_person") or pending is not None):
        st.error("人脸库还是空的，没法按人脸检索。先点左侧「人脸检测与编码」把它跑完。")
        return

    from src import search as SE
    from src.vector_store import open_store

    conn = _conn(cfg)
    try:
        store = open_store(cfg)
        ok, msg = store.health()
        if not ok:
            st.error(msg)
            return
        t0 = time.monotonic()
        # 重排序的量由界面滑块决定：0 = 关闭（只按召回余弦排），>0 = 重排前 N 条。
        # 走 cfg.override 而不是往 search 传参，是为了让「阈值都集中在 config」
        # 这条纪律不被破坏 —— 覆盖只发生在内存里，且路径与 config 键完全一致。
        rr_k = int(ss.get("rerank_input_k") or 0)
        cfg.override("retrieval.rerank_enabled", rr_k > 0)
        if rr_k > 0:
            cfg.override("retrieval.rerank_input_k", rr_k)

        with st.spinner(f"检索中…{'（含重排序，约 %d 秒）' % (rr_k * 0.2) if rr_k else ''}"):
            if pending is not None and mode in ("findsame", "text", "image", "video"):
                outcome = SE.search_by_face_id(conn, cfg, store, int(pending),
                                               k=int(ss["topk"]))
            else:
                outcome = SE.search(
                    conn, cfg, store,
                    query_mode=mode,
                    payload=payload,
                    exact_person=bool(ss.get("exact_person")),
                    thresh=float(ss["thresh"]),
                    k=int(ss["topk"]),
                )
        elapsed = time.monotonic() - t0
        if rr_k:
            from src.rerank import get_reranker

            ss["reranker_loaded"] = bool(get_reranker(cfg).status.loaded)
    except Exception as exc:  # noqa: BLE001
        LOG.exception("检索失败")
        st.error(f"检索失败：{exc}")
        return
    finally:
        conn.close()

    d = outcome.as_dict()
    d["elapsed_sec"] = round(elapsed, 2)
    ss["outcome"] = d
    if "face_id" in st.query_params:
        del st.query_params["face_id"]
    _render_last_outcome(cfg, srv)


def _render_last_outcome(cfg: Config, srv) -> None:
    outcome = st.session_state.get("outcome")
    if not outcome:
        st.info("尚未检索。输入文字 / 图片 / 视频后点「开始检索」。")
        return

    files = outcome.get("files") or []
    if outcome.get("warning"):
        st.warning(outcome["warning"])
    if outcome.get("cut_note"):
        st.caption(outcome["cut_note"] + f" · 模式={outcome.get('mode')} · "
                   f"耗时 {outcome.get('elapsed_sec', '?')}s")
    # 重排序的可见性：用户必须能知道「这批结果到底有没有重排、排了多少条」，
    # 否则没法判断该不该调大滑块（重排是这套系统里唯一能区分相关性的环节）。
    if outcome.get("rerank_note"):
        (st.success if outcome.get("reranked") else st.info)(outcome["rerank_note"])
    if outcome.get("effective_query"):
        st.caption(f"实际发出的 query：`{outcome['effective_query']}`")

    st.markdown(f"### 命中 {outcome.get('total_shots', 0)} 个镜头 / "
                f"{outcome.get('total_files', 0)} 个文件")

    if not files:
        st.error("未命中。")
        st.markdown(
            f"**三条建议：**\n"
            f"1. 降低阈值（当前 {st.session_state.get('thresh', 0.55):.2f}）后重试；\n"
            f"2. 换用更具体的描述，或直接上传一张参考图（以图搜图通常比文字更准）；\n"
            f"3. 确认目标素材已完成索引（侧边栏「状态数字」中的 done 数应大于 0）。"
        )
        return

    for i, fh in enumerate(files[:50]):
        _render_hit_card(cfg, srv, fh, idx=i)

    _export_bar(cfg, scope="search_hits", outcome=outcome)

    # 标注工具（第十二章）：judgment 写入
    with st.expander("🏷 标注（写入 judgments，用于 nDCG 基线）"):
        qid = st.text_input("query_id", value=st.session_state.get("qid", "q001"), key="qid")
        cols = st.columns(min(10, max(1, len(files))))
        for c, fh in zip(cols, files[:10]):
            with c:
                st.caption(Path(str(fh.get("path") or "")).name[:14])
                if st.button("✅", key=f"j_rel_{fh.get('file_id')}"):
                    _judge(cfg, qid, fh, 1)
                if st.button("❌", key=f"j_irr_{fh.get('file_id')}"):
                    _judge(cfg, qid, fh, 0)


def _judge(cfg: Config, qid: str, fh: Dict[str, Any], label: int) -> None:
    conn = D.connect(cfg.db_path, cfg=cfg)
    try:
        D.add_judgment(conn, qid, str(fh.get("file_id")), 0,
                       float(fh.get("best_score") or 0.0), label)
        conn.commit()
        _flash(f"已记录 judgment：{qid} / file_id={fh.get('file_id')} → {label}", "success")
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 侧边栏
# --------------------------------------------------------------------------- #



# --------------------------------------------------------------------------- #
# 页面路由（v5.3：六个页面，一次只渲染一个 —— 原来两个 Tab 每次全渲染，
# 首屏因此多花一倍时间）
# --------------------------------------------------------------------------- #

NAV_PAGES = [
    ("wall", "🖼 ", "nav.wall"),
    ("groups", "👥 ", "nav.groups"),
    ("search", "🔎 ", "nav.search"),
    ("tasks", "📋 ", "nav.tasks"),
    ("settings", "⚙️ ", "nav.settings"),
    ("maintain", "🛠 ", "nav.maintain"),
    ("about", "ℹ️ ", "nav.about"),
]


def _reload_all() -> None:
    """配置或数据变了：把配置缓存、NVMe 统计缓存、所有 cache_data 一起清掉。"""
    try:
        _cfg.clear()
    except Exception:  # noqa: BLE001
        pass
    st.cache_data.clear()


def _apply_goto() -> None:
    """把上一轮按钮请求的跳转应用掉。

    必须在**创建导航控件之前**调用：Streamlit 不允许在控件已实例化之后
    再改它的 session_state（会抛 StreamlitAPIException）。
    所以按钮只写 `_goto_target` 再 rerun，跳转在下一轮开头生效。
    """
    tgt = st.session_state.pop("_goto_target", None)
    if tgt:
        st.session_state["nav_seg"] = tgt
        st.session_state["page"] = tgt


def _goto(page: str) -> None:
    """跳到某页（下一轮生效）。"""
    st.session_state["_goto_target"] = page
    st.rerun()


def nav_bar() -> str:
    """顶部页面导航。做成横排分段控件，是为了让「搜索」这类入口一眼能看见。

    为什么不用 st.tabs：Streamlit 的 tabs 会把**所有**页面的内容都执行一遍
    （照片墙 + 搜索 + 维护全跑），首屏要多花一倍时间，还会白查一遍数据库。
    分段控件只渲染选中页，外观上和 tab 一样。
    """
    ss = st.session_state
    keys = [k for k, _, _ in NAV_PAGES]
    labels = {k: icon + U.t(key) for k, icon, key in NAV_PAGES}
    if ss.get("nav_seg") not in keys:
        ss["nav_seg"] = ss.get("page") if ss.get("page") in keys else "wall"
    pick = st.segmented_control(
        "页面", keys, format_func=lambda k: labels[k], key="nav_seg",
        label_visibility="collapsed", width="stretch",
    )
    page = pick if pick in keys else ss["nav_seg"]
    ss["page"] = page
    return page


def _index_progress_summary(cfg: Config, stats: Dict[str, Any]) -> None:
    """索引到底建到哪了 —— 只保留真正有用的三行。"""
    roots = stats.get("indexed_roots") or []
    for r in roots[:3]:
        st.caption(f"根 `{r['indexed_root']}` · {r['files']:,} 个文件 · "
                   f"上次扫描 {(r['last_scan'] or '—')[:16].replace('T', ' ')}")
    by = stats.get("by_status") or {}
    pending = int(by.get("pending") or 0)
    missing = int(by.get("missing") or 0)
    dup = int(by.get("skipped_dup") or 0)
    lines = [f"待处理 **{pending:,}**"]
    if dup:
        lines.append(f"重复跳过 **{dup:,}**")
    if missing:
        lines.append(f"缺失 **{missing:,}**")
    st.caption(" · ".join(lines))
    configured = str(cfg.media_root).rstrip("\\/")
    indexed = {str(r["indexed_root"]).rstrip("\\/") for r in roots}
    if roots and configured not in indexed:
        st.warning(f"设置的素材文件夹是 `{configured}`，但库里索引的是 {sorted(indexed)}。"
                   "请到「设置 → 更改素材文件夹」走一遍向导，别直接改。")


def sidebar(cfg: Config, stats: Dict[str, Any], nvme: Dict[str, Any]) -> None:
    ss = st.session_state

    with st.sidebar:
        # ---- 现在的库 ----
        st.markdown("### 现在的库")
        st.caption(f"素材文件夹 `{stats['root']}`")
        c1, c2 = st.columns(2)
        c1.metric("素材", f"{int(stats.get('files_total') or 0):,}")
        c2.metric("人脸", f"{int(stats.get('faces') or 0):,}")
        st.caption(f"照片 {int(stats.get('photos') or 0):,}（已处理 "
                   f"{int(stats.get('photos_done') or 0):,}） · "
                   f"视频 {int(stats.get('videos') or 0):,}（已处理 "
                   f"{int(stats.get('videos_done') or 0):,}）")
        _index_progress_summary(cfg, stats)

        # ---- 开始 ----
        st.divider()
        st.markdown("### 开始")
        busy = active_task(cfg) is not None
        auto = st.checkbox(U.t("scan.auto_process"), value=bool(ss.get("auto_process", True)),
                           key="auto_process", help=U.t("scan.auto_process.help"),
                           disabled=busy)
        if st.button(U.t("scan.new"), type="primary", use_container_width=True,
                     disabled=busy, help=U.t("scan.new.help")):
            if start_task(cfg, "scan_new", params={"auto_process": auto}):
                st.rerun()
        st.caption(U.t("scan.new.help"))

        if not ss.get("confirm_scan_all"):
            if st.button(U.t("scan.all"), use_container_width=True, disabled=busy,
                         help=U.t("scan.all.help")):
                ss["confirm_scan_all"] = True
                st.rerun()
        else:
            st.warning(U.t("scan.all.confirm"))
            cc1, cc2 = st.columns(2)
            if cc1.button("确定重做", type="primary", use_container_width=True):
                ss["confirm_scan_all"] = False
                if start_task(cfg, "scan_all", params={"auto_process": auto}):
                    st.rerun()
            if cc2.button("取消", use_container_width=True):
                ss["confirm_scan_all"] = False
                st.rerun()

        if st.button(U.t("act.faces"), use_container_width=True, disabled=busy,
                     help=U.t("act.faces.help")):
            if start_task(cfg, "faces"):
                st.rerun()

        with st.expander("更多操作"):
            if st.button(U.t("act.extract"), use_container_width=True, disabled=busy,
                         help=U.t("act.extract.help")):
                if start_task(cfg, "extract"):
                    st.rerun()
            if st.button(U.t("act.encode"), use_container_width=True, disabled=busy,
                         help=U.t("act.encode.help")):
                if start_task(cfg, "encode"):
                    st.rerun()
            if st.button(U.t("act.cluster"), use_container_width=True, disabled=busy,
                         help=U.t("act.cluster.help")):
                if start_task(cfg, "cluster"):
                    st.rerun()
            if st.button(U.t("act.reconcile"), use_container_width=True, disabled=busy,
                         help=U.t("act.reconcile.help")):
                if start_task(cfg, "reconcile"):
                    st.rerun()
            if st.button("管理素材文件夹…", use_container_width=True):
                _goto("settings")
                st.rerun()

        # ---- 当前任务 ----
        st.divider()
        st.markdown("### 当前任务")
        if busy:
            # 只在真有任务时才挂自动刷新（1.5 秒一次）。空闲时挂着一个定时器
            # 会让界面一直在「有东西在跑」的状态里，点起来发闷。
            live_task_fragment(cfg)
        else:
            st.caption("空闲")
            _lines.last_task_notice(cfg)
        watch_events(cfg, reload_cfg=_reload_all)

        # ---- 异常角标（§15.5）----
        n_err = int(stats.get("files_error") or 0)
        if n_err:
            if st.button(f"⚠️ 待处理异常 {n_err:,}", use_container_width=True,
                         help="点开看是哪些文件、为什么失败"):
                _goto("maintain")
                st.rerun()

        # ---- 空间 ----
        st.divider()
        st.markdown("### 空间")
        if nvme.get("pending") or not nvme["known"]:
            # 正在算：挂一个 4 秒自刷新的片段，算完自动显示新数字，
            # 不需要用户再点一次（否则会出现「点了还是 22 分钟前」的错觉）。
            _space_live(cfg)
        else:
            _space_block(cfg, nvme)


def _space_block(cfg: Config, nvme: Dict[str, Any]) -> None:
    if not nvme["known"]:
        st.caption("正在后台统计中间文件占用…（约 1 分钟，不影响使用）")
    else:
        st.progress(min(1.0, float(nvme["ratio"])),
                    text=f"中间文件 {human_bytes(nvme['used'])} / "
                         f"{nvme['budget_gb']:.0f} GB（{nvme['ratio']*100:.1f}%）")
        st.caption(f"统计于 {_age_text(nvme.get('measured_at'))}"
                   + ("　· 正在后台更新…" if nvme.get("pending") else ""))
        if nvme["warn"]:
            st.error(f"中间文件已超 {nvme['ratio']*100:.0f}%，建议先跑完编码再继续入库。")
    if st.button("重新统计占用", use_container_width=True):
        _start_nvme_walk(str(cfg.db_path), str(cfg.path("paths.nvme_root")))
        st.rerun()


@st.fragment(run_every="4s")
def _space_live(cfg: Config) -> None:
    st.caption("正在后台统计中间文件占用…（约 1 分钟，算完这里会自动更新）")
    nvme = _nvme_usage(cfg, start_walk=False)
    if nvme["known"] and not nvme.get("pending"):
        _space_block(cfg, nvme)
        st.rerun(scope="app")
        return
    # 自愈：标记还在、但本进程里没有统计线程在跑（界面重启过、线程挂了），
    # 就重新拉一次。标记超过 90 秒才重试，避免线程起不来时每 4 秒空跑一遍。
    from datetime import datetime

    if nvme.get("pending") and not _NVME_WALKING["on"]:
        try:
            age = (datetime.now() - datetime.fromisoformat(
                _read_setting_raw(str(cfg.db_path), _NVME_PENDING_KEY))).total_seconds()
        except ValueError:
            age = 999
        if age > 90:
            _start_nvme_walk(str(cfg.db_path), str(cfg.path("paths.nvme_root")))


def _age_text(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    from datetime import datetime

    try:
        sec = (datetime.now() - datetime.fromisoformat(str(iso))).total_seconds()
    except ValueError:
        return str(iso)
    if sec < 90:
        return "刚刚"
    if sec < 3600:
        return f"{int(sec // 60)} 分钟前"
    if sec < 86400:
        return f"{int(sec // 3600)} 小时前"
    return f"{int(sec // 86400)} 天前"


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #


def main() -> None:
    _apply_goto()          # 必须先做：导航控件一旦创建就不能再改它的状态
    cfg = _cfg()
    _check_config_fresh(cfg)   # 配置被改过就重载，绝不用旧配置去操作库
    _ensure_state(cfg)
    cfg.ensure_dirs()
    _ensure_schema_once(str(cfg.db_path))

    srv, srv_ok, srv_msg = _static_server()

    try:
        stats = _stats_cached(str(cfg.db_path), str(cfg.media_root), _data_version(cfg))
    except Exception:  # noqa: BLE001 - 空库/未迁移时也要能打开界面
        stats = {"root": str(cfg.media_root), "files_total": 0, "files_done": 0,
                 "files_pending": 0, "files_error": 0, "files_missing": 0,
                 "photos": 0, "videos": 0, "shots": 0, "faces": 0, "last_scan": None,
                 "photos_done": 0, "videos_done": 0, "videos_pending": 0,
                 "videos_error": 0, "faces_pending_vector": 0, "by_status": {}}

    nvme = _nvme_usage(cfg)
    page = nav_bar()                 # 顶部横排导航（照片墙 / 自动分组 / 搜索 / 任务 / 设置 / 维护 / 关于）
    sidebar(cfg, stats, nvme)
    _show_toast()
    reason = st.session_state.pop("_reload_reason", None)
    if reason:
        st.caption(f"✓ {U.t('wall.reloaded', reason=reason)}")
    if not srv_ok:
        st.warning(f"缩略图服务未启动（{srv_msg}）—— 照片墙会显示不出图片。")

    # ---- 路由：一次只渲染当前页 ----
    try:
        if page == "wall":
            tab_wall(cfg, srv, stats)
        elif page == "groups":
            _pages.page_groups(cfg)
        elif page == "search":
            tab_ask(cfg, srv, stats)
        elif page == "tasks":
            _pages.page_tasks(cfg)
        elif page == "settings":
            _pages.page_settings(cfg, reload_cfg=_reload_all)
        elif page == "maintain":
            _pages.page_maintain(cfg)
        elif page == "about":
            _pages.page_about(cfg, stats)
    except Exception as exc:  # noqa: BLE001 - 单页出错不该整站白屏
        LOG.exception("页面渲染失败：%s", page)
        st.error(U.t("error.generic", msg=exc))
        with st.expander("技术细节（排查用）"):
            st.exception(exc)

    # 渲染完成的证据行：出问题（白屏/卡住）时，日志里能明确区分
    # 「脚本根本没跑完」与「跑完了但前端没显示」，省掉一轮瞎猜。
    LOG.info("界面渲染完成：页面=%s · 文件 %s · 人脸 %s · 中间文件 %s · 静态服务 %s",
             page, f"{stats['files_done']:,}", f"{stats['faces']:,}",
             human_bytes(nvme["used"]), "ok" if srv_ok else "未启动")


if __name__ == "__main__":
    main()
