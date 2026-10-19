"""查清 frames/ 里那 277 个「不在已提交集合」的 file_id 是什么。

可能解释：
  a) 被 kill 时未提交的批次（stage_status 只在每 500 个文件提交一次）
  b) 旧版 03_extract 在 shots 被重建前留下的孤儿目录
  c) 我的测试脚本写入的残留
需要区分，否则清理会漏或误删。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
FRAMES = ROOT / "data" / "nvme" / "frames"


def main() -> int:
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row

    committed = {int(r[0]) for r in con.execute(
        "SELECT file_id FROM stage_status WHERE stage='extract'")}
    dir_ids = {int(p.name) for p in FRAMES.iterdir() if p.is_dir() and p.name.isdigit()}
    orphan = sorted(dir_ids - committed)
    print(f"已提交 file_id: {len(committed):,}   frames 目录: {len(dir_ids):,}   "
          f"孤儿: {len(orphan):,}\n")

    print("=== 孤儿 file_id 在 files 表里的状态 ===")
    rows = con.execute(
        f"""SELECT file_id, kind, status, duration_sec, path
              FROM files WHERE file_id IN ({','.join('?' * len(orphan))})""",
        orphan).fetchall()
    from collections import Counter
    print("  by kind/status:", dict(Counter((r["kind"], r["status"]) for r in rows)))
    durs = [r["duration_sec"] for r in rows if r["duration_sec"]]
    if durs:
        durs.sort()
        print(f"  时长：n={len(durs)}  min={durs[0]:.0f}s  "
              f"中位={durs[len(durs)//2]:.0f}s  max={durs[-1]:.0f}s  合计={sum(durs)/3600:.1f}h")

    print("\n=== 前 5 个孤儿的 shots 表记录 ===")
    for fid in orphan[:5]:
        r = con.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(n_frames),0) f FROM shots WHERE file_id=?",
            (fid,)).fetchone()
        nfs = len(list((FRAMES / str(fid)).glob("*.jpg"))) if (FRAMES / str(fid)).exists() else 0
        print(f"  file_id={fid:<6} shots={r['n']:<4} shots.n_frames合计={r['f']:<5} "
              f"磁盘实际帧={nfs}")

    print("\n=== 这些孤儿是否出现在 stage_status 其它阶段 ===")
    try:
        for row in con.execute(
            f"""SELECT stage, status, COUNT(*) n FROM stage_status
                 WHERE file_id IN ({','.join('?' * len(orphan))})
                 GROUP BY stage, status""", orphan):
            print(f"  {row['stage']}:{row['status']} = {row['n']:,}")
    except sqlite3.OperationalError as exc:
        print(f"  （查询失败：{exc}）")

    print("\n=== 帧数分布（孤儿目录）===")
    counts = sorted((len(list((FRAMES / str(i)).glob('*.jpg'))) for i in orphan), reverse=True)
    print(f"  最多的 8 个：{counts[:8]}")
    print(f"  最少的 8 个：{counts[-8:]}")
    print(f"  合计 {sum(counts):,} 帧")

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
