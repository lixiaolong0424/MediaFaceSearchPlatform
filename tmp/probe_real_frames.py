"""在真实 E:\\ 素材上验证抽帧：必须是偶数尺寸、可解码、非全黑。

覆盖规格第三节「视频抽帧」的真实数据风险（mjpeg 拒绝奇数宽高）。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.common import load_config  # noqa: E402
from src.extract import extract_frame, ffprobe_media, locate_tools, validate_frame  # noqa: E402

DB = ROOT / "tests" / "_real" / "library_real.db"
OUT = ROOT / "tests" / "_real" / "probe"


def main() -> int:
    cfg = load_config(None, None, overlay=[str(ROOT / "tests" / "config_real.yaml")])
    tools = locate_tools(cfg)
    if not tools.ok:
        print("ffmpeg/ffprobe 不可用")
        return 1

    if not DB.exists():
        print(f"库不存在：{DB}（先跑 01_manifest.py --config-overlay tests/config_real.yaml）")
        return 1

    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    rows = list(con.execute(
        "SELECT file_id, path, duration_sec FROM files WHERE kind='video' LIMIT 6"))
    con.close()
    print(f"库中视频样本：{len(rows)} 个")

    OUT.mkdir(parents=True, exist_ok=True)
    ok_cnt = 0
    even_cnt = 0
    total = 0
    for r in rows:
        p = Path(str(r["path"]))
        if not p.exists():
            print(f"  file_id={r['file_id']} 已不存在，跳过")
            continue
        pr = ffprobe_media(tools.ffprobe, p)
        dur = pr.duration or 2.0
        for ts in (0.5, dur / 2.0, max(0.0, dur - 0.5)):
            total += 1
            out = OUT / f"{r['file_id']}_{int(ts * 1000)}.jpg"
            ok, err = extract_frame(tools.ffmpeg, p, ts, out, 384)
            if not ok:
                print(f"  file_id={r['file_id']} {pr.width}x{pr.height} ts={ts:.1f} "
                      f"FAILED: {err[:140]}")
                continue
            good, reason, w, h = validate_frame(out, cfg)
            ok_cnt += 1
            if w % 2 == 0 and h % 2 == 0:
                even_cnt += 1
            print(f"  file_id={r['file_id']} src={pr.width}x{pr.height} ts={ts:6.1f} "
                  f"-> {w}x{h} even={w % 2 == 0 and h % 2 == 0} valid={good} {reason}")
    print(f"\n抽帧成功 {ok_cnt}/{total}，偶数尺寸 {even_cnt}/{ok_cnt}")
    return 0 if (ok_cnt == total and even_cnt == ok_cnt and total > 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
