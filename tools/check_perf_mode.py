# -*- coding: utf-8 -*-
"""验证 performance.mode 现在能真正改变行为（只靠 mode，数字留 0）。"""
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import extract as E  # noqa: E402
from src.common import load_config, perf_value  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {detail}")


cfg = load_config()
for k in ("performance.photo_prefetch", "performance.video_workers",
          "performance.encode_batch", "performance.encode_group"):
    cfg.override(k, 0)

for mode, want in (("balanced", 0), ("turbo", 6)):
    cfg.override("performance.mode", mode)
    got = perf_value(cfg, "photo_prefetch")
    check(f"mode={mode} → photo_prefetch={got}", got == want, f"want {want}")

cfg.override("performance.mode", "turbo")
cfg.override("performance.photo_prefetch", 2)
check("显式值优先于档位", perf_value(cfg, "photo_prefetch") == 2,
      perf_value(cfg, "photo_prefetch"))

cfg.override("performance.mode", "turbo")
cfg.override("performance.photo_prefetch", 0)
cfg.override("performance.video_workers", 0)
workers = E.compute_workers(cfg)
print(f"  compute_workers(turbo) = {workers}")
check("turbo 下视频并发被抬到 ≥10（受内存上限影响）", workers >= 10, workers)

print("\n" + "=" * 40)
print(f"PASS={PASS} FAIL={FAIL}")
print("=" * 40)
raise SystemExit(0 if FAIL == 0 else 1)
