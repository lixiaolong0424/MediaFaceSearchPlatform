"""03_extract.py · L2b 镜头切分抽帧 + EXIF/容器时间。

照片侧默认走**单趟读盘融合**（config scan.fuse_photo_pipeline）：
EXIF + pHash 去重（L2a）+ NVMe 缓存图一次做完，理由见 src/extract.py 的模块注释。

用法：
    .venv\\Scripts\\python.exe 03_extract.py --dry-run
    .venv\\Scripts\\python.exe 03_extract.py --kind photo        # 只跑照片（推荐）
    .venv\\Scripts\\python.exe 03_extract.py --kind video        # 只跑视频
    .venv\\Scripts\\python.exe 03_extract.py --shard 1/8 --json  # 分片（自动关掉融合去重）
    .venv\\Scripts\\python.exe 03_extract.py --limit 50          # 冒烟测试
    .venv\\Scripts\\python.exe 03_extract.py --no-fuse           # 关掉融合，照片只读 EXIF
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import extract as E  # noqa: E402
from src import tasks as T  # noqa: E402
from src.common import base_argparser, emit_summary, get_logger  # noqa: E402
from src.config_loader import cfg_from_args  # noqa: E402


def main(argv=None) -> int:
    p = base_argparser("L2b 镜头切分与抽帧 + EXIF/容器时间", needs_root=False)
    p.add_argument("--kind", choices=["photo", "video", "all"], default="all",
                   help="只处理某一类（照片与视频的成本差三个数量级，建议分开跑）")
    p.add_argument("--no-fuse", action="store_true",
                   help="关闭照片侧的 L2a 融合去重（改由 02_dedup.py 单独跑）")
    p.add_argument("--no-cache", action="store_true",
                   help="不写照片的 NVMe 缓存图（04/06 会回退去读机械盘原图）")
    args = p.parse_args(argv)
    cfg = cfg_from_args(args)
    log = get_logger("03_extract", cfg, args.log_level)
    try:
        summary = E.run(args, cfg)
    except T.TaskCancelled:
        log.warning("%s", T.CANCEL_MESSAGE)
        return T.CANCEL_EXIT_CODE
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2
    emit_summary(summary, args.json, log, "EXTRACT SUMMARY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
