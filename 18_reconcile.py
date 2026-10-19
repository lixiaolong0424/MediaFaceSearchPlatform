"""18_reconcile.py · 库完整性检查（规格 v5.3 §8.2 / §7.5）。

只出报告，不删任何东西。报告落在 data/reports/reconcile_<时间>.json。

用法：
    .venv\\Scripts\\python.exe 18_reconcile.py
    .venv\\Scripts\\python.exe 18_reconcile.py --json
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import reconcile as R  # noqa: E402
from src import tasks as T  # noqa: E402
from src.common import base_argparser, emit_summary, get_logger  # noqa: E402
from src.config_loader import cfg_from_args  # noqa: E402


def main(argv=None) -> int:
    p = base_argparser("库完整性检查：孤儿向量 / 悬空引用 / 内容漂移", needs_root=False)
    args = p.parse_args(argv)
    cfg = cfg_from_args(args)
    log = get_logger("18_reconcile", cfg, args.log_level)
    try:
        report = R.run(cfg)
    except T.TaskCancelled:
        log.warning("%s", T.CANCEL_MESSAGE)
        return T.CANCEL_EXIT_CODE
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2
    emit_summary({k: v for k, v in report.items() if k != "samples"}, args.json, log,
                 "RECONCILE SUMMARY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
