# -*- coding: utf-8 -*-
"""收拾 2026-10-24 那次失败留下的烂摊子，并用修好的代码 + 全速模式重跑。

1. 停掉 20:03 那个任务（它用的是有 bug 的代码，会重演同样的失败）
2. 把被 bug 打坏的文件改回 pending，清掉它们的死信（重试次数已烧掉）
3. 重新启用全速模式（配置刚才被误重置成 balanced）
4. 起新扫描
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import settings_store as SS  # noqa: E402
from src import taskplan  # noqa: E402
from src import tasks as T  # noqa: E402
from src.common import load_config, local_now_iso  # noqa: E402
from src.ui_pages import _performance_values  # noqa: E402


def main() -> int:
    cfg = load_config()
    T.init(cfg)

    # --- 1) 停掉坏任务 ---
    cur = T.running(cfg)
    if cur:
        tid, pid = cur["task_id"], int(cur.get("pid") or 0)
        print(f"停止 {tid}（{cur['stage']} {cur['done']}/{cur['total']}）")
        T.request_cancel(cfg, tid)
        time.sleep(2)
        if pid and T._pid_alive(pid):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=25,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for _ in range(20):
                if not T._pid_alive(pid):
                    break
                time.sleep(0.5)
        for _ in range(6):
            try:
                T.finish(cfg, tid, "cancelled", message="已停止：修复 prefetched 作用域 bug 后重跑")
                break
            except sqlite3.OperationalError:
                time.sleep(2)

    # --- 2) 清掉被 bug 打坏的状态 ---
    con = sqlite3.connect(str(cfg.db_path))
    con.row_factory = sqlite3.Row
    bad = [int(r[0]) for r in con.execute(
        "SELECT file_id FROM stage_status WHERE stage='extract' "
        "AND (error LIKE '%prefetched%' OR error LIKE '%全片抽帧失败%')")]
    print(f"被 bug 打坏的文件: {len(bad)} 个")
    now = local_now_iso()
    if bad:
        q = ",".join("?" * len(bad))
        n1 = con.execute(f"UPDATE files SET status='pending', updated_at=? "
                         f"WHERE file_id IN ({q}) AND status='error'", [now, *bad]).rowcount
        n2 = con.execute(f"DELETE FROM dead_letter WHERE file_id IN ({q})", bad).rowcount
        n3 = con.execute(f"DELETE FROM stage_status WHERE stage='extract' AND file_id IN ({q})",
                         bad).rowcount
        con.commit()
        print(f"  改回 pending {n1} 条；清死信 {n2} 条；清阶段状态 {n3} 条")
    pend = dict(con.execute("SELECT kind, COUNT(*) FROM files WHERE status='pending' GROUP BY kind"))
    print("  当前待处理:", pend)
    con.close()

    # --- 3) 重新打开全速模式 ---
    SS.save(cfg, _performance_values(True), who="界面/全速模式")
    cfg = load_config()
    print("全速配置:", {k: cfg.get(k) for k in (
        "performance.mode", "performance.photo_prefetch",
        "performance.video_workers", "performance.encode_batch", "performance.encode_group")})

    # --- 4) 起新扫描 ---
    steps = taskplan.plan_for("scan_new", cfg, {"auto_process": True})
    plan = [{"script": s.script, "args": list(s.args), "code": s.code, "event": s.event}
            for s in steps]
    tid = T.create(cfg, kind="scan_new", title=taskplan.title_for("scan_new"),
                   plan=plan, total=len(plan))
    log = (ROOT / "logs" / f"task_{tid}.log").open("a", encoding="utf-8")
    subprocess.Popen([sys.executable, str(ROOT / "run_task.py"), tid], cwd=str(ROOT),
                     stdout=log, stderr=subprocess.STDOUT,
                     creationflags=0x08000000 if sys.platform == "win32" else 0)
    print(f"已启动新扫描 {tid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
