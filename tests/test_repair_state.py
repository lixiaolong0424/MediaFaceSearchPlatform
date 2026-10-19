"""`12_maintain.py repair` 的状态迁移回归测试。

被测的不变量（2026-10-16 两个真实 bug 的教训）
---------------------------------------------
各阶段的取数条件：
    ＊ 03_extract      要求 `files.status IN ('pending','error')`
                       且 `stage_status(extract)` 未 done
    ＊ 04_encode / 07  要求 `stage_status(extract)='done'`
                       **且 `files.status='done'`**

由此推出两条**必须**成立的规则：

  R1. 抽帧已成功的文件（`stage_status(extract)='done'`），repair 之后
      `files.status` 必须是 `done` —— 否则它在三条路上全部隐身：
      抽帧因 stage 已 done 而跳过，编码因 status≠done 而跳过，
      于是**永远编码不了**（实测 25 个文件这样卡住）。
  R2. 抽帧没成功过的文件，repair 之后必须是 `pending`，否则抽帧不会重来。
  R3. 抽帧/编码之外的状态（missing / skipped_dup）绝不能被 repair 覆盖成 done。
  R4. 铁律 1：skipped_dup 必须是**两个模型各一行**。

用法：
    .venv\\Scripts\\python.exe tests\\test_repair_state.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import db as D  # noqa: E402
from src.common import load_config  # noqa: E402
from src.extract import ensure_stage_table, set_stage  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def build(tmp: Path):
    """建一个真 schema 的临时库，塞入各种边界行，返回 (cfg, db_path)。"""
    db = tmp / "repair_test.db"
    cfg = load_config(None, None, overrides={
        "db": {"path": str(db)},
        "paths": {
            "nvme_root": str(tmp / "nvme"),
            "frames": str(tmp / "nvme" / "frames"),
            "thumbs": str(tmp / "nvme" / "thumbs"),
            "faces_raw": str(tmp / "nvme" / "faces" / "raw"),
            "faces_thumbs": str(tmp / "nvme" / "faces" / "thumbs"),
            "embeddings": str(tmp / "nvme" / "embeddings"),
            "query_cache": str(tmp / "nvme" / "query_cache"),
            "export": str(tmp / "export"),
            "logs": str(tmp / "logs"),
            "tmp": str(tmp / "tmp"),
        },
    })
    D.ensure_schema(cfg.db_path, cfg.root, cfg)
    conn = D.connect(cfg.db_path, cfg=cfg)
    ensure_stage_table(conn)

    def add(fid: int, status: str, *, extract_done: bool,
            semantic: str, face: str = "skipped_dup") -> None:
        conn.execute(
            "INSERT INTO files(file_id, path, kind, status) VALUES (?,?, 'photo', ?)",
            (fid, f"E:/x/{fid}.jpg", status))
        if extract_done:
            set_stage(conn, fid, "extract", "done", shots=1, frames=1)
        for model, st in (("semantic", semantic), ("face", face)):
            conn.execute(
                "INSERT INTO file_encoding(file_id, model, status) VALUES (?,?,?)",
                (fid, model, st))
    # 1) 抽帧成功、编码 pending、status 被前一次坏 repair 写成了 pending  ← R1 核心
    add(1, "pending", extract_done=True, semantic="pending")
    # 2) 抽帧成功、编码 error，status 正常是 done
    add(2, "done", extract_done=True, semantic="error")
    # 3) 抽帧从未成功（没有 extract 行）、编码 error  ← R2
    add(3, "error", extract_done=False, semantic="error")
    # 4) 抽帧成功、编码 pending，但文件已缺失 ← R3：不许被改成 done
    add(4, "missing", extract_done=True, semantic="pending")
    # 5) 去重跳过的文件（两模型各一行 skipped_dup）← R3/R4：不许动
    add(5, "skipped_dup", extract_done=False, semantic="skipped_dup", face="skipped_dup")
    conn.commit()
    conn.close()
    return cfg, db


def main() -> int:
    print("=" * 74)
    print("12_maintain repair 状态迁移回归测试")
    print("=" * 74)

    with tempfile.TemporaryDirectory() as td:
        cfg, db = build(Path(td))

        import argparse

        import importlib.util

        spec = importlib.util.spec_from_file_location("maint12", ROOT / "12_maintain.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        args = argparse.Namespace(dry_run=False, json=False, days=30, confirm=False)
        out = mod.cmd_repair(cfg, args)
        print(f"  repair 返回：{out}\n")

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        st = {int(r["file_id"]): str(r["status"])
              for r in conn.execute("SELECT file_id, status FROM files")}
        stages = {}
        for r in conn.execute("SELECT file_id, stage, status FROM stage_status"):
            stages.setdefault(int(r["file_id"]), {})[str(r["stage"])] = str(r["status"])
        encs = {}
        for r in conn.execute("SELECT file_id, model, status FROM file_encoding"):
            encs.setdefault(int(r["file_id"]), {})[str(r["model"])] = str(r["status"])

        print("  修复后状态：")
        for fid in sorted(st):
            print(f"    fid={fid} files.status={st[fid]:<12} "
                  f"stage={stages.get(fid, {})} enc={encs.get(fid, {})}")
        print()

        # R1：抽帧成功 + 编码待做 → 必须恢复成 done，编码阶段才看得见
        check("R1 抽帧已完成 → status 恢复为 done", st[1] == "done", f"got {st[1]}")
        check("R1 抽帧已完成（error 编码）→ 也是 done", st[2] == "done", f"got {st[2]}")
        # R2：抽帧没成功过 → 必须 pending，抽帧才会重来
        check("R2 抽帧未完成 → status 变回 pending", st[3] == "pending", f"got {st[3]}")
        # R3：missing 不许被覆盖
        check("R3 missing 不被覆盖成 done", st[4] == "missing", f"got {st[4]}")
        check("R3 skipped_dup 不被覆盖", st[5] == "skipped_dup", f"got {st[5]}")
        # 编码行都被重置为 pending（让阶段重拾）
        check("fid=1 的 semantic 重置为 pending",
              encs.get(1, {}).get("semantic") == "pending", f"{encs.get(1)}")
        # 编码阶段那一行被清掉
        check("fid=2 的 semantic_encode 阶段行被清除",
              "semantic_encode" not in stages.get(2, {}), f"{stages.get(2)}")
        # 抽帧阶段行必须保留（否则会重读机械盘）
        check("fid=1/2 的 extract 阶段行保留（不重读机械盘）",
              stages.get(1, {}).get("extract") == "done"
              and stages.get(2, {}).get("extract") == "done", f"{stages.get(1)} {stages.get(2)}")
        # R4：铁律 1
        n_skip = conn.execute(
            "SELECT COUNT(*) FROM files WHERE status='skipped_dup'").fetchone()[0]
        check("R4 去重文件仍为 skipped_dup", n_skip == 1, f"got {n_skip}")

        # 可见性：修复后编码阶段应该能看到 fid=1
        vis = [int(r["file_id"]) for r in conn.execute(
            "SELECT f.file_id FROM files f "
            "JOIN stage_status ex ON ex.file_id=f.file_id AND ex.stage='extract' "
            "  AND ex.status='done' "
            "LEFT JOIN stage_status ss ON ss.file_id=f.file_id AND ss.stage='semantic_encode' "
            "WHERE f.status='done' AND f.kind IN ('photo','video') "
            "  AND (ss.status IS NULL OR ss.status IN ('pending','error')) ORDER BY f.file_id")]
        check("端到端可见性：04_encode 的目标集合含 fid=1", 1 in vis, f"vis={vis}")
        check("端到端可见性：不含 missing 的 fid=4", 4 not in vis, f"vis={vis}")
        conn.close()

    print("\n" + "=" * 74)
    print(f"PASS={PASS}  FAIL={FAIL}")
    print("=" * 74)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
