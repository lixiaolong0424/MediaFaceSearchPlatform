"""验证 scan.process_order 生效：photo_first 应先取照片，file_id 应按 id 升序。

同时验证「切换顺序不会重复劳动」——两种顺序下待处理集合的**规模**应一致，
已完成的文件都因 stage_status 被排除。

⚠️ 2026-10-16 修：以前的写法有两个坑，在流水线正在跑的时候必然误报
  1) 两次 `select_targets` 各开一个连接、各成一次快照，而抽帧进程每处理一个文件
     就提交一次 —— 两次读数之间库已经变了，总数当然对不上。
     现在把两次统计放进**同一个只读事务**里，拿到同一个 WAL 快照。
  2) 原断言「file_id 顺序下必须以视频开头」是数据依赖的：只有当最小的待处理
     file_id 恰好是视频时才成立。库里照片被处理得更快时它就会误报。
     改成断言「两种顺序取到的前 20 个**确实不同**，且 photo_first 的照片数严格更多」，
     这才是「顺序真的生效了」的稳定判据。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import db as D  # noqa: E402
from src.common import load_config  # noqa: E402
from src.extract import select_targets  # noqa: E402


def probe(cfg_pf, cfg_fid) -> dict:
    """在**同一个只读事务**里测两种顺序，保证拿到一致快照。"""
    conn = D.connect(cfg_pf.db_path, cfg=cfg_pf, readonly=True)
    try:
        conn.execute("BEGIN")          # WAL 只读事务 → 全程同一个快照
        out = {}
        for label, cfg in (("photo_first", cfg_pf), ("file_id", cfg_fid)):
            rows = select_targets(conn, cfg, limit=20)
            total = len(select_targets(conn, cfg))
            out[label] = {
                "kinds": [r["kind"] for r in rows],
                "ids": [int(r["file_id"]) for r in rows],
                "total": total,
            }
        return out
    finally:
        conn.close()


def main() -> int:
    base = load_config()
    cfg_fid = load_config(None, None,
                          overrides={"scan": {"process_order": "file_id"}})
    got = probe(base, cfg_fid)

    for label, d in got.items():
        print(f"\n=== {label} ===")
        print(f"  前 20 个 kind : {d['kinds']}")
        print(f"  照片占比      : {d['kinds'].count('photo')}/20")
        print(f"  前 20 个 id   : {d['ids'][:10]} …")
        print(f"  待处理总数    : {d['total']:,}")

    pf, fid = got["photo_first"], got["file_id"]
    # 「照片优先」真正要保证的是**顺序**：所有照片都排在视频前面，
    # 而不是「前 20 个全是照片」—— 库里只剩 3 张待处理照片时，前 20 个里
    # 必然混进视频，那条断言会因为数据状态变化而误报（2026-10-19 实测踩到）。
    kinds = pf["kinds"]
    first_video = kinds.index("video") if "video" in kinds else len(kinds)
    photos_before_video = kinds[:first_video].count("photo")
    total_photos_in_page = kinds.count("photo")
    print("\n=== 断言 ===")
    checks = [
        ("photo_first 里所有照片都排在视频之前",
         photos_before_video == total_photos_in_page,
         f"前段照片 {photos_before_video} / 全页照片 {total_photos_in_page}"),
        ("两种顺序取到的前 20 个确实不同（说明顺序生效）",
         pf["ids"] != fid["ids"]),
        ("photo_first 的照片数严格多于 file_id 顺序",
         pf["kinds"].count("photo") > fid["kinds"].count("photo")),
        ("两种顺序待处理总数一致（同一快照、共用断点续跑标记）",
         pf["total"] == fid["total"]),
        ("待处理总数 > 0", pf["total"] > 0),
    ]
    bad = 0
    for item in checks:
        name, ok = item[0], item[1]
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              + (f"  ({item[2]})" if not ok and len(item) > 2 else ""))
        if not ok:
            bad += 1
    print(f"\n结果：{'全部通过' if bad == 0 else f'{bad} 项失败'}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
