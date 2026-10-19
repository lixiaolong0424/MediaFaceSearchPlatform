"""写锁窗口实证探针：在抽帧正在运行时，从另一个连接抢写锁并测获取延迟。

判据（这是本次要验证的核心）
--------------------------
`scan.commit_batch_fused_photos` 控制融合照片趟的提交间隔，而提交间隔 = 写锁窗口。

  * 设成 1（现在的值）→ 每处理 1 个文件就提交，锁窗口应是**毫秒级**：
    抢锁延迟中位数 < 50ms，p95 < 300ms，几乎不该出现秒级。
  * 设成 100（修复前的值）→ 锁窗口 ≈ 12 秒：抢锁延迟会出现**几秒到十几秒**的长尾。

做法：`BEGIN IMMEDIATE; ROLLBACK;`
  * `BEGIN IMMEDIATE` 会真正申请写锁（不像普通 BEGIN 是延迟的）
  * 立刻 ROLLBACK → **不写任何数据**，对生产库零副作用
  * 测「申请到写锁花了多久」，就是当前锁窗口的直接度量

用法：
    .venv\\Scripts\\python.exe tmp\\probe_write_lock.py [samples]
"""

from __future__ import annotations

import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"

samples = int(sys.argv[1]) if len(sys.argv) > 1 else 200

# 与生产代码同一套 pragma（busy_timeout 给足，否则我们测到的是自己的超时而非真实等待）
conn = sqlite3.connect(str(DB), timeout=60.0, isolation_level=None)
conn.execute("PRAGMA busy_timeout=60000")

waits = []
fail = 0
before = time.perf_counter()
for _ in range(samples):
    t0 = time.perf_counter()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")
        waits.append((time.perf_counter() - t0) * 1000.0)
    except sqlite3.OperationalError:
        fail += 1
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
total = time.perf_counter() - before
conn.close()

if not waits:
    print(f"全部失败：fail={fail}")
    raise SystemExit(1)

waits.sort()
n = len(waits)


def pct(p: float) -> float:
    return waits[min(n - 1, max(0, int(round(p / 100.0 * (n - 1)))))]


print(f"样本 {n} 次（失败 {fail}），总耗时 {total:.1f}s，即每秒可抢锁 {n / total:.0f} 次")
print(f"  最小 {waits[0]:8.1f} ms")
print(f"  中位 {statistics.median(waits):8.1f} ms")
print(f"  p90  {pct(90):8.1f} ms")
print(f"  p99  {pct(99):8.1f} ms")
print(f"  最大 {waits[-1]:8.1f} ms")
over100 = sum(1 for w in waits if w > 100)
over1000 = sum(1 for w in waits if w > 1000)
print(f"  >100ms 的次数：{over100} ({over100 / n * 100:.1f}%)")
print(f"  >1000ms 的次数：{over1000} ({over1000 / n * 100:.1f}%)")
print()
if pct(99) < 300 and over1000 == 0:
    print("结论：锁窗口是毫秒级 —— commit_batch_fused_photos=1 生效，写锁不再被长时间独占。")
elif pct(99) > 1000:
    print("结论：仍有秒级锁窗口 —— 融合趟没有每文件提交，编码进程会被饿死。")
else:
    print("结论：介于两者之间，需要结合编码进程的实际进度一起判断。")
