"""估算抽帧剩余时间：用真实采样速率 + 剩余工作量拆分。

要点：
  * videos 与 photos 的成本差 2~3 个数量级，必须分开估
  * extract:done 只在每 500 文件提交一次，粒度太粗 → 用 frames 目录里
    「最新出现的 file_id」来定位当前处理到哪了
  * 采样窗口取 240 秒，避免短窗口被单个长视频带偏
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
FRAMES = ROOT / "data" / "nvme" / "frames"
WINDOW = 240


def frames_state() -> tuple[int, int, int]:
    """返回 (jpg 总数, 目录数, 最大 file_id)。"""
    if not FRAMES.exists():
        return 0, 0, 0
    jpgs = 0
    dirs = 0
    mx = 0
    for d in FRAMES.iterdir():
        if not d.is_dir() or not d.name.isdigit():
            continue
        dirs += 1
        fid = int(d.name)
        mx = max(mx, fid)
        jpgs += sum(1 for _ in d.glob("*.jpg"))
    return jpgs, dirs, mx


def main() -> int:
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    files = {r["status"]: r["n"] for r in con.execute(
        "SELECT status, COUNT(*) n FROM files GROUP BY status")}
    kinds = {r["kind"]: r["n"] for r in con.execute(
        "SELECT kind, COUNT(*) n FROM files GROUP BY kind")}
    # pending 的构成（这才是剩余工作量）
    pend = {r["kind"]: r["n"] for r in con.execute(
        "SELECT kind, COUNT(*) n FROM files WHERE status IN ('pending','error') GROUP BY kind")}
    done_by_kind = {r["kind"]: r["n"] for r in con.execute(
        "SELECT kind, COUNT(*) n FROM files WHERE status='done' GROUP BY kind")}
    try:
        stage_done = con.execute(
            "SELECT COUNT(*) FROM stage_status WHERE stage='extract' AND status='done'"
        ).fetchone()[0]
    except sqlite3.OperationalError:
        stage_done = 0
    avg_frames = con.execute(
        "SELECT COALESCE(AVG(n_frames),0) FROM shots WHERE file_id IN "
        "(SELECT file_id FROM files WHERE kind='video')").fetchone()[0]
    n_shots_video = con.execute(
        "SELECT COUNT(*) FROM shots s JOIN files f ON f.file_id=s.file_id "
        "WHERE f.kind='video'").fetchone()[0]
    con.close()

    print("=== 总盘子 ===")
    print(f"  files 总数   : {sum(files.values()):,}")
    print(f"  by_status    : {files}")
    print(f"  by_kind      : {kinds}")
    print(f"  已 committed : extract:done={stage_done:,}  "
          f"(其中 photo={done_by_kind.get('photo',0):,} video={done_by_kind.get('video',0):,})")
    print(f"\n=== 剩余工作量 ===")
    print(f"  待处理 photo : {pend.get('photo',0):,}")
    print(f"  待处理 video : {pend.get('video',0):,}")
    print(f"  视频已产出镜头/平均每视频帧数: {'%.1f' % (n_shots_video/max(1,done_by_kind.get('video',0)))}镜头, "
          f"{'%.1f' % avg_frames}帧")

    j0, d0, m0 = frames_state()
    print(f"\n=== 采样起点 ===")
    print(f"  jpg={j0:,}  目录={d0:,}  最大 file_id={m0:,}")
    print(f"  等待 {WINDOW} 秒…")
    time.sleep(WINDOW)
    j1, d1, m1 = frames_state()

    dt = WINDOW
    d_jpg = j1 - j0
    d_dir = d1 - d0
    print(f"\n=== 采样结果（{dt}s）===")
    print(f"  jpg +{d_jpg:,}  目录 +{d_dir:,}  最大 file_id {m0}→{m1}")
    print(f"  速率：{d_jpg/dt:.1f} jpg/秒 = {d_jpg/dt*3600:,.0f} jpg/小时"
          f"；{d_dir/dt*60:.1f} 视频/分钟")

    if d_dir == 0 and d_jpg == 0:
        print("\n  ⚠️ 窗口内无增量：可能卡在某个长视频上，或有写锁冲突。")
        return 1

    # ---- 估算 ----
    print(f"\n=== 剩余时间估算 ===")
    # 1) 视频阶段：用「视频/分钟」估
    rem_v = pend.get("video", 0)
    if d_dir > 0:
        vpm = d_dir / dt * 60
        # 注意：目录增量包含未提交批次，是当前真实速度
        eta_v_h = rem_v / vpm / 60 if vpm > 0 else float("inf")
        print(f"  视频: {rem_v:,} 个剩余，当前 {vpm:.1f} 个/分钟")
        print(f"        → 约 {eta_v_h:.1f} 小时（{eta_v_h/24:.1f} 天）")
    else:
        eta_v_h = float("inf")
        print(f"  视频: 本窗口未完成任何视频（{rem_v:,} 个剩余）")

    # 2) 照片阶段：用 jpg/小时 反推不可靠（照片不产 jpg），改用固定单价值
    rem_p = pend.get("photo", 0)
    # 实测单张照片 ~20ms（EXIF+解码尺寸），保守取 30ms
    eta_p_h = rem_p * 0.030 / 3600
    print(f"  照片: {rem_p:,} 个剩余，按 30ms/张 → 约 {eta_p_h:.1f} 小时")

    total = eta_v_h + eta_p_h
    print(f"\n  合计约 {total:.1f} 小时（{total/24:.1f} 天）")
    print(f"  注：视频与照片按 file_id 混排，上式为「先视频后照片」的粗算；"
          f"实际按 file_id 顺序穿插，总时长接近二者之和。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
