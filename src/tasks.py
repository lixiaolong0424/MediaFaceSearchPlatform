# -*- coding: utf-8 -*-
"""src/tasks.py · 任务队列与进度上报（规格 v5.3 §3.2 / §3.4 / §8.3）。

设计要点（为什么这么写）：

1. **状态存数据库，不存前端内存**。规格 §3.2 强制：「页面关闭再打开，任务状态必须
   恢复显示」。Streamlit 的 session_state 一刷新就没，所以任务行落在 SQLite。

2. **任务是子进程，不是线程**。抽帧/编码/检脸都是长任务，线程无法被干净地 kill；
   子进程还可以在界面关掉之后继续跑（用户关浏览器不该中断 30 小时的入库）。
   子进程通过 `attach()` 拿到 Reporter，把进度写回 tasks 表。

3. **进度写节流**。抽帧峰值 15 文件/秒，每 4 个文件一次 UPDATE 会跟编码进程抢写锁
   （这正是之前 commit_batch_fused_photos 那个坑）。所以默认 1 秒写一次。

4. **取消靠轮询标志位**。子进程在 tick() 里每 3 秒读一次 tasks.cancel；读到就抛
   `TaskCancelled`，由各脚本的 finally 收尾（不改已落库的数据，保证可续跑）。

5. **僵尸任务靠心跳回收**。进程被 kill -9 后 tasks 表会永远停在 running，
   界面就会一直显示「进行中」。所以 running 行的 updated_at 是心跳，
   超过 STALE_MIN 分钟没动就判定进程已死。
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from . import db as D
from . import ui_text as U
from .common import Config, local_now_iso

# 任务种类：界面按钮 → 执行计划（plan）由 src/taskplan.py 给出
KINDS = (
    "scan_new", "scan_all", "extract", "encode", "faces", "face_detect",
    "face_encode", "cluster", "migrate", "reconcile", "purge", "gold",
)

ACTIVE_STATES = ("queued", "running")
TERMINAL_STATES = ("success", "failed", "cancelled")

STALE_MIN = 5            # running 行超过这么久没心跳 → 进程已死
# 长视频解码期间也要把「正在处理」写出去（否则界面像卡死）
CURRENT_FLUSH_INTERVAL = 4.0
PROGRESS_MIN_INTERVAL = 1.0   # 进度写库最小间隔（秒）
CANCEL_POLL_INTERVAL = 3.0    # 取消标志轮询间隔（秒）
HISTORY_DAYS = 30


class TaskCancelled(Exception):
    """用户点了「停止任务」。各脚本捕获后正常收尾（已落库数据保持有效）。"""


CANCEL_EXIT_CODE = 130
CANCEL_MESSAGE = "任务已被用户停止；已提交的进度全部保留，可以再次启动续跑。"


# --------------------------------------------------------------------------- #
# 基础读写
# --------------------------------------------------------------------------- #


def _connect(cfg: Optional[Config] = None, *, timeout: float = 60.0) -> sqlite3.Connection:
    cfg = cfg or _CFG_HOLDER[0]
    if cfg is None:
        raise RuntimeError("tasks 模块需要 Config（先调用 tasks.init(cfg)）")
    conn = sqlite3.connect(str(cfg.db_path), timeout=timeout, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
    # 不要在这里执行 PRAGMA journal_mode=WAL：库早已是 WAL，而这个 pragma
    # 仍需短暂获取写锁。界面每次渲染都会建好几个连接，白付的锁竞争代价很大。
    return conn


_CFG_HOLDER: List[Optional[Config]] = [None]


def init(cfg: Config) -> None:
    """显式注入配置（脚本与界面启动时各调一次，幂等）。"""
    _CFG_HOLDER[0] = cfg


def new_id() -> str:
    return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def create(
    cfg: Optional[Config] = None,
    *,
    kind: str,
    title: str,
    plan: Optional[List[Dict[str, Any]]] = None,
    total: Optional[int] = None,
    state: str = "queued",
    params: Optional[Dict[str, Any]] = None,
) -> str:
    """建一条任务。返回 task_id。plan 是「要跑哪几步」，供子进程读取执行。"""
    if kind not in KINDS:
        raise ValueError(f"未知任务种类：{kind}")
    tid = new_id()
    now = local_now_iso()
    payload = {"steps": plan or [], **(params or {})}
    conn = _connect(cfg)
    try:
        conn.execute(
            "INSERT INTO tasks(task_id, kind, title, plan, state, stage, stage_ix, "
            "stage_total, done, total, message, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tid, kind, title, json.dumps(payload, ensure_ascii=False), state,
             None, 0, len(plan or []), 0, total, "已排队", now, now),
        )
    finally:
        conn.close()
    emit(cfg, "task_state", {"task_id": tid, "state": state}, task_id=tid)
    return tid


def get(cfg: Optional[Config], task_id: str) -> Optional[Dict[str, Any]]:
    conn = _connect(cfg)
    try:
        row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row else None


def plan_of(task: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(task.get("plan") or "{}")
    except (TypeError, ValueError):
        return {}


def update(cfg: Optional[Config], task_id: str, **fields: Any) -> None:
    """局部更新。state 变终态时自动补 finished_at。"""
    if not fields:
        return
    fields["updated_at"] = local_now_iso()
    if fields.get("state") in TERMINAL_STATES and "finished_at" not in fields:
        fields["finished_at"] = fields["updated_at"]
    allowed = {
        "state", "stage", "stage_ix", "stage_total", "done", "total", "message",
        "error", "pid", "cancel", "started_at", "finished_at", "updated_at", "title",
        "stage_started_at",
    }
    use = {k: v for k, v in fields.items() if k in allowed}
    if not use:
        return
    sets = ", ".join(f"{k}=?" for k in use)
    conn = _connect(cfg)
    try:
        conn.execute(f"UPDATE tasks SET {sets} WHERE task_id=?", (*use.values(), task_id))
    finally:
        conn.close()


def finish(
    cfg: Optional[Config],
    task_id: str,
    state: str,
    *,
    error: Optional[str] = None,
    message: Optional[str] = None,
    event: Optional[str] = None,
) -> None:
    update(cfg, task_id, state=state, error=error, message=message)
    tid = task_id
    kind = "task_state"
    if event:
        kind = event
    emit(cfg, kind, {"task_id": tid, "state": state,
                     "error": (error or "")[:500]}, task_id=tid)


def request_cancel(cfg: Optional[Config], task_id: str) -> None:
    update(cfg, task_id, cancel=1, message="正在停止…")


def cancel_requested(cfg: Optional[Config], task_id: str) -> bool:
    conn = _connect(cfg)
    try:
        row = conn.execute("SELECT cancel FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    finally:
        conn.close()
    return bool(row and row["cancel"])


# --------------------------------------------------------------------------- #
# 查询（界面用）
# --------------------------------------------------------------------------- #


def _row_to_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    return d


def active(cfg: Optional[Config]) -> List[Dict[str, Any]]:
    conn = _connect(cfg)
    try:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE state IN ('queued','running') "
            "ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def running(cfg: Optional[Config]) -> Optional[Dict[str, Any]]:
    for task in active(cfg):
        if task["state"] == "running":
            return task
    acts = active(cfg)
    return acts[0] if acts else None


def recent(cfg: Optional[Config], days: int = HISTORY_DAYS, limit: int = 200) -> List[Dict[str, Any]]:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    conn = _connect(cfg)
    try:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
            (since, limit),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def elapsed_sec(task: Dict[str, Any]) -> Optional[float]:
    st = task.get("started_at")
    if not st:
        return None
    try:
        t0 = datetime.fromisoformat(st)
    except ValueError:
        return None
    end = task.get("finished_at")
    t1 = datetime.fromisoformat(end) if end else datetime.now()
    return max(0.0, (t1 - t0).total_seconds())


def _stage_elapsed_sec(task: Dict[str, Any]) -> Optional[float]:
    """当前阶段已经跑了多久（没有 stage_started_at 的老任务退回整个任务时间）。"""
    base = task.get("stage_started_at") or task.get("started_at")
    if not base:
        return None
    try:
        t0 = datetime.fromisoformat(str(base))
    except ValueError:
        return None
    end = task.get("finished_at")
    t1 = datetime.fromisoformat(end) if end else datetime.now()
    return max(0.0, (t1 - t0).total_seconds())


def snapshot(task: Dict[str, Any]) -> Dict[str, Any]:
    """给界面用的派生量：速率、预计剩余、是否停滞。"""
    done = int(task.get("done") or 0)
    total = task.get("total")
    el = elapsed_sec(task) or 0.0
    # 速度必须用「**当前阶段**已用时间」，不能用整个任务的时间：
    # done 每切阶段就归零，分母却一直累加 → 速度被前几个阶段的时间拉低，
    # ETA 随之虚高；刚切阶段时还会出现「速度≈0、剩余几十小时」。
    el_stage = _stage_elapsed_sec(task) or 0.0
    rate = (done / el_stage) if el_stage > 1 and done else None
    eta = None
    if rate and total and total > done:
        eta = (total - done) / rate
    return {
        **task,
        "elapsed_sec": el,
        "stage_elapsed_sec": el_stage,
        "rate": rate,
        "eta_sec": eta,
        "is_active": task.get("state") in ACTIVE_STATES,
    }


def _pid_alive(pid: Any) -> bool:
    """这个进程还活着吗？**心跳判定必须问它**，不能只看时间戳。

    2026-10-20 误判：单个大视频抽帧可以合法地连续 10 分钟没有 tick
    （video.single_video_timeout_sec=600），只看时间戳就会把活着的任务判死。
    """
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        except Exception:  # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def pid_looks_like_task(pid: Any, task_id: Optional[str] = None) -> bool:
    """这个 pid 现在还是「那个任务的流水线进程」吗？

    光看 `_pid_alive()` 不够：Windows 会复用 PID。实测 2026-10-26，
    一个 2 天前失败任务的 pid 已经被 msedgewebview2.exe 占用，
    于是僵尸守卫每次都命中，**所有任务按钮都点不动**。

    判据：命令行里同时出现 `run_task.py`（或 01_/03_/04_/06_/07_ 阶段脚本）
    与 task_id。取不到命令行时保守返回 True（宁可误拦也不并发跑两份）。
    """
    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or not _pid_alive(pid):
        return False
    try:
        import psutil  # type: ignore

        cmd = " ".join(psutil.Process(pid).cmdline())
    except Exception:  # noqa: BLE001 - 没有 psutil / 没权限：保守一点
        return True
    if not cmd:
        return True
    markers = ("run_task.py", "01_manifest.py", "03_extract.py", "04_encode.py",
               "06_face_detect.py", "07_face_encode.py", "16_cluster.py",
               "17_gold.py", "18_reconcile.py", "12_maintain.py")
    if not any(m in cmd for m in markers):
        return False
    if task_id and str(task_id) not in cmd:
        return False
    return True


def clear_reused_pids(cfg: Optional[Config] = None) -> int:
    """把「终态任务但 pid 已被别的进程占用」的记录清空 pid（一次性清理）。"""
    try:
        conn = _connect(cfg, timeout=5.0)
    except sqlite3.Error:
        return 0
    n = 0
    try:
        rows = conn.execute(
            "SELECT task_id, pid FROM tasks WHERE pid IS NOT NULL "
            "AND state IN ('success','failed','cancelled') "
            "ORDER BY created_at DESC LIMIT 60").fetchall()
        for r in rows:
            if _pid_alive(r["pid"]) and not pid_looks_like_task(r["pid"], r["task_id"]):
                conn.execute("UPDATE tasks SET pid=NULL WHERE task_id=?", (r["task_id"],))
                n += 1
        if n:
            conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()
    return n


def live_terminal_tasks(cfg: Optional[Config], *, within_min: int = 1440) -> List[Dict[str, Any]]:
    """状态已是终态、但进程还活着的任务（说明当初判错了，或进程没被回收）。"""
    conn = _connect(cfg, timeout=5.0)
    try:
        rows = conn.execute(
            "SELECT task_id, title, state, pid, updated_at FROM tasks "
            "WHERE state IN ('failed','cancelled') AND pid IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 20").fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [dict(r) for r in rows if pid_looks_like_task(r["pid"], r["task_id"])]


def stale_minutes(cfg: Optional[Config] = None) -> int:
    """心跳超时阈值。必须比「单个文件最长合法耗时」大得多。

    视频侧 `video.single_video_timeout_sec` 默认 600 秒 = 10 分钟，
    这期间抽帧线程不会 tick，所以阈值至少 15 分钟。
    """
    base = 15
    try:
        cfg = cfg or _CFG_HOLDER[0]
        if cfg is not None:
            base = int(cfg.get("task.stale_minutes", 15))
            one = float(cfg.get("video.single_video_timeout_sec", 600)) / 60.0
            base = max(base, int(one) + 5)
    except Exception:  # noqa: BLE001
        pass
    return max(5, base)


def reap_stale(cfg: Optional[Config], minutes: Optional[int] = None) -> int:
    """把心跳超时的 running 任务判为失败（进程被强杀后的兜底）。

    ⚠️ 这个函数在**每次页面渲染**时都会被调用（由 active_task 触发），
    所以绝不能一上来就写库：抽帧进程持有写事务时，UPDATE 会排队等锁，
    用户感受就是「切个页面卡好几秒」。先做只读探测（WAL 下读不阻塞写），
    确认真有僵尸才付出一次 UPDATE 的代价。
    """
    minutes = int(minutes if minutes is not None else stale_minutes(cfg))
    cutoff = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        conn = _connect(cfg, timeout=2.0)
        try:
            # 先处理「用户已经点了停止、进程也已经不在」的任务：
            # 这种任务永远不会再写心跳，等 15 分钟才收尾会让界面卡在「正在停止…」。
            grace = (datetime.now() - timedelta(seconds=15)).strftime("%Y-%m-%dT%H:%M:%S")
            cancel_rows = conn.execute(
                "SELECT task_id, pid FROM tasks WHERE state='running' AND cancel=1 "
                "AND updated_at < ?", (grace,)).fetchall()
            stopped = [r["task_id"] for r in cancel_rows if not _pid_alive(r["pid"])]
            if stopped:
                now_c = local_now_iso()
                conn.executemany(
                    "UPDATE tasks SET state='cancelled', finished_at=?, updated_at=?, "
                    "message='已停止' WHERE task_id=?",
                    [(now_c, now_c, t) for t in stopped])
            rows = conn.execute(
                "SELECT task_id, pid FROM tasks WHERE state='running' AND updated_at < ?",
                (cutoff,)).fetchall()
            if not rows:
                return len(stopped)
            # 关键：心跳旧 ≠ 进程死。视频抽帧可以合法地 10 分钟不 tick。
            dead = [r["task_id"] for r in rows if not _pid_alive(r["pid"])]
            alive = [r["task_id"] for r in rows if _pid_alive(r["pid"])]
            if alive:
                # 进程还活着：只把心跳往前推，别动状态
                now0 = local_now_iso()
                conn.executemany(
                    "UPDATE tasks SET updated_at=? WHERE task_id=?",
                    [(now0, t) for t in alive])
            if not dead:
                return 0
            now = local_now_iso()
            conn.executemany(
                "UPDATE tasks SET state='failed', finished_at=?, updated_at=?, "
                "message='进程已退出（未正常结束）', error='heartbeat timeout' "
                "WHERE task_id=?", [(now, now, t) for t in dead])
            return len(dead)
        finally:
            conn.close()
    except sqlite3.Error:
        # 拿不到写锁（流水线正忙）就放弃：下次渲染会再试，僵尸任务不影响正确性
        return 0


def purge_history(cfg: Optional[Config], days: int = HISTORY_DAYS) -> Dict[str, Any]:
    """删除 30 天前的终态任务与事件（规格 §3.2 保留 30 天）。

    注意：这是**写操作**。抽帧/编码正在写库时它可能拿不到锁，所以：
      * 用 5 秒超时（不是默认 60 秒 —— 让用户干等一分钟再报错毫无意义）；
      * 拿不到锁就返回 {"error": ...}，由界面显示人话提示。
    """
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        conn = _connect(cfg, timeout=5.0)
        try:
            n1 = conn.execute(
                "DELETE FROM tasks WHERE state IN ('success','failed','cancelled') "
                "AND created_at < ?", (cutoff,)
            ).rowcount
            n2 = conn.execute("DELETE FROM task_events WHERE at < ?", (cutoff,)).rowcount
            return {"tasks": n1, "events": n2}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return {"tasks": 0, "events": 0,
                "error": f"数据库正忙（{type(exc).__name__}），请等当前任务结束后再清理。"}


# --------------------------------------------------------------------------- #
# 变更事件（§3.4 缓存失效）
# --------------------------------------------------------------------------- #


def emit(
    cfg: Optional[Config],
    kind: str,
    payload: Optional[Dict[str, Any]] = None,
    *,
    task_id: Optional[str] = None,
) -> None:
    conn = _connect(cfg)
    try:
        conn.execute(
            "INSERT INTO task_events(at, task_id, kind, payload) VALUES (?,?,?,?)",
            (local_now_iso(), task_id, kind, json.dumps(payload or {}, ensure_ascii=False)),
        )
    finally:
        conn.close()


def latest_event_id(cfg: Optional[Config]) -> int:
    conn = _connect(cfg)
    try:
        row = conn.execute("SELECT MAX(event_id) AS m FROM task_events").fetchone()
        return int(row["m"] or 0) if row else 0
    finally:
        conn.close()


def events_since(cfg: Optional[Config], event_id: int, limit: int = 50) -> List[Dict[str, Any]]:
    conn = _connect(cfg)
    try:
        rows = conn.execute(
            "SELECT * FROM task_events WHERE event_id > ? ORDER BY event_id ASC LIMIT ?",
            (event_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# app_settings（聚类版本指针等运行时状态）
# --------------------------------------------------------------------------- #


def setting_get(cfg: Optional[Config], key: str, default: Optional[str] = None) -> Optional[str]:
    conn = _connect(cfg)
    try:
        row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    finally:
        conn.close()
    return row["value"] if row else default


def setting_set(cfg: Optional[Config], key: str, value: str) -> None:
    conn = _connect(cfg)
    try:
        conn.execute(
            "INSERT INTO app_settings(key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, value, local_now_iso()),
        )
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 子进程侧：进度上报
# --------------------------------------------------------------------------- #


class Reporter:
    """子进程内的进度上报器。写库节流 + 取消轮询 + 心跳。"""

    def __init__(self, cfg: Config, task_id: str):
        self.cfg = cfg
        self.task_id = task_id
        self._last_write = 0.0
        self._last_cancel_check = 0.0
        self._cancel = False
        self._done = 0
        self._total: Optional[int] = None
        self._stage: Optional[str] = None
        self._message = ""
        # 是否由「本进程」负责进度数字。
        # 只有真正在跑循环的进程（阶段脚本）才该写 done/total；
        # 执行器（run_task.py）只在步与步之间切阶段，它结束时若也写 done/total，
        # 会把子进程刚报完的进度冲成 0 —— 2026-10-19 实测踩到过。
        self._owns = False
        self._skipped = 0      # 进度因写锁被跳过的次数（只用于诊断）
        self._current = ""     # 正在处理的文件（只放内存，跟 tick 一起 flush）

    # -- 生命周期 -------------------------------------------------------- #
    def start(self) -> None:
        update(self.cfg, self.task_id, state="running", pid=os.getpid(),
               started_at=local_now_iso(), stage_started_at=local_now_iso(),
               message="开始执行")
        emit(self.cfg, "task_state", {"task_id": self.task_id, "state": "running"},
             task_id=self.task_id)
        self._last_write = time.monotonic()

    def stage(self, code: str, total: Optional[int] = None, ix: int = 0, n: int = 0) -> None:
        """切到新阶段。code 是内部阶段码（extract/encode/face_detect/…），
        界面上显示的是 ui_text 里的业务词，绝不把代号漏给用户（规格 §2.2）。"""
        name = U.stage_name(code)
        self._stage = name
        self._done = 0
        self._total = total
        self._owns = total is not None
        f: Dict[str, Any] = {"stage": name, "done": 0, "total": total, "message": name,
                             "stage_started_at": local_now_iso()}
        if n:
            f["stage_ix"] = ix
            f["stage_total"] = n
        update(self.cfg, self.task_id, **f)
        self._last_write = time.monotonic()

    def say(self, msg: str) -> None:
        self._message = msg
        update(self.cfg, self.task_id, message=msg[:500])
        self._last_write = time.monotonic()

    def current(self, item: str) -> None:
        """记下「正在处理」的是哪个文件。

        每 4 秒才真正写一次库（避免抢写锁），但**大视频期间必须能写出去**：
        `tick()` 是每个文件结束后才调，一个 9 分钟 1.5GB 的视频解码要几分钟，
        期间界面既没有进度也没有文件名，用户会以为卡死
        （2026-10-23 实际反馈：「一直显示估算中…已用 55 秒」）。
        顺带把这次写入当心跳，长视频也不会被误判为心跳超时。
        """
        self._current = str(item or "")[:500]
        now = time.monotonic()
        if (now - self._last_write) >= CURRENT_FLUSH_INTERVAL:
            self._flush(now)

    def tick(self, n: int = 1, *, total: Optional[int] = None, force: bool = False) -> None:
        """累加进度。抽帧/编码/检脸的循环里每处理一个文件调一次。"""
        self._done += n
        self._owns = True
        if total is not None:
            self._total = total
        now = time.monotonic()
        if force or (now - self._last_write) >= PROGRESS_MIN_INTERVAL:
            self._flush(now)
        self.check_cancel()

    def _flush(self, now: Optional[float] = None, *, with_progress: Optional[bool] = None) -> None:
        """写进度。**绝不允许因为写锁失败而干掉整个流水线**。

        2026-10-19 真实事故：03_extract 跑了 130 秒后崩在 `T.tick()` 的写库上
        （`sqlite3.OperationalError: database is locked`）。
        原因是进度上报用自己的连接，而流水线主连接当时正握着一个未提交的写事务
        （视频侧每 100 个文件才 commit 一次，窗口可以长达几十分钟）——
        于是「同一个进程的两个连接」互相等锁，等到超时抛错。
        现在：进度写用 1 秒超时、失败就跳过（下个 tick 会再写），
        任务完成/失败这种关键写入仍走 60 秒超时。
        """
        now = now or time.monotonic()
        msg = f"正在处理：{self._current}" if self._current else (self._message or self._stage or "")
        f: Dict[str, Any] = {"message": msg[:500]}
        if self._owns if with_progress is None else with_progress:
            f["done"] = self._done
            f["total"] = self._total
            f["updated_at"] = local_now_iso()
        sets = ", ".join(f"{k}=?" for k in f)
        try:
            conn = _connect(self.cfg, timeout=1.0)
            try:
                conn.execute(f"UPDATE tasks SET {sets} WHERE task_id=?",
                             (*f.values(), self.task_id))
            finally:
                conn.close()
        except sqlite3.Error:
            self._skipped += 1
        self._last_write = now

    def final_flush(self) -> None:
        """进程退出前把最后一次进度落库（否则被 kill 前那 1 秒的进度会丢）。"""
        if not self._owns:
            return
        try:
            self._flush(with_progress=True)
        except Exception:  # noqa: BLE001 - 退出路径上不该再抛
            pass

    def check_cancel(self) -> None:
        """每 3 秒查一次取消标志；被取消就抛 TaskCancelled。"""
        now = time.monotonic()
        if self._cancel:
            raise TaskCancelled()
        if (now - self._last_cancel_check) < CANCEL_POLL_INTERVAL:
            return
        self._last_cancel_check = now
        if cancel_requested(self.cfg, self.task_id):
            self._cancel = True
            raise TaskCancelled()

    def cancelled(self) -> bool:
        self.check_cancel()
        return False

    def success(self, message: str = "已完成", event: Optional[str] = None) -> None:
        self._flush(with_progress=False)
        finish(self.cfg, self.task_id, "success", message=message, event=event)

    def fail(self, exc: BaseException) -> None:
        self._flush(with_progress=False)
        finish(self.cfg, self.task_id, "failed", error=f"{type(exc).__name__}: {exc}",
               message="执行失败")

    def cancel(self) -> None:
        self._flush(with_progress=False)
        finish(self.cfg, self.task_id, "cancelled", message="已停止")


_REPORTER: List[Optional[Reporter]] = [None]


def attach(cfg: Config, task_id: Optional[str]) -> Optional[Reporter]:
    """把本进程绑定到一个任务上（供 01~07 脚本的 --task-id 使用）。"""
    if not task_id:
        _REPORTER[0] = None
        return None
    rep = Reporter(cfg, task_id)
    _REPORTER[0] = rep
    rep.start()
    # 进程退出前补一次进度：扫描这类跑得很快的阶段，最后一次 tick 之后
    # 可能还不到 1 秒的写库间隔就结束了，不补的话界面会停在 0。
    import atexit

    atexit.register(rep.final_flush)
    return rep


def reporter() -> Optional[Reporter]:
    return _REPORTER[0]


def tick(n: int = 1, total: Optional[int] = None) -> None:
    """无任务时的空操作，所以业务代码可以无脑埋点。"""
    rep = _REPORTER[0]
    if rep is not None:
        rep.tick(n, total=total)


def stage(code: str, total: Optional[int] = None, ix: int = 0, n: int = 0) -> None:
    """切阶段。**ix/n 默认 0 = 不要覆盖执行器写好的「第几步/共几步」。**

    2026-10-19 的显示 bug：这里默认值原来是 ix=1, n=1，于是阶段脚本一调
    `T.stage("extract", total=N)` 就把执行器的「第 2/5 步」冲成「第 1/1 步」，
    界面看起来像「整个任务只有一步」，其实那是 5 步里的第 2 步。
    """
    rep = _REPORTER[0]
    if rep is not None:
        rep.stage(code, total=total, ix=ix, n=n)


def say(msg: str) -> None:
    rep = _REPORTER[0]
    if rep is not None:
        rep.say(msg)


def current(item: str) -> None:
    """告知「正在处理这个文件」（只记内存，不写库）。"""
    rep = _REPORTER[0]
    if rep is not None:
        rep.current(item)


def cancelled() -> bool:
    rep = _REPORTER[0]
    if rep is None:
        return False
    return rep.cancelled()


@contextmanager
def attached(cfg: Config, task_id: Optional[str]):
    rep = attach(cfg, task_id)
    try:
        yield rep
    except TaskCancelled:
        if rep is not None:
            rep.cancel()
        raise


def hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"
