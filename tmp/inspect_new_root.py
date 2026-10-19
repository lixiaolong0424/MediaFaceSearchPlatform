"""盘点新 media_root 的规模，估算完整跑一圈的耗时。

用途：用户把 media_root 改成某个子目录后，先看清有多少文件、多少视频，
才能判断「尽快跑完一圈」是否可行（照片 ~140 张/秒；视频 15~30s/个）。
"""

from __future__ import annotations

import os
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.common import load_config  # noqa: E402


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or u == "TB":
            return f"{n:,.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"


def main() -> int:
    cfg = load_config()
    root = Path(str(cfg.media_root))
    wl = cfg.ext_whitelist
    photo_ext = cfg.photo_ext
    video_ext = cfg.video_ext
    black = cfg.dir_blacklist

    print(f"media_root = {root}")
    print(f"存在       = {root.exists()}")
    if not root.exists():
        print("[错误] 目录不存在")
        return 1
    print(f"白名单扩展 = {sorted(wl)}")
    print(f"目录黑名单 = {sorted(black)}\n")

    cnt: Counter = Counter()
    size: Counter = Counter()
    n_photo = n_video = n_other = 0
    bytes_total = 0
    skipped_dirs = 0
    t0 = time.monotonic()

    for dirpath, dirnames, filenames in os.walk(root):
        # 就地过滤黑名单目录
        keep = [d for d in dirnames if d.lower() not in black]
        skipped_dirs += len(dirnames) - len(keep)
        dirnames[:] = keep
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower().lstrip(".")
            if ext not in wl:
                continue
            p = os.path.join(dirpath, fn)
            try:
                sz = os.path.getsize(p)
            except OSError:
                continue
            cnt[ext] += 1
            size[ext] += sz
            bytes_total += sz
            if ext in photo_ext:
                n_photo += 1
            elif ext in video_ext:
                n_video += 1
            else:
                n_other += 1

    dt = time.monotonic() - t0
    print(f"遍历耗时 {dt:.1f}s（跳过黑名单目录 {skipped_dirs} 个）\n")
    print("按扩展名：")
    for ext, c in cnt.most_common(20):
        print(f"  {ext:<6} {c:>8,}  {human(size[ext]):>12}")

    print(f"\n合计：{sum(cnt.values()):,} 个素材，{human(bytes_total)}")
    print(f"  照片 {n_photo:,}   视频 {n_video:,}   其它 {n_other:,}")

    # 耗时估算
    print("\n=== 完整跑一圈的估算 ===")
    photo_rate = 140.0        # 实测 141 张/秒（照片优先）
    video_sec = 20.0          # 实测每视频 15~30s，取 20s
    t_photo_scan = n_photo / photo_rate
    t_video_scan = n_video * video_sec
    t_dedup_hash = n_photo / 22.0          # pHash 22~46 张/秒，取保守 22
    t_encode = (n_photo + n_video) / 20.0  # 粗估：编码约 20 文件/秒
    print(f"  03_extract 照片部分 : {t_photo_scan/60:>7.1f} 分钟")
    print(f"  03_extract 视频部分 : {t_video_scan/3600:>7.1f} 小时")
    print(f"  02_dedup pHash      : {t_dedup_hash/3600:>7.1f} 小时")
    print(f"  04_encode（粗估）   : {t_encode/3600:>7.1f} 小时")
    print(f"  ─────────────────────────────")
    total = t_photo_scan + t_video_scan + t_dedup_hash + t_encode
    print(f"  合计                : {total/3600:>7.1f} 小时")
    print(f"\n  仅照片一遍（不跑视频）: {(t_photo_scan + t_dedup_hash + n_photo/20)/60:.1f} 分钟")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
