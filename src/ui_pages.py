# -*- coding: utf-8 -*-
"""src/ui_pages.py · 任务 / 设置 / 维护 / 关于 四个页面（规格 v5.3 §3）。

分层按规格 §3.3：
  L1 常用设置   → 设置页默认展开
  L2 高级设置   → 设置页折叠
  L3 危险操作   → 维护页，红按钮 + 二次确认
  L4 只读信息   → 关于页
"""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from . import migrate as MG
from . import settings_store as SS
from . import tasks as T
from . import ui_text as U
from .common import Config, human_bytes
from .ui_common import conn, flash, fmt_eta, fmt_rate, fmt_sec
from .ui_actions import active_task, media_root_wizard, progress_block, recent_tasks, start_task, stop_task

APP_VERSION = "5.3.0"


# =========================================================================== #
# 任务页
# =========================================================================== #


@st.fragment(run_every="2s")
def _tasks_live(cfg: Config) -> None:
    """任务页的实时进度。

    为什么必须是 fragment：fragment 只重跑自己这一小段。
    原来任务页直接画进度，只有整页重跑才会更新 —— 而整页重跑只在
    「数据变更事件」时发生（抽取画面不改数据快照），于是进度条看起来是冻住的。
    """
    task = active_task(cfg)
    if task is None:
        st.info("当前没有正在运行的任务。左侧「开始」里的按钮随点随跑。")
        return
    st.markdown(f"#### {task['title']} · {U.task_state_name(task.get('state') or '')}")
    progress_block(task, on_stop=lambda: stop_task(cfg, task["task_id"]))
    if int(task.get("stage_ix") or 0) and int(task.get("stage_total") or 0):
        st.caption(f"整体进度：第 {task['stage_ix']} / {task['stage_total']} 步")
    st.caption("关掉这个页面不会中断任务。重新打开还能看到进度（状态存在库里）。")


def page_tasks(cfg: Config) -> None:
    st.markdown(f"## {U.t('nav.tasks')}")
    _tasks_live(cfg)

    st.divider()
    st.markdown(f"### {U.t('task.history')}")
    rows = recent_tasks(cfg, days=30, limit=200)
    if not rows:
        st.caption(U.t("task.history.empty"))
        return
    table = []
    for t in rows:
        st_ = T.snapshot(t)
        took = st_.get("elapsed_sec")
        done, total = int(t.get("done") or 0), t.get("total")
        table.append({
            U.t("task.col.time"): (t.get("started_at") or t.get("created_at") or "")[:19].replace("T", " "),
            U.t("task.col.name"): t["title"],
            U.t("task.col.state"): U.task_state_name(t.get("state") or ""),
            U.t("task.col.result"): (f"{done:,} / {int(total):,}" if total else f"{done:,}"),
            U.t("task.col.took"): fmt_sec(took),
            "说明": (t.get("error") or t.get("message") or "")[:120],
        })
    st.dataframe(pd.DataFrame(table), use_container_width=True, hide_index=True,
                 height=min(600, 40 + 35 * len(table)))
    c1, c2 = st.columns([1, 4])
    with c1:
        if st.button("清理 30 天前记录"):
            n = T.purge_history(cfg)
            if n.get("error"):
                flash(n["error"], "warning")
            else:
                flash(f"已清理 {n['tasks']} 条任务记录、{n['events']} 条事件", "success")
            st.rerun()


# =========================================================================== #
# 设置页
# =========================================================================== #


def _clamp_num(value: Any, s: Setting, *, is_int: bool) -> Any:
    """把配置值夹到设置项允许的区间内。

    为什么需要：配置里缺键、或历史值越界时，`number_input(value=0, min_value=1000)`
    会让 Streamlit 直接抛异常，**整个设置页打不开**（2026-10-26 真实事故：
    `cluster.max_fit_faces` 写错层级取到 0，min 是 1000）。
    这里统一夹紧：缺值取 vmin（没有就用 0），越界取边界。
    """
    conv = int if is_int else float
    try:
        v = conv(value) if value is not None else None
    except (TypeError, ValueError):
        v = None
    if v is None:
        v = conv(s.vmin) if s.vmin is not None else conv(0)
    if s.vmin is not None and v < conv(s.vmin):
        v = conv(s.vmin)
    if s.vmax is not None and v > conv(s.vmax):
        v = conv(s.vmax)
    return v


def _widget(s: SS.Setting, value: Any, key: str) -> Any:
    label = s.label + ("（改完需重新扫描）" if s.rescan else "")
    if s.kind == "bool":
        return st.checkbox(label, value=bool(value), key=key, help=s.help or None)
    if s.kind == "int":
        return int(st.number_input(label, value=_clamp_num(value, s, is_int=True),
                                   min_value=int(s.vmin) if s.vmin is not None else None,
                                   max_value=int(s.vmax) if s.vmax is not None else None,
                                   step=int(s.step or 1), key=key, help=s.help or None))
    if s.kind == "float":
        return float(st.number_input(label, value=_clamp_num(value, s, is_int=False),
                                     min_value=float(s.vmin) if s.vmin is not None else None,
                                     max_value=float(s.vmax) if s.vmax is not None else None,
                                     step=float(s.step or 0.01), format="%.4f",
                                     key=key, help=s.help or None))
    if s.kind == "choice":
        vals = [c[0] for c in s.choices]
        labels = {c[0]: c[1] for c in s.choices}
        try:
            ix = vals.index(value)
        except ValueError:
            ix = 0
        return st.selectbox(label, vals, index=ix, key=key,
                            format_func=lambda v: labels.get(v, str(v)),
                            help=s.help or None)
    if s.kind == "multi":
        cur = value if isinstance(value, (list, tuple)) else []
        txt = st.text_area(label, value="\n".join(str(x) for x in cur), height=90,
                           key=key, help=s.help or None)
        return [ln.strip() for ln in txt.splitlines() if ln.strip()]
    return st.text_input(label, value=str(value or ""), key=key, help=s.help or None)


