"""用日志里真实失败的 file_id 重测：-strict unofficial 能不能救回来。"""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')

import sqlite3

from src.common import load_config
from src.extract import extract_frame, ffprobe_media, locate_tools

cfg = load_config(None, None)
tools = locate_tools(cfg)

REAL_FAILED = [395536, 483774, 346060]
conn = sqlite3.connect(r'data\library.db')
rows = []
for fid in REAL_FAILED:
    r = conn.execute("SELECT file_id, path, size FROM files WHERE file_id=?", (fid,)).fetchone()
    if r:
        rows.append(r)
conn.close()

td = Path(tempfile.mkdtemp(dir=r'D:\DATA\PROJECT\建设多媒体整理系统\tmp'))
EDGE = 384
scale = (f"scale=w='if(gt(iw,ih),trunc(iw*{EDGE}/ih/2)*2,{EDGE})':"
         f"h='if(gt(iw,ih),{EDGE},trunc(ih*{EDGE}/iw/2)*2)'")

for fid, path, size in rows:
    src = Path(str(path))
    print(f"\n=== fid={fid}  {src.name[:44]}  {size/1e6:.2f}MB ===")
    pr = ffprobe_media(tools.ffprobe, src)
    print(f"  probe: ok={pr.ok} dur={pr.duration:.2f}s {pr.width}x{pr.height} fps={pr.fps:.2f} err={pr.error[:60]}")
    # 源像素格式
    p = subprocess.run([str(tools.ffprobe), "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=pix_fmt,color_range,nb_frames",
                        "-of", "default=nw=1", str(src)],
                       capture_output=True, text=True, timeout=120)
    print(f"  stream: {' '.join(p.stdout.split())}")

    for ts in (0.05, 0.5, 1.0):
        # A 当前实现
        oa = td / f"a_{fid}_{int(ts*1000)}.jpg"
        ok_a, err_a = extract_frame(tools.ffmpeg, src, ts, oa, EDGE)
        sa = oa.stat().st_size if oa.exists() else 0
        # B + -strict unofficial
        ob = td / f"b_{fid}_{int(ts*1000)}.jpg"
        argv = [str(tools.ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-threads", "1", "-ss", f"{ts:.3f}", "-i", str(src), "-frames:v", "1",
                "-vf", scale, "-strict", "unofficial", "-q:v", "3", str(ob)]
        r = subprocess.run(argv, capture_output=True, text=True, timeout=180)
        sb = ob.stat().st_size if ob.exists() else 0
        print(f"    ts={ts:<5} A: ok={str(ok_a):<5} {sa:>7}B | "
              f"B(strict): rc={r.returncode:<3} {sb:>7}B"
              + (f"  Aerr={err_a[:48]}" if not ok_a else ""))
print(f"\n临时目录 {td}")
