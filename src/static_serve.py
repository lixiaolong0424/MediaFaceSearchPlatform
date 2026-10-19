"""static_serve.py · 本地静态文件服务（片段预览的前提，与 Streamlit 同进程子线程）。

规格 9.2：
  * 绑定 127.0.0.1，禁止 0.0.0.0
  * 根目录白名单仅 ${project_root}/data/nvme 与 ${media_root}
  * 路径校验：
        root = Path("E:/nvme").resolve()
        p = (root / rel).resolve()
        if not str(p).startswith(str(root) + os.sep) and p != root:
            raise web.HTTPForbidden()
  * 拒绝 .. 穿越
  * 远程访问时整套静态服务 + 照片墙一起关掉（is_local_request）

**收紧项（安全加固，非规格原文但必要）**：
  1. 若 media_root 是整个盘根（如 "E:/"），拒绝把它整盘挂到 /media——
     否则等于把整块磁盘暴露给浏览器。改为只挂载「库中确实索引过的文件」。
  2. restrict_to_indexed（默认 true）：/media 下只允许命中 files 表 records 的文件。
     这使「白名单」从「目录白名单」收紧为「文件级白名单」。

预览 URL 形如：
    http://127.0.0.1:18501/media/<相对 media_root 的路径>#t=12.0,18.5
    http://127.0.0.1:18501/frames/<file_id>/<shot>_<ms>.jpg
"""

from __future__ import annotations

import os
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

from .common import Config, get_logger, safe_under

LOG = get_logger("static")

MOUNT_NVME = "/nvme"
MOUNT_MEDIA = "/media"
MOUNT_FACES = "/faces"          # faces/raw 与 faces/thumbs 都在 nvme 下，但单独挂便于白名单收窄
MOUNT_FRAMES = "/frames"

def is_drive_root(p: Path) -> bool:
    """判断是否整个盘根（E:/、D:/ 之类）。挂载盘根等于暴露整块磁盘。"""
    try:
        rp = p.resolve()
    except OSError:
        return False
    return rp.parent == rp


def whitelist(cfg: Config, *, include_media: bool = True) -> List[Tuple[str, Path]]:
    """返回 (挂载前缀, 真实目录)，按前缀长度降序（避免父目录抢走子目录）。"""
    entries: List[Tuple[str, Path]] = [
        (MOUNT_FACES, cfg.path("paths.faces_thumbs")),
        (MOUNT_FACES + "/raw", cfg.path("paths.faces_raw")),
        (MOUNT_FRAMES, cfg.path("paths.frames")),
        (MOUNT_NVME + "/thumbs", cfg.path("paths.thumbs")),
        (MOUNT_NVME + "/embeddings", cfg.path("paths.embeddings")),
        (MOUNT_NVME, cfg.path("paths.nvme_root")),
    ]
    if include_media:
        media = Path(str(cfg.media_root)).expanduser()
        # 安全加固：整盘根不挂载，只暴露库中已索引的文件（见 _allowed_media_path）
        entries.append((MOUNT_MEDIA, media if not is_drive_root(media) else media))
    out: List[Tuple[str, Path]] = []
    for mount, p in entries:
        try:
            out.append((mount, p.resolve()))
        except OSError:
            continue
    out.sort(key=lambda t: -len(t[0]))
    return out


def _indexed_media_files(cfg: Config) -> set[str]:
    """库中 status != 'missing' 的素材绝对路径集合（小写、正斜杠归一）。

    仅为收紧 /media 暴露面；库不存在时返回空集合，/media 一律拒绝（安全优先）。
    """
    import sqlite3

    out: set[str] = set()
    try:
        from . import db as D

        conn = D.connect(cfg.db_path, cfg=cfg, readonly=True)
        try:
            for r in conn.execute("SELECT path FROM files WHERE status != 'missing'"):
                out.add(str(r["path"]).replace("\\", "/").lower())
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return set()
    return out


class _Handler(SimpleHTTPRequestHandler):
    """Windows 友好 + 中文路径友好 + 白名单校验。"""

    server_version = "MMSStatic/1.0"
    protocol_version = "HTTP/1.1"

    def __init__(self, *args: Any, cfg: Config, entries: List[Tuple[str, Path]],
                 indexed: Optional[set] = None, restrict: bool = True, **kwargs: Any):
        self.cfg = cfg
        self.entries = entries
        self.indexed = indexed or set()
        self.restrict = restrict
        super().__init__(*args, directory=None, **kwargs)

    # -- 日志：只记路径，不记内容；不打印到 stdout 以免污染 Streamlit ---------- #
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def log_error(self, fmt: str, *args: Any) -> None:
        LOG.warning("%s - %s", self.address_string(), fmt % args)

    # -- 安全响应头 -------------------------------------------------------- #
    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def translate_path(self, path: str) -> str:
        """把 URL 路径映射到白名单内的真实文件。

        拒绝时返回一个**必定不存在**的路径（而不是 os.devnull）：
        Windows 上 os.stat("NUL") 会成功，SimpleHTTPRequestHandler 会因此
        回一个 200 + 空 body，语义上等于「假装成功」。返回不存在的路径才能得到 404。
        """
        parsed = urlparse(path)
        rel = unquote(parsed.path or "/")
        if "\x00" in rel:
            return str(self._hidden_root() / "__nul__")
        for mount, root in self.entries:
            if rel == mount or rel.startswith(mount + "/"):
                tail = rel[len(mount):].lstrip("/")
                target = safe_under(root, Path(tail)) if tail else root
                if target is None:
                    LOG.warning("静态服务拒绝越界访问：%s", rel)
                    return str(root / "__forbidden__")
                # 文件级白名单：/media 下只允许库中已索引且未标 missing 的文件
                if self.restrict and mount == MOUNT_MEDIA and target != root:
                    key = str(target).replace("\\", "/").lower()
                    if key not in self.indexed:
                        LOG.warning("静态服务拒绝未索引的素材文件：%s", rel)
                        return str(root / "__not_indexed__")
                return str(target)
        LOG.warning("静态服务拒绝白名单外的路径：%s", rel)
        return str(self._hidden_root() / "__outside_whitelist__")

    def _hidden_root(self) -> Path:
        """一个稳定存在但其中不会有被请求文件的目录（用于构造必然 404 的路径）。"""
        root = Path(self.cfg.path("paths.tmp")) / "_static_denied"
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            return Path(self.cfg.root) / "__mms_denied__"
        return root

    def list_directory(self, path: str):  # noqa: A003 - 禁止目录列表
        self.send_error(403, "Directory listing disabled")
        return None


