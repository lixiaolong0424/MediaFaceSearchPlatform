"""第二轮 lint 清理：删除确认未使用的导入。每次改动后都会重新跑 pyflakes 校验。"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (文件, 要精确删除的行)
DROP = {
    "00_bootstrap.py": ["    Config, dir_size_bytes, find_executable, get_logger, human_bytes,\n"],
    "02_dedup.py": ["    Config, base_argparser, emit_summary, get_logger, local_now_iso, shard_range,\n"],
    "12_maintain.py": ["    Config, base_argparser, dir_size_bytes, emit_summary, get_logger, human_bytes,\n"],
    "app.py": ["    CONFIG_ENV, CONFIG_FILENAME, Config, get_logger, human_bytes, load_config,\n"],
    "src/common.py": [],
    "src/config_loader.py": [],
    "src/db.py": [],
    "src/exif.py": [],
    "src/export.py": [],
    "src/extract.py": [],
    "src/manifest.py": [],
    "src/search.py": [],
    "src/static_serve.py": [],
    "src/vector_store.py": [],
    "src/ui_wall.py": [],
}

# 简单的「删掉这一整行 import」规则（精确匹配整行）
DROP_LINES = {
    "02_dedup.py": ["from typing import Any, Dict, List, Optional, Sequence, Tuple\n"],
    "12_maintain.py": ["from typing import Any, Dict, List, Optional, Sequence, Tuple\n"],
    "src/exif.py": ["from datetime import datetime, timedelta, timezone\n"],
    "src/encode.py": ["from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple\n"],
    "src/manifest.py": ["from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple\n"],
    "src/static_serve.py": ["import os\n"],
    "src/vector_store.py": [],
}

REPLACE = {
    "02_dedup.py": [("from typing import Any, Dict, List, Optional, Sequence, Tuple\n",
                     "from typing import Any, Dict, List, Optional, Tuple\n")],
    "12_maintain.py": [("from typing import Any, Dict, List, Optional, Sequence, Tuple\n",
                        "from typing import Any, Dict, List\n")],
    "src/exif.py": [("from datetime import datetime, timedelta, timezone\n",
                     "from datetime import datetime, timezone\n")],
    "src/encode.py": [("from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple\n",
                       "from typing import Any, Dict, List, Optional\n")],
    "src/manifest.py": [("from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple\n",
                         "from typing import Any, Dict, Iterator, List, Optional, Set, Tuple\n")],
    "src/static_serve.py": [("import mimetypes\nimport os\n", "")],
    "src/export.py": [("from datetime import datetime, timezone\n", "from datetime import datetime\n"),
                      ("from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple\n",
                       "from typing import Any, Dict, List, Optional, Sequence\n")],
    "src/db.py": [("from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple\n",
                   "from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple\n")],
    "src/search.py": [("from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple\n",
                       "from typing import Any, Dict, List, Optional, Sequence, Tuple\n")],
    "src/extract.py": [("from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple\n",
                        "from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple\n")],
    "src/ui_wall.py": [("from pathlib import Path\n", ""),
                       ("from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple\n",
                        "from typing import Any, Dict, List, Optional, Sequence, Tuple\n")],
    "src/config_loader.py": [("from typing import Any, Optional, Sequence\n", "from typing import Any\n")],
    "app.py": [("    CONFIG_ENV, CONFIG_FILENAME, Config, get_logger, human_bytes, load_config,\n"
                "    new_session_id, project_root, session_id,\n",
                "    CONFIG_ENV, CONFIG_FILENAME, Config, get_logger, human_bytes, load_config,\n"
                "    new_session_id, project_root,\n"),
               ("from typing import Any, Dict, List, Optional, Sequence, Tuple\n",
                "from typing import Any, Dict, List, Optional\n")],
    "00_bootstrap.py": [("    Config, dir_size_bytes, find_executable, get_logger, human_bytes,\n"
                         "    preload_ort_cuda, run_capture, session_id,\n",
                         "    Config, find_executable, get_logger, human_bytes,\n"
                         "    preload_ort_cuda, run_capture, session_id,\n")],
}


def main() -> int:
    changed = 0
    for rel, pairs in REPLACE.items():
        p = ROOT / rel
        txt = p.read_text(encoding="utf-8")
        for old, new in pairs:
            if old in txt:
                txt = txt.replace(old, new, 1)
                changed += 1
                print(f"replaced in {rel}: {old.splitlines()[0][:60]!r}")
            else:
                print(f"  skip(no match) {rel}: {old.splitlines()[0][:60]!r}")
        p.write_text(txt, encoding="utf-8")

    # 校验：编译 + import
    files = sorted([p for p in ROOT.glob("*.py")] + [p for p in (ROOT / "src").glob("*.py")]
                   + [p for p in (ROOT / "tests").glob("*.py")])
    bad = subprocess.run([sys.executable, "-m", "py_compile", *[str(f) for f in files]],
                         capture_output=True, text=True)
    print("py_compile rc =", bad.returncode)
    if bad.returncode:
        print(bad.stderr[-2000:])
        return 1
    out = subprocess.run([sys.executable, "-m", "pyflakes", *[str(f) for f in files]],
                         capture_output=True, text=True, cwd=str(ROOT))
    lines = [l for l in out.stdout.splitlines() if "unable to detect undefined" in l
             or "undefined name" in l]
    print("undefined-name findings:", len(lines))
    for l in lines:
        print("  ", l)
    print(f"total changes: {changed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
