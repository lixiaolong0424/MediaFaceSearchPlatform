"""复现 crop_face 的报错并查清 normed_face 的实际类型/形状。

报错：unsupported operand type(s) for %: 'tuple' and 'int'
怀疑：face_align.norm_crop 返回的是 (image, matrix) 元组，而不是 ndarray；
      crop_face 里对它做 cv2.resize 或取 .shape[0] 就炸了。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.common import load_config  # noqa: E402
from src.face_search import _imread_unicode, crop_face, get_engine  # noqa: E402

OVERLAY = ROOT / "tests" / "_quickrun" / "cfg.yaml"


def main() -> int:
    cfg = load_config(None, None, overlay=[str(OVERLAY)])
    eng = get_engine(cfg, reload=True)
    print(f"engine loaded = {eng.loaded}")
    if not eng.loaded:
        print("error:", eng.error)
        return 1

    con = sqlite3.connect(str(cfg.db_path))
    rows = con.execute(
        "SELECT file_id, path FROM files WHERE kind='photo' AND status='done' LIMIT 30"
    ).fetchall()
    con.close()

    for fid, p in rows:
        img = _imread_unicode(Path(p))
        if img is None:
            continue
        faces = eng.detect_and_embed(img, cfg)
        if not faces:
            continue
        f = faces[0]
        print(f"\nfile_id={fid}  bbox={[round(v) for v in f.bbox]}")
        print(f"  normed_face type  : {type(f.normed_face).__name__}")
        nf = f.normed_face
        if isinstance(nf, tuple):
            print(f"  ★ 是 tuple，长度 {len(nf)}")
            for i, item in enumerate(nf):
                print(f"     [{i}] type={type(item).__name__} "
                      f"shape={getattr(item, 'shape', 'n/a')}")
        else:
            print(f"  shape             : {getattr(nf, 'shape', 'n/a')}")

        try:
            out = crop_face(img, f, size=112)
            print(f"  crop_face OK  -> {out.shape}")
        except Exception as exc:  # noqa: BLE001
            print(f"  crop_face FAIL-> {type(exc).__name__}: {exc}")
        break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
