# -*- coding: utf-8 -*-
"""src/settings_store.py · 设置的界面化（规格 v5.3 §3.3 L1~L4）。

原则（规格原文）：「用户能改的东西，必须有界面；界面上没有的东西，用户就不该去改。」

落地方式（记录在案的重要取舍）：
  界面把改动写到 **config.local.yaml**（叠加层），而不是直接改写 config.yaml。
  理由：config.yaml 里有大量解释性注释，用 PyYAML 重写会把注释全部抹掉；
  而叠加层是程序生成的文件，覆盖写没有损失，且「一键恢复默认」只需删键。
  加载顺序：config.yaml → config.local.yaml → 命令行 --config-overlay。
  （见 src/common.py::load_config，只有「纯默认加载」时才叠加，冒烟测试不受污染。）

每一项都带：
  * level：L1 常用 / L2 高级 / L3 危险（规格 §3.3 的三档分层）
  * restart：改完是否需要重启服务
  * rescan：改完是否需要重新扫描才对已入库文件生效
  * 旧值→新值写 audit_log（规格 §3.3 强制「所有设置修改必须写审计日志」）
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

from . import db as D
from .common import CONFIG_LOCAL_FILENAME, Config, local_now_iso

L1, L2, L3 = "common", "advanced", "danger"


@dataclass
class Setting:
    key: str                              # 配置里的 dotted key
    label: str
    level: str = L1
    kind: str = "float"                   # bool|int|float|choice|multi|text|path
    help: str = ""
    choices: Sequence[Tuple[Any, str]] = ()   # [(值, 界面文字)]
    vmin: Optional[float] = None
    vmax: Optional[float] = None
    step: Optional[float] = None
    restart: bool = False
    rescan: bool = False
    unit: str = ""
    special: str = ""                     # 特殊控件标记（media_root 走变更向导）
    hidden: bool = False


# --------------------------------------------------------------------------- #
# 全部设置项（界面按 level 分组渲染，顺序即展示顺序）
# --------------------------------------------------------------------------- #
SETTINGS: List[Setting] = [
    # ---------------- L1 常用 ----------------
    Setting("media_root", "素材文件夹", L1, "path", special="media_root",
            help="硬盘上存放照片和视频的根目录。换盘或改目录名请用「更改」向导，不要直接改。"),
    Setting("retrieval.min_score", "识别灵敏度", L1, "choice",
            choices=((0.40, "低 —— 宁可少找，也不找错"),
                     (0.30, "中 —— 推荐"),
                     (0.22, "高 —— 宁可多找，也别漏掉")),
            help="按内容搜索时的相似度门槛。调低=多召回但可能混入不相关的画面。"),
    Setting("ui.page_size", "照片墙每页张数", L1, "choice",
            choices=((30, "30 张"), (60, "60 张（推荐）"), (120, "120 张"), (200, "200 张")),
            help="一页显示多少人脸。张数越多首屏越慢。"),
    Setting("faces.quality_min_show", "照片墙清晰度下限", L1, "float",
            vmin=0.0, vmax=1.0, step=0.05,
            help="（只影响照片墙显示，**不影响自动分组**）低于这个清晰度的人脸不在照片墙上显示（数据仍保留，搜索仍能找到）。"),
    Setting("storage.face_thumb_quality", "人脸缩略图质量", L1, "int",
            vmin=60, vmax=95, step=5, rescan=True,
            help="值越高越清晰、占用越大。改动只影响以后新生成的人脸缩略图。"),
    Setting("library.dir_blacklist", "排除的文件夹名", L1, "multi",
            help="这些名字的文件夹会被整棵跳过（例如系统目录、回收站）。一行一个。"),
    Setting("library.photo_ext", "扫描哪些照片格式", L1, "multi", rescan=True,
            help="填扩展名，不带点。改完需要重新扫描才能发现被排除类型的文件。"),
    Setting("library.video_ext", "扫描哪些视频格式", L1, "multi", rescan=True,
            help="填扩展名，不带点。"),
    Setting("performance.mode", "性能模式", L2, "choice",
            choices=(("balanced", "均衡（默认，稳妥）"),
                     ("turbo", "全速（照片预读 + 更多视频并发 + 更大编码批）")),
            help="全速模式会明显提高 CPU/磁盘/GPU 占用，界面可能变卡；"
                 "想一键切换请用设置页顶部的「全速模式」开关。"),
    Setting("performance.photo_prefetch", "照片预读线程数", L2, "int",
            vmin=0, vmax=16, step=1,
            help="0=关闭。只并行「读盘」，去重顺序与写库顺序不变。"
                 "实测 3000×2000 照片：0→6 线程，13.9 张/秒提升到 24.6 张/秒。"),
    Setting("performance.video_workers", "视频并发数", L2, "int",
            vmin=0, vmax=24, step=1,
            help="0=沿用 video.max_worker_cap。视频解码是 CPU 密集型，"
                 "核数够多时可以开到 12。"),
    Setting("performance.frame_workers", "视频抽帧并行度", L2, "int",
            vmin=1, vmax=16, step=1,
            help="同一个视频里同时抓几帧。实测 7.2GB 视频：1→429 秒，8→67 秒（6.4 倍）。"),
    Setting("performance.encode_batch", "编码批大小", L2, "int",
            vmin=0, vmax=128, step=8,
            help="0=沿用 semantic.batch_size。显存够大就调大（OOM 会自动折半）。"),
    Setting("performance.encode_group", "编码分组大小", L2, "int",
            vmin=0, vmax=64, step=4,
            help="0=沿用 semantic.group_size。把更多文件合成一次前向。"),
    Setting("ui.start_page", "打开软件时默认显示", L1, "choice",
            choices=(("search", "搜索（推荐）"),
                     ("wall", "照片墙"),
                     ("tasks", "任务"),
                     ("settings", "设置"),
                     ("maintain", "维护"),
                     ("about", "关于")),
            help="每次启动（或刷新页面）先落在这个页面。默认「搜索」。"),
    Setting("scan.process_order", "处理顺序", L1, "choice",
            choices=(("photo_first", "先处理照片（推荐：先看到照片墙）"),
                     ("file_id", "按入库顺序（老文件优先）")),
            help="入库时先跑哪一类。只影响新文件的处理顺序，不影响已有数据。"),
    Setting("scan.video_size_order", "视频处理顺序", L1, "choice",
            choices=(("asc", "小文件优先（先看到成果，推荐）"),
                     ("desc", "大文件优先"),
                     ("off", "按入库顺序")),
            help="视频体量差别很大，小文件优先能让已完成的视频数尽快涨起来。"),

    # ---------------- L2 高级 ----------------
    Setting("scan.commit_batch_fused_photos", "抽帧写库批次", L2, "int",
            vmin=1, vmax=200, step=1,
            help="抽帧每处理几个文件提交一次事务。**必须保持 1**：设大了会饿死并行的编码进程。"),
    Setting("scan.batch_commit_files", "编码写库批次", L2, "int",
            vmin=10, vmax=2000, step=10,
            help="编码阶段每多少个文件提交一次。"),
    Setting("scan.max_extract_attempts", "单个文件最多重试次数", L2, "int",
            vmin=1, vmax=10, step=1,
            help="反复失败的坏文件达到这个次数后不再重试（否则会拖住整条流水线）。"),
    Setting("semantic.batch_size", "画面编码批大小", L2, "int",
            vmin=1, vmax=64, step=1,
            help="一次送进模型的图片张数。显存不足会自动折半，但手动调低更稳。"),
    Setting("video.max_worker_cap", "视频抽帧并发上限", L2, "int",
            vmin=1, vmax=8, step=1,
            help="同时用几个 ffmpeg 抽帧。机械硬盘建议 1~2，机械盘上开太多反而更慢。"),
    Setting("video.scdet_threshold", "镜头切分灵敏度", L2, "float",
            vmin=4.0, vmax=40.0, step=1.0, rescan=True,
            help="数值越小切得越碎。10 适合快剪短视频，30 适合长镜头。"),
    Setting("video.end_margin_sec", "视频末尾留白秒数", L2, "float",
            vmin=0.0, vmax=30.0, step=0.5,
            help="避免 seek 到最后一帧之后导致整段抽帧失败。"),
    Setting("video.min_shot_len_sec", "最短镜头时长", L2, "float",
            vmin=0.2, vmax=5.0, step=0.1,
            help="短于这个时长的镜头会被并入相邻镜头，减少重复画面。"),
    Setting("faces.det_thresh", "人脸检测置信度", L2, "float",
            vmin=0.2, vmax=0.9, step=0.05, rescan=True,
            help="调低能检出更多侧脸/遮挡脸，也可能检出更多误报。"),
    Setting("faces.quality_lap_norm", "清晰度归一化基准", L2, "float",
            vmin=50.0, vmax=400.0, step=10.0,
            help="把拉普拉斯方差换算成 0~1 清晰度的基准值。调大→同样的人脸评分更低。"),
    Setting("faces.max_faces_per_file", "单文件最多人脸数", L2, "int",
            vmin=50, vmax=2000, step=50,
            help="防止合照/人脸墙照片把整条流水线堵住。"),
    Setting("faces.burst_cosine", "连拍去重相似度", L2, "float",
            vmin=0.5, vmax=1.0, step=0.01, rescan=True,
            help="同一文件内相似度高于此值的人脸视为连拍重复，只留最清晰的一张。"),
    Setting("dedup.hamming_threshold", "照片去重严格度", L2, "int",
            vmin=0, vmax=20, step=1, rescan=True,
            help="感知哈希汉明距离阈值。数值越大越容易判为重复（8 实测较稳）。"),
    Setting("retrieval.rerank_enabled", "启用结果重排序", L2, "bool",
            help="用更贵的模型对初筛结果重排，显著更准但每次搜索多花十几秒。"),
    Setting("retrieval.rerank_input_k", "重排序候选数", L2, "choice",
            choices=((0, "关闭"), (20, "20（约 4 秒）"), (40, "40（约 8 秒）"),
                     (60, "60（约 12 秒，推荐）"), (120, "120（约 24 秒）"),
                     (400, "400（约 80 秒，最准）")),
            help="送进重排模型的候选数量。素材越多越该调大，但每次搜索更慢。"),
    Setting("retrieval.rerank_topk", "最多返回结果数", L2, "int",
            vmin=10, vmax=400000, step=10,
            help="搜索结果的上限。"),
    Setting("retrieval.rerank_min_score", "重排序后保留门槛", L2, "float",
            vmin=0.0, vmax=1.0, step=0.01,
            help="重排分数低于此值的结果不展示。与「识别灵敏度」是两个不同的分数体系。"),
    Setting("retrieval.rerank_skip_cosine", "跳过重排的相似度", L2, "float",
            vmin=0.5, vmax=1.0, step=0.01,
            help="初筛已经这么像的结果不再送重排，省时间。"),
    Setting("semantic.recall_k", "检索候选数（Qdrant 一次取多少）", L2, "int",
            vmin=100, vmax=400000, step=100,
            help="要返回很多结果时，这里必须够大；否则后面的上限再大也拿不到数据。"),
    Setting("retrieval.k_hard", "初筛候选上限", L2, "int",
            vmin=20, vmax=400000, step=20,
            help="向量检索一次最多取多少个候选。"),
    Setting("storage.nvme_budget_gb", "中间文件空间上限", L2, "int",
            vmin=50, vmax=2000, step=10, unit="GB",
            help="抽帧与人脸中间图允许占用的空间。超了会告警。"),
    Setting("storage.budget_warn_ratio", "空间告警水位", L2, "float",
            vmin=0.5, vmax=0.95, step=0.05,
            help="用到这个比例开始告警。"),
    Setting("ui.overview_cosine", "概览去重相似度", L2, "float",
            vmin=0.5, vmax=1.0, step=0.01,
            help="照片墙「概览」视图里，相似度高于此值的人脸合并成一张。"),
    Setting("ui.overview_use_qdrant", "概览跨文件去重", L2, "bool",
            help="开启后概览视图会用向量比对整库（更准但更慢，且需要额外显存）。"),
    Setting("logging.level", "日志级别", L2, "choice",
            choices=(("INFO", "INFO（推荐）"), ("DEBUG", "DEBUG（排查问题用）"),
                     ("WARNING", "WARNING（只记问题）")),
            restart=True, help="改完需要重启程序。"),
    Setting("monitor.webhook_url", "告警推送地址", L2, "text",
            help="留空=关闭。填一个 HTTP 地址，出现严重错误时会 POST 一条 JSON 过去。"),
    # 灵敏度标定用的照片集目录。hidden=True：不在「设置」表单里重复出现，
    # 它由「维护 → 灵敏度标定」那一块自己管；但必须登记，
    # 否则 settings_store.save() 会以「未登记的设置项」拒绝写入（2026-10-19 的 bug）。
    Setting("cluster.gold_root", "标定照片集文件夹", L2, "text", hidden=True, rescan=True,
            help="一人一个子文件夹；这些照片只用于算阈值，不会加入素材库。"),
    # v5.3 §7.4.2：允许多个目录。同一身份名（子文件夹同名）会合并成一个人。
    Setting("cluster.gold_roots", "标定照片集文件夹（可多个）", L2, "multi", hidden=True,
            rescan=True, help="一行一个目录；同名子文件夹视为同一个人。"),
    Setting("cluster.gold_layout", "标定集的目录约定", L2, "choice", hidden=True,
            choices=(("per_path", "一个目录 = 一个人（推荐）"),
                     ("subfolders", "目录里的子文件夹 = 人")),
            help="前者按规格 §7.4.2「一个路径下是一个人」；后者适合「一个总目录下每人一个子文件夹」。"),
    Setting("cluster.quality_gate", "参与分组的清晰度下限", L2, "float",
            vmin=0.0, vmax=1.0, step=0.05, rescan=True,
            help="低于这个清晰度的人脸不参与自动分组（仍可在照片墙与搜索里找到）。"),
    Setting("cluster.max_fit_faces", "单次分组的拟合上限（张脸）", L2, "int",
            vmin=1000, vmax=500000, step=10000,
            help="超过就先抽样拟合 HDBSCAN，其余按最近簇心分配。"
                 "30 万点一次性拟合会栈溢出崩溃；实测 19.8 万可完成。"),
    Setting("cluster.assign_dist_factor", "未参与拟合人脸的归组宽松度", L2, "float",
            vmin=0.5, vmax=4.0, step=0.1,
            help="阈值 = 簇半径中位数 × 这个系数。越大越容易把新脸归进已有组。"),
    Setting("cluster.assign_max_dist", "归组距离绝对上限（0=不设）", L2, "float",
            vmin=0, vmax=20, step=0.5,
            help="设成 0 就只用上面的自适应阈值；填正数则取两者较小值。"),
    Setting("cluster.hdbscan_min_cluster_size", "成组最少人数", L2, "int",
            vmin=2, vmax=20, step=1, rescan=True,
            help="低于这个张数的人脸不成组，直接进「未分组」。调小=更多人被分组。"),
    Setting("cluster.hdbscan_min_samples", "成组宽松度", L2, "int",
            vmin=1, vmax=10, step=1, rescan=True,
            help="越大越严格（更容易判为噪声）。1 = 最宽松，边界脸更容易并进已有的组。"),
    Setting("cluster.hdbscan_cluster_selection_epsilon", "合并相近组的距离", L2, "float",
            vmin=0.0, vmax=1.0, step=0.05, rescan=True,
            help="调大能把相近的小组合并成一个大组，未分组比例会下降。"),
    Setting("cluster.hdbscan_cluster_selection_method", "分组算法倾向", L2, "choice",
            choices=(("leaf", "leaf（更细、组更多、噪声更多）"),
                     ("eom", "eom（更粗、组更少、噪声更少）")),
            rescan=True,
            help="leaf 适合「把人分成尽量纯的小组」；eom 适合「按人浏览」。"),
]


BY_KEY: Dict[str, Setting] = {s.key: s for s in SETTINGS}


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #


def overlay_path(cfg: Config) -> Path:
    return Path(cfg.root) / CONFIG_LOCAL_FILENAME


def read_overlay(cfg: Config) -> Dict[str, Any]:
    p = overlay_path(cfg)
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 - 坏文件不该让界面打不开
        return {}


def _set_dotted(data: Dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _del_dotted(data: Dict[str, Any], dotted: str) -> bool:
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            return False
        node = nxt
    return node.pop(parts[-1], _MISSING) is not _MISSING


_MISSING = object()


def current(cfg: Config) -> Dict[str, Any]:
    """界面上要显示的「当前值」。"""
    out: Dict[str, Any] = {}
    for s in SETTINGS:
        v = cfg.get(s.key)
        if s.kind == "multi" and v is None:
            v = []
        out[s.key] = v
    return out


def _coerce(s: Setting, value: Any) -> Any:
    if s.kind == "bool":
        return bool(value)
    if s.kind == "int":
        return int(value)
    if s.kind == "float":
        return float(value)
    if s.kind == "multi":
        if isinstance(value, str):
            items = [x.strip() for x in value.replace(",", "\n").splitlines()]
        else:
            items = [str(x).strip() for x in (value or [])]
        return [x.lstrip(".").lower() for x in items if x]
    if s.kind == "choice":
        for cand, _label in s.choices:
            if cand == value:
                return cand
        # 允许数值型 choice 的字符串比较
        for cand, _label in s.choices:
            if str(cand) == str(value):
                return cand
        raise ValueError(f"{s.label}：不接受的值 {value!r}")
    return value


def validate(updates: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in updates.items():
        s = BY_KEY.get(k)
        if s is None:
            raise KeyError(f"未登记的设置项：{k}（界面不能改未登记的项）")
        try:
            v2 = _coerce(s, v)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{s.label}：{exc}") from exc
        if s.kind in ("int", "float"):
            if s.vmin is not None and v2 < s.vmin:
                raise ValueError(f"{s.label}：不能小于 {s.vmin}")
            if s.vmax is not None and v2 > s.vmax:
                raise ValueError(f"{s.label}：不能大于 {s.vmax}")
        out[k] = v2
    return out


def save(cfg: Config, updates: Dict[str, Any], *, who: str = "界面") -> Dict[str, Any]:
    """写 config.local.yaml 并写审计。返回 {changed, restart, rescan, notes}。"""
    clean = validate(updates)
    old_all = current(cfg)
    changed = {k: v for k, v in clean.items() if old_all.get(k) != v}
    result: Dict[str, Any] = {
        "changed": len(changed), "keys": sorted(changed),
        "restart": sorted(k for k in changed if BY_KEY[k].restart),
        "rescan": sorted(k for k in changed if BY_KEY[k].rescan),
        "notes": [],
    }
    if not changed:
        return result

    data = copy.deepcopy(read_overlay(cfg))
    for k, v in changed.items():
        _set_dotted(data, k, v)
    # 照片/视频扩展名改了要同步白名单，否则扫描阶段根本不看新格式。
    if "library.photo_ext" in changed or "library.video_ext" in changed:
        photo = clean.get("library.photo_ext", cfg.get("library.photo_ext") or [])
        video = clean.get("library.video_ext", cfg.get("library.video_ext") or [])
        merged: List[str] = []
        for e in list(photo) + list(video):
            if e not in merged:
                merged.append(e)
        _set_dotted(data, "library.ext_whitelist", merged)
        result["notes"].append("扫描范围已同步更新")

    path = overlay_path(cfg)
    header = (
        "# 由界面「设置」自动生成，请勿手工编辑（改了也会被下一次保存覆盖）。\n"
        "# 这里只记录与 config.yaml 不同的项；想恢复默认就在界面上点「恢复默认」。\n"
        f"# 最后修改：{local_now_iso()}\n"
    )
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    tmp.replace(path)

    # 顺手把新值写进内存里的 cfg：界面在 reload 之前还会再渲染一次，
    # 若不同步，用户会看到「保存了但显示还是旧值」，也会让「重复保存=无改动」判断失效。
    for k, v in changed.items():
        try:
            cfg.override(k, v)
        except Exception:  # noqa: BLE001
            pass

    # 审计：旧值→新值
    try:
        conn = D.connect(cfg.db_path, cfg=cfg)
        try:
            for k, v in changed.items():
                D.audit(conn, who, f"settings {k}", {"value": old_all.get(k)}, {"value": v})
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - 审计失败不该让保存失败
        result["notes"].append("审计日志写入失败（设置已保存）")

    from . import tasks as T  # 延迟导入避免循环

    T.init(cfg)
    T.emit(cfg, "settings_changed", {"keys": sorted(changed)})
    return result


def reset(cfg: Config, keys: Optional[Sequence[str]] = None, *, who: str = "界面") -> Dict[str, Any]:
    """恢复默认：从叠加层删掉这些键。不改 config.yaml 本身。"""
    keys = list(keys or [s.key for s in SETTINGS])
    data = copy.deepcopy(read_overlay(cfg))
    removed = [k for k in keys if _del_dotted(data, k)]
    path = overlay_path(cfg)
    if not removed:
        return {"removed": 0}
    header = (
        "# 由界面「设置」自动生成，请勿手工编辑。\n"
        f"# 最后修改：{local_now_iso()}\n"
    )
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                   encoding="utf-8")
    tmp.replace(path)

    # 恢复默认：把 base（config.yaml）里的值放回内存，界面立刻显示回默认值。
    base = _read_base_values(cfg, removed)
    for k, v in base.items():
        try:
            cfg.override(k, v)
        except Exception:  # noqa: BLE001
            pass

    try:
        conn = D.connect(cfg.db_path, cfg=cfg)
        try:
            for k in removed:
                D.audit(conn, who, f"settings reset {k}", {"value": cfg.get(k)}, {"value": None})
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass
    from . import tasks as T

    T.init(cfg)
    T.emit(cfg, "settings_changed", {"reset": removed})
    return {"removed": len(removed), "keys": removed}


def _read_base_values(cfg: Config, keys: Sequence[str]) -> Dict[str, Any]:
    """从 base 配置（config.yaml）读回这些键的出厂值，供「恢复默认」同步内存。"""
    from .common import CONFIG_FILENAME

    out: Dict[str, Any] = {}
    base_path = Path(cfg.root) / CONFIG_FILENAME
    if not base_path.exists():
        return out
    try:
        base = yaml.safe_load(base_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return out
    for k in keys:
        node: Any = base
        ok = True
        for part in k.split("."):
            if not isinstance(node, dict) or part not in node:
                ok = False
                break
            node = node[part]
        if ok:
            out[k] = node
    return out


def grouped() -> Dict[str, List[Setting]]:
    out: Dict[str, List[Setting]] = {L1: [], L2: [], L3: []}
    for s in SETTINGS:
        if not s.hidden:
            out.setdefault(s.level, []).append(s)
    return out


def sensitivity_options() -> List[Tuple[float, str]]:
    s = BY_KEY["retrieval.min_score"]
    return list(s.choices)
