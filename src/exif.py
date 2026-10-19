"""时间来源解析（规格 8.2 / 已拍板决策 ⑧）。

优先级：EXIF DateTimeOriginal（含 GPS 时区） → QuickTime Creation Time → 文件名日期 → mtime
落 files.captured_at + files.time_source，绝不在渲染时实时读。

time_source 取值：exif_datetime_original | quicktime_creation_time | filename_date | mtime | unknown
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

EXIF_ORIGINAL = "exif_datetime_original"
QUICKTIME = "quicktime_creation_time"
FILENAME = "filename_date"
MTIME = "mtime"
UNKNOWN = "unknown"

ISO_FMT = "%Y-%m-%dT%H:%M:%S"

# EXIF 里常见但无意义的占位值
_EXIF_SENTINELS = {"0000:00:00 00:00:00", "0000-00-00 00:00:00", "    :  :     :  :  "}

# 文件名日期：20230101_123456 / 2023-01-01 12-34-56 / 2023.01.01 等
_FN_PATTERNS = [
    re.compile(r"(?P<Y>19\d{2}|20\d{2})[-_.]?(?P<m>0[1-9]|1[0-2])[-_.]?(?P<d>0[1-9]|[12]\d|3[01])"
               r"[_\-T ]?(?P<H>[01]\d|2[0-3])[-_.:]?(?P<M>[0-5]\d)[-_.:]?(?P<S>[0-5]\d)"),
    re.compile(r"(?P<Y>19\d{2}|20\d{2})[-_.]?(?P<m>0[1-9]|1[0-2])[-_.]?(?P<d>0[1-9]|[12]\d|3[01])"
               r"[_\-T ]?(?P<H>[01]\d|2[0-3])[-_.:]?(?P<M>[0-5]\d)"),
    re.compile(r"(?P<Y>19\d{2}|20\d{2})[-_.]?(?P<m>0[1-9]|1[0-2])[-_.]?(?P<d>0[1-9]|[12]\d|3[01])"),
    # 微信/手机常见的毫秒时间戳
    re.compile(r"(?P<ms>1[5-9]\d{11}|20\d{11})"),
]


@dataclass
class CaptureTime:
    captured_at: Optional[str]      # ISO 本地时间字符串（无时区信息，展示用）
    time_source: str
    raw: Optional[str] = None
    tz_offset_min: Optional[int] = None

    @property
    def ok(self) -> bool:
        return bool(self.captured_at)


def _finish(dt: datetime, source: str, raw: Optional[str] = None,
            tz_offset_min: Optional[int] = None) -> CaptureTime:
    return CaptureTime(dt.strftime(ISO_FMT), source, raw, tz_offset_min)


def from_filename(path: Path) -> Optional[CaptureTime]:
    """文件名日期。识别不到 2000~2099 之外的年份一律放弃。"""
    stem = path.stem
    for pat in _FN_PATTERNS:
        m = pat.search(stem)
        if not m:
            continue
        gd = m.groupdict()
        try:
            if "ms" in gd and gd.get("ms"):
                dt = datetime.fromtimestamp(int(gd["ms"]) / 1000.0)
                if 1990 <= dt.year <= 2100:
                    return _finish(dt, FILENAME, m.group(0))
                continue
            y, mo, d = int(gd["Y"]), int(gd["m"]), int(gd["d"])
            hh = int(gd.get("H") or 0)
            mm = int(gd.get("M") or 0)
            ss = int(gd.get("S") or 0)
            dt = datetime(y, mo, d, hh, mm, ss)
            return _finish(dt, FILENAME, m.group(0))
        except (TypeError, ValueError):
            continue
    return None


def from_mtime(path: Path) -> CaptureTime:
    try:
        st = path.stat()
    except OSError:
        return CaptureTime(None, UNKNOWN)
    return _finish(datetime.fromtimestamp(st.st_mtime), MTIME)


def _parse_exif_datetime(value: str) -> Optional[datetime]:
    value = (value or "").strip().strip("\x00")
    if not value or value in _EXIF_SENTINELS:
        return None
    value = value.replace("-", ":", 2) if re.match(r"^\d{4}-\d{2}-\d{2}", value) else value
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y:%m:%d %H:%M", "%Y:%m:%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def from_exif_image(im: Any) -> Optional[CaptureTime]:
    """从**已经打开**的 PIL Image 里读 EXIF（不重新打开文件）。

    融合模式专用：E: 是机械盘，一张照片 6.76MB，重开一次文件就是多一次随机寻道。
    03_extract.py 为了算 pHash 本来就要把图解码到内存，EXIF 顺手取即可。
    """
    try:
        exif = im.getexif()
        if not exif:
            return None
        raw = None
        tag_used = None
        for tag, name in ((36867, "DateTimeOriginal"), (36868, "DateTimeDigitized"), (306, "DateTime")):
            v = exif.get(tag)
            if v:
                raw, tag_used = str(v), name
                break
        if raw is None:
            return None
        dt = _parse_exif_datetime(raw)
        if dt is None:
            return None
        return _finish(dt, EXIF_ORIGINAL, f"{tag_used}={raw}", _read_offset_minutes(exif))
    except Exception:  # noqa: BLE001
        return None


def from_exif(path: Path) -> Optional[CaptureTime]:
    """EXIF DateTimeOriginal（优先带 GPS 时区的 OffsetTimeOriginal）。仅图片。"""
    try:
        from PIL import Image  # noqa: F401
    except ImportError:  # pragma: no cover
        return None

    try:
        with Image.open(path) as im:
            return from_exif_image(im)
    except Exception:  # noqa: BLE001 - 任何解码异常都退化为「无 EXIF」
        return None


def _read_offset_minutes(exif: Any) -> Optional[int]:
    """读取 OffsetTimeOriginal (+08:00) 或 GPS 时间戳的时区。"""
    for tag in (36881, 36880, 36882):  # OffsetTimeOriginal / OffsetTime / OffsetTimeDigitized
        try:
            v = exif.get(tag)
        except Exception:  # noqa: BLE001
            v = None
        if v:
            m = re.match(r"^\s*([+-])(\d{2}):?(\d{2})", str(v))
            if m:
                sign = 1 if m.group(1) == "+" else -1
                return sign * (int(m.group(2)) * 60 + int(m.group(3)))
    return None


def from_quicktime(ffprobe_tags: Dict[str, Any]) -> Optional[CaptureTime]:
    """QuickTime / MP4 容器 Creation Time。"""
    for key in ("creation_time", "com.apple.quicktime.creationdate", "date", "DateTimeOriginal"):
        v = ffprobe_tags.get(key)
        if not v:
            continue
        s = str(v).strip()
        # 常见：2023-01-01T12:34:56.000000Z
        try:
            s2 = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s2)
            if dt.tzinfo is not None:
                off = int(dt.utcoffset().total_seconds() // 60)
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
                return _finish(dt, QUICKTIME, f"{key}={s}", off)
            return _finish(dt, QUICKTIME, f"{key}={s}")
        except ValueError:
            pass
        dt = _parse_exif_datetime(s)
        if dt:
            return _finish(dt, QUICKTIME, f"{key}={s}")
    return None


def resolve_capture_time(
    path: Path,
    *,
    kind: str,
    priority: Optional[list[str]] = None,
    ffprobe_tags: Optional[Dict[str, Any]] = None,
    image: Any = None,
) -> CaptureTime:
    """按优先级依次尝试，返回第一个可用的时间来源。

    ffprobe_tags：视频侧由 03_extract.py 用 ffprobe 提前取好（§「不可在渲染时实时读」）。
    image：已经打开的 PIL Image。传了它就不再重新打开文件读 EXIF（融合模式用）。
    """
    order = priority or [EXIF_ORIGINAL, QUICKTIME, FILENAME, MTIME]
    for source in order:
        got: Optional[CaptureTime] = None
        if source == EXIF_ORIGINAL and kind == "photo":
            got = from_exif_image(image) if image is not None else from_exif(path)
        elif source == QUICKTIME and ffprobe_tags:
            got = from_quicktime(ffprobe_tags)
        elif source == FILENAME:
            got = from_filename(path)
        elif source == MTIME:
            got = from_mtime(path)
        if got and got.ok:
            return got

    # 视频：即使 ffprobe 没给标签，也再试一次文件名与 mtime（上面的顺序已覆盖）
    fallback = from_mtime(path)
    return CaptureTime(fallback.captured_at, fallback.time_source or UNKNOWN)
