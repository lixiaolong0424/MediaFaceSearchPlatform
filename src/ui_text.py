# -*- coding: utf-8 -*-
"""src/ui_text.py · 界面文案字典（规格 v5.3 §2 术语清理，强制唯一来源）。

规矩（规格 §2.3）：
  * 所有界面文案集中在本文件，禁止硬编码在组件里；
  * 禁用文案见 BANNED，UI 里出现一个都算不通过（tests/test_ui_terms.py 会扫源码）；
  * 想改说法只改这里，不要改组件。

为什么值得单开一个文件：v3 的界面把「（规格 8.6）」「06 / F1 F2 / L2c」这类开发期
代号直接漏给了用户，用户既看不懂、又会在下次重构时全部失效。
"""

from __future__ import annotations

from typing import Dict, List

# --------------------------------------------------------------------------- #
# §2.2 文案映射（禁用 → 采用）
# --------------------------------------------------------------------------- #
# 说明：这里只做「界面可见文字」。日志、README、代码注释里保留技术名词是允许的
# （规格 §2.2 明确写「仅管理后台可见」「不得出现在日志以外的界面」）。
TEXTS: Dict[str, str] = {
    # ---- 应用与导航 ----
    "app.title": "素材整理与检索",
    "app.tagline": "扫一遍硬盘，之后按内容或人脸找回来",
    "nav.wall": "照片墙",
    "nav.groups": "自动分组",
    "nav.search": "搜索",
    "nav.tasks": "任务",
    "nav.settings": "设置",
    "nav.maintain": "维护",
    "nav.about": "关于",
    "nav.help": "使用说明",

    # ---- 扫描类动作（§2.2 强制映射）----
    "scan.new": "扫描新文件",
    "scan.new.help": "只处理上次扫描后新增或变化的文件",
    "scan.all": "重新扫描全部",
    "scan.all.help": "从零重做抽帧、编码与人脸检测，耗时数小时到数十小时",
    "scan.all.confirm": "重新扫描全部会重做抽帧、编码与人脸检测，预计耗时数小时以上。确定继续吗？",
    "scan.running": "正在扫描…",
    "scan.auto_process": "扫描后自动接着处理（抽帧 / 向量编码 / 人脸检测）",
    "scan.auto_process.help": "关掉只更新文件清单，界面上不会出现新的照片和人脸。这就是以前「点了扫描没反应」的原因。",

    # ---- 人脸与编码（§2.2：06 F1 F2 / L2c / F链 → 业务词）----
    "act.face_detect": "人脸检测",
    "act.face_detect.help": "在已抽好的画面里找出人脸并挑出最清晰的一张",
    "act.faces": "人脸检测与编码",
    "act.faces.help": "先找出人脸，再转成可比较的向量（两步连着跑，照片墙靠这一步）",
    "act.face_encode": "人脸向量编码",
    "act.face_encode.help": "把检出的人脸转成可比较的向量，并生成缩略图（这一步之后才会出现在照片墙）",
    "act.encode": "画面向量编码",
    "act.encode.help": "把画面转成可以按内容搜索的向量",
    "act.extract": "抽取画面",
    "act.extract.help": "从照片/视频里取出代表画面",
    "act.cluster": "自动分组",
    "act.cluster.help": "把长得像的人脸归到一组，方便按人浏览",
    "act.reconcile": "库完整性检查",
    "act.reconcile.help": "核对元数据与向量库是否一致，只出报告，不删任何东西",
    "act.backup": "备份数据库",
    "act.purge": "清理已删除文件的残留数据",

    # ---- 阶段名（进度条上只允许出现这些）----
    "stage.scan": "扫描文件清单",
    "stage.extract": "抽取画面",
    "stage.encode": "画面向量编码",
    "stage.face_detect": "人脸检测",
    "stage.face_encode": "人脸向量编码",
    "stage.cluster": "自动分组",
    "stage.migrate": "更新素材路径",
    "stage.reconcile": "库完整性检查",
    "stage.finalize": "收尾",

    # ---- 任务状态（§3.2 状态机）----
    "task.state.queued": "排队中",
    "task.state.running": "进行中",
    "task.state.success": "已完成",
    "task.state.failed": "失败",
    "task.state.cancelled": "已取消",
    "task.cancel": "停止任务",
    "task.cancel.confirm": "停止后可以再次启动，已完成的进度不会丢失。确定停止吗？",
    "task.cancelled": "任务已停止",
    "task.history": "任务历史",
    "task.history.empty": "还没有任务记录。点左边任意一个开始按钮试试。",
    "task.busy": "有任务正在运行，请等它结束或先停止它。",
    "task.col.time": "开始时间",
    "task.col.name": "任务",
    "task.col.state": "状态",
    "task.col.result": "处理结果",
    "task.col.took": "耗时",
    "task.remaining": "预计剩余",
    "task.rate": "速度",
    "task.no_eta": "估算中…",

    # ---- 进度条上用的人话 ----
    "progress.files": "已处理 {done:,} / {total:,} 个文件",
    "progress.count": "已处理 {done:,} 个文件",
    "progress.stage": "第 {ix}/{n} 步 · {name}",

    # ---- 照片墙 ----
    "wall.title": "照片墙",
    "wall.view.overview": "概览（同一人只出现一次）",
    "wall.view.tile": "平铺（全部人脸）",
    "wall.sort.captured_desc": "拍摄时间 新→旧",
    "wall.sort.captured_asc": "拍摄时间 旧→新",
    "wall.sort.quality_desc": "清晰度 高→低",
    "wall.sort.random": "随机",
    "wall.empty.no_index": "照片墙是空的。先点左边「扫描新文件」，然后接着跑「人脸检测」和「人脸向量编码」。",
    "wall.empty.no_faces": "这个范围里没有可显示的人脸，试试把「清晰度下限」调低，或清掉日期筛选。",
    "wall.count": "共 {total:,} 张人脸 · 本页 {n} 张 · 来自 {files:,} 个文件",
    "wall.reloaded": "内容已更新（{reason}）",

    # ---- 搜索 ----
    "search.title": "搜索",
    "search.placeholder.text": "说说你想找什么，例如：健身房里深蹲的人",
    "search.placeholder.face": "上传一张人脸照片",
    "search.mode.text": "按内容找",
    "search.mode.face": "按人脸找",
    "search.sensitivity": "识别灵敏度",
    "search.sensitivity.low": "低（宁可少找，也不找错）",
    "search.sensitivity.mid": "中（推荐）",
    "search.sensitivity.high": "高（宁可多找，也别漏掉）",
    "search.result.none": "没找到。可以换个说法、把「识别灵敏度」调高，或者确认相关文件已经处理完。",
    "search.result.n": "找到 {n:,} 个结果",

    # ---- 设置（§3.3 L1~L4）----
    "set.title": "设置",
    "set.common": "常用设置",
    "set.advanced": "高级设置",
    "set.show_advanced": "显示高级设置",
    "set.saved": "已保存：{n} 项。{restart}",
    "set.restart_needed": "其中 {n} 项需要重启程序后生效。",
    "set.restart_none": "全部即时生效。",
    "set.danger": "危险操作",
    "set.audit": "修改记录",
    "set.reset": "恢复默认",

    # ---- 维护 / 关于 ----
    "maint.title": "维护",
    "maint.deleted": "已删除（可恢复）",
    "maint.deleted.help": "这里只列文件，不动磁盘上的原文件。系统从不删除你的素材。",
    "maint.restore": "恢复选中项",
    "maint.error_badge": "待处理异常",
    "about.title": "关于",
    "about.version": "版本",
    "about.stats": "库统计",
    "about.disk": "磁盘占用",
    "about.last_scan": "上次扫描",
    "about.schema": "数据结构（自动探测）",
    "about.weights": "模型",
    "about.tech": "技术栈",

    # ---- 通用 ----
    "common.yes": "是",
    "common.no": "否",
    "common.ok": "知道了",
    "common.refresh": "刷新",
    "common.none": "—",
    "common.files": "{n:,} 个文件",
    "common.faces": "{n:,} 张人脸",
    "common.gb": "{n:.1f} GB",
    "error.locked": "有其他任务正在写入数据库，请等它结束再试。",
    "error.no_root": "素材文件夹不存在或没插好：{root}",
    "error.empty_root": "这个文件夹里没有找到可处理的照片或视频：{root}",
    "error.generic": "出错了：{msg}",
}

