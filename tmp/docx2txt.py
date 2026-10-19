# -*- coding: utf-8 -*-
"""把 .docx 按文档顺序导成纯文本（段落 + 表格），不依赖 python-docx。

用法：
    .venv\\Scripts\\python.exe tmp\\docx2txt.py "xxx.docx" [out.txt]
"""
from __future__ import annotations

import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _text_of(node) -> str:
    parts = []
    for t in node.iter():
        if t.tag == W + "t":
            parts.append(t.text or "")
        elif t.tag == W + "tab":
            parts.append("\t")
        elif t.tag in (W + "br", W + "cr"):
            parts.append("\n")
    return "".join(parts)


def _style_of(p) -> str:
    ppr = p.find(W + "pPr")
    if ppr is None:
        return ""
    st = ppr.find(W + "pStyle")
    if st is None:
        return ""
    return st.get(W + "val") or ""


def convert(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml")
    root = ET.fromstring(xml)
    body = root.find(W + "body")
    out = []
    for el in body:
        if el.tag == W + "p":
            txt = _text_of(el).rstrip()
            style = _style_of(el)
            if not txt.strip():
                out.append("")
                continue
            if style.lower().startswith("heading") or style.lower() == "title":
                lvl = "".join(ch for ch in style if ch.isdigit())
                lvl = int(lvl) if lvl else 1
                out.append("#" * min(lvl, 6) + " " + txt.strip())
            else:
                out.append(txt)
        elif el.tag == W + "tbl":
            rows = []
            for tr in el.findall(W + "tr"):
                cells = [_text_of(tc).strip().replace("\n", " ") for tc in tr.findall(W + "tc")]
                rows.append(cells)
            if not rows:
                continue
            out.append("")
            out.append("| " + " | ".join(rows[0]) + " |")
            out.append("|" + "|".join(["---"] * len(rows[0])) + "|")
            for r in rows[1:]:
                if len(r) < len(rows[0]):
                    r = r + [""] * (len(rows[0]) - len(r))
                out.append("| " + " | ".join(r[: len(rows[0])]) + " |")
            out.append("")
    return "\n".join(out)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_suffix(".txt")
    txt = convert(src)
    dst.write_text(txt, encoding="utf-8")
    print(f"{src.name} -> {dst}  ({len(txt)} chars, {txt.count(chr(10))+1} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
