# -*- coding: utf-8 -*-
"""补丁：migrate.preview/apply 支持指定「旧的根」，并在改前缀时同步 files.root 与登记表。

多素材目录之后，「改路径」不再只针对 config.media_root，而是针对列表里的某一项。
"""
import io

p = "src/migrate.py"
s = io.open(p, encoding="utf-8").read()

# 1) preview 增加 old_root 参数
s = s.replace(
    "def preview(cfg: Config, new_root: str, *, check_exists: bool = True,\n"
    "            limit: Optional[int] = None,\n"
    "            progress: Optional[Callable[[int, int], None]] = None) -> Preview:",
    "def preview(cfg: Config, new_root: str, *, old_root: Optional[str] = None,\n"
    "            check_exists: bool = True,\n"
    "            limit: Optional[int] = None,\n"
    "            progress: Optional[Callable[[int, int], None]] = None) -> Preview:", 1)
s = s.replace("    old_prefix = _prefix(cfg.media_root)",
              "    old_prefix = _prefix(old_root if old_root is not None else cfg.media_root)", 1)

# 2) apply 增加 old_root 参数
s = s.replace(
    "def apply(cfg: Config, new_root: str, *, strategy: str = STRATEGY_KEEP,\n"
    "          dry_run: bool = False,",
    "def apply(cfg: Config, new_root: str, *, old_root: Optional[str] = None,\n"
    "          strategy: str = STRATEGY_KEEP,\n"
    "          dry_run: bool = False,", 1)
s = s.replace("    old_prefix = _prefix(cfg.media_root)",
              "    old_prefix = _prefix(old_root if old_root is not None else cfg.media_root)", 1)

# 3) 改前缀时同步 root 列（多目录后 root 是缺失判定与计数的依据）
s = s.replace(
    '                "UPDATE files SET path = ? || rel_path, updated_at=? "\n'
    '                "WHERE path LIKE ? || \'%\'",\n'
    "                (new_prefix, local_now_iso(), old_prefix),",
    '                "UPDATE files SET path = ? || rel_path, root = ?, updated_at=? "\n'
    '                "WHERE path LIKE ? || \'%\'",\n'
    "                (new_prefix, new_prefix, local_now_iso(), old_prefix),", 1)

# 4) 收尾改为更新登记表（多目录），而不是硬写 media_root
s = s.replace("        _write_media_root(cfg, new_root)",
              "        _update_registry(cfg, old_prefix, new_root)", 1)

# 5) 追加登记表更新函数
s += '''

def _update_registry(cfg: Config, old_prefix: str, new_root: str) -> None:
    """把「改了路径」这件事同步到素材目录登记表（多目录之后 media_root 只是第一项）。"""
    try:
        from . import media_roots as MR

        MR.update(cfg, old_prefix, new_path=new_root, who="界面/路径变更")
        return
    except Exception as exc:  # noqa: BLE001 - 登记表不在（老配置）时退回写 media_root
        LOG.warning("更新素材目录登记失败，改回只写 media_root：%s", exc)
    _write_media_root(cfg, new_root)
'''
io.open(p, "w", encoding="utf-8", newline="\n").write(s)
print("patched migrate.py")
