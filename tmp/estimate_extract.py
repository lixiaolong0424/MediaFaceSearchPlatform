"""估算 03_extract 的剩余时间：区分「已处理段」与「剩余段」的视频/照片构成。

关键：03 按 file_id 升序处理，而 file_id 是按目录遍历顺序分配的。
若前段全是视频（每个几十秒），后半段是照片（每个几十毫秒），
则「用当前速率线性外推」会严重高估总时长。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".m4v"}


def main() -> int:
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row

    done_max = con.execute(
        "SELECT COALESCE(MAX(file_id),0) FROM stage_status WHERE stage='extract' AND status='done'"
    ).fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    n_video = con.execute("SELECT COUNT(*) FROM files WHERE kind='video'").fetchone()[0]
    n_photo = con.execute("SELECT COUNT(*) FROM files WHERE kind='photo'").fetchone()[0]

    print(f"已完成上限 file_id : {done_max:,} / 总计 {total:,}")
    print(f"库里视频/照片      : {n_video:,} / {n_photo:,}\n")

    # 按 file_id 分桶看构成（每 2 万一个桶）
    print("file_id 区间的类型构成（前 20 桶）：")
    print(f"  {'区间起':>9} {'视频':>8} {'照片':>8}  构成")
    step = 20000
    for lo in range(0, min(total, step * 20) + 1, step):
        row = con.execute(
            """SELECT SUM(kind='video') v, SUM(kind='photo') p
                 FROM files WHERE file_id > ? AND file_id <= ?""", (lo, lo + step)).fetchone()
        v, p = int(row["v"] or 0), int(row["p"] or 0)
        if v + p == 0:
            continue
        bar = ("V" * min(30, int(30 * v / (v + p)))) + ("." * (30 - min(30, int(30 * v / (v + p)))))
        mark = "  ← 已处理到这里附近" if lo <= done_max < lo + step else ""
        print(f"  {lo:>9,} {v:>8,} {p:>8,}  {bar}{mark}")

    # 剩余段的视频数（决定剩余时间的大头）
    rem_v = con.execute(
        "SELECT COUNT(*) FROM files WHERE kind='video' AND file_id > ?", (done_max,)).fetchone()[0]
    rem_p = con.execute(
        "SELECT COUNT(*) FROM files WHERE kind='photo' AND file_id > ?", (done_max,)).fetchone()[0]
    print(f"\n剩余待处理：视频 {rem_v:,} · 照片 {rem_p:,}")

    # 实测速率（从今天日志里的「进度」行提取）
    log = ROOT / "logs" / time.strftime("%Y-%m-%d.log")
    samples = []
    if log.exists():
        import re
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*进度 (\d+)/(\d+)", line)
            if m:
                from datetime import datetime
                samples.append((datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"),
                                int(m.group(2))))
    if len(samples) >= 2:
        t0, n0 = samples[0]
        t1, n1 = samples[-1]
        dt = (t1 - t0).total_seconds()
        dn = n1 - n0
        print(f"\n实测区间：{t0:%H:%M:%S} → {t1:%H:%M:%S}（{dt / 60:.1f} 分钟），"
              f"完成 {dn:,} 个 → {dn / dt * 60:.0f} 个/分钟")
        if dn > 0:
            rate = dn / dt
            # 用「已完成段的类型构成」分别估视频/照片耗时
            v_done = con.execute(
                "SELECT COUNT(*) FROM files WHERE kind='video' AND file_id <= ?",
                (n1,)).fetchone()[0]
            p_done = con.execute(
                "SELECT COUNT(*) FROM files WHERE kind='photo' AND file_id <= ?",
                (n1,)).fetchone()[0]
            print(f"  这段里视频 {v_done:,} / 照片 {p_done:,}")
            # 保守：按平均处理时间外推剩余
            per_file = dt / dn
            print(f"  平均每文件 {per_file * 1000:.0f} ms")
            print(f"  若后段以照片为主（~20ms/个）：剩余 ≈ {rem_p * 0.02 / 3600:.1f} 小时"
                  f" + 视频 {rem_v * per_file / 3600:.1f} 小时")
            print(f"  悲观（全按当前均值）：剩余 ≈ {(rem_p + rem_v) * per_file / 3600:.1f} 小时")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
