"""查清 ffmpeg scdet 输出的真实格式，找出可靠的切点解析方式。

已知：scdet=threshold=10 能跑（软解 1.7~3.6x 于 PySceneDetect），
但用 showinfo + 正则抓 lavfi.scd.score 得到 0 个切点。
可能原因：score 的键名/格式与预期不同，或 metadata 没被 showinfo 打印。
本脚本直接 dump 原始输出对比几种解析路径。
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "library.db"
FFMPEG = Path(r"D:\PROGRAM\1_ffmpeg-9.0.1-essentials_build\bin\ffmpeg.exe")


def pick() -> Path | None:
    con = sqlite3.connect(str(DB))
    row = con.execute(
        "SELECT path FROM files WHERE kind='video' AND duration_sec BETWEEN 60 AND 900 "
        "ORDER BY file_id LIMIT 1").fetchone()
    con.close()
    return Path(row[0]) if row and Path(row[0]).exists() else None


def run(argv: list[str], timeout: int = 1800) -> str:
    r = subprocess.run(argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return r.stderr or ""


def main() -> int:
    path = pick()
    if path is None:
        print("没有可用测试视频")
        return 1
    print(f"测试视频：{path.name}\n")

    print("=== A) scdet + showinfo，看 pts_time 附近有没有 scd 字段 ===")
    err = run([str(FFMPEG), "-hide_banner", "-nostdin", "-i", str(path),
               "-vf", "scdet=threshold=10,showinfo", "-an", "-frames:v", "60",
               "-f", "null", "-"])
    lines = [l for l in err.splitlines() if "pts_time" in l]
    for l in lines[:3]:
        print("  ", l[:220])
    print(f"  ... 共 {len(lines)} 行 showinfo")

    print("\n=== B) 是否出现任何 scd 相关字段 ===")
    for pat in ("scd", "score", "lavfi"):
        hits = [l for l in err.splitlines() if pat in l]
        print(f"  含 {pat!r}: {len(hits)} 行")
        for h in hits[:2]:
            print("     ", h[:200])

    print("\n=== C) scdet + metadata=print（写到文件，最可靠的解析路径）===")
    with tempfile.TemporaryDirectory(dir=str(ROOT / "tmp")) as td:
        mf = Path(td) / "meta.txt"
        err2 = run([str(FFMPEG), "-hide_banner", "-nostdin", "-i", str(path),
                    "-vf", f"scdet=threshold=10,metadata=print:file={mf.as_posix()}",
                    "-an", "-frames:v", "200", "-f", "null", "-"])
        if mf.exists():
            txt = mf.read_text(encoding="utf-8", errors="replace")
            head = txt.splitlines()[:12]
            print("  metadata 文件前 12 行：")
            for h in head:
                print("     ", h[:160])
            scores = re.findall(r"lavfi\.scd\.score=([0-9.]+)", txt)
            times = re.findall(r"pts_time:([0-9.]+)", txt)
            print(f"  score 出现 {len(scores)} 次，pts_time 出现 {len(times)} 次")
            if scores:
                vals = [float(s) for s in scores]
                hi = [(t, s) for t, s in zip(times, vals) if s >= 10.0]
                print(f"  score>=10 的帧数：{len(hi)}，前 5 个：{hi[:5]}")
        else:
            print("  metadata 文件未生成")

    print("\n=== D) scdet 带 sc_pass=1（只输出超过阈值的帧）===")
    err3 = run([str(FFMPEG), "-hide_banner", "-nostdin", "-i", str(path),
                "-vf", "scdet=threshold=10:sc_pass=1,showinfo", "-an",
                "-frames:v", "50", "-f", "null", "-"])
    pts = re.findall(r"pts_time:([0-9.]+)", err3)
    print(f"  通过阈值被放行的帧（前 50 上限）：{len(pts)} 个")
    print(f"  前 10 个时间点：{pts[:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
