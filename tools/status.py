# -*- coding: utf-8 -*-
"""打印当前任务状态（干净输出，供命令行核对）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import tasks as T  # noqa: E402
from src.common import load_config  # noqa: E402

cfg = load_config()
T.init(cfg)
t = T.running(cfg)
print("活动任务:", (t or {}).get("task_id"))
if t:
    s = T.snapshot(t)
    print("  阶段:", t.get("stage_ix"), "/", t.get("stage_total"), t.get("stage"))
    print("  进度:", t.get("done"), "/", t.get("total"))
    print("  速度:", s.get("rate"), "文件/秒 | 本步剩余:", s.get("eta_sec"), "秒")
    print("  消息:", str(t.get("message"))[:80])
    print("  pid:", t.get("pid"), "存活:", T._pid_alive(t.get("pid")))
    plan = json.loads(t.get("plan") or "{}")
    print("  步骤:", [x["script"] for x in plan.get("steps", [])])
else:
    print("  （没有运行中的任务）")
    rec = T.recent(cfg, days=2, limit=3)
    for r in rec:
        print("  最近:", r["task_id"], r["title"], r["state"], f"{r['done']}/{r['total']}")
