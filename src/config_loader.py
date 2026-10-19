"""加载 config 的统一助手：把 --config / --config-overlay / --project-root 一次搞定。"""
from __future__ import annotations

from typing import Any

from .common import Config, load_config


def cfg_from_args(args: Any, **overrides: Any) -> Config:
    """所有入口脚本都用它，避免每个 main() 重复三行且漏掉 overlay。

    v5.3 §3.2：顺带把 `--task-id` 绑上任务队列。放在这里而不是每个 main() 里，
    是为了让「以后新增的脚本自动具备进度上报」，不依赖谁记得写那两行。
    没带 --task-id 时 attach() 是空操作，命令行用法完全不变。
    """
    cfg = load_config(
        getattr(args, "config", None),
        getattr(args, "project_root", None),
        overrides=overrides or None,
        overlay=getattr(args, "config_overlay", None),
    )
    task_id = getattr(args, "task_id", None)
    if task_id:
        from . import tasks as T

        T.init(cfg)
        T.attach(cfg, task_id)
    return cfg
