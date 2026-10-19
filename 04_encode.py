"""04_encode.py · L2c 语义编码（Qwen3-VL-Embedding-2B）+ L3 入库。

权重必须离线加载，禁止无声下载；instruction 仅注入 query 侧。

用法：
    .venv\\Scripts\\python.exe 04_encode.py --dry-run
    .venv\\Scripts\\python.exe 04_encode.py --limit 20 --json
    .venv\\Scripts\\python.exe 04_encode.py --shard 1/4
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import encode as EN  # noqa: E402
from src import tasks as T  # noqa: E402
from src.common import base_argparser, emit_summary, get_logger  # noqa: E402
from src.config_loader import cfg_from_args  # noqa: E402


def main(argv=None) -> int:
    p = base_argparser("L2c 多模态编码 + L3 向量入库（Qwen3-VL-Embedding-2B）", needs_root=False)
    args = p.parse_args(argv)
    cfg = cfg_from_args(args)
    log = get_logger("04_encode", cfg, args.log_level)
    try:
        summary = EN.run(args, cfg)
    except T.TaskCancelled:
        log.warning("%s", T.CANCEL_MESSAGE)
        return T.CANCEL_EXIT_CODE
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2
    emit_summary(summary, args.json, log, "ENCODE SUMMARY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
