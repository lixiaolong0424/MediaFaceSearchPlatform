# -*- coding: utf-8 -*-
"""补丁：僵尸任务（进程已死但 state 仍是 running）自愈。

症状：停止按钮点了没反应；重启界面后仍显示「抽取画面 · 已用 13 分 25 秒」。
原因：进程被强杀时来不及写 state；而回收只在「点开始按钮」时才跑，
      所以界面一直拿着那条僵尸任务在画进度条。
"""
import io

# ---- 1) ui_actions.stop_task：请求取消后确认是否真有人响应 ----
p = "src/ui_actions.py"
s = io.open(p, encoding="utf-8").read()
old = '''def stop_task(cfg: Config, task_id: str) -> None:
    T.init(cfg)
    T.request_cancel(cfg, task_id)
    flash("已发出停止请求，正在等当前文件处理完…", "warning")'''
new = '''def stop_task(cfg: Config, task_id: str) -> None:
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
    flash("任务进程已经不在，已直接标记为停止。", "success")'''
assert old in s
s = s.replace(old, new, 1)

# ---- 2) active_task 顺手回收僵尸（每次界面渲染都调，很便宜）----
old2 = '''def active_task(cfg: Config) -> Optional[Dict[str, Any]]:
    """当前正在跑（或排队）的任务。界面据此禁用所有开始按钮。"""
    T.init(cfg)
    return T.running(cfg)'''
new2 = '''def active_task(cfg: Config) -> Optional[Dict[str, Any]]:
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
    return T.running(cfg)'''
assert old2 in s
s = s.replace(old2, new2, 1)
io.open(p, "w", encoding="utf-8", newline="\n").write(s)
print("ui_actions patched")
