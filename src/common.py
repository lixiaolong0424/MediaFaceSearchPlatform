"""素材人脸检索系统 · 公共基础设施。

规格纪律（第〇章）：
  * 所有路径用 pathlib.Path，禁止字符串拼接；全程绝对路径。
  * 日志双写 stdout 与 ${project_root}/logs/<date>.log，每条带 session_id。
  * 参数一律从 config.yaml 读取，禁止硬编码。
  * 每个模块支持 --dry-run / --shard N/M / --root <path>，单文件失败只写 error 字段。

本模块不依赖 torch / onnxruntime / qdrant，任何阶段都可安全 import。
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

SHOT_IDX_BITS = 20
SHOT_IDX_MASK = (1 << SHOT_IDX_BITS) - 1
MAX_FILE_ID = (1 << (64 - SHOT_IDX_BITS)) - 1  # 2**44-1

PROJECT_ROOT_ENV = "MMS_PROJECT_ROOT"
CONFIG_ENV = "MMS_CONFIG"
SESSION_ENV = "MMS_SESSION_ID"

CONFIG_FILENAME = "config.yaml"
# 界面「设置」写回的文件（v5.3 §3.3：用户能改的东西必须有界面，不必编辑 config.yaml）。
# 它只在**默认加载**（没有 --config / MMS_CONFIG / --config-overlay）时叠加，
# 因此冒烟测试、多环境配置完全不受影响。想临时关掉：设 MMS_NO_LOCAL_OVERLAY=1。
CONFIG_LOCAL_FILENAME = "config.local.yaml"
CONFIG_LOCAL_DISABLE_ENV = "MMS_NO_LOCAL_OVERLAY"

_LOGGER_READY: set[str] = set()
_LOGGER_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# PIL 解压炸弹上限（2026-10-16 修的真实数据丢失 bug）
# --------------------------------------------------------------------------- #
#
# 现象：全集跑到 E:\晚上 时，照片开始成批失败（实测 1,078 张、占该批 22%）：
#     图片解码失败：Image size (201326592 pixels) exceeds limit of 178956970 pixels,
#                   could be decompression bomb DOS attack.
#
# 原因：PIL 的 `MAX_IMAGE_PIXELS` 默认 89,478,485（≈89MP）。超过它发 **Warning**，
# 超过 **2 倍**（178,956,970）直接抛 `DecompressionBombError` —— 而 03_extract.py
# 的 `except` 会把它当「图片解码失败」，于是照片被标成 error、永远进不了库。
#
# 这个上限是防「恶意文件撑爆内存」的，对**本地自有素材**没有意义：
# 失败的都是 2~9 MB 的拼接长图/套图封面（实测最大 402,456,576 像素），
# 全解码 RGB 约 1.2 GB，本机 66 GB 内存扛得住，而且照片是**串行**处理的。
#
# 所以放宽到 10 亿像素（仍有上限，避免真的遇到异常文件时把内存吃光）。
# 这是「宁可放过、不可错杀」：错杀的代价是用户素材静默缺失，而放宽的代价只是内存峰值。
#
# 放在 src/common.py 里做，是因为所有阶段的模块都会 import 它 ——
# 避免在 extract / dedup / face / encode 各处漏掉一处解码入口。
PIL_MAX_IMAGE_PIXELS = 1_000_000_000


def relax_pil_pixel_limit(limit: int = PIL_MAX_IMAGE_PIXELS) -> int:
    """放宽 PIL 的解压炸弹上限（返回放宽后的值，未安装 PIL 时返回 0）。"""
    try:
        from PIL import Image

        current = Image.MAX_IMAGE_PIXELS
        if current is None or current < limit:
            Image.MAX_IMAGE_PIXELS = limit
        return int(Image.MAX_IMAGE_PIXELS or 0)
    except Exception:  # noqa: BLE001 - 没装 PIL 不影响其它阶段
        return 0


relax_pil_pixel_limit()


def register_heif_opener() -> bool:
    """让 PIL 能打开 HEIC/HEIF（iPhone 原片）。

    为什么放在 common.py：所有阶段模块都会 import 它，一处注册全局生效，
    避免在 extract / dedup / face / encode 各写一遍还漏掉。
    没装 pillow-heif 时静默跳过 —— 其他格式不受影响。
    """
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
        return True
    except Exception:  # noqa: BLE001 - 没装就别崩，HEIC 只是不可用
        return False


HEIF_READY = register_heif_opener()

# --------------------------------------------------------------------------- #
# 性能档位（方案 B）
# --------------------------------------------------------------------------- #
TURBO_DEFAULTS: Dict[str, Any] = {
    "photo_prefetch": 6,
    "video_workers": 12,
    "frame_workers": 8,
    "encode_batch": 32,
    "encode_group": 12,
}


def perf_value(cfg: Any, key: str) -> int:
    """读性能参数：**具体值优先，其次按 mode 给默认值**。

    为什么需要它：`performance.mode` 原本只是个标签，代码从不读它 ——
    把 mode 设成 turbo 而那几个数字仍是 0 时，行为完全不变
    （用户反馈"切了全速没感觉"的直接原因之一）。
    """
    try:
        explicit = int(cfg.get(f"performance.{key}", 0) or 0)
    except Exception:  # noqa: BLE001
        explicit = 0
    if explicit:
        return explicit
    try:
        turbo = str(cfg.get("performance.mode", "balanced") or "balanced") == "turbo"
    except Exception:  # noqa: BLE001
        turbo = False
    return int(TURBO_DEFAULTS.get(key, 0)) if turbo else 0


DEFAULT_CONFIG: Dict[str, Any] = {
    "project_root": str(Path(__file__).resolve().parent.parent),
    "media_root": "E:/",
    "db": {"path": "${project_root}/data/library.db"},
}

# --------------------------------------------------------------------------- #
# 会话 ID（每条日志都带，便于把一次扫描/检索的日志串起来）
# --------------------------------------------------------------------------- #


def session_id() -> str:
    """返回当前进程序列号；子进程通过环境变量继承同一个 session_id。"""
    sid = os.environ.get(SESSION_ENV)
    if not sid:
        sid = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        os.environ[SESSION_ENV] = sid
    return sid


def new_session_id() -> str:
    """显式开一个新会话（长驻 UI 进程在每次用户操作时可调用）。"""
    sid = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    os.environ[SESSION_ENV] = sid
    return sid


# --------------------------------------------------------------------------- #
# 路径与配置
# --------------------------------------------------------------------------- #


def project_root(explicit: Optional[str | Path] = None) -> Path:
    """定位项目根目录（优先级：显式参数 > 环境变量 > 模块所在位置的两级上）。"""
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get(PROJECT_ROOT_ENV)
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def _expand(value: Any, root: Path) -> Any:
    """递归展开 ${project_root} 占位符；不做 os.path.expandvars（避免误伤 %VAR%）。"""
    if isinstance(value, str):
        return value.replace("${project_root}", str(root)).replace("\\", "/")
    if isinstance(value, dict):
        return {k: _expand(v, root) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v, root) for v in value]
    return value


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    """config.yaml 的类型化只读视图。禁止在业务代码里出现魔法数字。"""

    def __init__(self, data: Dict[str, Any], root: Path, source: Optional[Path] = None):
        self._data = data
        self.root = root
        self.source = source

    # -- 通用访问 ---------------------------------------------------------- #
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def require(self, dotted: str) -> Any:
        sentinel = object()
        val = self.get(dotted, sentinel)
        if val is sentinel:
            raise KeyError(f"config.yaml 缺少必填项：{dotted}")
        return val

    def override(self, dotted: str, value: Any) -> None:
        """运行期覆盖单个配置项（供 CLI 的参数覆盖使用）。只改内存，不落盘。

        为什么需要：阈值集中在 config 是硬要求，但调阈值时要能不编辑文件就试
        （例如 `05_rerank.py --min-score 0.35` 做 A/B）。中间层按 dotted 路径
        自动建字典，免得调用方到处写 setdefault。
        """
        parts = dotted.split(".")
        node = self._data
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value

    def as_dict(self) -> Dict[str, Any]:
        return json.loads(json.dumps(self._data, ensure_ascii=False))

    # -- 高频字段的便捷访问 ------------------------------------------------ #
    @property
    def media_root(self) -> Path:
        return Path(self.get("media_root", "E:/"))

    def path(self, dotted: str) -> Path:
        """取一个目录/文件路径（已展开 ${project_root}）。"""
        val = self.require(dotted)
        return Path(str(val))

    @property
    def db_path(self) -> Path:
        return self.path("db.path")

    @property
    def nvme_root(self) -> Path:
        return self.path("paths.nvme_root")

    @property
    def log_dir(self) -> Path:
        return self.path("paths.logs")

    @property
    def tmp_dir(self) -> Path:
        return self.path("paths.tmp")

    @property
    def export_dir(self) -> Path:
        return self.path("paths.export")

    def ensure_dirs(self) -> List[Path]:
        """创建所有必须存在的目录，返回实际创建的列表。"""
        created: List[Path] = []
        for dotted in (
            "paths.data", "paths.nvme_root", "paths.frames", "paths.thumbs",
            "paths.faces_raw", "paths.faces_thumbs", "paths.embeddings",
            "paths.query_cache", "paths.export", "paths.logs", "paths.tmp",
        ):
            p = self.path(dotted)
            if not p.exists():
                p.mkdir(parents=True, exist_ok=True)
                created.append(p)
        return created

    @property
    def ext_whitelist(self) -> set[str]:
        return {str(e).lower().lstrip(".") for e in self.get("library.ext_whitelist", [])}

    @property
    def photo_ext(self) -> set[str]:
        return {str(e).lower().lstrip(".") for e in self.get("library.photo_ext", [])}

    @property
    def video_ext(self) -> set[str]:
        return {str(e).lower().lstrip(".") for e in self.get("library.video_ext", [])}

    @property
    def dir_blacklist(self) -> set[str]:
        return {str(d).lower() for d in self.get("library.dir_blacklist", [])}


def _read_yaml(path: Path) -> Dict[str, Any]:
    import yaml

    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_config(
    path: Optional[str | Path] = None,
    root: Optional[str | Path] = None,
    overrides: Optional[Dict[str, Any]] = None,
    overlay: Optional[Sequence[str | Path]] = None,
) -> Config:
    """加载 config.yaml。

    path     ：基座配置（默认 <project_root>/config.yaml）。**替换**语义。
    overlay  ：叠加配置（按顺序深合并到基座之上）。用于只覆盖少数键的场景，
               例如 tests/config_smoke.yaml 只改路径、其余阈值全部继承。
    overrides：内存中的最高优先级覆盖。

    config.yaml 缺失时返回内置默认（并告警），保证自检脚本仍能跑起来报错。
    """
    root_path = project_root(root)
    cfg_path: Optional[Path] = None
    if path:
        cfg_path = Path(path)
    elif os.environ.get(CONFIG_ENV):
        cfg_path = Path(os.environ[CONFIG_ENV])
    else:
        cfg_path = root_path / CONFIG_FILENAME

    if cfg_path.exists():
        data = _read_yaml(cfg_path)
        source: Optional[Path] = cfg_path
    else:
        data = json.loads(json.dumps(DEFAULT_CONFIG))
        source = None

    for extra in (overlay or []):
        ep = Path(extra)
        if not ep.exists():
            raise FileNotFoundError(f"--config-overlay 指定的文件不存在：{ep}")
        data = _deep_merge(data, _read_yaml(ep))

    # 界面「设置」写回的覆盖层（v5.3 §3.3）。
    # 只在「纯默认加载」时生效：只要调用方显式指定了基座或叠加文件，就完全忽略它 ——
    # 否则 tests/config_smoke.yaml 会被生产的 config.local.yaml 污染，冒烟测试会写到真实库上。
    if (path is None and not os.environ.get(CONFIG_ENV) and not overlay
            and not os.environ.get(CONFIG_LOCAL_DISABLE_ENV)):
        local = root_path / CONFIG_LOCAL_FILENAME
        if local.exists():
            try:
                data = _deep_merge(data, _read_yaml(local))
            except Exception as exc:  # noqa: BLE001 - 覆盖层坏了也不该让界面打不开
                print(f"[config] 读取 {local.name} 失败，已忽略：{exc}")

    # config.yaml 里的 project_root 是权威值（用户 2026-10 拍板）
    declared_root = data.get("project_root")
    if declared_root and not root:
        root_path = Path(str(declared_root).replace("\\", "/")).expanduser().resolve()

    data = _expand(data, root_path)
    data["project_root"] = str(root_path).replace("\\", "/")
    if overrides:
        data = _deep_merge(data, overrides)
    return Config(data, root_path, source)


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #


class _SessionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        record.session_id = session_id()
        return True


def _quote_paths(text: str) -> str:
    """隐私红线：日志中的 Windows 绝对路径做 quote 显示（避免日志泄漏目录结构）。"""
    return re.sub(r"([A-Za-z]:[\\/][^\s\"']*)", lambda m: f'"{m.group(1)}"', text)


class _PathQuoteFormatter(logging.Formatter):
    def __init__(self, *args: Any, quote: bool = True, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._quote = quote

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        return _quote_paths(msg) if self._quote else msg


def get_logger(
    name: str,
    cfg: Optional[Config] = None,
    level: Optional[str] = None,
    console: bool = True,
) -> logging.Logger:
    """日志双写 stdout 与 logs/<date>.log；幂等，重复调用不会重复挂 handler。"""
    logger = logging.getLogger(name)
    with _LOGGER_LOCK:
        if name in _LOGGER_READY:
            return logger
        lvl = (level or (cfg.get("logging.level", "INFO") if cfg else "INFO")).upper()
        logger.setLevel(getattr(logging, lvl, logging.INFO))
        logger.propagate = False
        fmt = "%(asctime)s | %(levelname)-7s | %(session_id)s | %(name)s | %(message)s"
        datefmt = cfg.get("logging.date_format", "%Y-%m-%d %H:%M:%S") if cfg else "%Y-%m-%d %H:%M:%S"
        quote = bool(cfg.get("logging.log_paths_quoted", True)) if cfg else True

        if console:
            sh = logging.StreamHandler(stream=sys.stdout)
            sh.setFormatter(_PathQuoteFormatter(fmt, datefmt=datefmt, quote=quote))
            sh.addFilter(_SessionFilter())
            logger.addHandler(sh)

        if cfg is not None and cfg.get("logging.double_write", True):
            try:
                log_dir = cfg.log_dir
                log_dir.mkdir(parents=True, exist_ok=True)
                logfile = log_dir / f"{datetime.now().strftime('%Y-%m-%d')}.log"
                fh = logging.FileHandler(logfile, encoding="utf-8")
                fh.setFormatter(_PathQuoteFormatter(fmt, datefmt=datefmt, quote=quote))
                fh.addFilter(_SessionFilter())
                logger.addHandler(fh)
            except OSError as exc:  # 日志目录不可写不应中断业务
                logger.warning("无法写入日志文件（仅 stdout）：%s", exc)

        _LOGGER_READY.add(name)
    return logger


# --------------------------------------------------------------------------- #
# 命令行（每个模块都必须支持 --dry-run / --shard N/M / --root <path>）
# --------------------------------------------------------------------------- #


def shard_range(shard: Optional[str], total_items: int) -> Optional[range]:
    """解析 "N/M"（1-based shard，M 份），返回该分片负责的索引 range。

    None 表示不分片。非整除时前余数个分片多承担一个元素。
    """
    if not shard:
        return None
    m = re.fullmatch(r"\s*(\d+)\s*/\s*(\d+)\s*", shard)
    if not m:
        raise ValueError(f"--shard 需形如 N/M，收到 {shard!r}")
    n, total = int(m.group(1)), int(m.group(2))
    if total < 1 or not (1 <= n <= total):
        raise ValueError(f"--shard {shard!r} 越界：N 必须满足 1 <= N <= M")
    base, rem = divmod(total_items, total)
    start = (n - 1) * base + min(n - 1, rem)
    size = base + (1 if n <= rem else 0)
    return range(start, start + size)


def iter_shard(items: Sequence[Any], shard: Optional[str]) -> Iterator[Any]:
    rng = shard_range(shard, len(items))
    if rng is None:
        yield from items
    else:
        for i in rng:
            yield items[i]


def base_argparser(description: str, *, needs_root: bool = True) -> argparse.ArgumentParser:
    """规格第〇章要求的统一 CLI 骨架。"""
    p = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="所有阶段模块都必须支持 --dry-run / --shard N/M / --root <path>（规格第〇章第 2 条）",
    )
    p.add_argument("--config", default=None, help="config.yaml 路径（默认 <project_root>/config.yaml）")
    p.add_argument("--config-overlay", action="append", default=None, metavar="FILE",
                   help="叠加配置（深合并到基座之上，只覆盖少量键时用它；可重复）")
    p.add_argument("--project-root", default=None, help="覆盖项目根目录")
    if needs_root:
        p.add_argument("--root", default=None, help="素材根目录（默认取 config.yaml 的 media_root）")
    p.add_argument("--dry-run", action="store_true", help="只报告将要做什么，不写库/不写盘")
    p.add_argument("--shard", default=None, metavar="N/M", help="分片处理，例如 1/4")
    p.add_argument("--limit", type=int, default=None, help="只处理前 N 条（冒烟测试用）")
    p.add_argument("--log-level", default=None, help="覆盖日志级别（DEBUG/INFO/WARNING）")
    p.add_argument("--json", action="store_true", help="把结果摘要以 JSON 打到 stdout 末尾")
    # v5.3 §3.2：所有耗时阶段都要能在界面上看到进度，脚本侧统一用这个参数绑定任务。
    p.add_argument("--task-id", default=None, metavar="ID",
                   help="绑定到任务队列里的一条任务（界面启动时会自动带上；命令行可留空）")
    return p


def resolve_media_root(args: argparse.Namespace, cfg: Config) -> Path:
    """--root > config.media_root（默认 E:/）。"""
    raw = getattr(args, "root", None)
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(str(cfg.media_root)).expanduser().resolve()


def emit_summary(summary: Dict[str, Any], as_json: bool, logger: logging.Logger, title: str = "SUMMARY") -> None:
    """统一的收尾输出：人类可读 + 可选机器可读 JSON。"""
    logger.info("=== %s ===", title)
    for k, v in summary.items():
        logger.info("  %-22s %s", k, v)
    if as_json:
        print(json.dumps({"summary": summary}, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def utc_now_iso() -> str:
    """导出物首行注释用的 ISO8601 UTC。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def path_hash(rel_path: str | Path) -> str:
    """path_hash = md5(相对路径)，统一小写 + 正斜杠，保证跨次扫描稳定。"""
    norm = str(rel_path).replace("\\", "/").lower()
    return hashlib.md5(norm.encode("utf-8")).hexdigest()


