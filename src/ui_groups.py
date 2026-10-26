# -*- coding: utf-8 -*-
"""src/ui_groups.py · 「按人浏览」视图（并入照片墙，规格 v5.3 §6.5）。

为什么单独成模块：照片墙的「全部人脸」逻辑有 200 多行且已稳定，不该为了
并入分组视图而动它。这里做成一个并列视图，由照片墙顶部开关切换。

交互约定（与用户确认过的清单一致）：
  * 一张卡 = 一个人（封面 + 人数 + 姓名 + 相似度 + 低置信度标记）
  * **单击卡片 → 弹窗**看这个人的全部照片与操作（Streamlit 没有原生双击，
    双击要靠「300ms 内点两次」，每次点击都要重跑脚本，不可靠，故用单击）
  * 弹窗里可命名 / 合并到别的组 / 拆出选中的人脸 / 撤销
  * 主网格不再用下拉框展示同一组人
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import streamlit as st

from . import cluster as C
from . import db as D
from . import tasks as T
from . import ui_text as U
from .common import Config
from .ui_actions import active_task, start_task
from .ui_common import flash


def _thumb(cfg: Config, thumb: Optional[str]) -> Optional[Path]:
    if not thumb:
        return None
    p = Path(str(thumb))
    if p.is_absolute():
        return p if p.exists() else None
    cand = Path(str(cfg.get("paths.nvme_root"))) / p
    return cand if cand.exists() else None


def _members(cfg: Config, version: str, cluster_id: int, limit: int = 60) -> List[Dict[str, Any]]:
    from . import db as D

    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        rows = conn.execute(
            "SELECT fc.face_id, fc.confidence, fr.thumb_path, fr.quality, fr.sec, "
            "fr.file_id, f.path AS file_path, f.kind, f.captured_at, f.status AS file_status "
            "FROM face_clusters fc "
            "JOIN face_records fr ON fr.face_id = fc.face_id "
            "LEFT JOIN files f ON f.file_id = fr.file_id "
            "WHERE fc.version=? AND fc.cluster_id=? AND fr.status='alive' "
            "  AND (f.visible IS NULL OR f.visible=1) AND (f.status IS NULL OR f.status != 'missing') "
            "ORDER BY fr.quality DESC LIMIT ?", (version, cluster_id, limit)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _all_clusters(cfg: Config, version: str) -> List[Dict[str, Any]]:
    """同名/人数索引，供合并下拉用（一次查询，避免 N 次子查询）。"""
    return C.groups_overview(cfg, version, limit=5000)


def _photo_cell(cfg: Config, srv: Any, m: Dict[str, Any], cluster_id: int) -> None:
    """一张人脸照片：悬停显示原图路径，单击/双击打开所在文件夹。

    st.image 没有 tooltip 也没有点击事件，所以：
      * 用 HTML <img title="路径"> 渲染（title 就是浏览器原生悬停提示）；
      * 照片下方留一个极简的「打开」文字按钮 —— 不做双击模拟：
        双击要靠「两次点击同一目标且间隔 <700ms」，而每次点击都要重跑脚本，
        实测在 8 列网格上不可靠，会变成「有时能开有时不能」。
    """
    path = str(m.get("file_path") or "")
    url = None
    try:
        url = srv.thumb_url(str(m.get("thumb_path"))) if m.get("thumb_path") else None
    except Exception:  # noqa: BLE001
        url = None
    if url:
        # title=完整路径 → 鼠标悬停即显示；alt 作兜底
        st.markdown(
            f"<img src='{url}' title='{path}' alt='{path}' "
            "style='width:100%;border-radius:6px;display:block' />",
            unsafe_allow_html=True)
    else:
        st.markdown("<div style='height:56px'></div>", unsafe_allow_html=True)
    file_missing = str(m.get("file_status") or "") == "missing"
    if file_missing:
        # 源文件已被删除/移走：不再给「打开」按钮，否则一点就报「文件不存在」，
        # 让人以为是系统坏了。缩略图仍在 nvme 里，所以照片本身还看得到。
        st.caption("原文件已不在")
    elif path and st.button("📂", key=f"dlgopen_{cluster_id}_{int(m['face_id'])}",
                            help=path, use_container_width=True):
        from .ui_common import open_folder

        err = open_folder(path)
        if err:
            flash(err, "error")
    # 恢复显示编号：否则一排照片看不出「这是哪一张」，用户没法对照原图
    st.caption(f"`#{int(m['face_id'])}`")


def _group_panel(cfg: Config, srv: Any, version: str, cluster_id: int) -> None:
    if st.button("← 返回分组列表", key=f"back_{cluster_id}"):
        st.session_state.pop("open_group", None)
        st.rerun()
    info = next((g for g in _all_clusters(cfg, version)
                 if int(g["cluster_id"]) == int(cluster_id)), None)
    members = _members(cfg, version, cluster_id, limit=60)
    name = (info or {}).get("name") or ""
    conf = float((info or {}).get("confidence") or 0.0)
    n_alive = sum(1 for m in members if str(m.get("file_status") or "") != "missing")
    n_missing = len(members) - n_alive
    st.markdown(f"**第 {cluster_id} 组** · {int((info or {}).get('size') or 0)} 张 · "
                f"相似度 {conf:.2f}" + (f" · **{name}**" if name else ""))
    if n_missing:
        st.caption(f"本组显示 {len(members)} 张，其中 **{n_missing} 张的原文件已不在磁盘上**"
                   f"（缩略图仍在缓存里，可以看但打不开原图）。"
                   f"删掉那些文件后跑一次扫描，它们会自动从墙上消失。")

    c1, c2 = st.columns([3, 1])
    nm = c1.text_input("给这个人起个名字", value=name, placeholder="例如：张三",
                       key=f"dlg_name_{cluster_id}")
    if c2.button("保存名字", use_container_width=True, key=f"dlg_save_{cluster_id}"):
        try:
            C.set_name(cfg, cluster_id, nm)
            flash(f"已命名为「{nm or '（清空）'}」。", "success")
        except Exception as exc:  # noqa: BLE001
            flash(f"命名失败：{exc}", "error")
        st.rerun()

    # ---- 成员缩略图（点开大图靠卡片下方的路径按钮）----
    ncols = 6
    for start in range(0, len(members), ncols):
        cols = st.columns(ncols)
        for j, m in enumerate(members[start:start + ncols]):
            with cols[j]:
                _photo_cell(cfg, srv, m, cluster_id)

    picked = st.multiselect(
        "要拆出去的人脸（选中的会成立一个新组）",
        [int(m["face_id"]) for m in members], key=f"dlg_split_{cluster_id}",
        format_func=lambda f: f"face_id {f}")
    d1, d2 = st.columns(2)
    if d1.button("拆分出去", disabled=not picked, key=f"dlg_do_{cluster_id}"):
        try:
            r = C.split_group(cfg, cluster_id, picked)
            flash(f"已拆出 {r['moved']} 张，成立第 {r['new']} 组（可撤销）。", "success")
        except Exception as exc:  # noqa: BLE001
            flash(f"拆分失败：{exc}", "error")
        st.rerun()
    if d2.button("撤销上一次合并/拆分", key=f"dlg_undo_{cluster_id}"):
        try:
            r = C.undo_last(cfg)
            flash(f"已撤销「{r['undone']}」，影响 {r['faces']} 张。", "success")
        except Exception as exc:  # noqa: BLE001
            flash(f"撤销失败：{exc}", "error")
        st.rerun()

    others = [int(g["cluster_id"]) for g in _all_clusters(cfg, version)
              if int(g["cluster_id"]) != int(cluster_id)]
    tgt = st.selectbox("把这一组合并到…", others, key=f"dlg_merge_{cluster_id}",
                       format_func=lambda c: f"第 {c} 组")
    if st.button("合并过去", key=f"dlg_mergebtn_{cluster_id}", type="primary"):
        try:
            r = C.merge_groups(cfg, cluster_id, int(tgt))
            flash(f"已把 {r['moved']} 张并入第 {tgt} 组（可撤销）。", "success")
        except Exception as exc:  # noqa: BLE001
            flash(f"合并失败：{exc}", "error")
        st.rerun()

    # 原文件动作
    if members:
        # 优先挑「原文件还在」的那张当代表，避免一点开就报文件不存在
        first = next((m for m in members
                      if str(m.get("file_status") or "") != "missing"), members[0])
        if str(first.get("file_status") or "") == "missing":
            st.caption("代表图的原文件已不在磁盘上（缩略图仍可查看）。")
        else:
            st.caption(f"代表图来自：`{first.get('file_path') or '—'}`")
        if first.get("file_path"):
            if st.button("在资源管理器里定位这张原图", key=f"dlg_open_{cluster_id}"):
                from .ui_common import open_folder

                err = open_folder(str(first["file_path"]))
                if err:
                    flash(err, "error")


def _unclustered_count(cfg: Config, version: str) -> int:
    """当前版本里没有归组、但能显示的人脸数量。"""
    try:
        c = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    except Exception:  # noqa: BLE001
        return 0
    try:
        row = c.execute(
            "SELECT COUNT(*) FROM face_records fr "
            "JOIN files f ON f.file_id = fr.file_id "
            "LEFT JOIN face_clusters fc ON fc.version = ? AND fc.face_id = fr.face_id "
            "WHERE fc.face_id IS NULL AND fr.status='alive' AND fr.has_vector=1 "
            "  AND f.visible=1 AND f.status != 'missing'", (version,)).fetchone()
        return int(row[0] or 0)
    except Exception:  # noqa: BLE001
        return 0
    finally:
        c.close()


def _unclustered_faces(cfg: Config, version: str, *, limit: int = 120,
                       offset: int = 0) -> List[Dict[str, Any]]:
    """未归组的新脸（face_id 倒序 = 最新入库的在前）。"""
    try:
        c = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    except Exception:  # noqa: BLE001
        return []
    try:
        rows = c.execute(
            "SELECT fr.face_id, fr.thumb_path, fr.quality, fr.sec, fr.file_id, "
            "       f.path AS file_path, f.kind, f.captured_at, f.status AS file_status "
            "FROM face_records fr "
            "JOIN files f ON f.file_id = fr.file_id "
            "LEFT JOIN face_clusters fc ON fc.version = ? AND fc.face_id = fr.face_id "
            "WHERE fc.face_id IS NULL AND fr.status='alive' AND fr.has_vector=1 "
            "  AND f.visible=1 AND f.status != 'missing' "
            "ORDER BY fr.face_id DESC LIMIT ? OFFSET ?",
            (version, int(limit), int(offset))).fetchall()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []
    finally:
        c.close()


@st.cache_data(ttl=120, show_spinner=False)
def _unclustered_count_cached(db_path: str, version: str) -> int:
    """缓存版计数（全表 join 要 1 秒左右，照片墙不该每次都付这个代价）。"""
    import sqlite3

    try:
        c = sqlite3.connect(db_path, timeout=10)
    except Exception:  # noqa: BLE001
        return 0
    try:
        row = c.execute(
            "SELECT COUNT(*) FROM face_records fr "
            "JOIN files f ON f.file_id = fr.file_id "
            "LEFT JOIN face_clusters fc ON fc.version = ? AND fc.face_id = fr.face_id "
            "WHERE fc.face_id IS NULL AND fr.status='alive' AND fr.has_vector=1 "
            "  AND f.visible=1 AND f.status != 'missing'", (version,)).fetchone()
        return int(row[0] or 0)
    except Exception:  # noqa: BLE001
        return 0
    finally:
        c.close()


def _unclustered_banner(cfg: Config, srv: Any, version: Optional[str]) -> None:
    """按人浏览顶部的「新增未归组」提示（缩略图按需展开）。"""
    if not version:
        return
    n = _unclustered_count_cached(str(cfg.db_path), version)
    if n <= 0:
        return
    gate = float(cfg.get("cluster.quality_gate", 0.3))
    st.warning(
        f"有 **{n:,} 张人脸没有参与本次分组**（`{version}`）。\n\n"
        f"原因：他们的清晰度低于「**参与分组的清晰度下限 = {gate:.2f}**」"
        f"（设置 → 高级 → 「参与分组的清晰度下限」）。注意「照片墙清晰度下限」是**另一个开关**，"
        f"只管这里显示哪些脸，**不影响分组**；想让他们也归组，请把分组那项调低，"
        f"再点「重新分组」——分组是快照，改门槛不会追溯。")
    if not st.toggle(f"看看这些新脸（共 {n:,} 张，最新在前）", key="unc_show"):
        return
    page_size = 120
    key = "unclustered_page"
    page = int(st.session_state.get(key, 0) or 0)
    items = _unclustered_faces(cfg, version, limit=page_size, offset=page * page_size)
    if not items:
        st.caption("这一页没有数据。")
        return
    cols = st.columns(8)
    for i, m in enumerate(items):
        with cols[i % 8]:
            _photo_cell(cfg, srv, m, -1)
    nav1, nav2, nav3 = st.columns([1, 2, 1])
    if nav1.button("← 上一页", disabled=page <= 0, key="unc_prev", use_container_width=True):
        st.session_state[key] = max(0, page - 1)
        st.rerun()
    nav2.caption(f"第 {page + 1} / {max(1, (n + page_size - 1) // page_size)} 页")
    if nav3.button("下一页 →", disabled=(page + 1) * page_size >= n,
                   key="unc_next", use_container_width=True):
        st.session_state[key] = page + 1
        st.rerun()


def groups_view(cfg: Config, srv: Any) -> None:
    """按人浏览：版本信息 + 分组入口 + 筛选 + 网格 + 弹窗。"""
    ss = st.session_state
    cur = C.current_version(cfg)
    runs = C.version_history(cfg, limit=10)

    # 「当前正在看哪一组」必须是**持久**状态：点 📂 / 命名 / 合并 / 拆分都会
    # 触发重跑，如果这里把状态消费掉（pop），界面就会退回列表 ——
    # 2026-10-19 的真实 bug：点 📂 打开文件夹，结果界面先退回列表。
    if ss.get("open_group") is not None and cur:
        try:
            _group_panel(cfg, srv, cur, int(ss["open_group"]))
        except Exception as exc:  # noqa: BLE001
            flash(f"打开分组失败：{exc}", "error")
            ss.pop("open_group", None)
        return

    # 扫描后新增、但还没归组的人脸：按人浏览看不到他们，必须明确告知并给出入口
    _unclustered_banner(cfg, srv, cur)

    running_task = active_task(cfg)
    active = running_task is not None
    c0, c1 = st.columns([1, 3])
    with c0:
        if st.button("重新分组" if cur else "开始分组", type="primary",
                     use_container_width=True, disabled=active):
            ss["confirm_cluster"] = True
        if active:
            st.caption(f"有任务在跑（{running_task['title']} {running_task['done']}/"
                       f"{running_task['total'] if running_task['total'] else '?'}），"
                       f"结束后才能分组。")
    with c1:
        if cur:
            info = next((r for r in runs if r["version"] == cur), runs[0] if runs else {})
            live = C.live_meta_stats(cfg, cur)
            st.caption(f"当前版本 `{cur}` · **{live['groups']:,} 组**（当前可见）· "
                       f"{live['faces']:,} 张已归类 · "
                       f"分组时记录 {int(info.get('n_clusters') or 0):,} 组 / "
                       f"{int(info.get('n_faces') or 0):,} 张")
        else:
            st.caption("还没有分组结果。点左侧按钮开始（2 万张试跑约 10 秒）。")

    if ss.get("confirm_cluster"):
        st.warning("重新分组会产出新版本并切换过去（旧版本保留、可回退）。确定开始吗？")
        cc1, cc2 = st.columns(2)
        if cc1.button("确定开始分组", type="primary", use_container_width=True):
            ss["confirm_cluster"] = False
            tid = start_task(cfg, "cluster")
            if tid:
                flash("分组任务已启动，进度见左侧「当前任务」。", "success")
                st.rerun()
            else:
                # start_task 已经 flash 过原因，这里再用 error 明确显示一次
                reason = (st.session_state.get("toast") or {}).get("msg") or "未知原因"
                st.error(f"没有启动分组：{reason}")
                st.session_state["toast"] = None
        if cc2.button("取消", use_container_width=True):
            ss["confirm_cluster"] = False
            st.rerun()

    if not cur or not runs:
        st.info("还没有分组结果。先点「开始分组」。")
        return

    with st.expander("分组版本与回退"):
        for r in runs:
            mark = " ✅ 当前" if r["version"] == cur else ""
            cc1, cc2 = st.columns([3, 1])
            cc1.caption(f"`{r['version']}` · {int(r['n_clusters'] or 0):,} 组 · "
                        f"{int(r['n_faces'] or 0):,} 张 · "
                        f"{str(r['created_at'] or '')[:16].replace('T', ' ')}{mark}")
            if r["version"] != cur and cc2.button("切到这个版本", key=f"sw_{r['version']}"):
                C.switch_version(cfg, r["version"])
                flash(f"已切到 {r['version']}", "success")
                st.rerun()

    f1, f2 = st.columns([1, 1])
    only_low = f1.toggle("只看待审核（低置信度）", value=bool(ss.get("gv_low", False)),
                         key="gv_low_t")
    only_named = f2.toggle("只看已命名", value=bool(ss.get("gv_named", False)),
                           key="gv_named_t")
    ss["gv_low"], ss["gv_named"] = only_low, only_named

    groups = C.groups_overview(cfg, cur, only_low=only_low, limit=300)
    if only_named:
        groups = [g for g in groups if g.get("name")]
    if not groups:
        st.caption("没有符合条件的组。")
        return

    low_thr = float(cfg.get("cluster.low_confidence_threshold", 0.45))
    st.caption(f"共 {len(groups):,} 组（按人数排序）· 单击卡片看这个人的全部照片")
    ncols = 8
    for start in range(0, len(groups), ncols):
        cols = st.columns(ncols)
        for j, g in enumerate(groups[start:start + ncols]):
            cid = int(g["cluster_id"])
            with cols[j]:
                tp = _thumb(cfg, g.get("thumb_path"))
                if tp is not None:
                    st.image(str(tp), width="stretch")
                else:
                    st.markdown("<div style='height:56px'></div>", unsafe_allow_html=True)
                nm = g.get("name") or ""
                flag = " ⚠️" if float(g.get("confidence") or 0) < low_thr else ""
                st.markdown(f"**{nm}**" if nm else f"第 {cid} 组")
                st.caption(f"{int(g.get('size') or 0)} 张 · "
                           f"{float(g.get('confidence') or 0):.2f}{flag}")
                if st.button("打开", key=f"go_{cid}", use_container_width=True):
                    ss["open_group"] = cid
                    st.rerun()
