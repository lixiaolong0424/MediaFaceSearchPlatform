"""诊断 03_extract 的 1,078 个失败：分清「真的坏」与「被 PIL 上限误杀」。

PIL 默认 MAX_IMAGE_PIXELS = 89,478,485（超过只警告），
硬上限约 2 倍（178,956,970）——超过就抛 DecompressionBombError，open 直接失败。
本机素材里有一批 1.79 亿~4.02 亿像素的大图（AI 放大 / 拼接长图），
它们是完全正常的图片，却被上限拒之门外。
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, r'D:\DATA\PROJECT\建设多媒体整理系统')

from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True

conn = sqlite3.connect(r'data\library.db')
conn.row_factory = sqlite3.Row
rows = list(conn.execute(
    "SELECT file_id, path, size, status FROM files WHERE status='error'"))
print(f"files.status='error' 共 {len(rows)} 个")
conn.close()

kinds = {"bomb": 0, "unidentified": 0, "other": 0, "ok_with_high_cap": 0}
samples = {"bomb": [], "unidentified": [], "other": [], "ok_with_high_cap": []}
Image.MAX_IMAGE_PIXELS = None          # 关掉上限再试一次

for r in rows:
    p = Path(str(r["path"]))
    if not p.exists():
        kinds["other"] += 1
        if len(samples["other"]) < 5:
            samples["other"].append((r["file_id"], "文件不存在", str(p)[:60]))
        continue
    # 1) 用默认上限试
    try:
        Image.MAX_IMAGE_PIXELS = 89478485
        with Image.open(p) as im:
            im.size
        kinds["ok_with_high_cap"] += 1
        continue
    except Image.DecompressionBombError as e:
        kinds["bomb"] += 1
        if len(samples["bomb"]) < 5:
            samples["bomb"].append((r["file_id"], str(e)[:60], f"{r['size']/1e6:.1f}MB"))
    except Exception as e:
        # 2) 关掉上限再试，判断是不是「上限误杀」还是「真解码不了」
        try:
            Image.MAX_IMAGE_PIXELS = None
            with Image.open(p) as im:
                w, h = im.size
            kinds["ok_with_high_cap"] += 1
            if len(samples["ok_with_high_cap"]) < 5:
                samples["ok_with_high_cap"].append(
                    (r["file_id"], f"{w}x{h} = {w*h/1e6:.0f}MP", f"{r['size']/1e6:.1f}MB"))
        except Exception as e2:
            kinds["unidentified"] += 1
            if len(samples["unidentified"]) < 8:
                samples["unidentified"].append(
                    (r["file_id"], type(e2).__name__, f"{r['size']/1e6:.2f}MB", p.suffix,
                     str(p)[:70]))

print("\n分类：", kinds)
for k, v in samples.items():
    if v:
        print(f"\n--- {k} 样本 ---")
        for s in v:
            print("   ", s)
