"""说明 frames/ 目录的真实结构：谁在里面、命名规则是什么（只读）。"""

from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
FRAMES = ROOT / "data" / "nvme" / "frames"


def main() -> int:
    con = sqlite3.connect(f"file:{DB.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    kind_of = {int(r["file_id"]): str(r["kind"])
               for r in con.execute("SELECT file_id, kind FROM files")}

    dirs = sorted((p for p in FRAMES.iterdir() if p.is_dir()),
                  key=lambda p: int(p.name) if p.name.isdigit() else 0)
    print(f"frames 根目录 : {FRAMES}")
    print(f"子目录数量    : {len(dirs):,}\n")

    print("=== 这些子目录对应什么类型的文件 ===")
    c = Counter(kind_of.get(int(d.name), "?") for d in dirs if d.name.isdigit())
    print(f"  {dict(c)}")
    print("  → 照片**不在**这里（照片的关键帧就是原图本身）\n")

    print("=== 命名规则 ===")
    print("  目录名      = <file_id>            （数据库 files.file_id，全局唯一）")
    print("  文件名      = <shot_ix:04d>_<ts_ms:09d>.jpg")
    print("                shot_ix  = 该文件内的第几个镜头（从 0 开始，4 位补零）")
    print("                ts_ms    = 该帧在视频内的时间点，毫秒（9 位补零）\n")

    print("=== 实例（取前 3 个目录各列几个文件）===")
    for d in dirs[:3]:
        fid = int(d.name)
        jpgs = sorted(d.glob("*.jpg"))
        row = con.execute("SELECT path, duration_sec FROM files WHERE file_id=?",
                          (fid,)).fetchone()
        name = Path(str(row["path"])).name if row else "?"
        print(f"\n  frames\\{d.name}\\   （{kind_of.get(fid)}，{len(jpgs)} 帧）")
        print(f"    源文件: {name[:56]}")
        for j in jpgs[:5]:
            stem, _, ms = j.stem.partition("_")
            try:
                sec = int(ms) / 1000.0
            except ValueError:
                sec = -1
            print(f"      {j.name}   →  shot_ix={int(stem)}  时间点={sec:.3f}s  "
                  f"{j.stat().st_size//1024}KB")
        if len(jpgs) > 5:
            print(f"      …（共 {len(jpgs)} 个）")

    print("\n=== 与 shots 表对照（证明目录名=file_id，文件名前缀=shot_ix）===")
    if dirs:
        fid = int(dirs[0].name)
        rows = con.execute(
            "SELECT shot_ix, shot_start, shot_end, n_frames FROM shots "
            "WHERE file_id=? ORDER BY shot_ix LIMIT 6", (fid,)).fetchall()
        print(f"  shots 表里 file_id={fid} 的前 6 行：")
        for r in rows:
            n_disk = len(list((FRAMES / str(fid)).glob(f"{r['shot_ix']:04d}_*.jpg")))
            print(f"    shot_ix={r['shot_ix']:<3} {r['shot_start']:>9.2f}~{r['shot_end']:<9.2f}"
                  f" n_frames={r['n_frames']:<3} 磁盘上同名前缀文件={n_disk}")

    print("\n=== 其它派生物目录 ===")
    for key, p in (("frames（关键帧，可再生）", FRAMES),
                   ("thumbs（素材缩略图）", ROOT / "data/nvme/thumbs"),
                   ("faces/raw（人脸原图）", ROOT / "data/nvme/faces/raw"),
                   ("faces/thumbs（人脸墙）", ROOT / "data/nvme/faces/thumbs"),
                   ("embeddings（编码缓存）", ROOT / "data/nvme/embeddings"),
                   ("query_cache（上传的检索图）", ROOT / "data/nvme/query_cache")):
        if p.exists():
            n = sum(1 for _ in p.rglob("*") if _.is_file())
            b = sum(_.stat().st_size for _ in p.rglob("*") if _.is_file())
            print(f"  {key:<26} {n:>8,} 个文件  {b/1024/1024:>8.1f} MB  {p}")
        else:
            print(f"  {key:<26} {'（不存在）':>8}            {p}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
