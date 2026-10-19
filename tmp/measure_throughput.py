"""测 03_extract 在长时间窗内的真实速率，推算剩余时间与整体可行性。

要点：短窗口（60s）会被「正在处理单个长视频」干扰而得出 0。
这里用 10 分钟窗口，同时看 stage_status 与关键帧两个口径。
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
FRAMES = ROOT / "data" / "nvme" / "frames"

WINDOW = 600  # 秒


def snap() -> tuple[int, int, int]:
    try:
        con = sqlite3.connect(str(DB))
        stage = con.execute(
            "SELECT COUNT(*) FROM stage_status WHERE stage='extract'").fetchone()[0]
        total = con.execute(
            "SELECT COUNT(*) FROM files WHERE kind IN ('photo','video')").fetchone()[0]
        done = con.execute(
            "SELECT COUNT(*) FROM files WHERE status='done'").fetchone()[0]
        con.close()
    except Exception as exc:  # noqa: BLE001
        print(f"读库失败：{exc}")
        return -1, -1, -1
    return int(stage), int(total), int(done)


def frames() -> int:
    return sum(1 for _ in FRAMES.rglob("*.jpg")) if FRAMES.exists() else 0


def main() -> int:
    a_stage, total, a_done = snap()
    a_frames = frames()
    t0 = time.monotonic()
    print(f"T0    stage={a_stage:,}  done={a_done:,}  关键帧={a_frames:,}  / 总数={total:,}")
    print(f"      采样窗口 {WINDOW}s ……（每 60s 打一行）")

    prev_stage, prev_frames = a_stage, a_frames
    for i in range(WINDOW // 60):
        time.sleep(60)
        s, _t, d = snap()
        f = frames()
        el = time.monotonic() - t0
        print(f"  T+{el:4.0f}s  stage={s:,} (+{s - prev_stage})  done={d:,}  "
              f"关键帧={f:,} (+{f - prev_frames})")
        prev_stage, prev_frames = s, f

    b_stage, _t, b_done = snap()
    b_frames = frames()
    dt = time.monotonic() - t0
    d_stage = b_stage - a_stage
    d_done = b_done - a_done
    d_frames = b_frames - a_frames

    print(f"\n=== {dt:.0f}s 窗口结果 ===")
    print(f"  stage_status 增量 : {d_stage:,}  → {d_stage / dt * 3600:,.0f} 个/小时")
    print(f"  files.done 增量   : {d_done:,}  → {d_done / dt * 3600:,.0f} 个/小时")
    print(f"  关键帧增量        : {d_frames:,}  → {d_frames / dt * 60:,.0f} 帧/分钟")

    remaining = total - max(b_stage, a_stage)
    print(f"\n=== 推算 ===")
    print(f"  剩余文件 : {remaining:,}")
    if d_stage > 0:
        eta_h = remaining / (d_stage / dt) / 3600
        print(f"  按 stage 速率 → 剩余约 {eta_h:.1f} 小时（{eta_h/24:.1f} 天）")
    elif d_done > 0:
        eta_h = remaining / (d_done / dt) / 3600
        print(f"  stage 未提交，按 files.done 速率 → 剩余约 {eta_h:.1f} 小时")
    else:
        print(f"  两个口径都没增长 → 可能卡在单个超长视频上，或进程已死")

    alive = False
    try:
        import psutil  # type: ignore

        for p in psutil.process_iter(["name"]):
            if p.info.get("name", "").lower().startswith("python"):
                alive = True
                break
    except Exception:  # noqa: BLE001
        alive = True
    print(f"  python 进程存活   : {alive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
