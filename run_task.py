"""run_task.py · 任务执行器（规格 v5.3 §3.2 任务队列的实际执行者）。

界面点一个按钮时发生的事：
    1) app.py 往 tasks 表插一条 queued 任务，plan 里写好「要跑哪几步」；
    2) app.py 用子进程启动本脚本：`python run_task.py <task_id>`；
    3) 本脚本逐步启动各阶段脚本（每步一个独立子进程，带 --task-id 回报进度）；
    4) 全部成功 → success；任一步返回非 0 → failed；被用户停止 → cancelled。

为什么执行器要独立成一个进程：
    * 浏览器关掉之后任务还能继续跑（30 小时的入库不该因为手滑关页面而中断）；
    * 界面刷新不会杀掉任务；
    * 每个阶段脚本是孙进程，显存随步骤结束而释放。

用法：
    .venv\\Scripts\\python.exe run_task.py <task_id>
    .venv\\Scripts\\python.exe run_task.py --list
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import tasks as T  # noqa: E402
from src.common import get_logger, load_config  # noqa: E402
from src import taskplan  # noqa: E402


def _python() -> str:
    return sys.executable


def _spawn(step: dict, task_id: str, log_fh) -> subprocess.Popen:
    args = [_python(), str(_ROOT / step["script"]), *map(str, step.get("args") or []),
            "--task-id", task_id]
    log_fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 {step['script']} "
                 f"{' '.join(map(str, step.get('args') or []))} =====\n")
    log_fh.flush()
    flags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW：不弹黑框
    return subprocess.Popen(args, cwd=str(_ROOT), stdout=log_fh,
                            stderr=subprocess.STDOUT, creationflags=flags)


def run(task_id: str) -> int:
    cfg = load_config()
    T.init(cfg)
    task = T.get(cfg, task_id)
    if task is None:
        print(f"任务不存在：{task_id}", file=sys.stderr)
        return 2
    payload = T.plan_of(task)
    steps = payload.get("steps") or []
    log = get_logger("run_task", cfg)
    log_dir = cfg.log_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"task_{task_id}.log"

    rep = T.Reporter(cfg, task_id)
    T._REPORTER[0] = rep          # noqa: SLF001 - 执行器自己就是「当前任务」
    rep.start()

    if not steps:
        rep.fail(RuntimeError("这个任务没有任何步骤（计划为空）"))
        return 2

    total = len(steps)
    started = time.monotonic()
    log.info("任务 %s「%s」开始，共 %d 步 → %s", task_id, task["title"], total, log_path)

    with open(log_path, "a", encoding="utf-8", errors="replace", buffering=1) as fh:
        for ix, step in enumerate(steps, 1):
            code = step.get("code") or "finalize"
            try:
                T.cancelled()      # 步与步之间也响应停止
            except T.TaskCancelled:
                rep.cancel()
                return T.CANCEL_EXIT_CODE
            rep.stage(code, ix=ix, n=total)
            t0 = time.monotonic()
            try:
                proc = _spawn(step, task_id, fh)
            except Exception as exc:  # noqa: BLE001
                rep.fail(exc)
                log.error("启动 %s 失败：%s", step["script"], exc)
                return 3
            T.update(cfg, task_id, pid=proc.pid)
            rc = proc.wait()
            took = time.monotonic() - t0
            fh.write(f"===== {step['script']} 退出码 {rc}，耗时 {took:.1f}s =====\n")
            fh.flush()

            if rc == T.CANCEL_EXIT_CODE:
                rep.cancel()
                log.warning("步骤 %s 被用户停止", step["script"])
                return T.CANCEL_EXIT_CODE
            if rc != 0:
                rep.fail(RuntimeError(f"{step['script']} 退出码 {rc}，详见 {log_path.name}"))
                log.error("步骤 %s 失败（退出码 %d），任务中止", step["script"], rc)
                return rc
            if step.get("event"):
                T.emit(cfg, step["event"],
                       {"task_id": task_id, "step": step["script"]}, task_id=task_id)
            log.info("步骤 %d/%d 完成：%s（%.1fs）", ix, total, step["script"], took)

    took = time.monotonic() - started
    rep.success(message=f"已完成 {total} 步，用时 {took/60:.1f} 分钟",
                event="task_state")
    T.emit(cfg, "library_changed", {"task_id": task_id, "final": True}, task_id=task_id)
    log.info("任务 %s 全部完成，用时 %.1f 分钟", task_id, took / 60)
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 2
    if argv[0] == "--list":
        cfg = load_config()
        for t in T.recent(cfg, days=7, limit=30):
            print(f"{t['task_id']}  {t['state']:<9} {t['title']:<16} "
                  f"{t['done']}/{t['total']}  {t['message'] or ''}")
        return 0
    return run(argv[0])


if __name__ == "__main__":
    raise SystemExit(main())
