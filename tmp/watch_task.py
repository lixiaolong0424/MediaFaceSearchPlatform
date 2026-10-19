# -*- coding: utf-8 -*-
"""看运行中的任务是否真的在动（间隔 8 秒采样两次），并给出「卡住」的判定依据。"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.common import load_config  # noqa: E402


def sample(db):
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    try:
        t = con.execute("SELECT task_id, state, stage, done, total, stage_ix, stage_total, "
                        "updated_at, message FROM tasks WHERE state IN ('queued','running') "
                        "ORDER BY created_at DESC LIMIT 1").fetchone()
        pend = con.execute("SELECT kind, COUNT(*) n FROM files "
                           "WHERE status IN ('pending','error') GROUP BY kind").fetchall()
        return (dict(t) if t else None), {r["kind"]: r["n"] for r in pend}
    finally:
        con.close()


def main() -> int:
    cfg = load_config()
    a, pa = sample(cfg.db_path)
    print("第一次采样:", a)
    print("待处理:", pa)
    time.sleep(8)
    b, pb = sample(cfg.db_path)
    print("8 秒后    :", b)
    print("待处理:", pb)
    if a is None or b is None:
        return 0
    done_moved = (b.get("done") or 0) - (a.get("done") or 0)
    hb_moved = a.get("updated_at") != b.get("updated_at")
    print(f"\n结论：done +{done_moved}，心跳{'在动' if hb_moved else '没动'}"
          f" → {'正常推进' if (done_moved > 0 or hb_moved) else '可能卡住/进程已死'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
