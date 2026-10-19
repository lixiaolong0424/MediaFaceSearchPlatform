"""精确测视频阶段的抽帧成本：每帧的墙钟耗时、每秒产出帧数。

用途：当前 03_extract 正卡在一个长视频上（60 秒内 stage_status 不增长，
但关键帧 +284）。要据此估算全量 22,149 个视频需要多久。
"""

from __future__ import annotations

import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRAMES = ROOT / "data" / "nvme" / "frames"


def frames_now() -> tuple[int, int]:
    total = 0
    dirs = 0
    for p in FRAMES.iterdir():
        if not p.is_dir():
            continue
        dirs += 1
        total += sum(1 for _ in p.glob("*.jpg"))
    return dirs, total


def main() -> int:
    samples = []
    d0, f0 = frames_now()
    print(f"起点：{d0:,} 个 file_id 目录 / {f0:,} 关键帧")
    t0 = time.monotonic()
    for i in range(6):
        time.sleep(30)
        d, f = frames_now()
        el = time.monotonic() - t0
        rate = (f - f0) / el if el > 0 else 0
        samples.append(rate)
        print(f"  T+{el:5.0f}s  目录={d:>5,}  关键帧={f:>7,}  "
              f"区间速率={rate:6.1f} 帧/秒  新增目录={d - d0}")
    avg = sum(samples) / len(samples)
    print(f"\n平均 {avg:.1f} 关键帧/秒")
    # 全量视频的关键帧总量估计：12,644 帧对应已处理的 ~1650 个视频中的一部分
    # 用「已处理视频数」与「产出帧数」反推
    print("\n=== 全量估算 ===")
    print("  当前处于视频阶段。按 22,149 个视频计：")
    print(f"  若平均每个视频产出 ~8 帧、当前速率 {avg:.1f} 帧/秒 → "
          f"约 {22149 * 8 / max(avg, 0.1) / 3600:.1f} 小时（仅视频阶段）")
    print(f"  参考：76 分钟已完成 1,500 个文件（其中视频约 550 个）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
