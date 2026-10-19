"""统计 03_extract 实跑中的抽帧损失：从日志里分类失败原因并量化。"""

from __future__ import annotations

import collections
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"


def main() -> int:
    files = sorted(LOGS.glob("*.log"))
    if not files:
        print("没有日志文件")
        return 1

    kinds = collections.Counter()
    per_file_fail = collections.Counter()
    no_frame_shots = []
    shot_density_warn = 0
    lines = 0
    for f in files:
        for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
            if "抽帧失败" not in raw and "镜头无可用帧" not in raw and "镜头密度异常" not in raw:
                continue
            lines += 1
            if "镜头密度异常" in raw:
                shot_density_warn += 1
                continue
            if "镜头无可用帧" in raw:
                no_frame_shots.append(raw)
                continue
            m = re.search(r"file_id=(\d+)", raw)
            fid = m.group(1) if m else "?"
            per_file_fail[fid] += 1
            if "Non full-range YUV" in raw:
                kinds["mjpeg: Non full-range YUV（seek 越过末帧）"] += 1
            elif "全黑/近黑" in raw:
                kinds["全黑/近黑（暗场被丢弃）"] += 1
            elif "体积过小" in raw:
                kinds["体积过小（暗场/纯色被丢弃）"] += 1
            elif "ffmpeg 抽帧失败" in raw:
                kinds["其它 ffmpeg 失败"] += 1
            else:
                kinds["未分类"] += 1

    print(f"=== 日志：{len(files)} 个文件，共 {lines} 条相关记录 ===\n")
    print("失败原因分布：")
    for k, v in kinds.most_common():
        print(f"  {v:>6}  {k}")
    print(f"\n整个镜头无可用帧的次数：{len(no_frame_shots)}")
    for s in no_frame_shots[:6]:
        print("   ", re.sub(r"^\S+ \S+ \| \S+ \| \S+ \| \S+ \| ", "", s)[:120])
    print(f"\n镜头密度异常告警：{shot_density_warn}")
    print(f"\n失败最集中的文件（前 10）：")
    for fid, n in per_file_fail.most_common(10):
        print(f"  file_id={fid:<7} 失败 {n} 次")

    # 与总进度对比
    print()
    try:
        import sqlite3
        con = sqlite3.connect(str(ROOT / "data" / "library.db"))
        total = con.execute("SELECT COUNT(*) FROM shots").fetchone()[0]
        nf = con.execute("SELECT COALESCE(SUM(n_frames),0) FROM shots").fetchone()[0]
        zero = con.execute("SELECT COUNT(*) FROM shots WHERE COALESCE(n_frames,0)=0").fetchone()[0]
        print(f"shots 总数        : {total:,}")
        print(f"关键帧总数        : {nf:,}")
        print(f"0 帧的镜头        : {zero:,}  ({zero / max(1, total) * 100:.1f}%)")
        con.close()
    except Exception as exc:  # noqa: BLE001
        print(f"（查库失败：{exc}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
