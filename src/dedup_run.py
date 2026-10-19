"""L2a 去重的**可复用编排层**（索引 + 代表项维护 + 落库）。

为什么要有这一层
----------------
去重现在有两个入口，必须共用同一套语义，否则两边的判定会漂移：
    1. `02_dedup.py`            独立阶段（规格 L2a，可单独跑 / 可修数据）
    2. `03_extract.py` 融合模式 照片一趟读盘同时做 EXIF + pHash + 384/768px 缓存

融合的动机是硬件事实（2026-10-16 实测）：
    E: 是 14TB SATA 机械盘，纯读约 100~170 MB/s，照片共 3.235 TB。
    一趟读盘 ≈ 6~9 小时。规格原本的「去重 → 抽帧 → 编码 → 检脸」四遍读盘
    在机械盘上等于 30 小时以上，因此在照片侧把 L2a 折进 L2b 的同一趟读盘里。

代表项维护（2026-10-16 修的真实 bug）
------------------------------------
近重复分支必须**同步更新索引里的代表**：

    keep, drop = _decide_keeper(fid, matched)
    mark_skipped_dup(drop)
    if drop == matched:            # 旧代表被淘汰 → 索引换人
        idx.remove(other_hash); reps.pop(other_hash); reps[h] = keep; idx.insert(h)
    else:                           # 新文件被淘汰 → 不登记它的哈希
        reps.pop(h, None)

早期版本只标 skipped_dup、不换人，于是同一个代表被反复标记，
真正该淘汰的新文件反而留了下来（日志里连着几行 `去重：file_id=57（保留 60/61/62）`）。
回归测试见 tests/test_dedup_keeper.py。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import db as D
from .common import get_logger, local_now_iso
from .dedup_core import DedupIndex

LOG = get_logger("dedup_run")

KEEP = "keep"
DUP = "dup"


def compute_phash_file(path: Path, hash_size: int = 8,
                       max_pixels: Optional[int] = None) -> Optional[str]:
    """64 位 pHash（16 位十六进制）。任何解码失败返回 None。

    max_pixels：放开 PIL 的解压炸弹上限，见 src/extract.py::_apply_pil_pixel_cap。
    独立跑 02_dedup.py 时也要放开，否则同一批巨图在去重侧被记成
    「pHash 计算失败」、在抽帧侧被记成「图片解码失败」，两处都进不了库。
    """
    try:
        import imagehash
        from PIL import Image, ImageFile

        ImageFile.LOAD_TRUNCATED_IMAGES = True
        if max_pixels is not None:
            Image.MAX_IMAGE_PIXELS = int(max_pixels) or None
        with Image.open(path) as im:
            return str(imagehash.phash(im.convert("RGB"), hash_size=hash_size))
    except Exception as exc:  # noqa: BLE001 - 单文件失败绝不中断整批
        LOG.debug("pHash 失败 %s：%s", path, exc)
        return None


def compute_phash_image(im: Any, hash_size: int = 8) -> Optional[str]:
    """对**已打开**的 PIL Image 算 pHash（融合模式用，省一次读盘）。

    注意：绝不能用 Image.draft() 加速 —— 实测 draft 会改变解码像素，
    导致 107/500（21%）的照片哈希与全解码不一致，去重判定会漂移。
    （巨图例外：调用方已通过 Image.MAX_IMAGE_PIXELS 放开上限，
      仍然走全解码，代价是单张约 0.5~1 秒、峰值约 1.2GB，一次性可接受。）
    """
    try:
        import imagehash

        return str(imagehash.phash(im.convert("RGB"), hash_size=hash_size))
    except Exception as exc:  # noqa: BLE001
        LOG.debug("pHash(内存图) 失败：%s", exc)
        return None


class DedupRunner:
    """一次去重会话的状态：分块索引 + 代表项映射 + 计数。"""

    def __init__(self, *, hash_size: int = 8, threshold: int = 8) -> None:
        self.hash_size = int(hash_size)
        self.threshold = int(threshold)
        self.idx = DedupIndex(radius=self.threshold, hash_size=self.hash_size)
        self.reps: Dict[str, int] = {}
        self.stats: Dict[str, int] = {
            "hashed": 0, "duplicates": 0, "unique": 0, "errors": 0,
            "demoted": 0, "known_hashes": 0,
        }

    # -- 历史 -------------------------------------------------------------- #
    def load_history(self, conn) -> int:
        """把库里已有 phash 灌进索引，保证跨次扫描/跨进程续跑也能去重。

        只登记「未因去重被跳过、也未丢失」的文件：否则代表项自己已经被删/被跳过，
        新照片会去和一条不该存在的记录比。
        """
        for r in conn.execute(
            "SELECT phash, file_id FROM files "
            "WHERE phash IS NOT NULL AND status NOT IN ('skipped_dup','missing') "
            "ORDER BY file_id"
        ):
            h = str(r["phash"])
            if h not in self.reps:
                self.reps[h] = int(r["file_id"])
                self.idx.insert(h)
                self.stats["known_hashes"] += 1
        return self.stats["known_hashes"]

    def describe(self) -> str:
        st = self.idx.stats()
        return (f"分块索引 {st['chunk_count']} 块，块宽 {st['chunk_widths']}"
                f"（鸽巢原理保证零漏检），历史哈希 {self.stats['known_hashes']} 个")

    # -- 单个哈希 ---------------------------------------------------------- #
    def feed(self, conn, file_id: int, hex_hash: Optional[str], *,
             dry_run: bool = False) -> Tuple[str, Optional[int], int]:
        """判定一条哈希。

        返回 (KEEP|DUP, 对手 file_id 或 None, 汉明距离)。
        KEEP 表示该文件应作为代表继续走后面的阶段；DUP 表示它被淘汰。

        调用方负责：KEEP 时写缓存/标 done；DUP 时不要写缓存。
        `_on_demote` 回调（若已设置）会在**旧代表被淘汰**时被调用，
        以便融合模式把上一轮已经写好的缓存/状态回滚掉。
        """
        if hex_hash is None:
            self.stats["errors"] += 1
            D.enc_error(conn, file_id, D.MODEL_SEMANTIC, "pHash 计算失败")
            D.enc_error(conn, file_id, D.MODEL_FACE, "pHash 计算失败")
            return DUP, None, -1

        self.stats["hashed"] += 1
        hit = self.idx.lookup(hex_hash)
        if hit is None:
            self.stats["unique"] += 1
            if not dry_run:
                _set_phash(conn, file_id, hex_hash)
            self.reps.setdefault(hex_hash, file_id)
            self.idx.insert(hex_hash)
            return KEEP, None, -1

        other_hash, dist = hit
        matched = self.reps.get(other_hash, -1)

        # 续跑/重跑时必须先认自己：历史索引里可能已经有本文件的哈希
        # （例如先用 02_dedup.py 跑过一段，再跑融合模式的 03_extract.py）。
        # 不特判的话会走到 decide_keeper(fid, fid) → (fid, fid)，
        # 于是这个文件被判定为「自己的近重复」而被标 skipped_dup —— 数据直接丢。
        if matched == file_id:
            self.stats["unique"] += 1
            return KEEP, None, dist

        self.stats["duplicates"] += 1
        if dry_run:
            return DUP, (matched if matched > 0 else None), dist
        _set_phash(conn, file_id, hex_hash)
        if matched <= 0:
            self.reps[hex_hash] = file_id
            self.idx.insert(hex_hash)
            return KEEP, None, dist

        keep, drop = decide_keeper(conn, file_id, matched)
        if drop == matched:
            # 旧代表被淘汰：索引必须换人，并把它的下游产物回滚
            self.idx.remove(other_hash)
            self.reps.pop(other_hash, None)
            self.reps[hex_hash] = keep
            self.idx.insert(hex_hash)
            mark_dup(conn, drop, keep, dist, self.threshold)
            self.stats["demoted"] += 1
            on_demote = getattr(self, "on_demote", None)
            if on_demote is not None:
                on_demote(drop)
            LOG.info("去重：file_id=%s 让位给 %s（hamming=%d，旧代表已回滚）", drop, keep, dist)
            return KEEP, drop, dist

        # 新文件被淘汰：不登记它的哈希，代表保持不变
        self.reps.pop(hex_hash, None)
        mark_dup(conn, drop, keep, dist, self.threshold)
        LOG.info("去重：file_id=%s（保留 %s，hamming=%d）", drop, keep, dist)
        return DUP, matched, dist


def _set_phash(conn, file_id: int, hex_hash: str) -> None:
    conn.execute("UPDATE files SET phash=?, updated_at=? WHERE file_id=?",
                 (hex_hash, local_now_iso(), file_id))


def decide_keeper(conn, a: int, b: int) -> Tuple[int, int]:
    """返回 (保留的 file_id, 丢弃的 file_id)：分辨率更高、体积更大、mtime 更新者胜出。"""
    rows = D.files_by_ids(conn, [a, b])
    ra, rb = rows.get(a), rows.get(b)
    if ra is None:
        return b, a
    if rb is None:
        return a, b
    area_a = int(ra["width"] or 0) * int(ra["height"] or 0)
    area_b = int(rb["width"] or 0) * int(rb["height"] or 0)
    if area_a != area_b:
        return (a, b) if area_a > area_b else (b, a)
    if int(ra["size"] or 0) != int(rb["size"] or 0):
        return (a, b) if int(ra["size"] or 0) > int(rb["size"] or 0) else (b, a)
    return (a, b) if int(ra["mtime"] or 0) >= int(rb["mtime"] or 0) else (b, a)


def mark_dup(conn, drop: int, keep: int, dist: int, threshold: int) -> None:
    """把 drop 标为 skipped_dup，并为**每个模型**各记一行 skipped_dup（铁律 1）。"""
    conn.execute(
        "UPDATE files SET status='skipped_dup', visible=0, "
        "kind_hint='duplicate', updated_at=? WHERE file_id=?",
        (local_now_iso(), drop))
    D.all_skipped_dup(conn, drop,
                      f"near-duplicate of file_id={keep} hamming<={threshold}")
