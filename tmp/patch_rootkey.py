# -*- coding: utf-8 -*-
"""修两个归一化 bug：

BUG1 `config media_root = E:`（丢了结尾斜杠）
      normalize("E:/") 把 "E:/" rstrip 成 "E:" —— 盘符根被吃掉了斜杠。

BUG2 F 目录显示「文件 0」
      files.root 有两种写法：
        'F:\\...\\20250828照片\\'   ← 迁移回填 / MR._register 写的（带结尾分隔符）
        'F:\\...\\20250828照片'     ← 01_manifest 写的（str(Path(...))，不带）
      统计按带分隔符的前缀去比对，于是 7,987 条一条都对不上。
      同时 libraries 里也因此出现两行。

修法：统一成「带结尾分隔符」一种写法，并把历史数据纠正过来。
"""
import io
import re

# ---------- 1) media_roots.normalize：盘符根保留斜杠 ----------
p = "src/media_roots.py"
s = io.open(p, encoding="utf-8").read()
old = '''    return str(p).replace("\\\\", "/").rstrip("/") or "/"'''
new = '''    out = str(p).replace("\\\\", "/").rstrip("/")
    # 盘符根（E:）必须写成 E:/：否则 Path("E:") 会变成「当前目录在 E 盘上」
    if re.fullmatch(r"[A-Za-z]:", out):
        out += "/"
    return out or "/"'''
assert old in s, "normalize pattern not found"
s = s.replace(old, new, 1)
s = s.replace("import os\nfrom pathlib import Path",
              "import os\nimport re\nfrom pathlib import Path", 1)
io.open(p, "w", encoding="utf-8", newline="\n").write(s)
print("media_roots patched")

# ---------- 2) manifest：files.root / libraries 统一用带分隔符的前缀 ----------
p2 = "src/manifest.py"
s2 = io.open(p2, encoding="utf-8").read()

helper = '''def _root_key(root) -> str:
    """素材根的规范写法：绝对路径 + 结尾分隔符。

    ⚠️ 必须统一：files.root 只允许这一种写法。
    2026-10-19 踩过——迁移回填与界面登记写的是 `...\\照片\\`，
    而 01_manifest 写的是 `...\\照片`（str(Path) 不带结尾分隔符），
    于是按前缀统计文件数时一条都对不上，界面上显示「未扫描 / 0 个文件」。
    """
    s = str(Path(str(root)))
    return s if s.endswith(("\\\\", "/")) else s + os.sep


'''
anchor = "def probe_inode_stability"
s2 = s2.replace(anchor, helper + anchor, 1)

# classify：path_index 与 files_table 的 root 过滤都用规范写法
s2 = s2.replace("    root_s = str(root)\n    path_index = D.load_path_index(conn, root_s)",
                "    root_s = _root_key(root)\n    path_index = D.load_path_index(conn, root_s)", 1)

# apply_classification：root_s 也用规范写法
s2 = s2.replace('    root_s = str(root)\n\n    # --dry-run',
                '    root_s = _root_key(root)\n\n    # --dry-run', 1)

# missing 差集里的 str(root) / scope_like
s2 = s2.replace("    scope_like = str(root).rstrip(\"\\\\/\") + \"%\"",
                "    scope_like = root_s.rstrip(\"\\\\/\") + \"%\"", 1)
s2 = s2.replace("            (str(root), scope_like)):", "            (root_s, scope_like)):", 1)
s2 = s2.replace("        (str(root), scope_like)).fetchone()[0] or 0)",
                "        (root_s, scope_like)).fetchone()[0] or 0)", 1)

io.open(p2, "w", encoding="utf-8", newline="\n").write(s2)
print("manifest patched")
