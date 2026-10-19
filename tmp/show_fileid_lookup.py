"""演示：如何由 file_id 查到源视频的文件名与位置（只读）。

三种入口都给出来：
  A. 用 sqlite3 命令行（最简单，一条 SELECT）
  B. 用 Python 一行式（需要中文正常显示时更稳）
  C. 反向：从 frames/ 里的文件名反查是哪个源视频
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
FRAMES = ROOT / "data" / "nvme" / "frames"


def main() -> int:
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    print("=== files 表里与「源文件位置」相关的列 ===")
    cols = [r[1] for r in con.execute("PRAGMA table_info(files)")]
    for c in ("file_id", "path", "rel_path", "kind", "status", "duration_sec",
              "captured_at", "size"):
        if c in cols:
            print(f"  {c}")
    print("  → 路径在 path 列（绝对路径）；rel_path 是相对素材根的路径\n")

    print("=== A) 一条 SELECT 就能拿到（任意 file_id）===")
    print("  SELECT file_id, kind, path, duration_sec FROM files WHERE file_id = 1;")
    r = con.execute("SELECT file_id, kind, path, duration_sec FROM files WHERE file_id=1"
                    ).fetchone()
    if r:
        print(f"  → {r['file_id']} | {r['kind']} | {r['path']} | {r['duration_sec']}s\n")

    print("=== 批量：frames/ 下有关键帧的那些视频 ===")
    ids = sorted(int(p.name) for p in FRAMES.iterdir() if p.is_dir())
    ph = ",".join("?" * len(ids))
    rows = con.execute(
        f"""SELECT file_id, kind, ROUND(duration_sec,1) dur, path
              FROM files WHERE file_id IN ({ph}) ORDER BY file_id LIMIT 8""", ids).fetchall()
    print(f"  （frames/ 下共 {len(ids)} 个 file_id，列前 8 个）")
    for r in rows:
        print(f"  file_id={r['file_id']:<6} {r['kind']:<6} {str(r['dur']):>8}s  {r['path']}")

    print("\n=== B) 反向：从 frames 里的某个 jpg 反查源视频 ===")
    d = next((p for p in sorted(FRAMES.iterdir(), key=lambda x: int(x.name))
              if p.is_dir() and list(p.glob('*.jpg'))), None)
    if d:
        fid = int(d.name)
        sample = sorted(d.glob("*.jpg"))[0]
        stem, _, ms = sample.stem.partition("_")
        r = con.execute("SELECT path, duration_sec FROM files WHERE file_id=?", (fid,)).fetchone()
        print(f"  文件: {FRAMES}\\{d.name}\\{sample.name}")
        print(f"  → 目录名 {d.name} 就是 file_id")
        print(f"  → shot_ix={int(stem)}，时间点={int(ms)/1000:.3f}s")
        print(f"  → 源视频: {r['path']}")
        print(f"  → 该帧对应视频内 {int(ms)/1000:.1f}s 处（共 {r['duration_sec']}s）")

    print("\n=== C) 想按名字找：用 LIKE 模糊查 ===")
    print("  SELECT file_id, path FROM files WHERE path LIKE '%关键词%';")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
