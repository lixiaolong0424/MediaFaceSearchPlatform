"""02_dedup 的「代表项被淘汰」回归测试。

背景（2026-10-16 实测抓到的真实 bug）
------------------------------------
早期实现的近重复分支长这样：

    keep, drop = _decide_keeper(conn, fid, matched)
    mark_skipped_dup(drop)
    # 然后……就没有然后了：索引里的代表仍然是 matched

于是当「新文件更好、旧代表该被淘汰」时：
  * 旧代表被标 skipped_dup（对）
  * 但索引里代表没换成新文件 → 后续同组文件继续与这条**已死**的代表比
  * 结果同一个 file_id 被反复标 skipped_dup，而真正该淘汰的新文件留在库里
落在真实日志里就是连续多行 `去重：file_id=57（保留 60）/ 去重：file_id=57（保留 61）/ …`

断言（修复后必须成立）
--------------------
1. 同一哈希组最终**只剩一个** status != 'skipped_dup' 的文件
2. 胜出者由 _decide_keeper 的分辨率/体积/mtime 规则决定
3. 被淘汰者都有 skipped_dup 记录（semantic + face 各一行，铁律 1）
4. 同一个 file_id 绝不会被标 skipped_dup 两次（幂等）

用法：
    .venv\\Scripts\\python.exe tests\\test_dedup_keeper.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.dedup_core import DedupIndex  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


# --------------------------------------------------------------------------- #
# 一个最小可用的内存库，字段与 migrations/001_init.sql 对齐
# --------------------------------------------------------------------------- #

DDL = """
CREATE TABLE files (
    file_id INTEGER PRIMARY KEY, path TEXT, width INTEGER, height INTEGER,
    size INTEGER, mtime INTEGER, kind TEXT, status TEXT, phash TEXT,
    visible INTEGER DEFAULT 1, kind_hint TEXT, updated_at TEXT
);
CREATE TABLE file_encoding (
    file_id INTEGER, model TEXT, status TEXT, n_frames INTEGER,
    error TEXT, encoded_at TEXT, PRIMARY KEY (file_id, model)
);
"""


def make_conn(rows) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(DDL)
    conn.executemany(
        "INSERT INTO files(file_id, path, width, height, size, mtime, kind, status) "
        "VALUES (?,?,?,?,?,?, 'photo', 'pending')", rows)
    return conn


def decide_keeper(conn, a: int, b: int):
    """与 02_dedup._decide_keeper 同逻辑（在该文件内是模块级函数，这里内联避免
    为了 import 而执行 02_dedup 的 argparse）。"""
    ra = conn.execute("SELECT * FROM files WHERE file_id=?", (a,)).fetchone()
    rb = conn.execute("SELECT * FROM files WHERE file_id=?", (b,)).fetchone()
    if ra is None:
        return b, a
    if rb is None:
        return a, b
    area_a = int(ra["width"] or 0) * int(ra["height"] or 0)
    area_b = int(rb["width"] or 0) * int(rb["height"] or 0)
    if area_a != area_b:
        return (a, b) if area_a > area_b else (b, a)
    if int(ra["size"] or 0) != int(rb["size"] or 0):
        return (a, b) if int(ra["size"] or 0) > int(rb["size"] or 0) else (b, a)
    return (a, b) if int(ra["mtime"] or 0) >= int(rb["mtime"] or 0) else (b, a)


def mark_dup(conn, drop: int, keep: int, threshold: int = 8) -> None:
    conn.execute("UPDATE files SET status='skipped_dup', visible=0, "
                 "kind_hint='duplicate' WHERE file_id=?", (drop,))
    for model in ("semantic", "face"):
        conn.execute(
            "INSERT INTO file_encoding(file_id, model, status, error) "
            "VALUES (?,?, 'skipped_dup', ?) ON CONFLICT(file_id, model) DO UPDATE SET "
            "status='skipped_dup', error=excluded.error",
            (drop, model, f"near-duplicate of file_id={keep} hamming<={threshold}"))


def dedup_group(conn, items, threshold: int = 8):
    """复刻修复后的 02_dedup.run() 内核（不含 pHash 计算与 DB 往返）。

    items: [(file_id, hex_hash), ...] 按 file_id 升序。
    返回 (存活 file_id 列表, skipped_dup 次数统计 dict)。
    """
    idx = DedupIndex(radius=threshold, hash_size=8)
    reps: dict[str, int] = {}
    marked: dict[int, int] = {}
    for fid, h in items:
        conn.execute("UPDATE files SET phash=? WHERE file_id=?", (h, fid))
        hit = idx.lookup(h)
        if hit is None:
            reps.setdefault(h, fid)
            idx.insert(h)
            continue
        other_hash, dist = hit
        matched = reps.get(other_hash, -1)
        if matched <= 0:
            reps[h] = fid
            idx.insert(h)
            continue
        keep, drop = decide_keeper(conn, fid, matched)
        mark_dup(conn, drop, keep, threshold)
        marked[drop] = marked.get(drop, 0) + 1
        if drop == matched:
            idx.remove(other_hash)
            reps.pop(other_hash, None)
            reps[h] = keep
            idx.insert(h)
        else:
            reps.pop(h, None)
    alive = [int(r["file_id"]) for r in conn.execute(
        "SELECT file_id FROM files WHERE status != 'skipped_dup' ORDER BY file_id")]
    return alive, marked


def test_rep_replaced_by_better_file() -> None:
    print("\n[1] 新文件更好 → 旧代表淘汰，新文件成为代表，整组只活 1 张")
    # 后 4 张分辨率更高，应胜出；但它们是相互近重复，最终只能活一个
    rows = [
        (1, "a.png", 100, 100, 5000, 1000),
        (2, "b.png", 100, 100, 5000, 1000),
        (3, "c.png", 100, 100, 5000, 1000),
        (4, "d.png", 400, 400, 9000, 2000),
        (5, "e.png", 400, 400, 9000, 2000),
    ]
    conn = make_conn(rows)
    H = "0000000000000000"
    # 全部哈希只差 ≤8 位（模拟近重复）
    hashes = [(1, H), (2, "0000000000000003"), (3, "000000000000000f"),
              (4, "000000000000003f"), (5, "00000000000000ff")]
    alive, marked = dedup_group(conn, hashes)
    check("整组只剩 1 张存活", len(alive) == 1, f"alive={alive}")
    check("存活的是分辨率最高的 4 或 5", alive and alive[0] in (4, 5), f"alive={alive}")
    check("没有任何 file_id 被重复标记",
          all(v == 1 for v in marked.values()), f"marked={marked}")
    n_dup = conn.execute("SELECT COUNT(*) FROM files WHERE status='skipped_dup'").fetchone()[0]
    check("被淘汰 4 张", n_dup == 4, f"got {n_dup}")
    for model in ("semantic", "face"):
        n = conn.execute("SELECT COUNT(*) FROM file_encoding WHERE model=? AND status='skipped_dup'",
                         (model,)).fetchone()[0]
        check(f"铁律 1：{model} 侧 skipped_dup 各 4 行", n == 4, f"got {n}")


def test_old_rep_wins_keeps_working() -> None:
    print("\n[2] 旧代表更好 → 新文件淘汰，代表不变，后续新文件仍被正确淘汰")
    rows = [
        (1, "a.png", 400, 400, 9000, 2000),   # 最好的，先出现
        (2, "b.png", 100, 100, 5000, 1000),
        (3, "c.png", 100, 100, 5000, 1000),
        (4, "d.png", 100, 100, 5000, 1000),
    ]
    conn = make_conn(rows)
    hashes = [(1, "0000000000000000"), (2, "0000000000000003"),
              (3, "000000000000000f"), (4, "000000000000003f")]
    alive, marked = dedup_group(conn, hashes)
    check("只活 file_id=1", alive == [1], f"alive={alive}")
    check("每张各标一次", sorted(marked) == [2, 3, 4], f"marked={marked}")


def test_chain_of_near_dups() -> None:
    print("\n[3] 链式近重复（1~2~3~…~20，相邻距离 2）→ 只活 1 张")
    conn = make_conn([(i, f"{i}.png", 200, 200, 5000, 1000) for i in range(1, 21)])
    # 位模式每次翻 2 位：链式相邻近重复，但首尾距离很远
    hashes = []
    v = 0
    for i in range(1, 21):
        hashes.append((i, f"{v:016x}"))
        v ^= 0b11 << ((i - 1) % 30)
    alive, marked = dedup_group(conn, hashes)
    check("链式近重复后只活 1 张", len(alive) == 1, f"alive={alive}")
    check("无重复标记", all(x == 1 for x in marked.values()), f"marked={marked}")


def test_unrelated_all_alive() -> None:
    print("\n[4] 互不相似的 10 张 → 全部存活，零 skipped_dup")
    conn = make_conn([(i, f"{i}.png", 200, 200, 5000, 1000) for i in range(1, 11)])
    # 这些位模式两两汉明距离都 > 8（注意不能用 1<<(i*6)：相邻只差 1 位，反而是近重复）
    patterns = ["0f0f0f0f0f0f0f0f", "f0f0f0f0f0f0f0f0", "00ff00ff00ff00ff",
                "ff00ff00ff00ff00", "3333333333333333", "cccccccccccccccc",
                "5555555555555555", "aaaaaaaaaaaaaaaa", "1111111111111111",
                "eeeeeeeeeeeeeeee"]
    hashes = list(zip(range(1, 11), patterns))
    alive, marked = dedup_group(conn, hashes)
    check("10 张全活", len(alive) == 10, f"alive={len(alive)}")
    check("零标记", not marked)


def test_zero_distance_same_rep() -> None:
    print("\n[5] d=0 完全相同：稳定命中原代表，代表不抖动")
    rows = [(1, "a.png", 300, 300, 9000, 1000),
            (2, "b.png", 300, 300, 9000, 2000),   # 同分辨率同体积，mtime 更新
            (3, "c.png", 300, 300, 9000, 1000)]
    conn = make_conn(rows)
    H = "abcdef0123456789"
    alive, marked = dedup_group(conn, [(1, H), (2, H), (3, H)])
    check("只活 1 张", len(alive) == 1, f"alive={alive}")
    check("存活的是 mtime 最新的 2", alive == [2], f"alive={alive}")
    check("1 与 3 各标一次", sorted(marked) == [1, 3], f"marked={marked}")


def main() -> int:
    print("=" * 72)
    print("02_dedup「代表项被淘汰」回归测试")
    print("=" * 72)
    test_rep_replaced_by_better_file()
    test_old_rep_wins_keeps_working()
    test_chain_of_near_dups()
    test_unrelated_all_alive()
    test_zero_distance_same_rep()
    print("\n" + "=" * 72)
    print(f"PASS={PASS}  FAIL={FAIL}")
    print("=" * 72)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
