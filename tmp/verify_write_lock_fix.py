"""写锁修复的阳性对照验证：证明探针真的能测出长锁窗口。

只报「中位 0.02ms」是不够的 —— 万一探针本身不灵敏，干净的结果就毫无意义。
所以这里注入一个**已知长度的写锁**，看探针能否如实报出来：

  阶段 A  无注入        → 期望 p99 小、最大 < 1s（生产环境的真实情况）
  阶段 B  注入 4 秒锁   → 期望最大 ≈ 4s（证明仪器灵敏，能抓到长锁窗口）

同时顺带量一下编码进程在同一时间窗里的真实进度速率（最终要看的其实是这个）。

用法：
    .venv\\Scripts\\python.exe tmp\\verify_write_lock_fix.py
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
PY = str(ROOT / ".venv" / "Scripts" / "python.exe")

HOLD_SECONDS = 4.0


def probe(duration: float, interval: float = 0.002) -> dict:
    conn = sqlite3.connect(str(DB), timeout=60.0, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=60000")
    waits: list[float] = []
    t_end = time.perf_counter() + duration
    while time.perf_counter() < t_end:
        t0 = time.perf_counter()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            waits.append((time.perf_counter() - t0) * 1000.0)
        except sqlite3.OperationalError:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
        time.sleep(interval)
    conn.close()
    waits.sort()
    n = len(waits)

    def pct(p: float) -> float:
        return waits[min(n - 1, max(0, int(round(p / 100.0 * (n - 1)))))] if n else 0.0

    return {"n": n, "min": waits[0] if n else 0.0,
            "median": waits[n // 2] if n else 0.0,
            "p99": pct(99), "max": waits[-1] if n else 0.0}


HOLDER = r"""
import sqlite3, sys, time
conn = sqlite3.connect(r'{db}', timeout=60.0, isolation_level=None)
conn.execute("PRAGMA busy_timeout=60000")
conn.execute("BEGIN IMMEDIATE")
conn.execute("UPDATE files SET updated_at=updated_at WHERE file_id=(SELECT MIN(file_id) FROM files)")
print("holder: 已取得写锁", flush=True)
time.sleep({hold})
conn.execute("ROLLBACK")
print("holder: 已释放（ROLLBACK，零数据改动）", flush=True)
"""


def encoder_rate(seconds: float = 60.0) -> dict:
    """量最近 seconds 秒里编码进程的推进速度（读日志里的进度行）。"""
    logs = sorted((ROOT / "logs").glob("chain_r*_encode_*.log"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    if not logs:
        return {}
    log = logs[0]
    pat = re.compile(r"^(\S+ \S+).*进度 (\d+)/(\d+)")
    rows = []
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = pat.search(line)
        if m:
            rows.append((m.group(1), int(m.group(2))))
    if len(rows) < 2:
        return {"log": log.name, "samples": len(rows)}
    # 取最后两条相隔 >= seconds 的记录
    last_t, last_n = rows[-1]
    prev = None
    for t, n in reversed(rows[:-1]):
        try:
            d = (time.strptime(last_t, "%Y-%m-%d %H:%M:%S")
                 and (time.mktime(time.strptime(last_t, "%Y-%m-%d %H:%M:%S"))
                      - time.mktime(time.strptime(t, "%Y-%m-%d %H:%M:%S"))))
        except ValueError:
            d = 0
        if d >= seconds:
            prev = (t, n, d)
            break
    if prev is None:
        prev = (rows[0][0], rows[0][1], 0)
    t, n, d = prev
    if d <= 0:
        return {"log": log.name, "samples": len(rows), "note": "间隔不足，无法计算"}
    return {"log": log.name, "from": t, "to": last_t, "span_sec": d,
            "files": last_n - n, "rate": (last_n - n) / d}


def main() -> int:
    print("=" * 74)
    print("写锁修复验证 · 阳性对照")
    print("=" * 74)

    print(f"\n[阶段 A] 无注入，探针跑 20 秒（生产真实情况）")
    a = probe(20.0)
    print(f"  采样 {a['n']} 次  中位 {a['median']:.2f}ms  p99 {a['p99']:.2f}ms  最大 {a['max']:.1f}ms")

    print(f"\n[阶段 B] 注入一个已知的 {HOLD_SECONDS:.0f} 秒写锁（阳性对照）")
    code = HOLDER.format(db=str(DB).replace("\\", "\\\\"), hold=HOLD_SECONDS)
    holder = subprocess.Popen([PY, "-u", "-c", code],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    time.sleep(1.0)          # 等它真的拿到锁
    b = probe(8.0)
    try:
        out, _ = holder.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        holder.kill()
        out = ""
    for line in (out or "").splitlines():
        print(f"  {line}")
    print(f"  采样 {b['n']} 次  中位 {b['median']:.2f}ms  p99 {b['p99']:.2f}ms  最大 {b['max']:.1f}ms")

    print("\n[阶段 C] 编码进程在最近 60 秒里的真实推进速度")
    r = encoder_rate(60.0)
    if "rate" in r:
        print(f"  {r['log']}: {r['from']} → {r['to']}（{r['span_sec']:.0f}s）"
              f"  {r['files']} 个文件 = {r['rate']:.2f} 文件/秒")
    else:
        print(f"  {r}")

    print("\n" + "=" * 74)
    print("判定")
    print("=" * 74)
    sens = b["max"] >= HOLD_SECONDS * 1000 * 0.5
    clean = a["max"] < 1000 and a["p99"] < 100
    print(f"  仪器灵敏度（注入 {HOLD_SECONDS:.0f}s 锁 → 探针报最大 {b['max']:.0f}ms）："
          f"{'通过，探针确实能抓到长锁' if sens else '不通过，结论不可信'}")
    print(f"  生产环境锁窗口（无注入 → 最大 {a['max']:.0f}ms，p99 {a['p99']:.2f}ms）："
          f"{'毫秒级，写锁修复生效' if clean else '仍有秒级长尾，修复不彻底'}")
    return 0 if (sens and clean) else 1


if __name__ == "__main__":
    raise SystemExit(main())
