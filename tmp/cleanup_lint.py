"""清理 pyflakes 报告的未使用导入与死变量（一次性脚本，可重复运行）。"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

EDITS = [
    # (file, old, new)
    ("src/export.py", 'fh.write(f"# 素材检索结果（供下游 agent 剪辑 / 归档，也供人工快捷操作）\\n")',
     'fh.write("# 素材检索结果（供下游 agent 剪辑 / 归档，也供人工快捷操作）\\n")'),
    ("src/ui_wall.py", '''    if cursor and cursor.valid and sort in (SORT_CAPTURED_DESC, SORT_CAPTURED_ASC):
        op = "<" if sort == SORT_CAPTURED_DESC else ">"
        if sort == SORT_CAPTURED_DESC:''',
     '''    if cursor and cursor.valid and sort in (SORT_CAPTURED_DESC, SORT_CAPTURED_ASC):
        if sort == SORT_CAPTURED_DESC:'''),
    ("src/manifest.py", '''    shard_note = None
    if args.shard:
        LOG.info("--shard %s 仅作用于后续处理阶段；清单扫描必须全量才能正确判定 missing", args.shard)
        shard_note = args.shard
''', '''    if args.shard:
        # 清单扫描必须全量：只扫一半会导致另一半被误判 missing。
        # --shard 是留给 02/03/04 的，这里只提示，不做分片。
        LOG.info("--shard %s 仅作用于后续处理阶段；清单扫描必须全量才能正确判定 missing", args.shard)
'''),
    ("00_bootstrap.py", '''        try:
            import numpy as np
            import onnxruntime as ort
''', '''        try:
            import onnxruntime as ort
'''),
]

# 未使用导入清理：(file, line_to_delete_exactly)
DROP_LINES = {
    "00_bootstrap.py": ["import subprocess\n", "import unicodedata\n"],
    "02_dedup.py": ["import time\n"],
    "12_maintain.py": ["import time\n"],
    "src/db.py": ["import time\n"],
    "src/encode.py": ["import os\n"],
    "src/extract.py": ["import shutil\n", "import subprocess\n"],
    "src/static_serve.py": ["import mimetypes\n"],
    "src/vector_store.py": ["import math\n"],
    "tests/make_media.py": ["import os\n"],
    "tests/p0_smoke.py": ["import time\n"],
}


def main() -> int:
    changed = 0
    for rel, old, new in EDITS:
        p = ROOT / rel
        txt = p.read_text(encoding="utf-8")
        if old in txt:
            p.write_text(txt.replace(old, new, 1), encoding="utf-8")
            print(f"edited  {rel}")
            changed += 1
        else:
            print(f"SKIP(no match) {rel}: {old[:50]!r}")
    for rel, lines in DROP_LINES.items():
        p = ROOT / rel
        txt = p.read_text(encoding="utf-8")
        for ln in lines:
            if ln in txt:
                txt = txt.replace(ln, "", 1)
                print(f"dropped {rel}: {ln.strip()}")
                changed += 1
        p.write_text(txt, encoding="utf-8")
    print(f"total changes: {changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
