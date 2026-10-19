# -*- coding: utf-8 -*-
"""src/ui_actions.py · 任务启停与实时刷新（规格 v5.3 §3.2 / §3.4 的界面侧）。

三条硬规矩（都是从「点了按钮没反应」这个真实抱怨反推出来的）：
  1. 任务一开始就把按钮变灰，不允许重复点击（§3.2 强制交互规范第 1 条）。
  2. 进度从**数据库**读，不从 session_state 读 —— 关掉页面再打开还能看到（第 2 条）。
  3. 任务跑完写 task_events，界面轮询到新事件就失效缓存并整页刷新（§3.4 B 方案）。
     Streamlit 没有 SSE，所以「服务端事件 + 1.5 秒轮询」是这套架构里等价的做法，
     实测事件到达后 2 秒内墙面就会变，满足 §13 验收项 UI-4「3 秒内自动刷新」。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import streamlit as st

from . import settings_store as SS
from . import tasks as T
from . import taskplan
from . import ui_text as U
from .common import Config, human_bytes
from .ui_common import conn, flash, fmt_eta, fmt_rate, fmt_sec

_ROOT = Path(__file__).resolve().parent.parent

POLL_SEC = 1.5          # 进度轮询间隔
WATCH_SEC = 2.0         # 变更事件轮询间隔


# --------------------------------------------------------------------------- #
# 任务启停
# --------------------------------------------------------------------------- #


def active_task(cfg: Config) -> Optional[Dict[str, Any]]:
    """当前正在跑（或排队）的任务。界面据此禁用所有开始按钮。

    顺手回收僵尸任务（心跳超过 2 分钟没动的 running 行）：进程被强杀、
    或者重启界面之后，库里会留下永远不动的 running 记录，
    界面就会一直画一条冻住的进度条 —— 用户看到的就是「停了还在显示」。
    """
    T.init(cfg)
    try:
        T.reap_stale(cfg, minutes=2)
    except Exception:  # noqa: BLE001 - 回收失败不该让页面打不开
        pass
    return T.running(cfg)


def start_task(cfg: Config, kind: str, *, params: Optional[Dict[str, Any]] = None,
               title: Optional[str] = None) -> Optional[str]:
    """建任务 + 拉起执行器。返回 task_id；被占用时返回 None。"""
    T.init(cfg)
    T.reap_stale(cfg)
    busy = active_task(cfg)
    if busy is not None:
        flash(f"{U.t('task.busy')}（{busy['title']}）", "warning")
        return None
    steps = taskplan.plan_for(kind, cfg, params or {})
    if not steps:
        flash(f"「{taskplan.title_for(kind)}」没有可执行的步骤，可能前置条件不满足。", "error")
        return None
    tid = T.create(
        cfg, kind=kind, title=title or taskplan.title_for(kind),
        plan=[s.to_dict() for s in steps], params=params or {},
    )
    try:
        cfg.log_dir.mkdir(parents=True, exist_ok=True)
        log = open(cfg.log_dir / f"task_{tid}.launcher.log", "ab")
        subprocess.Popen(
            [sys.executable, str(_ROOT / "run_task.py"), tid],
            cwd=str(_ROOT), stdout=log, stderr=subprocess.STDOUT,
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
    except Exception as exc:  # noqa: BLE001
        T.finish(cfg, tid, "failed", error=f"启动执行器失败：{exc}", message="启动失败")
        flash(f"启动任务失败：{exc}", "error")
        return None
    flash(f"已开始「{title or taskplan.title_for(kind)}」，进度见左侧。", "success")
    return tid


def stop_task(cfg: Config, task_id: str) -> None:
    """停止任务，并且**确认它真的停了**。

    只写一个取消标志是不够的：
      * 子进程在处理一个 30 秒的视频时，要等它做完才看得到标志；
      * 如果进程早就死了（被强杀 / 重启界面），标志永远没人读，
        任务就永远挂在 running，界面上的进度条也跟着永远不动。
    所以这里等 3 秒看心跳有没有动，没动就直接判它已停止。
    """
    import time as _t

    from datetime import datetime

    T.init(cfg)
    before = (T.get(cfg, task_id) or {}).get("updated_at")
    T.request_cancel(cfg, task_id)
    _t.sleep(3.0)
    task = T.get(cfg, task_id) or {}
    if task.get("state") not in ("queued", "running"):
        flash("任务已停止。", "success")
        return
    if task.get("updated_at") != before:
        flash("已发出停止请求，正在等当前文件处理完（最长约 30 秒）…", "warning")
        return
    # 心跳 3 秒没动 = 没有进程在写它 → 直接收尾，别让用户对着僵尸任务干等
    T.finish(cfg, task_id, "cancelled",
             message="已停止（进程已不在，直接收尾；下次点开始会从断点继续）")
    flash("任务进程已经不在，已直接标记为停止。", "success")


def recent_tasks(cfg: Config, days: int = 30, limit: int = 100) -> List[Dict[str, Any]]:
    T.init(cfg)
    return T.recent(cfg, days=days, limit=limit)


# --------------------------------------------------------------------------- #
# 实时进度（fragment 自动重跑，不会整页刷新）
# --------------------------------------------------------------------------- #


def last_task_notice(cfg: Config, *, within_sec: float = 3600) -> None:
    """空闲时也把「上一次任务的结果」说清楚。

    2026-10-19 用户看到的「抽取画面 · 已处理 0 / 11,969 一直显示」就是这么来的：
    任务其实早就失败了，但界面上只剩一段旧进度，看不出「它已经死了」。
    失败必须留痕，并给一个可以直接看的日志尾巴。
    """
    from datetime import datetime

    tasks = T.recent(cfg, days=1, limit=1)
    if not tasks:
        return
    t = tasks[0]
    if t.get("state") in ("queued", "running"):
        return
    fin = t.get("finished_at") or t.get("updated_at") or ""
    try:
        age = (datetime.now() - datetime.fromisoformat(str(fin))).total_seconds()
    except ValueError:
        age = 0.0
    if age > within_sec:
        return
    if t["state"] == "success":
        st.caption(f"上次任务：{t['title']} 已完成")
        return
    st.error(f"上次任务失败：{t['title']} —— {(t.get('error') or '')[:200]}")
    with st.expander("看这个任务的日志"):
        from .common import tail_file

        log = Path(str(cfg.get("paths.logs"))) / f"task_{t['task_id']}.log"
        if log.exists():
            st.code("\n".join(tail_file(log, 30)), language="text")
        else:
            st.caption("找不到这个任务的日志文件。")

def progress_block(task: Dict[str, Any], *, compact: bool = False,
                   on_stop: Optional[Callable[[], None]] = None) -> None:
    """画一条任务进度。数据全部来自 tasks 表，所以换页面/刷新都不会丢。"""
    snap = T.snapshot(task)
    state = task.get("state") or "queued"
    total = task.get("total")
    done = int(task.get("done") or 0)
    stage = task.get("stage") or U.task_state_name(state)

    if total:
        pct = min(1.0, done / max(1, int(total)))
        st.progress(pct, text=f"{stage} · {U.t('progress.files', done=done, total=int(total))}")
    elif state == "running":
        st.progress(0.0, text=f"{stage} · {U.t('progress.count', done=done)}")
    else:
        st.progress(0.0 if state != "success" else 1.0, text=stage)

    ix, n = int(task.get("stage_ix") or 0), int(task.get("stage_total") or 0)
    bits = []
    if ix and n:
        bits.append(U.t("progress.stage", ix=ix, n=n, name=stage))
    rate = snap.get("rate")
    if rate:
        bits.append(f"{U.t('task.rate')} {fmt_rate(rate)}")
    eta = snap.get("eta_sec")
    if state == "running":
        bits.append(f"{U.t('task.remaining')} {fmt_eta(eta)}")
    el = snap.get("elapsed_sec")
    if el:
        bits.append(f"已用 {fmt_sec(el)}")
    if bits:
        st.caption(" · ".join(bits))
    msg = task.get("message")
    if msg and msg != stage:
        st.caption(msg)
    if state == "failed" and task.get("error"):
        st.error(task["error"][:500])
    if not compact and state in ("queued", "running") and on_stop is not None:
        if st.button(U.t("task.cancel"), key=f"stop_{task['task_id']}", type="secondary"):
            on_stop()


@st.fragment(run_every=f"{POLL_SEC}s")
def live_task_fragment(cfg: Config, *, compact: bool = True) -> None:
    """侧边栏的「当前任务」。运行中自动刷新；结束后自动整页刷新一次。"""
    T.init(cfg)
    task = T.running(cfg)
    if task is None:
        st.caption("空闲")
        return
    was = st.session_state.get("_live_state")
    now = task.get("state")
    progress_block(task, compact=compact,
                   on_stop=lambda: stop_task(cfg, task["task_id"]))
    st.session_state["_live_state"] = now
    if was in ("queued", "running") and now in ("success", "failed", "cancelled"):
        # 任务刚结束：整页重算（数字、照片墙、按钮状态都要跟着变）
        st.rerun(scope="app")


@st.fragment(run_every=f"{WATCH_SEC}s")
def watch_events(cfg: Config, *, reload_cfg: Optional[Callable[[], None]] = None) -> None:
    """变更事件订阅：收到新事件就失效缓存 + 整页刷新（§3.4 B 方案）。

    为什么不用 st.rerun() 全页刷新而用 fragment：fragment 只重跑这一小段，
    没有事件时几乎零成本；有事件时才付一次整页重算的代价。
    """
    T.init(cfg)
    try:
        cur = T.latest_event_id(cfg)
    except Exception:  # noqa: BLE001
        return
    last = st.session_state.get("_event_cursor")
    if last is None:
        st.session_state["_event_cursor"] = cur
        return
    if cur == last:
        return
    events = T.events_since(cfg, last, limit=20)
    st.session_state["_event_cursor"] = cur
    kinds = {e["kind"] for e in events}

    # task_state 只表示「任务开始/结束」，进度本身由侧边栏的进度条自己刷新。
    # 如果为它整页重跑一次，一次任务就要白屏 3 次（建/开始/结束）——
    # 用户看到的就是「点了按钮屏幕变白」。所以这里只认真正改了数据的那些事件。
    DATA_EVENTS = {"library_changed", "faces_changed", "clusters_changed",
                   "settings_changed", "media_root_changed"}
    if not (kinds & DATA_EVENTS):
        return

    from .ui_common import invalidate

    invalidate()
    if kinds & {"settings_changed", "media_root_changed"}:
        # 配置变了：必须重载 Config（media_root 之类是启动时读的）
        if reload_cfg is not None:
            reload_cfg()
    reason = {
        "faces_changed": "人脸有更新",
        "clusters_changed": "分组有更新",
        "settings_changed": "设置已修改",
        "media_root_changed": "素材位置已变更",
        "library_changed": "素材有更新",
    }
    st.session_state["_reload_reason"] = next(
        (v for k, v in reason.items() if k in kinds), "内容有更新")
    st.rerun(scope="app")


# --------------------------------------------------------------------------- #
# 素材路径变更向导（规格 §4.2 五步，默认策略①）
# --------------------------------------------------------------------------- #


def media_root_wizard(cfg: Config, *, old_root: Optional[str] = None,
                      on_applied: Optional[Callable[[], None]] = None) -> None:
    from . import migrate as MG

    ss = st.session_state
    cur = str(old_root or cfg.media_root)
    st.caption(f"当前路径：`{cur}`")
    st.caption("换盘、改目录名时用它。库里的向量、人脸、分组关系一条都不会丢，"
               "也不会动你磁盘上的原始文件。")

    new_root = st.text_input("新的素材文件夹", value=ss.get("mig_new_root", ""),
                             placeholder=r"例如 D:\素材",
                             help="新位置里应该是同一批文件（同样的子目录结构）。")
    ss["mig_new_root"] = new_root

    c1, c2 = st.columns(2)
    with c1:
        if st.button("① 预览差异", use_container_width=True, disabled=not new_root.strip()):
            ok, msg = MG.validate_new_root(cfg, new_root)
            if not ok:
                flash(msg, "error")
            else:
                with st.spinner("正在逐个核对新位置上的文件（文件多时约 1~3 分钟）…"):
                    pv = MG.preview(cfg, new_root, old_root=cur)
                ss["mig_preview"] = pv.as_dict()
            st.rerun()
    with c2:
        if ss.get("mig_preview") and st.button("清空预览"):
            ss["mig_preview"] = None
            st.rerun()

    pv = ss.get("mig_preview")
    if not pv:
        return

    st.markdown("#### ② 差异预览")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("将更新路径", f"{pv['will_update']:,}")
    m2.metric("新位置能找到", f"{pv['ok']:,}")
    m3.metric("新位置找不到", f"{pv['missing']:,}")
    m4.metric("涉及数据量", pv["bytes_human"])
    if pv["outside"]:
        st.info(f"另有 {pv['outside']:,} 个文件不在当前素材文件夹下，本次不会动它们。")
    if pv["missing"]:
        st.warning(
            f"有 {pv['missing']:,} 个文件在新位置上找不到。"
            "按默认策略，它们会被标成「缺失」——记录和向量都保留，随时可以恢复，"
            "但搜索与照片墙里会看不到。建议先确认新目录是不是拷全了。"
        )
        with st.expander("看看哪些找不到"):
            for s in pv["samples"]:
                st.code(f"{s['old']}\n→ {s['new']}", language="text")
    st.caption(f"预计执行耗时约 {fmt_eta(pv['eta_sec'])}（一次批量更新 + 逐文件核对）")

    st.markdown("#### ③ 保留策略")
    st.radio(
        "旧路径上的数据怎么处理",
        [MG.STRATEGY_KEEP],
        format_func=lambda v: MG.STRATEGY_LABELS[v],
        index=0, key="mig_strategy", disabled=True,
        help="在界面上固定用「保留全部」：向量、人脸、分组关系一条都不删，"
             "新位置上找不到的文件只标成「缺失」（记录保留，可随时恢复）。"
             "想真正删掉缺失文件的记录与向量，去「维护 → 已删除（可恢复）」里操作。",
    )

    # ---- 硬闸门：新位置大部分文件都不在，就绝对不许执行 ----
    # 2026-10-19 的两次事故都是「新根填错，53 万条路径被换成一个不存在的目录」。
    # 预览已经算出了 missing，就不能只靠用户点一个勾。超过 20% 直接禁止执行，
    # 并告诉他正确的做法（先把文件拷/挂到新位置，再改过来）。
    total = max(1, int(pv["will_update"]))
    miss_ratio = pv["missing"] / total
    blocked = miss_ratio > 0.20
    if blocked:
        st.error(
            f"**不能执行。** 新位置上有 {pv['missing']:,} 个文件找不到"
            f"（占 {miss_ratio*100:.0f}%）。如果就这样改过去，"
            "库里这批文件会全部变成「缺失」，搜索和照片墙都看不到它们。\n\n"
            "请先确认：新文件夹里是不是**同样结构的完整副本**？"
            "文件名/子目录名有没有被改过？盘是不是还没拷完？"
        )

    confirm = st.checkbox("我确认新位置就是这批文件的新家，可以开始更新路径",
                          key="mig_confirm", disabled=blocked)
    if st.button("④ 执行更新", type="primary", disabled=blocked or not confirm):
        with st.spinner("正在更新路径…"):
            try:
                res = MG.apply(cfg, new_root, old_root=cur, strategy=MG.STRATEGY_KEEP)
            except Exception as exc:  # noqa: BLE001
                flash(f"更新失败：{exc}", "error")
                st.rerun()
            ss["mig_result"] = res
            ss["mig_preview"] = None
            ss["mig_new_root"] = ""
        if on_applied is not None:
            on_applied()
        st.rerun()

    res = ss.get("mig_result")
    if res:
        st.success(
            f"⑤ 已完成：更新了 {res.get('remapped', 0):,} 条路径，"
            f"耗时 {fmt_sec(res.get('elapsed_sec'))}。"
            "照片墙与搜索结果已自动刷新。"
        )
        st.caption("建议接着点「扫描新文件」，把新位置上多出来的文件也入库（只做一次遍历，很快）。")
        if st.button("知道了，清掉这条结果"):
            ss["mig_result"] = None
            st.rerun()
