# -*- coding: utf-8 -*-
"""补丁：任务页
  1) 去掉重复的「停止任务」按钮（progress_block 已经画了一个）
  2) 任务页的进度自己刷新（原来只有侧边栏有 fragment，任务页不刷新）
"""
import io

p = "src/ui_pages.py"
s = io.open(p, encoding="utf-8").read()

start = s.index("def page_tasks(cfg: Config) -> None:")
end = s.index("    st.divider()\n    st.markdown(f\"### {U.t('task.history')}\")")

new_head = '''def page_tasks(cfg: Config) -> None:
    st.markdown(f"## {U.t('nav.tasks')}")
    _tasks_live(cfg)

'''
s = s[:start] + new_head + s[end:]

frag = '''@st.fragment(run_every="2s")
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


'''
anchor = "def page_tasks(cfg: Config) -> None:"
s = s.replace(anchor, frag + anchor, 1)
io.open(p, "w", encoding="utf-8", newline="\n").write(s)
print("ui_pages patched")
