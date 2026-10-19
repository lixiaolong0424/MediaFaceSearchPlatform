"""核实两件事：
  1) 照片的关键帧到底在哪（验证「照片不在 frames/ 下」）
  2) frames/ 里有没有「孤儿 JPEG」——目录里存在、但 shots 表已经不认的帧
     （若旧运行留下的帧没被新运行覆盖，就会成为永久孤儿，白占空间）
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

    # ---------- 1) 照片的关键帧在哪 ----------
    print("=== 1) 照片：shots 记录 vs 磁盘帧 ===")
    rows = con.execute("""
        SELECT f.file_id, f.path, s.shot_ix, s.n_frames
          FROM files f JOIN shots s ON s.file_id = f.file_id
         WHERE f.kind='photo' AND f.status='done'
         ORDER BY f.file_id LIMIT 5""").fetchall()
    for r in rows:
        fid = int(r["file_id"])
        d = FRAMES / str(fid)
        n_disk = len(list(d.glob("*.jpg"))) if d.is_dir() else 0
        exists = Path(str(r["path"])).exists()
        print(f"  file_id={fid:<6} shot_ix={r['shot_ix']} n_frames={r['n_frames']}  "
              f"frames/{fid}/ 存在={d.is_dir()}  磁盘帧数={n_disk}  "
              f"原图存在={exists}")
    n_photo_dirs = sum(1 for r in con.execute(
        """SELECT f.file_id FROM files f WHERE f.kind='photo' AND f.status='done'""")
        if (FRAMES / str(r["file_id"])).is_dir())
    print(f"  → 已完成照片共 {con.execute(chr(83)+'ELECT COUNT(*) FROM files WHERE kind=' + chr(39) + 'photo' + chr(39) + ' AND status=' + chr(39) + 'done' + chr(39)).fetchone()[0]:,} 个，"
          f"其中在 frames/ 下有目录的：{n_photo_dirs} 个")
    print("  结论：照片的关键帧 = 原图本身，不会复制进 frames/\n")

    # ---------- 2) frames/ 里的孤儿 JPEG ----------
    print("=== 2) frames/ 孤儿 JPEG 检查 ===")
    # shots 表认账的帧：用 (file_id, shot_ix) 前缀匹配
    claimed: dict[int, set[str]] = {}
    for r in con.execute("SELECT file_id, shot_ix, n_frames FROM shots"):
        fid = int(r["file_id"])
        if int(r["n_frames"] or 0) > 0:
            claimed.setdefault(fid, set()).add(f"{int(r['shot_ix']):04d}_")

    dirs = sorted((p for p in FRAMES.iterdir() if p.is_dir()), key=lambda p: int(p.name))
    total = orphan = 0
    orphan_bytes = 0
    dirs_all_orphan = 0
    samples = []
    for d in dirs:
        fid = int(d.name)
        jpgs = list(d.glob("*.jpg"))
        if not jpgs:
            continue
        total += len(jpgs)
        prefs = claimed.get(fid)
        if prefs is None:
            # shots 表里这个 file_id 一条记录都没有 → 整个目录都是未提交/孤儿
            orphans = jpgs
        else:
            orphans = [j for j in jpgs if j.name[:5] not in prefs]
        if len(orphans) == len(jpgs):
            dirs_all_orphan += 1
        orphan += len(orphans)
        for j in orphans:
            try:
                orphan_bytes += j.stat().st_size
            except OSError:
                pass
        if orphans and len(samples) < 6:
            samples.append((fid, len(jpgs), len(orphans),
                            [j.name for j in orphans[:3]]))

    print(f"  frames 下 JPEG 总数     : {total:,}")
    print(f"  孤儿 JPEG（shots 不认） : {orphan:,}  ({orphan/max(1,total)*100:.1f}%)")
    print(f"  孤儿占用                : {orphan_bytes/1024/1024:.1f} MB")
    print(f"  整个目录都是孤儿的      : {dirs_all_orphan:,} 个（多为未提交批次）")
    for fid, tot, orp, names in samples:
        print(f"    file_id={fid:<6} 共{tot:<4} 孤儿{orp:<4} 例: {names}")

    # 区分：孤儿目录是否属于「已提交」的 file_id
    committed = {int(r[0]) for r in con.execute(
        "SELECT file_id FROM stage_status WHERE stage='extract'")}
    orphan_in_committed = [d for d in dirs
                           if int(d.name) in committed
                           and any(j.name[:5] not in claimed.get(int(d.name), set())
                                   for j in d.glob("*.jpg"))]
    print(f"\n  在「已提交」file_id 里仍有孤儿的目录: {len(orphan_in_committed):,} 个")
    print(f"    file_id 示例: {[int(d.name) for d in orphan_in_committed[:10]]}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
