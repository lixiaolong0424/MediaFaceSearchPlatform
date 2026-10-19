"""拆解 quality 的四个因子，找出为什么多数人脸 quality < 0.3。

quality = 人脸像素面积项 × 拉普拉斯清晰度项 × 正面度项 × det置信度

照片墙（Tab1）默认 min_quality=0.3，所以这个值直接决定墙上有没有脸。
若普遍偏低，说明公式的归一化常数不适合这类素材。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from src.common import load_config  # noqa: E402
from src.face_search import _imread_unicode, frontal_ratio, get_engine  # noqa: E402

OVERLAY = ROOT / "tests" / "_quickrun" / "cfg.yaml"


def factors(img: np.ndarray, f) -> dict:
    h, w = img.shape[:2]
    x1, y1, x2, y2 = f.bbox
    crop = img[int(max(0, y1)):int(min(h, y2)), int(max(0, x1)):int(min(w, x2))]
    if crop.size == 0:
        return {}
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    lap = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    sharp = min(1.0, lap / 500.0)
    area_term = min(1.0, f.area / (112.0 * 112.0))
    fr = frontal_ratio(f.kps) if f.kps is not None else 0.5
    det = max(0.0, min(1.0, f.det_score))
    return {"lap_var": lap, "sharp": sharp, "area": f.area,
            "area_term": area_term, "frontal": fr, "det": det,
            "product": area_term * sharp * fr * det,
            "crop_wh": (crop.shape[1], crop.shape[0])}


def main() -> int:
    cfg = load_config(None, None, overlay=[str(OVERLAY)])
    eng = get_engine(cfg, reload=True)
    con = sqlite3.connect(str(cfg.db_path))
    rows = con.execute(
        "SELECT path FROM files WHERE kind='photo' AND status='done' LIMIT 40").fetchall()
    con.close()

    print(f"{'file':>5} {'lap_var':>9} {'sharp':>6} {'area':>9} {'areaT':>6} "
          f"{'front':>6} {'det':>6} {'乘积':>7}")
    vals = []
    for i, (p,) in enumerate(rows, 1):
        img = _imread_unicode(Path(p))
        if img is None:
            continue
        for f in eng.detect_and_embed(img, cfg):
            d = factors(img, f)
            if not d:
                continue
            vals.append(d["product"])
            print(f"{i:>5} {d['lap_var']:>9.1f} {d['sharp']:>6.3f} {d['area']:>9.0f} "
                  f"{d['area_term']:>6.3f} {d['frontal']:>6.3f} {d['det']:>6.3f} "
                  f"{d['product']:>7.4f}")

    if vals:
        a = np.array(vals)
        print(f"\n样本数={len(a)}")
        print(f"  min={a.min():.4f}  中位={np.median(a):.4f}  mean={a.mean():.4f}  max={a.max():.4f}")
        for th in (0.1, 0.2, 0.3, 0.5):
            print(f"  >= {th}: {(a >= th).sum()}/{len(a)} "
                  f"({(a >= th).mean()*100:.0f}%)")
        print(f"\n  结论：照片墙默认 min_quality=0.3 → 只有 {(a >= 0.3).mean()*100:.0f}% 的脸能上墙")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
