"""16_cluster.py · 人脸自动分组（规格 v5.3 §6）。

产出带版本号的分组快照，并原子切换「当前版本」指针；旧版本可一键回退。

用法：
    .venv\\Scripts\\python.exe 16_cluster.py --trial 100000   # 先试跑，量内存与耗时
    .venv\\Scripts\\python.exe 16_cluster.py                  # 全量
    .venv\\Scripts\\python.exe 16_cluster.py --json
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import cluster as C  # noqa: E402
from src import tasks as T  # noqa: E402
from src.common import base_argparser, emit_summary, get_logger  # noqa: E402
from src.config_loader import cfg_from_args  # noqa: E402


def main(argv=None) -> int:
    p = base_argparser("人脸自动分组（降维 + 密度聚类，版本化快照）", needs_root=False)
    p.add_argument("--trial", type=int, default=None, metavar="N",
                   help="先只取 N 个人脸试跑，量内存与耗时（规格 §6.3 硬约束）")
    args = p.parse_args(argv)
    cfg = cfg_from_args(args)
    log = get_logger("16_cluster", cfg, args.log_level)
    try:
        result = C.run(cfg, trial=bool(args.trial), limit=args.trial)
    except T.TaskCancelled:
        log.warning("%s", T.CANCEL_MESSAGE)
        return T.CANCEL_EXIT_CODE
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2
    emit_summary(result, args.json, log, "CLUSTER SUMMARY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
