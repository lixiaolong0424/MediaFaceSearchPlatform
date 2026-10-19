# -*- coding: utf-8 -*-
"""src/taskplan.py · 任务种类 → 执行计划（规格 v5.3 §3.2）。

一个「任务」由若干「步骤」组成，每一步就是一个既有 CLI 脚本的子进程。
为什么不把步骤写进 app.py：命令行、界面、以后可能的定时任务都要用同一份计划，
计划只有一处定义，才不会出现「界面上是 4 步、脚本里是 5 步」的错位。

设计取舍（记录在案）：
  * 步骤之间**串行**。抽帧与编码抢同一个 SQLite 写锁，并行只会互相饿死
    （2026-10-16 实测过 3 个编码进程并发的教训）。
  * 每个步骤是**独立进程**。这样 04 的语义模型显存会在步骤结束时被释放，
    不会与 06/07 的人脸模型同时占着 24 GB 显存。
  * 进度由**步骤自己**上报（脚本里的 --task-id），计划只负责「第几步/共几步」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .common import Config


@dataclass
class Step:
    script: str                    # 入口脚本文件名
    args: List[str] = field(default_factory=list)
    code: str = "finalize"         # 阶段码（ui_text.stage.* 的 key）
    event: Optional[str] = None    # 该步结束后广播的变更事件

    def to_dict(self) -> Dict[str, Any]:
        return {"script": self.script, "args": list(self.args), "code": self.code,
                "event": self.event}


def _root_arg(cfg: Config, params: Dict[str, Any]) -> List[str]:
    root = params.get("root") or str(cfg.media_root)
    return ["--root", str(root)]


def scan_steps(cfg: Config, params: Dict[str, Any], *, full: bool) -> List[Step]:
    """扫描：**每个登记的素材目录一步**（v5.3 多目录支持）。

    多目录能安全并存的前提是 manifest 里两处已改：
      * 缺失判定限定在本次扫描的根内（不会把别的根标成缺失）；
      * path_index 主键带 root（两个根下有同名相对路径不会互相覆盖）。
    """
    from . import media_roots as MR

    roots = params.get("roots")
    if not roots:
        try:
            roots = MR.enabled_roots(cfg)
        except Exception:  # noqa: BLE001
            roots = [str(cfg.media_root)]
    steps: List[Step] = []
    for r in roots:
        args = ["--root", str(r)]
        if full:
            args.append("--reprocess-all")
        steps.append(Step("01_manifest.py", args, "scan", "library_changed"))
    if params.get("auto_process", True):
        steps += pipeline_steps(cfg, params, kinds=("photo", "video"))
    return steps


def pipeline_steps(cfg: Config, params: Dict[str, Any],
                   kinds: tuple = ("photo", "video")) -> List[Step]:
    """抽帧 → 画面编码 → 人脸检测 → 人脸编码。照片侧自带去重与缓存图，一趟读盘。"""
    steps: List[Step] = []
    if "photo" in kinds or "video" in kinds:
        kind = "photo" if kinds == ("photo",) else ("video" if kinds == ("video",) else "all")
        steps.append(Step("03_extract.py", ["--kind", kind], "extract", "library_changed"))
    steps += [
        Step("04_encode.py", [], "encode", "library_changed"),
        Step("06_face_detect.py", [], "face_detect", "faces_changed"),
        Step("07_face_encode.py", [], "face_encode", "faces_changed"),
    ]
    return steps


def faces_steps(cfg: Config, params: Dict[str, Any]) -> List[Step]:
    return [Step("06_face_detect.py", [], "face_detect", "faces_changed"),
            Step("07_face_encode.py", [], "face_encode", "faces_changed")]


def plan_for(kind: str, cfg: Config, params: Optional[Dict[str, Any]] = None) -> List[Step]:
    """返回某类任务的步骤列表。未知种类返回空列表（由调用方报错）。"""
    p = dict(params or {})
    if kind == "scan_new":
        return scan_steps(cfg, p, full=False)
    if kind == "scan_all":
        return scan_steps(cfg, p, full=True)
    if kind == "extract":
        return [Step("03_extract.py", ["--kind", p.get("kind", "all")], "extract",
                     "library_changed")]
    if kind == "encode":
        return [Step("04_encode.py", [], "encode", "library_changed")]
    if kind == "faces":
        if p.get("only") == "detect":
            return [Step("06_face_detect.py", [], "face_detect", "faces_changed")]
        if p.get("only") == "encode":
            return [Step("07_face_encode.py", [], "face_encode", "faces_changed")]
        return faces_steps(cfg, p)
    if kind == "face_detect":
        return [Step("06_face_detect.py", [], "face_detect", "faces_changed")]
    if kind == "face_encode":
        return [Step("07_face_encode.py", [], "face_encode", "faces_changed")]
    if kind == "cluster":
        return [Step("16_cluster.py", [], "cluster", "clusters_changed")]
    if kind == "reconcile":
        return [Step("18_reconcile.py", [], "reconcile", None)]
    if kind == "gold":
        return [Step("17_gold.py", [], "reconcile", "settings_changed")]
    if kind in ("migrate", "purge"):
        # 这两类在界面进程内执行（需要逐步确认 + 预览差异），见 src/migrate.py
        return []
    return []


TITLES = {
    "scan_new": "扫描新文件",
    "scan_all": "重新扫描全部",
    "extract": "抽取画面",
    "encode": "画面向量编码",
    "faces": "人脸检测与编码",
    "face_detect": "人脸检测",
    "face_encode": "人脸向量编码",
    "cluster": "自动分组",
    "migrate": "更新素材路径",
    "reconcile": "库完整性检查",
    "purge": "清理残留数据",
    "gold": "灵敏度标定",
}


def title_for(kind: str) -> str:
    return TITLES.get(kind, kind)
