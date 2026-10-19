"""拿到 embed() 失败的完整 traceback，定位 `%` 报错的真实位置。

先前猜测是 norm_crop 返回 tuple，但看源码它返回 ndarray，所以猜测不成立。
本脚本把 detect → norm_crop → get_feat 三步分开做，逐步打印类型与形状，
并完整打印 traceback。
"""

from __future__ import annotations

import sqlite3
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from src.common import load_config  # noqa: E402
from src.face_search import _imread_unicode, get_engine  # noqa: E402

OVERLAY = ROOT / "tests" / "_quickrun" / "cfg.yaml"


def main() -> int:
    cfg = load_config(None, None, overlay=[str(OVERLAY)])
    eng = get_engine(cfg, reload=True)
    print(f"engine loaded={eng.loaded}")

    con = sqlite3.connect(str(cfg.db_path))
    rows = con.execute(
        "SELECT path FROM files WHERE kind='photo' AND status='done' LIMIT 40").fetchall()
    con.close()

    from insightface.utils import face_align

    done = 0
    for (p,) in rows:
        img = _imread_unicode(Path(p))
        if img is None:
            continue
        faces = eng.detect(img)
        if not faces:
            continue

        f = faces[0]
        print(f"\n图片: {Path(p).name[:40]}")
        print(f"  bbox      : {[round(v) for v in f.bbox]}")
        print(f"  kps is None: {f.kps is None}")
        if f.kps is not None:
            print(f"  kps shape  : {f.kps.shape}  dtype={f.kps.dtype}")

        aligned = None
        if f.kps is not None:
            try:
                aligned = face_align.norm_crop(img, f.kps, (112, 112))
                print(f"  norm_crop -> type={type(aligned).__name__} "
                      f"shape={getattr(aligned, 'shape', 'n/a')}")
            except Exception:  # noqa: BLE001
                print("  norm_crop FAILED:")
                traceback.print_exc()
        else:
            print("  kps 为 None → 走 _crop_fallback")
            from src.face_search import _crop_fallback

            aligned = _crop_fallback(img, f.bbox, (112, 112))
            print(f"  fallback  -> type={type(aligned).__name__} "
                  f"shape={getattr(aligned, 'shape', 'n/a')}")

        if aligned is not None:
            try:
                feat = eng.rec.get_feat(aligned)
                arr = np.asarray(feat)
                print(f"  get_feat OK -> shape={arr.shape} dtype={arr.dtype}")
            except Exception:  # noqa: BLE001
                print("  get_feat FAILED:")
                traceback.print_exc()

        done += 1
        if done >= 2:
            break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
