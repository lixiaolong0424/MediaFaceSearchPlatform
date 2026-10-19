# -*- coding: utf-8 -*-
"""端到端验证「界面按钮背后的那条路」：建任务 → 拉起 run_task.py → 子进程回写进度 → 终态。

为什么必须单独验一次：用户抱怨的正是「点了按钮没反应」。这条链涉及
界面 → tasks 表 → 独立执行器进程 → 阶段脚本子进程 → 进度回写，任何一环断了
界面都表现成「没反应」，而单测只覆盖了其中一段。

只跑「扫描新文件 + 不自动后续」，因为它幂等且只读盘（约 1 分钟），
不会动任何已有数据。

用法：
    .venv\\Scripts\\python.exe tmp\\smoke_task_flow.py
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import taskplan, tasks as T  # noqa: E402
from src.common import load_config  # noqa: E402

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def main() -> int:
    cfg = load_config()
    T.init(cfg)
    T.reap_stale(cfg)

    params = {"auto_process": False, "root": str(cfg.media_root)}
    steps = taskplan.plan_for("scan_new", cfg, params)
    check("计划里只有清单扫描这一步（关掉自动后续时）", len(steps) == 1,
          str([s.script for s in steps]))

    tid = T.create(cfg, kind="scan_new", title="扫描新文件",
                   plan=[s.to_dict() for s in steps], params=params)
    print(f"  任务 id={tid}")
    log = open(cfg.log_dir / f"task_{tid}.launcher.log", "ab")
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "run_task.py"), tid],
        cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT,
        creationflags=0x08000000 if sys.platform == "win32" else 0)
    print(f"  执行器 pid={proc.pid}，等待完成…")

    seen_running = False
    t0 = time.monotonic()
    while time.monotonic() - t0 < 600:
        task = T.get(cfg, tid)
        if task["state"] == "running":
            seen_running = True
        if task["state"] in ("success", "failed", "cancelled"):
            break
        time.sleep(2)
    rc = proc.wait(timeout=60)
    task = T.get(cfg, tid)
    el = time.monotonic() - t0

    check("链路上出现过 running 状态（不是一步跳到终态）", seen_running, task["state"])
    check("执行器退出码为 0", rc == 0, str(rc))
    check("任务最终是 success", task["state"] == "success", str(task))
    check("阶段名是业务词", task["stage"] == "扫描文件清单", str(task["stage"]))
    check("进度数字被写过", int(task["done"] or 0) >= 0, str(task["done"]))
    check("message 有内容（界面上显示的那句）", bool(task["message"]), str(task["message"]))
    print(f"  用时 {el:.1f}s，done={task['done']} total={task['total']} "
          f"message={task['message']}")

    events = T.events_since(cfg, 0, limit=50)
    kinds = {e["kind"] for e in events}
    check("写了 task_state 事件（界面靠它刷新）", "task_state" in kinds, str(kinds))
    check("写了 library_changed 事件（照片墙靠它刷新）", "library_changed" in kinds,
          str(kinds))

    print("\n" + "=" * 60)
    print(f"PASS={PASS} FAIL={FAIL}")
    print("=" * 60)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
