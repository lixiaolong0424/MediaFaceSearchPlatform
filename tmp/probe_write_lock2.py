"""写锁窗口实证探针（按时间铺开采样版）。

为什么第一版不够：`BEGIN IMMEDIATE; ROLLBACK;` 单次只要微秒级，
200 次全在 1 毫秒内跑完 —— 那只采样了一个瞬间，而抽帧的提交周期是
「每文件一次」（约 180ms）。必须把采样铺开到多个提交周期才有意义。

判据
----
* `commit_batch_fused_photos = 1`（现在的值）→ 锁窗口毫秒级：
  抢锁延迟中位 < 5ms，p99 < 100ms，**不该出现秒级长尾**。
* `= 100`（修复前的值）→ 锁窗口 ≈ 12 秒：会稳定出现数秒级等待。

做法：`BEGIN IMMEDIATE; ROLLBACK;`（申请真写锁后立刻回滚，零数据副作用）
用法：
    .venv\\Scripts\\python.exe tmp\\probe_write_lock2.py [秒数] [采样间隔ms]
"""

from __future__ import annotations

import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"

duration = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
interval_ms = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
interval = interval_ms / 1000.0

conn = sqlite3.connect(str(DB), timeout=60.0, isolation_level=None)
conn.execute("PRAGMA busy_timeout=60000")

waits = []
fail = 0
t_end = time.perf_counter() + duration
while time.perf_counter() < t_end:
    t0 = time.perf_counter()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        waits.append((time.perf_counter() - t0) * 1000.0)
    except sqlite3.OperationalError as exc:
        fail += 1
        print(f"  !! {exc}")
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
    time.sleep(interval)
conn.close()

if not waits:
    print(f"全部失败：fail={fail}")
    raise SystemExit(1)

waits.sort()
n = len(waits)


def pct(p: float) -> float:
    return waits[min(n - 1, max(0, int(round(p / 100.0 * (n - 1)))))]


print(f"采样 {n} 次 / {duration:.0f}s（间隔 {interval_ms:.0f}ms，失败 {fail}）")
print(f"  最小 {waits[0]:8.2f} ms")
print(f"  中位 {statistics.median(waits):8.2f} ms")
print(f"  p90  {pct(90):8.2f} ms")
print(f"  p99  {pct(99):8.2f} ms")
print(f"  最大 {waits[-1]:8.2f} ms")
for thr in (1, 5, 20, 100, 1000):
    c = sum(1 for w in waits if w > thr)
    print(f"  >{thr:>4}ms : {c:>5} 次 ({c / n * 100:5.2f}%)")
print()
if pct(99) < 100 and sum(1 for w in waits if w > 1000) == 0:
    print("结论：锁窗口毫秒级 —— commit_batch_fused_photos=1 生效，写锁没有被长时间独占。")
elif pct(99) >= 1000:
    print("结论：存在秒级锁窗口 —— 融合趟没有每文件提交，并发编码会被饿死。")
else:
    print("结论：有百毫秒级等待但无秒级长尾；结合编码进程实际进度一起判断。")
