"""02_dedup.py · L2a pHash 感知哈希去重（仅图片）。

规则（规格 L2a）：
  * 64 位感知哈希，汉明距离 <= 8 视为近重复
  * 保留分辨率更高、mtime 更新者；另一记 skipped_dup
  * 铁律 1：去重跳过的文件必须在 file_encoding 里为每个模型各记一行 skipped_dup

与 03_extract.py 的关系（重要）
------------------------------
照片侧现在默认把 L2a **折进** 03_extract.py 的同一趟读盘（config
scan.fuse_photo_pipeline: true），因为 E: 是机械盘，照片共 3.235 TB，
一趟读盘 6~9 小时，按规格原本的四阶段划分要读四遍盘。

本脚本仍然完整可用，是规格 L2a 的**独立入口**，用于：
  * 单独重跑去重（换阈值、修数据）
  * 融合模式已跑完后补跑增量
  * 把 L2a 与 L2b 分开验收
两个入口共用 src/dedup_run.py 的同一套索引与代表项维护逻辑，判定不会漂移。

用法：
    .venv\\Scripts\\python.exe 02_dedup.py --dry-run
    .venv\\Scripts\\python.exe 02_dedup.py --shard 1/4 --json
    .venv\\Scripts\\python.exe 02_dedup.py --limit 5000
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import db as D  # noqa: E402
from src.common import (  # noqa: E402
    Config, base_argparser, emit_summary, get_logger, local_now_iso, shard_range,
)
from src.config_loader import cfg_from_args  # noqa: E402
from src.dedup_core import hamming  # noqa: E402,F401  (对外保留，供测试/审计引用)
from src.dedup_run import (  # noqa: E402,F401
    DUP, KEEP, DedupRunner, compute_phash_file, decide_keeper, mark_dup,
)

LOG = get_logger("02_dedup")


def compute_phash(path: Path, hash_size: int = 8) -> Optional[str]:
    """兼容旧调用点：转发到 src/dedup_run.compute_phash_file。"""
    return compute_phash_file(path, hash_size)


def _decide_keeper(conn, a: int, b: int):
    """兼容旧调用点（tests/p0_smoke.py 等）。"""
    return decide_keeper(conn, a, b)


def _photo_targets(conn, limit: Optional[int]) -> List[Any]:
    rows = list(conn.execute(
        "SELECT file_id, path, size, mtime FROM files "
        "WHERE kind='photo' AND status IN ('pending','error') ORDER BY file_id"))
    return rows[:limit] if limit else rows


def run(args, cfg: Config) -> Dict[str, Any]:
    D.ensure_schema(cfg.db_path, cfg.root, cfg)
    conn = D.connect(cfg.db_path, cfg=cfg)
    rows = _photo_targets(conn, args.limit)
    rng = shard_range(args.shard, len(rows))
    if rng is not None:
        rows = [rows[i] for i in rng]

    hash_size = int(cfg.get("dedup.hash_size", 8))
    threshold = int(cfg.get("dedup.hamming_threshold", 8))
    batch = max(1, int(cfg.get("scan.batch_commit_files", 500)))

    runner = DedupRunner(hash_size=hash_size, threshold=threshold)
    runner.load_history(conn)

    stats = {"targets": len(rows), "errors": 0}
    LOG.info("pHash 去重：%d 个候选，阈值 汉明 <= %d，dry_run=%s",
             len(rows), threshold, args.dry_run)
    LOG.info("%s", runner.describe())

    t0 = time.monotonic()
    for i, r in enumerate(rows, 1):
        fid, p = int(r["file_id"]), Path(str(r["path"]))
        if not p.exists():
            conn.execute("UPDATE files SET status='missing', updated_at=? WHERE file_id=?",
                         (local_now_iso(), fid))
            continue
        h = compute_phash_file(p, hash_size)
        verdict, _other, dist = runner.feed(conn, fid, h, dry_run=args.dry_run)
        if args.dry_run and verdict == DUP and h is not None:
            LOG.info("[dry-run] file_id=%s 近重复（hamming=%d）", fid, dist)

        if i % batch == 0:
            if not args.dry_run:
                conn.commit()
            el = max(1e-9, time.monotonic() - t0)
            rate = i / el
            LOG.info("  进度 %d/%d  重复=%d  唯一=%d  失败=%d  %.1f 张/秒  预计剩余 %.1f 分钟",
                     i, len(rows), runner.stats["duplicates"], runner.stats["unique"],
                     runner.stats["errors"], rate, (len(rows) - i) / max(rate, 1e-9) / 60.0)

    if not args.dry_run:
        conn.commit()
    conn.close()

    idx_st = runner.idx.stats()
    elapsed = time.monotonic() - t0
    return {
        **stats,
        **runner.stats,
        "threshold": threshold,
        "elapsed_sec": round(elapsed, 1),
        "rate_per_sec": round(runner.stats["hashed"] / max(1e-9, elapsed), 1),
        "index_comparisons": idx_st["comparisons"],
        "naive_comparisons_would_be": (
            runner.stats["hashed"] * max(0, runner.stats["hashed"] - 1) // 2),
        "dry_run": bool(args.dry_run),
    }


def main(argv=None) -> int:
    p = base_argparser("L2a pHash 去重（仅图片）")
    args = p.parse_args(argv)
    cfg = cfg_from_args(args)
    log = get_logger("02_dedup", cfg, args.log_level)
    summary = run(args, cfg)
    emit_summary(summary, args.json, log, "DEDUP SUMMARY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
