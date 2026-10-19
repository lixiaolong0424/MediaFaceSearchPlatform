"""17_gold.py · 用照片集标定识别灵敏度（规格 v5.3 §7.4）。

目录约定：一个子文件夹=一个人，`_` 开头的文件夹忽略。
这些照片只用来算阈值，不会加入素材库，也不会上传。

用法：
    .venv\\Scripts\\python.exe 17_gold.py --root "D:/金标准集"
    .venv\\Scripts\\python.exe 17_gold.py --json
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import gold as G  # noqa: E402
from src import tasks as T  # noqa: E402
from src.common import base_argparser, emit_summary, get_logger  # noqa: E402
from src.config_loader import cfg_from_args  # noqa: E402


def main(argv=None) -> int:
    p = base_argparser("识别灵敏度标定（照片集 → PR 曲线 → 建议阈值）", needs_root=True)
    args = p.parse_args(argv)
    cfg = cfg_from_args(args)
    log = get_logger("17_gold", cfg, args.log_level)
    try:
        result = G.calibrate(cfg, getattr(args, "root", None))
    except T.TaskCancelled:
        log.warning("%s", T.CANCEL_MESSAGE)
        return T.CANCEL_EXIT_CODE
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2
    if not result.get("ok"):
        log.error("%s", result.get("reason"))
        print(f"降级默认值：{result.get('fallback_threshold')}")
        return 3
    emit_summary({k: v for k, v in result.items() if k != "curve"}, args.json, log,
                 "GOLD SUMMARY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