# --------------------------------------------------------------------------- #
# 禁用词（§2.2）。出现在界面源码里即判定 UI-1 不通过。
# 注意：只扫「界面文案」所在的位置；日志/注释里的技术名词不受限制，
# 所以测试只检查被 TEXTS/字面量渲染到页面的字符串。
# --------------------------------------------------------------------------- #
BANNED_IN_UI: List[str] = [
    "规格 8.6", "规格8.6", "（规格", "(规格",
    "L2a", "L2b", "L2c", "L2d",
    "F1", "F2", "F3", "F 系", "F链", "F 链",
    "tombstone", "reconcile", "upsert", "payload", "collection",
    "HDBSCAN", "UMAP", "IVF-PQ", "RRF", "min_score", "threshold",
    "dead_letter", "orphan", "ingest",
    "06_face_detect", "07_face_encode", "01_manifest", "03_extract", "04_encode",
]

# 允许出现技术名词的位置（「关于」页技术栈列表，规格 §2.2 明确允许一次）
TECH_STACK_ALLOWED = ("关于", "技术栈")


def t(key: str, **kwargs) -> str:
    """取一条文案。缺 key 时把 key 原样返回（便于立刻发现漏配）。"""
    raw = TEXTS.get(key)
    if raw is None:
        return key
    if kwargs:
        try:
            return raw.format(**kwargs)
        except (KeyError, IndexError):
            return raw
    return raw


def stage_name(code: str) -> str:
    """内部阶段码 → 业务阶段名。未知阶段按原样返回，绝不显示开发代号。"""
    return TEXTS.get(f"stage.{code}", TEXTS["stage.finalize"])


def task_state_name(state: str) -> str:
    return TEXTS.get(f"task.state.{state}", state)


def missing_keys() -> List[str]:
    """自检用：占位符数量与 kwargs 不匹配的键无法静态检查，这里只查空值。"""
    return [k for k, v in TEXTS.items() if not v]
