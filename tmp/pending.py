"""打印某一类素材「还能处理多少」（供 chain_rounds.ps1 做分轮次与停滞判断）。

注意：必须与 03_extract.py::select_targets 的口径一致 —— 也就是说要
**排除已被死信表拦住的文件**（抽帧重试超过 scan.max_extract_attempts 次）。
否则「pending 永远 > 0」会让链误以为还有活可干：
2026-10-18 的真实事故就是这样 —— 1,506 个永远失败的文件让链空转了 291 轮。
"""

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

kind = sys.argv[1] if len(sys.argv) > 1 else "photo"
max_attempts = 3
try:
    from src.common import load_config

    max_attempts = int(load_config(None, None).get("scan.max_extract_attempts", 3))
except Exception:  # noqa: BLE001 - 读配置失败就用默认值，不要因此让链停摆
    pass

conn = sqlite3.connect(str(ROOT / "data" / "library.db"))
n = conn.execute(
    """SELECT COUNT(*) FROM files f
        WHERE f.kind=? AND f.status IN ('pending','error')
          AND (SELECT COUNT(*) FROM dead_letter dl
                WHERE dl.file_id=f.file_id AND dl.stage='extract') < ?""",
    (kind, max_attempts)).fetchone()[0]
blocked = conn.execute(
    """SELECT COUNT(*) FROM files f
        WHERE f.kind=? AND f.status IN ('pending','error')
          AND (SELECT COUNT(*) FROM dead_letter dl
                WHERE dl.file_id=f.file_id AND dl.stage='extract') >= ?""",
    (kind, max_attempts)).fetchone()[0]
conn.close()
if len(sys.argv) > 2 and sys.argv[2] == "--verbose":
    print(f"{kind}: 可处理 {n:,}，已放弃 {blocked:,}")
else:
    print(int(n))
