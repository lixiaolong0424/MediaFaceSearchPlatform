"""任务1：后台监视 03_extract 直到结束，并汇报最终状态。

用法（后台运行）：
    .venv\\Scripts\\python.exe tmp\watch_extract.py
每 5 分钟写一行到 tmp\watch_extract.log，检测到 03 进程消失即输出最终汇总并退出。
"""

from __future__ import annotations

import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
FRAMES = ROOT / "data" / "nvme" / "frames"
LOG = ROOT / "tmp" / "watch_extract.log"
INTERVAL = 300


def extract_running() -> int:
    """返回 03_extract 的 python 进程数。"""
    try:
        r = subprocess.run(["wmic", "process", "where", "name='python.exe'",
                            "get", "CommandLine"],
                           capture_output=True, text=True, timeout=60,
                           encoding="utf-8", errors="replace")
        return sum(1 for l in (r.stdout or "").splitlines() if "03_extract" in l)
    except Exception:  # noqa: BLE001
        # wmic 不可用时用 CIM 兜底
        try:
            r2 = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                 "Where-Object { $_.CommandLine -match '03_extract' }).Count"],
                capture_output=True, text=True, timeout=60)
            return int((r2.stdout or "0").strip() or 0)
        except Exception:  # noqa: BLE001
            return -1


def snap() -> dict:
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    try:
        files = {r[0]: r[1] for r in con.execute(
            "SELECT status, COUNT(*) FROM files GROUP BY status")}
        try:
            stage = {f"{r[0]}:{r[1]}": r[2] for r in con.execute(
                "SELECT stage, status, COUNT(*) FROM stage_status GROUP BY stage, status")}
        except sqlite3.OperationalError:
            stage = {}
        shots = con.execute("SELECT COUNT(*) FROM shots").fetchone()[0]
        frames_db = con.execute("SELECT COALESCE(SUM(n_frames),0) FROM shots").fetchone()[0]
        zero = con.execute(
            "SELECT COUNT(*) FROM shots WHERE COALESCE(n_frames,0)=0").fetchone()[0]
    finally:
        con.close()
    dirs = sum(1 for p in FRAMES.iterdir() if p.is_dir()) if FRAMES.exists() else 0
    files_n = sum(1 for _ in FRAMES.rglob("*.jpg")) if FRAMES.exists() else 0
    return {"files": files, "stage": stage, "shots": shots, "frames_db": frames_db,
            "zero": zero, "dirs": dirs, "frame_files": files_n}


def line(tag: str, s: dict, extra: str = "") -> str:
    done = s["files"].get("done", 0)
    pend = s["files"].get("pending", 0)
    err = s["files"].get("error", 0)
    return (f"[{datetime.now():%H:%M:%S}] {tag:<8} done={done:<7,} pending={pend:<7,} "
            f"error={err:<5,} shots={s['shots']:<7,} 关键帧={s['frames_db']:<8,} "
            f"目录={s['dirs']:<5,} jpg={s['frame_files']:<8,} 0帧镜头={s['zero']:,} "
            f"{extra}")


def main() -> int:
    print(f"监视开始，每 {INTERVAL}s 一行，输出到 {LOG}", flush=True)
    prev = snap()
    t_prev = time.time()
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"\n===== 监视开始 {datetime.now():%Y-%m-%d %H:%M:%S} =====\n")
        fh.write(line("START", prev) + "\n")
        fh.flush()
        while True:
            time.sleep(INTERVAL)
            cur = snap()
            now = time.time()
            dt = now - t_prev
            d_done = cur["files"].get("done", 0) - prev["files"].get("done", 0)
            d_jpg = cur["frame_files"] - prev["frame_files"]
            rate = d_done / dt * 3600 if dt else 0
            extra = f"| +{d_done}文件/{dt/60:.0f}min ({rate:.0f}/h) +{d_jpg}jpg"
            running = extract_running()
            msg = line(f"RUN({running})" if running > 0 else "STOPPED", cur, extra)
            print(msg, flush=True)
            fh.write(msg + "\n")
            fh.flush()
            prev, t_prev = cur, now

            if running == 0:
                # 再等一轮确认不是进程切换间隙
                time.sleep(60)
                if extract_running() == 0:
                    fin = snap()
                    tail = (f"\n===== 03_extract 已结束 {datetime.now():%Y-%m-%d %H:%M:%S} "
                            f"=====\n" + line("FINAL", fin) + "\n")
                    print(tail, flush=True)
                    fh.write(tail)
                    fh.flush()
                    print(f"最终：done={fin['files'].get('done',0):,} "
                          f"error={fin['files'].get('error',0):,} "
                          f"remaining={fin['files'].get('pending',0):,} "
                          f"shots={fin['shots']:,} 关键帧={fin['frames_db']:,} "
                          f"0帧镜头={fin['zero']:,}", flush=True)
                    return 0


if __name__ == "__main__":
    raise SystemExit(main())
