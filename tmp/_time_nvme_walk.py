"""量化 app 启动时 _nvme_usage() 的目录遍历开销。

data/nvme 下有 frames/<file_id>/ 这种「一个文件一个目录」的布局，
现在有 5 万多个目录 + 十几万个文件。Path.rglob 在 Windows 上每层都要开目录，
这条路径如果没缓存、每次 rerun 都跑，界面就会长时间白屏（浏览器显示 Stop）。
"""

import os
import time
from pathlib import Path

root = Path(r'D:\DATA\PROJECT\建设多媒体整理系统\data\nvme')

# 先数一遍规模（不计时）
n_files = sum(1 for p in root.rglob("*") if p.is_file())
print(f"data/nvme 下文件数 ≈ {n_files:,}")

t0 = time.monotonic()
used = 0
for p in root.rglob("*"):
    try:
        if p.is_file():
            used += p.stat().st_size
    except OSError:
        pass
t_rglob = time.monotonic() - t0
print(f"Path.rglob 方式：{t_rglob:.1f}s，{used/1e9:.2f} GB")

t0 = time.monotonic()
used2 = 0
n2 = 0
stack = [root]
while stack:
    d = stack.pop()
    try:
        with os.scandir(d) as it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(Path(e.path))
                    elif e.is_file(follow_symlinks=False):
                        used2 += e.stat().st_size
                        n2 += 1
                except OSError:
                    continue
    except OSError:
        continue
t_scandir = time.monotonic() - t0
print(f"os.scandir 方式 ：{t_scandir:.1f}s，{used2/1e9:.2f} GB，{n2:,} 个文件")
print(f"\n加速比：{t_rglob/max(t_scandir,1e-9):.1f}x")
print(f"结论：{'这是白屏主因' if t_rglob > 5 else '遍历不算慢，另有原因'}")
