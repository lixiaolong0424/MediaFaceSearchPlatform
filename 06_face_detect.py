"""06_face_detect.py · B 人脸管线 F1+F2（SCRFD 检测 + shot 内 track 合并 + 代表帧优选）。

用法：
    .venv\\Scripts\\python.exe 06_face_detect.py --dry-run --limit 20
    .venv\\Scripts\\python.exe 06_face_detect.py --json
    .venv\\Scripts\\python.exe 06_face_detect.py --shard 1/4

产出：
    * face_records 行（has_vector=0，thumb_path=NULL）
    * data/nvme/faces/raw/<face_id>.webp（对齐后的 112×112 人脸，供 07 直接编码）

下一步：07_face_encode.py 才会写缩略图与 Qdrant 向量；照片墙只认 07 之后的记录。
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import face_pipeline as FP  # noqa: E402
from src import tasks as T  # noqa: E402
from src.common import base_argparser, emit_summary, get_logger  # noqa: E402
from src.config_loader import cfg_from_args  # noqa: E402


def main(argv=None) -> int:
    p = base_argparser("F1+F2 人脸检测与 track 合并（SCRFD det_10g）", needs_root=False)
    args = p.parse_args(argv)
    cfg = cfg_from_args(args)
    log = get_logger("06_face_detect", cfg, args.log_level)
    try:
        summary = FP.run_detect(args, cfg)
    except T.TaskCancelled:
        log.warning("%s", T.CANCEL_MESSAGE)
        return T.CANCEL_EXIT_CODE
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2
    emit_summary(summary, args.json, log, "FACE DETECT SUMMARY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
