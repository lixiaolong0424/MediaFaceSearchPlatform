"""7.4 文字检索鲁棒性：query 侧轻量改写。

只做三件事（规格明确「不做重量级重写」）：
  1. 自动补前缀「视频中，」
  2. 繁简转换（内置常用字表，不引入 opencc 依赖）
  3. 常见同义替换表（config 可扩展的少量口语归一）

绝不改写用户的核心名词，避免把「会议室」改成「会场」这类语义漂移。
"""

from __future__ import annotations

import re
from typing import Dict, List, Tuple

from .common import Config

# 常用繁体 → 简体（覆盖素材检索里高频出现的字，不做全量转换）
T2S = str.maketrans({
    "這": "这", "個": "个", "們": "们", "來": "来", "去": "去", "說": "说", "時": "时",
    "間": "间", "見": "见", "現": "现", "對": "对", "開": "开", "關": "关", "車": "车",
    "門": "门", "問": "问", "題": "题", "東": "东", "西": "西", "風": "风", "雲": "云",
    "電": "电", "話": "话", "機": "机", "後": "后", "頭": "头", "臉": "脸", "髮": "发",
    "體": "体", "樂": "乐", "園": "园", "學": "学", "會": "会", "議": "议", "辦": "办",
    "公": "公", "場": "场", "館": "馆", "廳": "厅", "廚": "厨", "臥": "卧", "衛": "卫",
    "陽": "阳", "臺": "台", "灣": "湾", "國": "国", "華": "华", "語": "语", "講": "讲",
    "聽": "听", "讀": "读", "寫": "写", "畫": "画", "攝": "摄", "像": "像", "鏡": "镜",
    "頭": "头", "錄": "录", "影": "影", "視": "视", "頻": "频", "聲": "声", "響": "响",
    "顏": "颜", "紅": "红", "藍": "蓝", "綠": "绿", "黃": "黄", "黑": "黑", "白": "白",
    "燈": "灯", "光": "光", "線": "线", "網": "网", "絡": "络", "軟": "软", "體": "体",
    "應": "应", "該": "该", "個": "个", "幾": "几", "麼": "么", "樣": "样", "種": "种",
    "類": "类", "長": "长", "短": "短", "高": "高", "矮": "矮", "寬": "宽", "窄": "窄",
    "輕": "轻", "重": "重", "快": "快", "慢": "慢", "遠": "远", "近": "近",
    "覺": "觉", "讓": "让", "給": "给", "從": "从", "為": "为", "與": "与", "並": "并",
    "還": "还", "過": "过", "進": "进", "出": "出", "無": "无", "數": "数", "張": "张",
    "歲": "岁", "兒": "儿", "媽": "妈", "爸": "爸", "親": "亲", "愛": "爱", "歡": "欢",
    "鬧": "闹", "靜": "静", "舊": "旧", "新": "新", "壞": "坏", "對": "对", "錯": "错",
    "麼": "么", "買": "买", "賣": "卖", "錢": "钱", "價": "价", "貴": "贵", "幣": "币",
})

# 口语归一（保守）：只做明显同义的场景词
DEFAULT_SYNONYMS: List[Tuple[str, str]] = [
    ("娃", "小孩"),
    ("孩子", "小孩"),
    ("小朋友", "小孩"),
    ("小盆友", "小孩"),
    ("baby", "婴儿"),
    ("草坪", "草地"),
    ("绿地", "草地"),
    ("奔跑", "跑"),
    ("走动", "走路"),
    ("吃饭", "就餐"),
    ("聚餐", "就餐"),
    ("讲话", "说话"),
    ("合影", "合照"),
    ("视频会议", "会议"),
    ("访谈", "采访"),
    ("车里", "车内"),
    ("路上", "街道"),
    ("房间里", "室内"),
]


def to_simplified(text: str) -> str:
    return text.translate(T2S)


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def apply_synonyms(text: str, synonyms: List[Tuple[str, str]]) -> str:
    out = text
    for src, dst in synonyms:
        if src in out and dst not in out:
            out = out.replace(src, dst)
    return out


def rewrite_query(
    text: str,
    cfg: Config | None = None,
    *,
    synonyms: List[Tuple[str, str]] | None = None,
) -> str:
    """返回改写后的 query。空输入原样返回。"""
    raw = normalize_whitespace(text or "")
    if not raw:
        return raw

    out = to_simplified(raw)
    out = apply_synonyms(out, synonyms if synonyms is not None else DEFAULT_SYNONYMS)

    prefix = ""
    if cfg is not None:
        prefix = str(cfg.get("retrieval.query_prefix", "") or "")
        extra = cfg.get("retrieval.synonyms")
        if extra:
            out = apply_synonyms(out, [(str(a), str(b)) for a, b in extra])

    if prefix and not out.startswith(prefix):
        # 前缀只在纯文字 query 上生效，且不掩盖原始词
        out = prefix + out
    return normalize_whitespace(out)


def rewrite_report(text: str, cfg: Config | None = None) -> Dict[str, str]:
    """给 UI 展示「我实际发出去的 query 是什么」，避免黑箱。"""
    return {"input": text, "effective": rewrite_query(text, cfg)}
