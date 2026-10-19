"""诊断视频抽帧失败：「Non full-range YUV is non-standard」是不是能靠 -strict unofficial 修好。

这条报错来自 **ffmpeg 的 mjpeg 编码器**：源视频是 full-range(JPEG) YUV，
而 mjpeg 默认按 standard range 编码，遇到非标准组合直接拒绝。
加 `-strict unofficial` 允许非标准行为即可 —— 输出仍是合法 JPEG，只是不保证
在所有解码器上逐位一致（对本项目「给编码/检脸用」完全够）。

对照试验：
  A. 当前实现（extract_frame）                      → 预期失败
  B. 同样命令 + `-strict unofficial`                → 预期成功
  C. 同 B + `-pix_fmt yuvj420p`（显式 full-range）  → 也预期成功
"""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')

from src.common import load_config
from src.extract import extract_frame, locate_tools

cfg = load_config(None, None)
tools = locate_tools(cfg)

import sqlite3
conn = sqlite3.connect(r'data\library.db')
rows = list(conn.execute(
    "SELECT f.file_id, f.path FROM files f "
    "JOIN file_encoding fe ON fe.file_id=f.file_id AND fe.model='semantic' "
    "WHERE f.kind='video' AND f.status='error' "
    "AND fe.error LIKE '%全片抽帧失败%' LIMIT 3"))
conn.close()
if not rows:
    print("没有可测的失败视频")
    raise SystemExit(0)

td = Path(tempfile.mkdtemp(dir=r'D:\DATA\PROJECT\建设多媒体整理系统\tmp'))
for fid, path in rows:
    src = Path(str(path))
    print(f"\n=== fid={fid}  {src.name[:46]}  {src.stat().st_size/1e6:.1f}MB ===")

    # A：当前实现
    out_a = td / f"a_{fid}.jpg"
    ok, err = extract_frame(tools.ffmpeg, src, 0.5, out_a, 384)
    print(f"  A 当前实现           : ok={ok} size={out_a.stat().st_size if out_a.exists() else 0}")
    if not ok:
        print(f"    err={err[:110]}")

    # B：+ -strict unofficial
    edge = 384
    scale = (f"scale=w='if(gt(iw,ih),trunc(iw*{edge}/ih/2)*2,{edge})':"
             f"h='if(gt(iw,ih),{edge},trunc(ih*{edge}/iw/2)*2)'")
    out_b = td / f"b_{fid}.jpg"
    argv = [str(tools.ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-threads", "1", "-ss", "0.5", "-i", str(src), "-frames:v", "1",
            "-vf", scale, "-strict", "unofficial", "-q:v", "3", str(out_b)]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    print(f"  B + -strict unofficial: rc={r.returncode} size={out_b.stat().st_size if out_b.exists() else 0}")
    if r.returncode != 0:
        print(f"    err={(r.stderr or '')[:110]}")

    # C：+ -strict unofficial + -pix_fmt yuvj420p
    out_c = td / f"c_{fid}.jpg"
    argv2 = argv[:-2] + ["-strict", "unofficial", "-pix_fmt", "yuvj420p", "-q:v", "3", str(out_c)]
    r2 = subprocess.run(argv2, capture_output=True, text=True, timeout=180)
    print(f"  C + yuvj420p          : rc={r2.returncode} size={out_c.stat().st_size if out_c.exists() else 0}")

    # 顺带看这个视频到底有没有可解码的帧
    pr = subprocess.run([str(tools.ffprobe), "-v", "error", "-select_streams", "v:0",
                         "-show_entries", "stream=pix_fmt,width,height,nb_frames",
                         "-of", "default=nw=1", str(src)],
                        capture_output=True, text=True, timeout=120)
    print(f"  ffprobe: {' '.join(pr.stdout.split())}")
print(f"\n临时输出目录: {td}")
