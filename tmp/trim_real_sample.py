"""把真实素材抽查库裁剪成小样本：只留少量照片 + 1 个最短视频，便于快速跑通全链路。"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "tests" / "_real" / "library_real.db"

N_PHOTO = 6
N_VIDEO = 1


def main() -> int:
    if not DB.exists():
        print(f"库不存在：{DB}")
        return 1
    con = sqlite3.connect(str(DB))
    try:
        con.execute("DELETE FROM stage_status")
        con.execute("DELETE FROM file_encoding")
        short_videos = [r[0] for r in con.execute(
            "SELECT file_id FROM files WHERE kind='video' "
            "ORDER BY COALESCE(duration_sec, 1e9) LIMIT ?", (N_VIDEO,))]
        photos = [r[0] for r in con.execute(
            "SELECT file_id FROM files WHERE kind='photo' LIMIT ?", (N_PHOTO,))]

        def keep(table_ids: list[int]) -> str:
            return ",".join(str(i) for i in table_ids) or "-1"

        con.execute(
            f"UPDATE files SET status='skipped_dup' "
            f"WHERE (kind='video' AND file_id NOT IN ({keep(short_videos)})) "
            f"   OR (kind='photo' AND file_id NOT IN ({keep(photos)}))")
        con.commit()
        rows = con.execute(
            "SELECT kind, status, COUNT(*) FROM files GROUP BY kind, status").fetchall()
        print("file_id 分布：")
        for k, st, n in rows:
            print(f"  {k:<8} {st:<12} {n}")
        kept = con.execute(
            "SELECT file_id, kind, duration_sec, path FROM files WHERE status='pending' "
            "ORDER BY kind").fetchall()
        print(f"将进入编码的文件 {len(kept)} 个：")
        for fid, kind, dur, path in kept:
            print(f"  {fid} {kind:<6} dur={dur} {Path(path).name}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
