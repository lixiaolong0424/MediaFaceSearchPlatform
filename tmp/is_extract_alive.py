"""实时判断：03_extract 还在跑吗？在推进吗？（只读，30 秒两次采样）"""

from __future__ import annotations

import sqlite3
import subprocess
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
FRAMES = ROOT / "data" / "nvme" / "frames"


def procs() -> list[str]:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
             "ForEach-Object { $_.ProcessId.ToString() + ' ' + $_.CommandLine }"],
            capture_output=True, text=True, timeout=90,
            encoding="utf-8", errors="replace")
        return [l.strip() for l in (r.stdout or "").splitlines()
                if "03_extract" in l or "02_dedup" in l or "04_encode" in l]
    except Exception as exc:  # noqa: BLE001
        return [f"(无法枚举进程：{exc})"]


def snap() -> dict:
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    try:
        files = {r[0]: r[1] for r in con.execute(
            "SELECT status, COUNT(*) FROM files GROUP BY status")}
        try:
            stage = {f"{r[0]}:{r[1]}": r[2] for r in con.execute(
                "SELECT stage, status, COUNT(*) FROM stage_status GROUP BY stage,status")}
        except sqlite3.OperationalError:
            stage = {}
        shots = con.execute("SELECT COUNT(*) FROM shots").fetchone()[0]
        frames_db = con.execute("SELECT COALESCE(SUM(n_frames),0) FROM shots").fetchone()[0]
    finally:
        con.close()
    dirs = sum(1 for p in FRAMES.iterdir() if p.is_dir()) if FRAMES.exists() else 0
    jpgs = sum(1 for _ in FRAMES.rglob("*.jpg")) if FRAMES.exists() else 0
    return {"files": files, "stage": stage, "shots": shots,
            "frames_db": frames_db, "dirs": dirs, "jpgs": jpgs}


def show(tag: str, s: dict) -> None:
    st = s["stage"]
    ext_done = st.get("extract:done", 0)
    stage_total = sum(st.values())
    print(f"[{datetime.now():%H:%M:%S}] {tag}")
    print(f"   files.status  : {s['files']}")
    print(f"   stage_status  : {st}   (合计 {stage_total:,})")
    print(f"   shots         : {s['shots']:,}   关键帧(DB) : {s['frames_db']:,}")
    print(f"   frames 目录   : {s['dirs']:,}      jpg 文件   : {s['jpgs']:,}")


def main() -> int:
    print("=== 进程 ===")
    ps = procs()
    if ps:
        for p in ps:
            print(f"  {p[:130]}")
    else:
        print("  没有 02/03/04 的 python 进程 → 抽帧【已停止】")
    print()

    a = snap()
    show("采样 1", a)
    print("\n   等 45 秒…")
    time.sleep(45)
    b = snap()
    show("采样 2", b)

    print("\n=== 45 秒增量 ===")
    d_done = b["files"].get("done", 0) - a["files"].get("done", 0)
    d_ext = b["stage"].get("extract:done", 0) - a["stage"].get("extract:done", 0)
    d_jpg = b["jpgs"] - a["jpgs"]
    d_dir = b["dirs"] - a["dirs"]
    print(f"   done +{d_done}   extract:done +{d_ext}   frames 目录 +{d_dir}   jpg +{d_jpg}")

    alive = bool(ps)
    progressing = (d_jpg > 0) or (d_ext > 0) or (d_dir > 0)
    print()
    if alive and progressing:
        print(f"   ✅ 正在推进。按 jpg 增量估：{d_jpg/45:.1f} jpg/秒 = {d_jpg/45*3600:.0f} jpg/小时")
        rem = b["files"].get("pending", 0)
        if d_ext > 0:
            rate_h = d_ext / 45 * 3600
            print(f"      按文件增量估：{rate_h:.0f} 文件/小时 → 剩余 {rem:,} 个需 "
                  f"{rem/max(1,rate_h):.0f} 小时")
        else:
            print(f"      注意：extract:done 未变，说明还在同一批 500 个的提交窗口内"
                  f"（未提交的进度看不到，只能靠 jpg 数）")
    elif alive and not progressing:
        print("   ⚠️ 进程在，但 45 秒内无任何增量 → 可能卡在某个长视频上")
    else:
        print("   ⛔ 进程已退出。抽帧停止了。")
        print(f"      已完成 extract:done = {b['stage'].get('extract:done',0):,} / "
              f"总计 {sum(b['files'].values()):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