def media_roots_panel(cfg: Config, *, reload_cfg=None) -> None:
    """素材文件夹管理：增 / 删 / 改 / 停用 / 单独扫描（v5.3 §4 多目录）。"""
    from . import media_roots as MR

    ss = st.session_state
    st.markdown("### 素材文件夹")
    st.caption("可以登记多个文件夹，各自单独扫描、单独统计。"
               "**删除默认不删任何数据**，只是不再扫描它。")

    roots = MR.list_roots(cfg)
    if not roots:
        st.info("还没有登记任何素材文件夹。先在下面添加一个。")
    else:
        rows = []
        for r in roots:
            state = []
            state.append("启用" if r["enabled"] else "已停用（不显示/不搜索）")
            if not r["exists"]:
                state.append("⚠️ 路径不存在")
            state.append("已建立索引" if r["files"] else "未扫描")
            rows.append({
                "文件夹": r["path"],
                "备注": r["label"] or "—",
                "状态": " · ".join(state),
                "文件数": f"{r['files']:,}",
                "已处理": f"{r['done']:,}",
                "上次扫描": str(r["last_scan"] or "—")[:16].replace("T", " "),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        paths = [r["path"] for r in roots]
        pick = st.selectbox("选择要操作的文件夹", paths, key="mr_pick")
        cur = next((r for r in roots if r["path"] == pick), roots[0])
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            if st.button("扫描这个文件夹", use_container_width=True,
                         disabled=not cur["enabled"] or not cur["exists"]):
                # 只扫这一个根：plan 里只有一个 01_manifest 步骤（+ 是否接连处理）
                tid = start_task(cfg, "scan_new",
                                 params={"roots": [cur["path"]],
                                         "auto_process": bool(ss.get("auto_process", True))})
                if tid:
                    st.rerun()
        with c2:
            if st.button("修改路径…", use_container_width=True):
                ss["media_edit"] = cur["path"]
                ss["media_remove"] = None
                st.rerun()
        with c3:
            label = "停用（不显示·不搜索）" if cur["enabled"] else "重新启用"
            if st.button(label, use_container_width=True):
                try:
                    MR.set_enabled(cfg, cur["path"], not cur["enabled"])
                    flash("已停用：不再扫描，也不会出现在搜索 / 照片墙 / 人脸分组里；记录与向量都还在，点「重新启用」立刻恢复。" if cur["enabled"] else "已启用：这个文件夹的素材重新参与扫描、搜索与照片墙。", "success")
                except ValueError as exc:
                    flash(str(exc), "error")
                if reload_cfg:
                    reload_cfg()
                st.rerun()
        with c4:
            if st.button("移除…", use_container_width=True):
                ss["media_remove"] = cur["path"]
                ss["media_edit"] = None
                st.rerun()

    # ---- 添加 ----
    with st.expander("＋ 添加素材文件夹", expanded=not roots):
        with st.form("mr_add_form", border=False):
            newp = st.text_input("文件夹路径", placeholder=r"例如 D:\素材",
                                 key="mr_new_path")
            lbl = st.text_input("备注（可选）", key="mr_new_label")
            if st.form_submit_button("添加", type="primary"):
                try:
                    MR.add(cfg, newp, label=lbl)
                    flash("已添加。接着点「扫描新文件」把里面的素材入库。", "success")
                    if reload_cfg:
                        reload_cfg()
                    st.rerun()
                except ValueError as exc:
                    flash(str(exc), "error")
                    st.rerun()

    # ---- 修改路径（走差异向导，带 20% 硬闸门）----
    if ss.get("media_edit"):
        oldp = ss["media_edit"]
        with st.expander(f"修改路径：{oldp}", expanded=True):
            media_root_wizard(cfg, old_root=oldp, on_applied=reload_cfg)
            if st.button("收起", key="mr_edit_close"):
                ss["media_edit"] = None
                st.rerun()

    # ---- 移除 ----
    if ss.get("media_remove"):
        rp = ss["media_remove"]
        st.warning(f"要移除的是：`{rp}`")
        st.markdown("**移除 = 把这个文件夹的记录与向量从数据库里删掉**"
                    "（磁盘上的图片/视频一个都不会动，随时可以重新扫回来）。\n\n"
                    "如果只是想暂时「不显示、不搜索」它，请用上方的 **停用**。")
        mode = st.radio(
            "移除后怎么处理这个文件夹里的数据",
            ["purge", "only"],
            format_func=lambda v: {
                "purge": "从库中完整删除 —— 记录、向量、人脸一起删（磁盘原文件不动）",
                "only": "只从列表移除、数据保留 —— 记录还在库里，仍会被搜到（不推荐）",
            }[v], index=0, key="mr_remove_mode")
        ok = st.checkbox("我确认", key="mr_remove_ok")
        c1, c2 = st.columns(2)
        if c1.button("确认移除", type="primary", disabled=not ok):
            try:
                res = MR.remove(cfg, rp, purge=(mode == "purge"))
                flash(f"已移除。{'清理了 %d 个文件的记录与向量' % res.get('purged', 0) if mode == 'purge' else '数据全部保留'}",
                      "success")
            except ValueError as exc:
                flash(str(exc), "error")
            ss["media_remove"] = None
            if reload_cfg:
                reload_cfg()
            st.rerun()
        if c2.button("取消", key="mr_remove_cancel"):
            ss["media_remove"] = None
            st.rerun()



def _performance_values(turbo: bool) -> Dict[str, Any]:
    """「全速模式」对应的几项配置值（一键应用 / 一键恢复都用它）。"""
    if turbo:
        return {
            "performance.mode": "turbo",
            "performance.photo_prefetch": 6,
            "performance.video_workers": 12,
            "performance.frame_workers": 8,
            "performance.encode_batch": 32,
            "performance.encode_group": 12,
        }
    return {
        "performance.mode": "balanced",
        "performance.photo_prefetch": 0,
        "performance.video_workers": 0,
        "performance.encode_batch": 0,
        "performance.encode_group": 0,
    }


def page_settings(cfg: Config, *, reload_cfg=None) -> None:
    ss = st.session_state
    st.markdown(f"## {U.t('set.title')}")
    st.caption("这里的改动会立即写入 `config.local.yaml`（不会动 config.yaml 的注释），"
               "并记录到修改记录里。")

    # ---- 素材文件夹：增 / 删 / 改 / 单独扫描（多目录）----
    media_roots_panel(cfg, reload_cfg=reload_cfg)
    st.divider()

    # ---- 性能 / 全速模式（方案 B）----
    st.markdown("### 性能")
    _turbo_now = str(cfg.get("performance.mode", "balanced")) == "turbo"
    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        # key 跟随当前配置状态：配置变了 widget 会重建，
        # 避免「session 里还记着旧的关，一点应用就把全速撤销」
        turbo = st.toggle(
            "全速模式（照片预读 + 视频并发 12 + 编码批 32）",
            value=_turbo_now,
            key=f"perf_turbo_{'on' if _turbo_now else 'off'}",
            help="打开后：照片预读 6 线程、视频并发 12、编码批大小 32。"
                 "CPU/磁盘/GPU 会更满，界面可能变卡；随时可以关掉。")
    with c2:
        if st.button("立即应用", use_container_width=True):
            try:
                SS.save(cfg, _performance_values(turbo), who="界面")
                flash("已切换到「全速模式」" if turbo else "已恢复「均衡模式」", "success")
            except Exception as exc:  # noqa: BLE001
                flash(f"应用失败：{exc}", "error")
            if reload_cfg:
                reload_cfg()
            st.rerun()
    with c3:
        st.caption(f"当前：照片预读 {int(cfg.get('performance.photo_prefetch', 0) or 0)} 线程 · "
                   f"视频 {int(cfg.get('performance.video_workers', 0) or 0) or '默认'} 并发 · "
                   f"编码批 {int(cfg.get('performance.encode_batch', 0) or 0) or '默认'}")
    st.divider()

    cur = SS.current(cfg)
    groups = SS.grouped()

    show_adv = st.toggle(U.t("set.show_advanced"), value=bool(ss.get("show_adv", False)),
                         key="show_adv_toggle",
                         help="并发、批次、阈值精确值等。改错了会明显变慢，谨慎。")

    with st.form("settings_form", border=False):
        st.markdown(f"### {U.t('set.common')}")
        new_vals: Dict[str, Any] = {}
        for s in groups[SS.L1]:
            if s.special == "media_root":
                continue
            new_vals[s.key] = _widget(s, cur.get(s.key), f"set_{s.key}")
        if show_adv:
            st.markdown(f"### {U.t('set.advanced')}")
            for s in groups[SS.L2]:
                new_vals[s.key] = _widget(s, cur.get(s.key), f"set_{s.key}")
        submitted = st.form_submit_button("保存设置", type="primary")

    if submitted:
        try:
            res = SS.save(cfg, new_vals, who=f"界面/{ss.get('session_id', '?')[:8]}")
        except Exception as exc:  # noqa: BLE001
            flash(f"保存失败：{exc}", "error")
            st.rerun()
        if res["changed"] == 0:
            flash("没有改动。", "info")
        else:
            extra = (U.t("set.restart_needed", n=len(res["restart"]))
                     if res["restart"] else U.t("set.restart_none"))
            if res["rescan"]:
                extra += " 其中部分项需要重新扫描才对已入库文件生效。"
            flash(U.t("set.saved", n=res["changed"], restart=extra), "success")
            if reload_cfg is not None:
                reload_cfg()
        st.rerun()

    c1, c2 = st.columns([1, 3])
    with c1:
        if st.button(U.t("set.reset")):
            n = SS.reset(cfg)
            flash(f"已恢复默认（删掉 {n.get('removed', 0)} 项覆盖）", "success")
            if reload_cfg is not None:
                reload_cfg()
            st.rerun()
    with c2:
        with st.expander(U.t("set.audit")):
            _settings_audit(cfg)


def _settings_audit(cfg: Config, limit: int = 50) -> None:
    try:
        c = conn(cfg)
        try:
            rows = c.execute(
                "SELECT who, at, what, before, after FROM audit_log "
                "WHERE what LIKE 'settings%' ORDER BY at DESC LIMIT ?", (limit,)
            ).fetchall()
        finally:
            c.close()
    except Exception as exc:  # noqa: BLE001
        st.caption(f"读取修改记录失败：{exc}")
        return
    if not rows:
        st.caption("还没有修改记录。")
        return
    for r in rows:
        st.caption(f"`{r['at']}` **{r['what']}** · {r['who']} · {r['before']} → {r['after']}")


# =========================================================================== #
# 维护页（L3 危险操作 + 异常面板）
# =========================================================================== #


def page_maintain(cfg: Config) -> None:
    st.markdown(f"## {U.t('maint.title')}")

    # ---- 完整性检查 ----
    st.markdown("### 库完整性检查")
    st.caption("核对元数据与向量库是否对得上。**只出报告，不删任何东西**。")
    _start_btn(cfg, "reconcile", "开始检查", help_text=None)
    _latest_report_tail(cfg, "reconcile")

    st.divider()
    # ---- 已删除（可恢复）----
    st.markdown(f"### {U.t('maint.deleted')}")
    st.caption(U.t("maint.deleted.help"))
    rep = MG.missing_report(cfg, limit=300)
    if rep["total"] == 0:
        st.caption("没有缺失文件。")
    else:
        m1, m2, m3 = st.columns(3)
        m1.metric("缺失文件数", f"{rep['total']:,}")
        m2.metric("涉及数据量", rep["bytes_human"])
        m3.metric("其中照片 / 视频", f"{rep['by_kind'].get('photo', 0):,} / "
                                    f"{rep['by_kind'].get('video', 0):,}")
        df = pd.DataFrame([
            {"file_id": r["file_id"], "路径": r["path"], "类型": r["kind"],
             "大小": human_bytes(int(r["size"] or 0)), "标记时间": r["updated_at"]}
            for r in rep["items"]
        ])
        sel = st.dataframe(df, use_container_width=True, hide_index=True, height=280,
                           on_select="rerun", selection_mode="multi-row",
                           key="missing_table")
        picked = [int(df.iloc[i]["file_id"]) for i in (sel.selection.rows if sel else [])]

        c1, c2, c3 = st.columns(3)
        with c1:
            if st.button(U.t("maint.restore"), disabled=not picked,
                         help="把它们改回待处理，下次扫描如果文件又在磁盘上就会自动复活"):
                n = MG.restore_missing(cfg, picked)
                flash(f"已把 {n['restored']} 个文件改回待处理", "success")
                st.rerun()
        with c2:
            if st.button("生成清理报告", help="只统计，不删任何东西"):
                res = MG.purge_missing(cfg, picked or None, dry_run=True)
                st.session_state["purge_report"] = res
                st.rerun()
        with c3:
            confirm = st.checkbox("我确认要物理删除", key="purge_confirm")
            if st.button("🗑 删除记录与向量", type="primary", disabled=not confirm,
                         help="只删库里的记录和向量，磁盘上的文件一个都不动"):
                try:
                    res = MG.purge_missing(cfg, picked or None, dry_run=False)
                    n = int(res.get("purged") or 0)
                    left = int(MG.missing_report(cfg, limit=1)["total"] or 0)
                    if n == 0:
                        flash("匹配到 0 条可删记录：选中的行可能已被恢复或已清理，"
                              "请点「重新读取」后再试（这次没有删任何东西）。", "warning")
                    else:
                        flash(f"已删除 {n} 条记录及其向量（磁盘文件未动）。"
                              f"现在还剩 {left} 条缺失记录。", "success")
                except Exception as exc:  # noqa: BLE001 - 一定要把真实原因告诉用户
                    flash(f"清理失败：{type(exc).__name__}: {exc}", "error")
                st.rerun()
        if st.button("重新读取列表", help="如果列表和实际不一致，点这里重新查一遍"):
            st.rerun()
        pr = st.session_state.get("purge_report")
        if pr:
            st.info(f"将清理 {pr['would_purge']:,} 个文件的记录与向量。前几条示例：")
            for s in pr["samples"][:10]:
                st.code(s, language="text")

    st.divider()
    # ---- 待处理异常 ----
    st.markdown(f"### {U.t('maint.error_badge')}")
    _error_panel(cfg)

    st.divider()
    # ---- 日志与告警 ----
    st.markdown("### 日志与告警")
    _log_panel(cfg)

    st.divider()
    # ---- 备份 ----
    st.markdown("### 数据库备份")
    st.caption("把 data/library.db 复制一份到 data/backup/。大库复制要几十秒。")
    if st.button("立即备份"):
        src = cfg.db_path
        dst_dir = src.parent / "backup"
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / f"library_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        try:
            t0 = time.monotonic()
            shutil.copy2(src, dst)
            flash(f"已备份到 {dst.name}（{human_bytes(dst.stat().st_size)}，"
                  f"{fmt_sec(time.monotonic()-t0)}）", "success")
        except Exception as exc:  # noqa: BLE001
            flash(f"备份失败：{exc}", "error")
        st.rerun()

    st.divider()
    # ---- 回填与标定 ----
    with st.expander("灵敏度标定（用你自己的照片集）"):
        st.caption(
            "准备一个文件夹，里面按「人名」建子文件夹，每人放 5~20 张照片"
            "（正脸、侧脸、戴眼镜、遮挡各几张更好）。系统会算出最合适的识别灵敏度。"
            "这些照片只用来算阈值，不会加入素材库。"
        )
        cur_roots = list(cfg.get("cluster.gold_roots") or [])
        if not cur_roots and cfg.get("cluster.gold_root"):
            cur_roots = [str(cfg.get("cluster.gold_root"))]
        gold_text = st.text_area(
            "照片集文件夹（一行一个，可加多个）",
            value="\n".join(str(x) for x in cur_roots), height=110,
            placeholder="D:\\金标准集\nE:\\另一个来源",
            help="每个目录里按「人名」建子文件夹；也可以直接把「一个人」的目录加进来"
                 "（此时用目录名当人名）。同名子文件夹会被合并成同一个人。")
        col1, col2 = st.columns([1, 1])
        if col1.button("保存这些路径"):
            lines = [ln.strip() for ln in gold_text.splitlines() if ln.strip()]
            try:
                # 同时写列表与首项：gold_root 供 17_gold.py 的命令行兜底使用
                SS.save(cfg, {"cluster.gold_roots": lines,
                              "cluster.gold_root": lines[0] if lines else ""},
                        who="界面/维护")
                flash(f"已保存 {len(lines)} 个目录。", "success")
            except Exception as exc:  # noqa: BLE001
                flash(f"保存失败：{exc}", "error")
            st.rerun()

        lines_now = [ln.strip() for ln in gold_text.splitlines() if ln.strip()]
        if col2.button("开始标定", type="primary",
                       disabled=active_task(cfg) is not None or not lines_now):
            try:
                SS.save(cfg, {"cluster.gold_roots": lines_now,
                              "cluster.gold_root": lines_now[0]},
                        who="界面/维护")
            except Exception as exc:  # noqa: BLE001
                flash(f"保存失败：{exc}", "error")
                st.rerun()
            if start_task(cfg, "gold"):
                st.rerun()
        st.caption("标定结果看 data/gold/：result_*.json（PR 曲线数据）、"
                   "false_positives.csv（被判为同一人的错对）。")

def _start_btn(cfg: Config, kind: str, label: str, *, help_text: Optional[str] = None,
               disabled_if_empty: bool = False, params: Optional[Dict[str, Any]] = None) -> None:
    from . import taskplan

    busy = active_task(cfg) is not None
    if st.button(label, disabled=busy or disabled_if_empty,
                 help=help_text or None, key=f"start_{kind}_{label}"):
        tid = start_task(cfg, kind, params=params)
        if tid:
            st.rerun()
    if disabled_if_empty:
        st.caption("先填好上面的路径。")


def _latest_report_tail(cfg: Config, prefix: str) -> None:
    rep_dir = Path(cfg.get("paths.data")) / "reports"
    if not rep_dir.exists():
        return
    files = sorted(rep_dir.glob(f"{prefix}_*.json"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    if not files:
        return
    latest = files[0]
    st.caption(f"最近一次报告：{latest.name}（{datetime.fromtimestamp(latest.stat().st_mtime):%Y-%m-%d %H:%M}）")
    try:
        import json

        data = json.loads(latest.read_text(encoding="utf-8"))
        st.json(data, expanded=False)
    except Exception:  # noqa: BLE001
        pass


def _error_panel(cfg: Config) -> None:
    c = conn(cfg)
    try:
        enc_err = c.execute(
            "SELECT model, COUNT(*) FROM file_encoding WHERE status='error' GROUP BY model"
        ).fetchall()
        dl = c.execute(
            "SELECT stage, COUNT(*) FROM dead_letter GROUP BY stage ORDER BY 2 DESC"
        ).fetchall()
        samples = c.execute(
            "SELECT f.path, e.model, e.error FROM file_encoding e "
            "JOIN files f ON f.file_id=e.file_id WHERE e.status='error' LIMIT 30"
        ).fetchall()
    finally:
        c.close()
    total = sum(int(r[1]) for r in enc_err)
    st.metric(U.t("maint.error_badge"), f"{total:,}")
    if total == 0 and not dl:
        st.caption("没有待处理异常。")
        return
    for model, n in enc_err:
        st.caption(f"{model}: **{int(n):,}** 个文件编码失败（可在任务页重跑对应阶段）")
    for stage, n in dl:
        st.caption(f"{stage} 阶段反复失败：**{int(n):,}** 个文件（已达重试上限，不再自动重试）")
    with st.expander("看失败原因（前 30 条）"):
        for r in samples:
            st.code(f"{r['path']}\n{r['model']}: {(r['error'] or '')[:300]}", language="text")


def _log_panel(cfg: Config) -> None:
    log_dir = Path(cfg.get("paths.logs"))
    today = log_dir / f"{datetime.now():%Y-%m-%d}.log"
    n_err = 0
    if today.exists():
        try:
            with open(today, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if " ERROR " in line or line.startswith("ERROR"):
                        n_err += 1
        except OSError:
            pass
    c1, c2 = st.columns([1, 3])
    with c1:
        st.metric("今天日志里的错误行", f"{n_err:,}")
    with c2:
        st.caption(f"日志目录：`{log_dir}`　单文件上限 "
                   f"{int(cfg.get('monitor.rotate_max_mb', 100))} MB，"
                   f"保留 {int(cfg.get('monitor.rotate_keep', 10))} 个。")
    with st.expander("今天日志的最后 80 行"):
        if not today.exists():
            st.caption("今天还没有日志。")
            return
        try:
            lines = today.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
            st.code("\n".join(lines), language="text")
        except OSError as exc:
            st.caption(f"读日志失败：{exc}")


# =========================================================================== #
# 关于页（L4 只读）
# =========================================================================== #


@st.cache_data(ttl=600, show_spinner=False)
def _disk_rows(db_path_str: str, root_str: str, logs: str, export: str,
               nvme_used: int) -> List[Dict[str, Any]]:
    """磁盘占用表。**中间文件不在这里遍历**：用后台已算好的值。

    2026-10-20 修：原实现对 nvme 目录调 dir_size_bytes（14 万文件、47 秒），
    每次进「关于」页都要重走一遍 —— 用户感受是「切到这个 tab 卡半分钟」。
    """
    from .common import dir_size_bytes

    rows: List[Dict[str, Any]] = []
    rows.append({"项目": "数据库", "位置": db_path_str,
                 "大小": human_bytes(Path(db_path_str).stat().st_size)
                 if Path(db_path_str).exists() else "—"})
    rows.append({"项目": "中间文件", "位置": root_str,
                 "大小": human_bytes(nvme_used) if nvme_used else "统计中…"})
    for label, p in (("导出", export), ("日志", logs)):
        try:
            size = human_bytes(dir_size_bytes(Path(p))) if Path(p).exists() else "—"
        except Exception:  # noqa: BLE001
            size = "—"
        rows.append({"项目": label, "位置": p, "大小": size})
    return rows


def page_about(cfg: Config, stats: Dict[str, Any]) -> None:
    st.markdown(f"## {U.t('about.title')}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("版本", APP_VERSION)
    c2.metric("素材文件", f"{int(stats.get('files_total') or 0):,}")
    c3.metric("人脸", f"{int(stats.get('faces') or 0):,}")
    c4.metric("上次扫描", (stats.get("last_scan") or "从未")[:16].replace("T", " "))

    st.markdown(f"### {U.t('about.stats')}")
    rows = [
        ("照片", f"{int(stats.get('photos') or 0):,}", f"已处理 {int(stats.get('photos_done') or 0):,}"),
        ("视频", f"{int(stats.get('videos') or 0):,}",
         f"已处理 {int(stats.get('videos_done') or 0):,} · 待处理 "
         f"{int(stats.get('videos_pending') or 0):,}"),
        ("镜头", f"{int(stats.get('shots') or 0):,}", ""),
        ("人脸", f"{int(stats.get('faces') or 0):,}",
         f"待编码 {int(stats.get('faces_pending_vector') or 0):,}"),
        ("缺失文件", f"{int(stats.get('files_missing') or 0):,}", "磁盘上找不到，记录保留"),
        ("编码失败", f"{int(stats.get('files_error') or 0):,}", ""),
    ]
    st.dataframe(pd.DataFrame(rows, columns=["项目", "数量", "说明"]),
                 use_container_width=True, hide_index=True)

    st.markdown(f"### {U.t('about.disk')}")
    _nv = 0
    try:
        import json as _json

        _raw = T.setting_get(cfg, "nvme_usage")
        _nv = int((_json.loads(_raw) or {}).get("bytes") or 0) if _raw else 0
    except Exception:  # noqa: BLE001
        _nv = 0
    st.dataframe(pd.DataFrame(_disk_rows(
        str(cfg.db_path), str(cfg.get("paths.nvme_root")),
        str(cfg.get("paths.logs")), str(cfg.get("paths.export")), _nv)),
        use_container_width=True, hide_index=True)

    st.markdown(f"### {U.t('about.schema')}")
    st.caption("启动时自动探测（表名/字段名不写死，直接从库里读）。")
    if st.button("重新探测"):
        _discover_schema.clear()
    schema = _discover_schema(str(cfg.db_path))
    st.session_state["_schema_cache"] = schema
    if schema.get("error"):
        st.error(schema["error"])
    for t in schema.get("tables", []):
        with st.expander(f"{t['name']}（约 {t['rows']:,} 行）"):
            st.code(", ".join(t["cols"]), language="text")
    st.caption("向量库：")
    for c in schema.get("collections", []):
        st.caption(f"`{c['name']}` — {c['points']:,} 个向量 · 维度 {c['dim']}")

    st.markdown(f"### {U.t('about.weights')}")
    st.dataframe(pd.DataFrame([
        {"用途": "画面理解", "模型": Path(str(cfg.get("semantic.model_path"))).name,
         "维度": str(cfg.get("semantic.dimension"))},
        {"用途": "结果重排", "模型": Path(str(cfg.get("semantic.rerank_model_path"))).name,
         "维度": "—"},
        {"用途": "人脸检测", "模型": Path(str(cfg.get("faces.det_onnx"))).name, "维度": "—"},
        {"用途": "人脸特征", "模型": Path(str(cfg.get("faces.rec_onnx"))).name,
         "维度": str(cfg.get("faces.dimension"))},
    ]), use_container_width=True, hide_index=True)

    with st.expander(U.t("about.tech")):
        st.markdown(
            "- 本地多模态向量模型（Qwen3-VL-Embedding）+ 本地重排模型\n"
            "- 人脸检测与识别（SCRFD + ArcFace，buffalo_l）\n"
            "- 向量数据库 Qdrant（int8 量化）· 元数据 SQLite（WAL）\n"
            "- 自动分组：UMAP 降维 + HDBSCAN 密度聚类\n"
            "- 界面 Streamlit · 视频抽帧 ffmpeg\n\n"
            "全部在本机运行，不上传任何素材与人脸。"
        )


def _thumb_path(cfg: Config, thumb: Optional[str]) -> Optional[Path]:
    if not thumb:
        return None
    p = Path(str(thumb))
    if p.is_absolute():
        return p if p.exists() else None
    cand = Path(str(cfg.get("paths.nvme_root"))) / p
    return cand if cand.exists() else None


def page_groups(cfg: Config) -> None:
    """人脸自动分组：按人浏览 + 低置信度审核 + 版本回退（规格 §6.5 / §15.6）。"""
    from . import cluster as C

    st.markdown("## 自动分组")
    cur = C.current_version(cfg)
    runs = C.version_history(cfg, limit=10)

    # ---- 常驻入口：不管有没有跑过，这里都能发起分组 ----
    active = active_task(cfg) is not None
    c0, c1 = st.columns([1, 3])
    with c0:
        label = "开始分组" if not cur else "重新分组"
        if st.button(label, type="primary", use_container_width=True, disabled=active,
                     help="读取全部人脸向量 → 降维 → 自动分组，产出新版本；"
                          "旧版本保留、可一键回退"):
            st.session_state["confirm_cluster"] = True
    with c1:
        st.caption("29 万张脸：降维约几十分钟到一两个小时（纯 CPU，可同时浏览其他页面）。"
                   + ("　⚠️ 有任务正在运行，先等它结束或停止它。" if active else ""))

    if st.session_state.get("confirm_cluster"):
        st.warning("重新分组会产出新版本并切换过去（旧版本保留、可随时回退）。确定现在开始吗？")
        cc1, cc2 = st.columns(2)
        if cc1.button("确定开始分组", type="primary", use_container_width=True):
            st.session_state["confirm_cluster"] = False
            if start_task(cfg, "cluster"):
                flash("分组任务已启动，进度见左侧「当前任务」。", "success")
                st.rerun()
        if cc2.button("取消", use_container_width=True):
            st.session_state["confirm_cluster"] = False
            st.rerun()

    if not cur or not runs:
        st.info(
            "还没有分组结果。\n\n"
            "点上面的「开始分组」：系统会把长得像的人脸归到一组，之后就能按人浏览。"
            "先用 2 万张试跑只要 10 秒，确认参数合适再跑全量。"
        )
        return

    info = next((r for r in runs if r["version"] == cur), runs[0])
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("分组数", f"{int(info['n_clusters'] or 0):,}")
    c2.metric("已归类人脸", f"{int(info['n_faces'] or 0):,}")
    c3.metric("未分组", f"{int(info['n_noise'] or 0):,}")
    c4.metric("生成时间", str(info["created_at"] or "")[:16].replace("T", " "))
    st.caption(f"当前版本 `{cur}` · 算法 {info['algo']}")

    with st.expander("版本历史与回退"):
        for r in runs:
            mark = "✅ 当前" if r["version"] == cur else ""
            cc1, cc2 = st.columns([3, 1])
            cc1.caption(f"`{r['version']}` · {int(r['n_clusters'] or 0):,} 组 · "
                        f"{int(r['n_faces'] or 0):,} 张 · {mark}")
            if r["version"] != cur and cc2.button("切到这个版本", key=f"sw_{r['version']}"):
                C.switch_version(cfg, r["version"])
                flash(f"已切到 {r['version']}", "success")
                st.rerun()
        st.caption("切换只改一个指针，秒级生效，可以随时切回来。")

    only_low = st.toggle("只看低置信度的组（需要人工确认的）", value=False)
    groups = C.groups_overview(cfg, cur, only_low=only_low, limit=240)
    if not groups:
        st.caption("没有符合条件的组。")
        return

    st.caption(f"共 {len(groups):,} 组（按人数从多到少）")
    ncols = 8
    for start in range(0, len(groups), ncols):
        cols = st.columns(ncols)
        for j, g in enumerate(groups[start:start + ncols]):
            with cols[j]:
                tp = _thumb_path(cfg, g.get("thumb_path"))
                if tp is not None:
                    st.image(str(tp), width="stretch")
                else:
                    st.markdown("<div style='height:64px'></div>", unsafe_allow_html=True)
                conf = float(g.get("confidence") or 0.0)
                st.caption(f"**{int(g.get('size') or 0)}** 张 · 相似 {conf:.2f}")

    st.divider()
    labels = {int(g["cluster_id"]): f"第 {int(g['cluster_id'])} 组 · {int(g['size'])} 张"
              for g in groups}
    pick = st.selectbox("查看某一组的全部成员", list(labels), format_func=labels.get)
    if pick is not None:
        members = _group_members(cfg, cur, int(pick), limit=48)
        if not members:
            st.caption("这一组没有可取出的成员。")
            return
        for start in range(0, len(members), ncols):
            cols = st.columns(ncols)
            for j, m in enumerate(members[start:start + ncols]):
                with cols[j]:
                    tp = _thumb_path(cfg, m.get("thumb_path"))
                    if tp is not None:
                        st.image(str(tp), width="stretch")
                    st.caption(f"`{int(m['face_id'])}`")
        # ---- 人工修正（规格 §6.5：聚类一定会错，错得有出口）----
        st.markdown("#### 修正这一组")
        col1, col2 = st.columns([3, 1])
        cur_name = next((g.get("name") for g in groups
                         if int(g["cluster_id"]) == int(pick)), None)
        nm = col1.text_input("给这个人起个名字", value=str(cur_name or ""),
                             placeholder="例如：张三", key="gp_name")
        if col2.button("保存名字", use_container_width=True):
            try:
                C.set_name(cfg, int(pick), nm)
                flash(f"第 {pick} 组已命名为「{nm}」。", "success")
            except Exception as exc:  # noqa: BLE001
                flash(f"命名失败：{exc}", "error")
            st.rerun()

        col3, col4 = st.columns([3, 1])
        others = [int(g["cluster_id"]) for g in groups
                  if int(g["cluster_id"]) != int(pick)]
        tgt = col3.selectbox("把这一组合并到…", others, key="gp_merge_tgt",
                             format_func=lambda c: f"第 {c} 组 · "
                                                   f"{int(next(g['size'] for g in groups if int(g['cluster_id']) == c))} 张")
        if col4.button("合并过去", use_container_width=True, type="primary"):
            try:
                r = C.merge_groups(cfg, int(pick), int(tgt))
                flash(f"已把 {r['moved']} 张脸并入第 {tgt} 组（可撤销）。", "success")
            except Exception as exc:  # noqa: BLE001
                flash(f"合并失败：{exc}", "error")
            st.rerun()

        picked_faces = st.multiselect(
            "要拆出去的人脸（选中的会成立一个新组）",
            [int(m["face_id"]) for m in members], key="gp_split_faces",
            format_func=lambda f: f"face_id {f}")
        c5, c6 = st.columns(2)
        if c5.button("拆分出去", disabled=not picked_faces):
            try:
                r = C.split_group(cfg, int(pick), picked_faces)
                flash(f"已拆出 {r['moved']} 张脸，成立第 {r['new']} 组（可撤销）。", "success")
            except Exception as exc:  # noqa: BLE001
                flash(f"拆分失败：{exc}", "error")
            st.rerun()
        if c6.button("撤销上一次合并/拆分"):
            try:
                r = C.undo_last(cfg)
                flash(f"已撤销「{r['undone']}」，影响 {r['faces']} 张脸。", "success")
            except Exception as exc:  # noqa: BLE001
                flash(f"撤销失败：{exc}", "error")
            st.rerun()


def _group_members(cfg: Config, version: str, cluster_id: int,
                   limit: int = 48) -> List[Dict[str, Any]]:
    from . import db as D

    conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        rows = conn.execute(
            "SELECT fc.face_id, fc.confidence, fr.thumb_path, fr.quality "
            "FROM face_clusters fc JOIN face_records fr ON fr.face_id = fc.face_id "
            "WHERE fc.version=? AND fc.cluster_id=? AND fr.status='alive' "
            "ORDER BY fr.quality DESC LIMIT ?", (version, cluster_id, limit)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


@st.cache_data(ttl=600, show_spinner=False)
def _discover_schema(db_path: str) -> Dict[str, Any]:
    """探测 SQLite 表结构与 Qdrant collection 列表（只读）。

    缓存键用 db_path（Config 不可哈希）—— 进函数后用 load_config() 重建一份 cfg，
    只在缓存未命中时执行一次，代价可忽略。
    """
    from .common import load_config

    cfg = load_config()

    out: Dict[str, Any] = {"tables": [], "collections": [], "error": None}
    try:
        c = conn(cfg)
        try:
            names = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            for n in names:
                # COUNT(*) 在 160 万行表上要全表扫；MAX(rowid) 走索引是 O(1)。
                # 删除过的表会有空洞，所以界面上标「约」。
                try:
                    rows = int(c.execute(f"SELECT MAX(rowid) FROM [{n}]").fetchone()[0] or 0)
                except Exception:  # noqa: BLE001
                    rows = 0
                cols = [r[1] for r in c.execute(f"PRAGMA table_info([{n}])")]
                out["tables"].append({"name": n, "rows": rows, "cols": cols})
        finally:
            c.close()
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"读取数据库结构失败：{exc}"
    try:
        from .vector_store import open_store

        store = open_store(cfg)
        for name in (cfg.get("semantic.collection"), cfg.get("faces.collection")):
            try:
                info = store.collection_info(name)
                out["collections"].append({
                    "name": name,
                    "points": int(info.get("points_count") or info.get("points") or 0),
                    "dim": info.get("dim") or "?",
                })
            except Exception:  # noqa: BLE001
                out["collections"].append({"name": name, "points": 0, "dim": "?"})
    except Exception as exc:  # noqa: BLE001
        out["error"] = (out["error"] or "") + f" 向量库不可达：{exc}"
    return out