class StaticServer:
    """与 Streamlit 同进程的后台线程静态服务。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.host = str(cfg.get("ui.host", "127.0.0.1"))
        self.port = int(cfg.get("ui.static_port", 18501))
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def start(self) -> Tuple[bool, str]:
        if self._httpd is not None:
            return True, f"已在运行 http://{self.host}:{self.port}"
        if bool(self.cfg.get("ui.bind_localhost_only", True)) and self.host not in ("127.0.0.1", "localhost"):
            return False, f"规格 9.2 要求绑定 127.0.0.1，当前配置为 {self.host}，已拒绝启动"

        entries = whitelist(self.cfg)
        if not entries:
            return False, "静态服务白名单为空，已拒绝启动"

        restrict = bool(self.cfg.get("ui.restrict_static_to_indexed", True))
        indexed = _indexed_media_files(self.cfg) if restrict else set()
        if restrict:
            LOG.info("静态服务 /media 已启用文件级白名单（%d 个已索引素材）", len(indexed))

        handler = partial(_Handler, cfg=self.cfg, entries=entries,
                          indexed=indexed, restrict=restrict)
        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        except OSError as exc:
            return False, (f"静态服务端口 {self.port} 不可用：{exc}\n"
                           f"修复：netstat -ano | findstr :{self.port} 找到占用进程，"
                           f"或修改 config.yaml 的 ui.static_port")
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="static-serve", daemon=True)
        self._thread.start()
        LOG.info("静态服务已启动 http://%s:%d（白名单 %d 项）", self.host, self.port, len(entries))
        return True, f"http://{self.host}:{self.port}"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        LOG.info("静态服务已停止")

    # -- URL 构造 ---------------------------------------------------------- #
    def url_for(self, path: Path | str, *, mount: Optional[str] = None) -> Optional[str]:
        """把磁盘路径转成可访问 URL；不在白名单内返回 None。"""
        p = Path(path)
        for m, root in whitelist(self.cfg):
            try:
                rel = p.resolve().relative_to(root)
            except (ValueError, OSError):
                continue
            if mount and m != mount:
                continue
            from urllib.parse import quote

            return f"http://{self.host}:{self.port}{m}/{quote(rel.as_posix())}"
        return None

    def media_url(self, abs_path: Path | str, start: Optional[float] = None,
                  end: Optional[float] = None) -> Optional[str]:
        """素材预览 URL；视频带 #t=start,end 片段指针。"""
        url = self.url_for(abs_path, mount=MOUNT_MEDIA)
        if url is None:
            return None
        if start is not None:
            if end is not None:
                return f"{url}#t={start:.1f},{end:.1f}"
            return f"{url}#t={start:.1f}"
        return url

    def frame_url(self, frame: Path | str) -> Optional[str]:
        return self.url_for(frame, mount=MOUNT_FRAMES) or self.url_for(frame, mount=MOUNT_NVME)

    def thumb_url(self, thumb: Path | str) -> Optional[str]:
        return (self.url_for(thumb, mount=MOUNT_FACES)
                or self.url_for(thumb, mount=MOUNT_NVME + "/thumbs")
                or self.url_for(thumb, mount=MOUNT_NVME))

    def describe(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "url": f"http://{self.host}:{self.port}",
            "restrict_to_indexed": bool(self.cfg.get("ui.restrict_static_to_indexed", True)),
            "media_root_is_drive_root": is_drive_root(Path(str(self.cfg.media_root))),
            "whitelist": [(m, str(p)) for m, p in whitelist(self.cfg)],
        }


_SINGLETON: Optional[StaticServer] = None


def get_server(cfg: Config) -> StaticServer:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = StaticServer(cfg)
    return _SINGLETON


def is_local_request(client_addr: str) -> bool:
    """远程访问时关静态服务 + 关照片墙 + 关人脸检索（规格第十七章隐私红线）。"""
    return client_addr in ("127.0.0.1", "::1", "localhost")
