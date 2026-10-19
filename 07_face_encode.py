"""07_face_encode.py · B 人脸管线 F3（ArcFace 512 维 + 同步写 faces/thumbs + Qdrant）。

用法：
    .venv\\Scripts\\python.exe 07_face_encode.py --dry-run
    .venv\\Scripts\\python.exe 07_face_encode.py --json
    .venv\\Scripts\\python.exe 07_face_encode.py --shard 1/4

前置：06_face_detect.py 已跑完（face_records 里有 has_vector=0 的行 + faces/raw 图）。

产出（严格同步写，绝不事后补批处理 —— 缺失则照片墙刷不出）：
    * data/nvme/faces/thumbs/<face_id>.webp（128×128，质量 80）
    * Qdrant collection materials_faces，point_id = face_id，dim=512，Cosine
    * face_records.has_vector=1 + thumb_path，file_encoding(face)=done + encoded_at

检索阈值固定 0.55~0.60（config faces.score_thresh / retrieval.min_score）。
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
    p = base_argparser("F3 人脸编码入库（ArcFace w600k_r50）", needs_root=False)
    args = p.parse_args(argv)
    cfg = cfg_from_args(args)
    log = get_logger("07_face_encode", cfg, args.log_level)
    try:
        summary = FP.run_encode(args, cfg)
    except T.TaskCancelled:
        log.warning("%s", T.CANCEL_MESSAGE)
        return T.CANCEL_EXIT_CODE
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2
    emit_summary(summary, args.json, log, "FACE ENCODE SUMMARY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
