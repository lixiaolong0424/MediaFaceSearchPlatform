"""01_manifest.py · L1 清单扫描（五类判定）。

用法：
    .venv\\Scripts\\python.exe 01_manifest.py --root "E:/" --dry-run
    .venv\\Scripts\\python.exe 01_manifest.py --root "E:/晚上" --json
    .venv\\Scripts\\python.exe 01_manifest.py --shard 1/4 --root "E:/晚上"
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import manifest as M  # noqa: E402
from src import tasks as T  # noqa: E402
from src.common import base_argparser, emit_summary, get_logger, resolve_media_root  # noqa: E402
from src.config_loader import cfg_from_args  # noqa: E402


def main(argv=None) -> int:
    p = base_argparser("L1 清单扫描：五类判定 new/modified/moved/unchanged/missing")
    p.add_argument("--reprocess-all", action="store_true",
                   help="把已完成（done）的文件重新排队，即界面上的「重新扫描全部」")
    args = p.parse_args(argv)
    cfg = cfg_from_args(args)
    args.root = str(resolve_media_root(args, cfg))
    log = get_logger("01_manifest", cfg, args.log_level)
    try:
        summary = M.run(args, cfg)
    except T.TaskCancelled:
        log.warning("%s", T.CANCEL_MESSAGE)
        return T.CANCEL_EXIT_CODE
    except FileNotFoundError as exc:
        log.error("%s", exc)
        return 2
    except sqlite3.OperationalError as exc:
        if "locked" not in str(exc).lower():
            raise
        log.error(
            "数据库被占用（database is locked）：另一个进程正在写 %s。\n"
            "  最常见原因：03_extract.py / 04_encode.py / 界面里的扫描还在跑。\n"
            "  处理办法（三选一）：\n"
            "    1) 等它跑完再来扫（推荐）；\n"
            "    2) 只想预估结果就加 --dry-run（dry-run 不写库，可与写进程并存）；\n"
            "    3) 确认可以中断的话，先停止那个进程。\n"
            "  查看谁在跑：Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
            "Select-Object ProcessId, CommandLine",
            cfg.db_path,
        )
        return 3
    emit_summary(summary, args.json, log, "MANIFEST SUMMARY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
