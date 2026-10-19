# -*- coding: utf-8 -*-
"""列出 config.yaml 的全部叶子键（含类型与当前值），用于设计设置界面。"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.common import load_config  # noqa: E402


def walk(node, prefix=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from walk(v, f"{prefix}{k}.")
    else:
        yield prefix[:-1], node


def main() -> int:
    cfg = load_config()
    flat = dict(walk(cfg.as_dict()))
    print(f"{len(flat)} leaves")
    for k, v in flat.items():
        t = type(v).__name__
        s = json.dumps(v, ensure_ascii=False) if not isinstance(v, (str, int, float, bool)) else str(v)
        print(f"{k:<46} {t:<6} {s[:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
