# -*- coding: utf-8 -*-
"""验证 HEIC 全链路：PIL 能开、imread_bgr 能读、抽帧能落库、列表能扫到。

用法：.venv\\Scripts\\python.exe tests\\test_heic.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import db as D  # noqa: E402
from src import manifest as M  # noqa: E402
from src.common import HEIF_READY, load_config  # noqa: E402

SAMPLE = Path(r"E:\晚上\115网盘\14\【苹果原相机】388 江苏白虎JK女孩王欣楠\苹果原图片\IMG_2301.HEIC")

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


def main() -> int:
    print(f"pillow-heif 已注册: {HEIF_READY}")
    check("pillow-heif 已注册（PIL 能开 HEIC）", HEIF_READY)

    if not SAMPLE.exists():
        print(f"  样例不存在：{SAMPLE}")
        return 1

    # 1) PIL 能打开 + 拿到尺寸
    from PIL import Image

    with Image.open(SAMPLE) as im:
        w, h = im.size
        print(f"  PIL 打开成功：{w}x{h} mode={im.mode}")
    check("PIL 打开 HEIC 成功", w > 0 and h > 0, f"{w}x{h}")

    # 2) imread_bgr 能读成 BGR ndarray
    from src.face_pipeline import imread_bgr

    arr = imread_bgr(SAMPLE)
    check("imread_bgr 读到数组", arr is not None and getattr(arr, "ndim", 0) == 3,
          None if arr is None else arr.shape)
    if arr is not None:
        print(f"  imread_bgr 形状 {arr.shape}")

    # 3) 抽帧（照片路径）能把 HEIC 处理成 done
    tmp = Path(tempfile.mkdtemp(prefix="heic_"))
    media = tmp / "media"
    media.mkdir(parents=True)
    shutil.copy2(SAMPLE, media / "IMG_2301.HEIC")
    cfg = load_config()
    cfg.root = tmp
    cfg.override("db.path", str(tmp / "library.db"))
    cfg.override("scan.min_photo_bytes", 1)
    D.ensure_schema(cfg.db_path, ROOT, cfg)

    args = SimpleNamespace(root=str(media), dry_run=False, limit=None, shard=None,
                          json=False, log_level=None, task_id=None,
                          reprocess_all=False)
    rep = M.run(args, cfg)
    print(f"  清单扫描: new={rep.get('new')}")
    check("HEIC 已进入清单（白名单生效）", int(rep.get("new") or 0) == 1, rep.get("new"))

    from src import extract as E

    eargs = SimpleNamespace(kind="all", limit=None, shard=None, dry_run=False,
                            json=False, log_level=None, task_id=None,
                            no_fuse=False, no_cache=True)
    summary = E.run(eargs, cfg)
    print(f"  抽帧: {summary}")
    con = D.connect(cfg.db_path, cfg=cfg, readonly=True)
    try:
        st = con.execute("SELECT status FROM files WHERE lower(path) LIKE '%.heic'").fetchone()[0]
        n_shots = con.execute("SELECT COUNT(*) FROM shots").fetchone()[0]
        ph = con.execute("SELECT phash FROM files WHERE lower(path) LIKE '%.heic'").fetchone()[0]
    finally:
        con.close()
    check("HEIC 抽帧后 status=done", st == "done", st)
    check("HEIC 生成了镜头记录", n_shots >= 1, n_shots)
    check("HEIC 算出了 pHash（可参与去重）", bool(ph), ph)

    shutil.rmtree(tmp, ignore_errors=True)
    print("\n" + "=" * 50)
    print(f"PASS={PASS} FAIL={FAIL}")
    print("=" * 50)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
