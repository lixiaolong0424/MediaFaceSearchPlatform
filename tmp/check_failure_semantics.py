"""确认失败文件的重试语义 + 当前失败统计（只读）。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"


def main() -> int:
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    print("=== files.status ===")
    for r in con.execute("SELECT status, COUNT(*) n FROM files GROUP BY status"):
        print(f"  {r['status']:<12} {r['n']:>9,}")

    print("\n=== shots 质量 ===")
    r = con.execute("""SELECT COUNT(*) total, COALESCE(SUM(n_frames),0) frames,
                              SUM(CASE WHEN COALESCE(n_frames,0)=0 THEN 1 ELSE 0 END) zero
                         FROM shots""").fetchone()
    print(f"  镜头总数   : {r['total']:,}")
    print(f"  关键帧总数 : {r['frames']:,}")
    print(f"  0 帧镜头   : {r['zero']:,}  (占 {r['zero']/max(1,r['total'])*100:.2f}%)")

    print("\n=== file_encoding 里的错误记录 ===")
    try:
        for r in con.execute(
            "SELECT model, status, COUNT(*) n FROM file_encoding GROUP BY model, status"):
            print(f"  {r['model']}:{r['status']:<10} {r['n']:>8,}")
    except sqlite3.OperationalError as exc:
        print(f"  （{exc}）")

    print("\n=== 关键：失败文件会不会被下次重试 ===")
    # 失败时不会写 stage_status，所以 LEFT JOIN 得到 NULL → 条件成立 → 会被重试
    n_stage = con.execute(
        "SELECT COUNT(*) FROM stage_status WHERE stage='extract'").fetchone()[0]
    n_pickable = con.execute("""
        SELECT COUNT(*) FROM files f
          LEFT JOIN stage_status ss ON ss.file_id=f.file_id AND ss.stage='extract'
         WHERE f.kind IN ('photo','video')
           AND f.status IN ('pending','error')
           AND (ss.status IS NULL OR ss.status IN ('pending','error'))""").fetchone()[0]
    n_err_no_stage = con.execute("""
        SELECT COUNT(*) FROM files f
          LEFT JOIN stage_status ss ON ss.file_id=f.file_id AND ss.stage='extract'
         WHERE f.status='error' AND ss.file_id IS NULL""").fetchone()[0]
    print(f"  stage_status(extract) 已有 : {n_stage:,}")
    print(f"  下次会被拾取的总数         : {n_pickable:,}")
    print(f"  其中 status='error' 且无阶段记录（=会重试的失败文件）: {n_err_no_stage:,}")

    print("\n=== 死信表 ===")
    try:
        n_dl = con.execute("SELECT COUNT(*) FROM dead_letter").fetchone()[0]
        print(f"  dead_letter 记录 : {n_dl:,}（03_extract 不写这张表，只有 12_maintain repair 会写）")
    except sqlite3.OperationalError as exc:
        print(f"  （{exc}）")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
