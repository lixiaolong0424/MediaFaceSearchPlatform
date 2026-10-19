"""量化「每文件提交」的代价：修写锁饿死问题的同时，抽帧付出了多少？

背景
----
`scan.commit_batch_fused_photos` 从 100 降到 1 之后：
  * 好处：写锁窗口从约 12 秒降到毫秒级，并发的语义编码进程不再饿死；
  * 疑问：每处理 1 个文件就提交一次，抽帧自己会不会被拖慢？
    （WAL + synchronous=NORMAL 下提交不需要 fsync，理论上很便宜，但必须量。）

两部分测量
----------
A. **真实库上的实际提交延迟**（含与抽帧抢锁的等待）
   对同一行的 `updated_at` 写回它自己的值 —— 语义上零改动，只为触发一次真实事务。
B. **副本库上的纯提交开销**（无并发争抢）
   复刻 process_photo 一次真实写入的语句集合，比 commit-every-1 与 commit-every-100。

用法：
    .venv\\Scripts\\python.exe tmp\\measure_commit_cost.py
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
COPY = ROOT / "tmp" / "_commit_cost.db"


def pct(vals, p):
    vals = sorted(vals)
    n = len(vals)
    return vals[min(n - 1, max(0, int(round(p / 100.0 * (n - 1)))))]


def part_a(samples: int = 600) -> None:
    print("=" * 74)
    print("A. 真实库上的提交延迟（与正在跑的抽帧进程真实争抢）")
    print("=" * 74)
    conn = sqlite3.connect(str(DB), timeout=60.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=60000")
    row = conn.execute("SELECT file_id, updated_at FROM files LIMIT 1").fetchone()
    fid, ts = row[0], row[1]
    waits = []
    for _ in range(samples):
        t0 = time.perf_counter()
        conn.execute("BEGIN IMMEDIATE")
        # 写回自己的值：不改变任何语义，只为走一次完整的真实事务
        conn.execute("UPDATE files SET updated_at=? WHERE file_id=?", (ts, fid))
        conn.execute("COMMIT")
        waits.append((time.perf_counter() - t0) * 1000.0)
        time.sleep(0.02)          # 铺开到多个提交周期
    conn.close()
    n = len(waits)
    med = statistics.median(waits)
    print(f"  样本 {n}  中位 {med:.2f} ms  p90 {pct(waits,90):.2f} ms  "
          f"p99 {pct(waits,99):.2f} ms  最大 {max(waits):.2f} ms")
    total_s = sum(waits) / 1000.0
    print(f"  合计 {total_s:.2f}s / {n} 次")
    print(f"  → 按 44.4 万张照片、每张一次提交外推：{med*n and med*444000/1000/60:.1f} 分钟"
          f"（占 17~20 小时抽帧的 {(med*444000/1000/60)/(18*60)*100:.2f}%）")


def part_b(rows: int = 3000) -> None:
    print()
    print("=" * 74)
    print("B. 副本库上的纯提交开销（无并发）：commit=1 vs commit=100")
    print("=" * 74)
    if COPY.exists():
        COPY.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(COPY) + suffix)
        if p.exists():
            p.unlink()
    t0 = time.monotonic()
    shutil.copy2(DB, COPY)
    print(f"  复制库 {DB.stat().st_size/1e6:.0f} MB 用了 {time.monotonic()-t0:.1f}s")

    def bench(commit_every: int) -> float:
        conn = sqlite3.connect(str(COPY), timeout=60.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=60000")
        ids = [r[0] for r in conn.execute("SELECT file_id FROM files LIMIT ?", (rows,))]
        n = len(ids)
        t0 = time.monotonic()
        for i, fid in enumerate(ids, 1):
            # 复刻 process_photo 的一次真实写入：files + 两个 stage + shots + phash
            conn.execute("UPDATE files SET captured_at=captured_at, time_source=time_source,"
                         " width=width, height=height, status='done', indexed_at=indexed_at,"
                         " updated_at=updated_at WHERE file_id=?", (fid,))
            for stage in ("dedup", "extract"):
                conn.execute(
                    "INSERT INTO stage_status(file_id,stage,status,shots,frames,updated_at)"
                    " VALUES(?,?,'done',1,1,'x') ON CONFLICT(file_id,stage) DO UPDATE SET"
                    " status='done'", (fid, stage))
            conn.execute(
                "INSERT INTO shots(file_id,shot_ix,shot_start,shot_end,n_frames)"
                " VALUES(?,0,0.0,0.0,1) ON CONFLICT(file_id,shot_ix) DO UPDATE SET n_frames=1",
                (fid,))
            conn.execute("UPDATE files SET phash=? WHERE file_id=?", ("0" * 16, fid))
            if i % commit_every == 0:
                conn.execute("COMMIT") if conn.in_transaction else None
                conn.commit()
        conn.commit()
        el = time.monotonic() - t0
        conn.close()
        return n / el

    r1 = bench(1)
    r100 = bench(100)
    print(f"  commit=1    {r1:7.1f} 文件/秒")
    print(f"  commit=100  {r100:7.1f} 文件/秒")
    print(f"  → 每文件提交的代价：{(1/r1 - 1/r100)*1000:+.2f} ms/文件"
          f"（{(r100 - r1)/r100*100:+.1f}% 吞吐）")
    print(f"  → 按 44.4 万张外推，多花 {444000*(1/r1 - 1/r100)/60:+.1f} 分钟")


def main() -> int:
    part_a()
    part_b()
    print()
    print("=" * 74)
    print("结论：见上面两段。若 A 的中位数是毫秒级、B 的差值是零点几毫秒/文件，")
    print("      说明「每文件提交」换来的并发安全几乎是免费的。")
    print("=" * 74)
    if COPY.exists():
        COPY.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(COPY) + suffix)
        if p.exists():
            p.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