def make_point_id(file_id: int, shot_ix: int) -> int:
    """命名空间 point_id = (file_id << 20) | shot_ix（规格架构章：两个管线的耦合点）。"""
    if file_id < 0 or file_id > MAX_FILE_ID:
        raise ValueError(f"file_id 越界：{file_id}")
    if shot_ix < 0 or shot_ix > SHOT_IDX_MASK:
        raise ValueError(f"shot_ix 越界（必须 < 2^20）：{shot_ix}")
    return (file_id << SHOT_IDX_BITS) | shot_ix


def split_point_id(point_id: int) -> Tuple[int, int]:
    return point_id >> SHOT_IDX_BITS, point_id & SHOT_IDX_MASK


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0 or unit == "TB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} TB"


def dir_size_bytes(path: Path) -> int:
    """统计目录占用字节数。

    ⚠️ 2026-10-18：原实现用 `Path.rglob("*")`，在 data/nvme 上实测 **133.9 秒**
    （那里是「一张照片一个目录」的布局，几十万个目录 + 三十多万个文件，
    Windows 上 rglob 要逐层开目录、每个 Path 再各 stat 一次），
    直接把 `12_maintain.py status` 拖成两分多钟。
    改用 `os.scandir` 迭代：DirEntry 自带 stat 缓存，并且避免为每个条目构造 Path。
    """
    total = 0
    root = Path(path)
    if not root.exists():
        return 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat().st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def safe_under(root: Path, candidate: Path) -> Optional[Path]:
    """把 candidate 解析到 root 之下；越界（含 .. 穿越）返回 None。"""
    try:
        root_r = root.resolve()
        cand = (root_r / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    except (OSError, ValueError):
        return None
    if cand == root_r:
        return cand
    try:
        cand.relative_to(root_r)
    except ValueError:
        return None
    return cand


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def tail_file(path: Path, n: int = 50) -> List[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.readlines()[-n:]
    except OSError:
        return []


# --------------------------------------------------------------------------- #
# 子进程（Windows：绝不拼 shell 字符串；中文/特殊字符路径必须走 list 形式）
# --------------------------------------------------------------------------- #

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


@dataclass
class RunResult:
    argv: List[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    duration_sec: float = 0.0
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run_capture(
    argv: Sequence[str],
    *,
    timeout: Optional[float] = None,
    cwd: Optional[Path] = None,
    no_window: bool = True,
    errors: str = "replace",
) -> RunResult:
    """执行外部命令并捕获输出。路径含中文/&/()/空格一律安全（list 形式，无 shell）。"""
    argv = [str(a) for a in argv]
    t0 = time.monotonic()
    creationflags = CREATE_NO_WINDOW if (no_window and os.name == "nt") else 0
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd) if cwd else None,
            creationflags=creationflags,
            timeout=timeout,
        )
        return RunResult(
            argv=argv,
            returncode=proc.returncode,
            stdout=proc.stdout.decode("utf-8", errors=errors),
            stderr=proc.stderr.decode("utf-8", errors=errors),
            duration_sec=time.monotonic() - t0,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode("utf-8", errors=errors) if exc.stdout else ""
        err = exc.stderr.decode("utf-8", errors=errors) if exc.stderr else ""
        return RunResult(argv=argv, returncode=-9, stdout=out, stderr=err,
                         duration_sec=time.monotonic() - t0, timed_out=True)
    except FileNotFoundError as exc:
        return RunResult(argv=argv, returncode=-2, stderr=str(exc),
                         duration_sec=time.monotonic() - t0)
    except OSError as exc:
        return RunResult(argv=argv, returncode=-3, stderr=str(exc),
                         duration_sec=time.monotonic() - t0)


def find_executable(cfg: Config, dotted: str, name: str) -> Optional[Path]:
    """先找 config 指定的路径，再按 fallback_to_path 找 PATH。"""
    configured = Path(str(cfg.get(dotted, "")))
    if configured and configured.exists():
        return configured
    if cfg.get("ffmpeg.fallback_to_path", True):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


# --------------------------------------------------------------------------- #
# onnxruntime CUDA DLL 预加载
# --------------------------------------------------------------------------- #

_ORT_PRELOADED = False


def preload_ort_cuda() -> Tuple[bool, str]:
    """让 onnxruntime 复用 PyTorch 自带的 CUDA / cuDNN DLL。

    为什么必须做：Windows 上 onnxruntime-gpu 的 pip wheel **不捆绑** CUDA/cuDNN DLL，
    会话创建时若找不到 cublasLt64_*.dll / cudnn*_9.dll 会静默回退到 CPU
    （规格「故障归因速查」点名的坑）。torch 的 wheel 则自带这些 DLL
    （本机 torch 2.8.0+cu126 → cublasLt64_12.dll / cudnn_*64_9.dll）。

    顺序很重要：DLL 目录必须在创建 InferenceSession **之前**进入搜索路径。
      * onnxruntime >= 1.21 提供 ort.preload_dlls()
      * 老版本用「先 import torch」达到同样效果（官方文档推荐的 Example 1）
    """
    global _ORT_PRELOADED
    if _ORT_PRELOADED:
        return True, "已预加载"
    try:
        import onnxruntime as ort
    except ImportError:
        return False, "onnxruntime 未安装"

    preload = getattr(ort, "preload_dlls", None)
    if callable(preload):
        try:
            preload()
            _ORT_PRELOADED = True
            return True, "ort.preload_dlls() 成功"
        except Exception as exc:  # noqa: BLE001
            msg = f"ort.preload_dlls() 失败：{exc}"
    else:
        msg = "该版本 onnxruntime 无 preload_dlls()，改用 import torch"

    try:
        import torch  # noqa: F401 - 仅为把 torch/lib 加入 DLL 搜索路径
        _ORT_PRELOADED = True
        return True, f"{msg} → 已 import torch 预加载"
    except ImportError:
        return False, f"{msg}，且 torch 不可用"


def ort_status() -> Dict[str, Any]:
    """汇报 onnxruntime 的真实可用性（供自检与 UI banner 使用）。"""
    try:
        import onnxruntime as ort
    except ImportError as exc:
        return {"installed": False, "error": str(exc)}
    providers = list(ort.get_available_providers())
    return {
        "installed": True,
        "version": getattr(ort, "__version__", "?"),
        "providers": providers,
        "cuda_available": "CUDAExecutionProvider" in providers,
        "preload": preload_ort_cuda()[1],
    }


# --------------------------------------------------------------------------- #
# 结构化 TXT（供下游 agent 剪辑归档 / 用户快捷操作）
# --------------------------------------------------------------------------- #


def write_structured_txt(
    path: Path,
    *,
    title: str,
    scope: str,
    rows: Iterable[Dict[str, Any]],
    columns: Sequence[str],
    notes: Sequence[str] = (),
) -> Dict[str, Any]:
    """人类可读 + 机器可解析的结构化 TXT（tab 分隔，带 UTF-8 BOM）。"""
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="\n") as fh:
        fh.write(f"# {title}\n")
        fh.write(f"# scope={scope}\n")
        fh.write(f"# exported_at={utc_now_iso()}\n")
        fh.write(f"# session_id={session_id()}\n")
        fh.write(f"# columns={chr(9).join(columns)}\n")
        for n in notes:
            fh.write(f"# note={n}\n")
        fh.write("#" + "-" * 78 + "\n")
        fh.write("\t".join(columns) + "\n")
        for r in rows:
            fh.write("\t".join("" if r.get(c) is None else str(r.get(c)) for c in columns) + "\n")
    return {"written": len(rows), "path": str(path)}


# --------------------------------------------------------------------------- #
# 主函数包装：统一异常出口（保证任何模块崩溃都给 session_id 与可读信息）
# --------------------------------------------------------------------------- #


def cli_main(fn):
    """装饰器：把 main(args, cfg, logger) 包成规范的 CLI 入口。"""

    @functools.wraps(fn)
    def wrapper(argv: Optional[Sequence[str]] = None) -> int:
        raise SystemExit(fn(argv))  # noqa: B904  (SystemExit 由调用方决定)

    return wrapper


@contextlib.contextmanager
def timer(label: str, logger: Optional[logging.Logger] = None):
    t0 = time.monotonic()
    box: Dict[str, float] = {}
    yield box
    box["elapsed"] = time.monotonic() - t0
    if logger:
        logger.info("%s 耗时 %.2fs", label, box["elapsed"])
