"""用「像素吞吐」算全集合成 ETA（比文件/秒靠谱得多）。

为什么：融合趟的成本 ≈ 解码全图（∝像素数）+ pHash + 降采样存缓存，
而文件体积与像素数并不成比例 —— 实测 200MP 的巨图只有 6~13MB（高压缩），
按「文件/秒」会把它们算得和 20MP 的普通照片一样快，严重低估总耗时。

做法：取最近已完成的 N 张照片，累加它们的像素数，除以这段时间，得到 MP/s；
再用剩余待处理照片的总像素数除，得到 ETA。
"""

import sqlite3
import sys
from datetime import datetime

conn = sqlite3.connect(r'data\library.db')
conn.row_factory = sqlite3.Row

# 用 files.width/height（抽帧时写回）统计已完成照片的像素
row = conn.execute(
    "SELECT COUNT(*) n, SUM(width*height) px, SUM(size) b FROM files "
    "WHERE kind='photo' AND status='done' AND width>0 AND height>0").fetchone()
print(f"已完成照片 {row['n']:,} 张，合计 {row['px']/1e12:.2f} 亿亿像素"
      f"（{row['px']/1e9:.0f} G像素），{row['b']/1e12:.3f} TB")

pend = conn.execute(
    "SELECT COUNT(*) n, SUM(size) b FROM files WHERE kind='photo' AND status='pending'").fetchone()
done_avg_px = row["px"] / max(1, row["n"])
print(f"待处理照片 {pend['n']:,} 张，{pend['b']/1e12:.3f} TB")
print(f"已完成照片平均 {done_avg_px/1e6:.1f} MP/张")

# 从日志算实测吞吐（最近一段进度行的 elapsed 与文件数）
import re
from pathlib import Path

log = Path("logs/chain_r1_extract_photo.log")
txt = log.read_text(encoding="utf-8", errors="replace")
prog = re.findall(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d).*?进度 (\d+)/(\d+).*?([\d.]+) 文件/秒", txt)
if len(prog) >= 2:
    (t0s, i0, _, _r0), (t1s, i1, _, r1) = prog[0], prog[-1]
    f0 = datetime.strptime(t0s, "%Y-%m-%d %H:%M:%S")
    f1 = datetime.strptime(t1s, "%Y-%m-%d %H:%M:%S")
    el = (f1 - f0).total_seconds()
    dfiles = int(i1) - int(i0)
    fps = dfiles / max(el, 1)
    print(f"\n本进程实测：{dfiles:,} 个文件 / {el/60:.1f} 分钟 = {fps:.2f} 文件/秒（最近一行报 {r1}）")
    # 已处理区间的平均体积 → MB/s
    rows = conn.execute(
        "SELECT AVG(size) a FROM (SELECT size FROM files WHERE kind='photo' AND status='done' "
        "ORDER BY file_id DESC LIMIT ?)", (max(1, dfiles),)).fetchone()
    avg_b = rows["a"] or 0
    print(f"  最近完成的平均体积 {avg_b/1e6:.2f} MB → 约 {fps*avg_b/1e6:.1f} MB/s")
    # 用平均像素数估算 MP/s（最近完成的照片）
    r2 = conn.execute(
        "SELECT AVG(width*height) a FROM (SELECT width,height FROM files "
        "WHERE kind='photo' AND status='done' AND width>0 ORDER BY file_id DESC LIMIT ?)",
        (max(1, dfiles),)).fetchone()
    avg_px = r2["a"] or done_avg_px
    mps = fps * avg_px / 1e6
    print(f"  最近完成的平均 {avg_px/1e6:.1f} MP → 约 {mps:.0f} MP/s")
    eta_h = (pend["n"] * avg_px / 1e6) / max(mps, 1e-9) / 3600
    print(f"\n按 {mps:.0f} MP/s 外推剩余 {pend['n']:,} 张：**{eta_h:.1f} 小时**")
    print(f"（若剩余素材的平均像素数与最近完成的一致；巨图占比越高越慢）")
conn.close()
