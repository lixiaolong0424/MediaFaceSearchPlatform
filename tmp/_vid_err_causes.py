"""统计 13 个视频失败的成因分布：超短视频（可修）vs 真损坏（不可修）。"""

import sqlite3
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')
from src.common import load_config  # noqa: E402
from src.extract import locate_tools  # noqa: E402

cfg = load_config(None, None)
tools = locate_tools(cfg)
conn = sqlite3.connect(r'data\library.db')
rows = list(conn.execute(
    "SELECT f.file_id, f.path, f.size, f.duration_sec FROM files f "
    "WHERE f.kind='video' AND f.status='error' ORDER BY f.file_id"))
conn.close()

print(f"video error 共 {len(rows)} 个\n")
print(f"{'fid':>8} {'体积MB':>7} {'时长s':>8} {'像素格式':>14} {'帧数':>7}  {'文件'}")
short = broken = other = 0
for fid, path, size, dur in rows:
    p = Path(str(path))
    try:
        pr = subprocess.run([str(tools.ffprobe), "-v", "error", "-select_streams", "v:0",
                             "-show_entries", "stream=pix_fmt,nb_frames",
                             "-of", "default=nw=1", str(p)],
                            capture_output=True, text=True, timeout=60)
        info = dict(l.split("=", 1) for l in pr.stdout.strip().splitlines() if "=" in l)
        pix = info.get("pix_fmt", "?")
        nbf = info.get("nb_frames", "?")
    except Exception as e:  # noqa: BLE001
        pix, nbf = f"probe失败", "?"
    d = dur or 0
    kind = "超短(可修)" if d and d < 2.0 else ("读不出流(不可修)" if pix in ("unknown", "?") or nbf in ("N/A", "?") else "其它")
    if "超短" in kind:
        short += 1
    elif "不可修" in kind:
        broken += 1
    else:
        other += 1
    print(f"{fid:>8} {size/1e6:>7.2f} {d:>8.2f} {pix:>14} {nbf:>7}  {p.name[:34]}  {kind}")

print(f"\n汇总：超短视频(有解)={short}  真损坏/无视频流(无解)={broken}  其它={other}")
