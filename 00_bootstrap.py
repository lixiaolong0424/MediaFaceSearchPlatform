"""00_bootstrap.py · 启动自检（规格第十一章）。

任一项 FAIL → 打印修复命令并 exit(1)。
[警告] 项只提示不阻塞。

注意：新版 onnxruntime（>=1.23）已移除 ort.get_device()，一律用
ort.get_available_providers() 判断，绝不调用 get_device()。

额外职责：权重完整性自检（防止「首次启动卡在无声下载 5GB」）、
inode 稳定性探测、端口 bind 探测、Defender 排除项提示。
"""

from __future__ import annotations

import os
import socket
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# 允许以 `python 00_bootstrap.py` 直接运行
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.common import (  # noqa: E402
    Config, find_executable, get_logger, human_bytes,
    preload_ort_cuda, run_capture, session_id,
)
from src.config_loader import cfg_from_args  # noqa: E402

LOG = get_logger("bootstrap")

FAIL = "FAIL"
WARN = "WARN"
OK = "OK"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", fix: str = "") -> None:
        self.checks.append(Check(name, status, detail, fix))

    @property
    def failed(self) -> List[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warned(self) -> List[Check]:
        return [c for c in self.checks if c.status == WARN]

    def ok(self) -> bool:
        return not self.failed


# --------------------------------------------------------------------------- #
# 各项检查
# --------------------------------------------------------------------------- #


def check_python(rep: Report) -> None:
    v = sys.version_info
    if v < (3, 10):
        rep.add("Python >= 3.10", FAIL, f"当前 {v.major}.{v.minor}.{v.micro}",
                "安装 python.org 的 3.10+ 安装包（禁止 Microsoft Store 版："
                "其 KnownFolder 重定向会破坏相对路径）")
        return
    exe = sys.executable
    lowered = exe.lower().replace("\\", "/")
    if "windowsapps" in lowered or "microsoft" in lowered and "store" in lowered:
        rep.add("Python 非 Store 版", FAIL, exe,
                "Store 版 Python 的 KnownFolder 重定向会破坏相对路径，请改用 python.org 版")
        return
    rep.add("Python >= 3.10 且非 Store 版", OK, f"{v.major}.{v.minor}.{v.micro} @ {exe}")


def check_torch(rep: Report) -> None:
    try:
        import torch
    except ImportError:
        rep.add("torch + CUDA 可用", FAIL, "未安装 torch",
                ".venv\\Scripts\\python.exe -m pip install torch==2.8.0+cu126 "
                "--index-url https://mirror.sjtu.edu.cn/pytorch-wheels/cu126 "
                "--extra-index-url https://pypi.tuna.tsinghua.edu.cn/simple")
        return
    avail = bool(torch.cuda.is_available())
    detail = f"torch {torch.__version__} / cuda {torch.version.cuda} / available={avail}"
    if not avail:
        rep.add("torch + CUDA 可用", FAIL, detail,
                "确认驱动 >= 55x 且 torch 为 cu126 版："
                "pip install torch==2.8.0+cu126 --index-url "
                "https://mirror.sjtu.edu.cn/pytorch-wheels/cu126")
        return
    if torch.cuda.device_count() > 0:
        detail += f" / {torch.cuda.get_device_name(0)}"
    rep.add("torch + CUDA 可用", OK, detail)


def check_onnxruntime(rep: Report) -> None:
    try:
        import onnxruntime as ort
    except ImportError:
        rep.add("onnxruntime-gpu + CUDAExecutionProvider", FAIL, "未安装 onnxruntime",
                ".venv\\Scripts\\python.exe -m pip install onnxruntime-gpu")
        return
    # 规格明确：新版 onnxruntime（>=1.23）已移除 ort.get_device()，用 providers 判断
    providers = list(ort.get_available_providers())
    has_cuda = "CUDAExecutionProvider" in providers
    detail = f"onnxruntime {ort.__version__} / providers={providers}"

    # 额外体检：insightface 等包会把 CPU 版 onnxruntime 作为依赖静默装回来，
    # 两个包共存时 CPU 版抢占命名空间 → CUDA provider 消失。
    # 这是规格「故障归因速查」点名的坑，必须自动识别而不是等人肉排查。
    import importlib.metadata as md

    names = set()
    for dist in ("onnxruntime", "onnxruntime-gpu"):
        try:
            names.add(dist)
            md.version(dist)
        except md.PackageNotFoundError:
            names.discard(dist)
    if "onnxruntime" in names:
        rep.add("onnxruntime 未被 CPU 版抢占", FAIL,
                f"CPU 版与 GPU 版共存（{sorted(names)}），providers={providers}",
                "pip uninstall -y onnxruntime onnxruntime-gpu && pip install onnxruntime-gpu\n"
                "（insightface 会把 CPU 版 onnxruntime 装回来，装完必须复查这一项）")
        return
    rep.add("onnxruntime 未被 CPU 版抢占", OK, f"仅 {sorted(names) or ['onnxruntime-gpu']}")

    if not has_cuda:
        rep.add("onnxruntime-gpu + CUDAExecutionProvider", FAIL, detail,
                "pip uninstall onnxruntime onnxruntime-gpu -y && pip install onnxruntime-gpu\n"
                "若仍失败：确认 nvidia-smi 正常、驱动 >= 55x，"
                "或临时改用 onnxruntime-directml 保底（providers=['DmlExecutionProvider']）")
        return
    rep.add("onnxruntime-gpu + CUDAExecutionProvider", OK, detail)


def check_ffmpeg(rep: Report, cfg: Config) -> Tuple[Optional[Path], Optional[Path]]:
    ffmpeg = find_executable(cfg, "ffmpeg.bin", "ffmpeg")
    ffprobe = find_executable(cfg, "ffmpeg.probe", "ffprobe")
    if not ffmpeg or not ffprobe:
        rep.add("ffmpeg.exe / ffprobe.exe 可达", FAIL,
                f"ffmpeg={ffmpeg} ffprobe={ffprobe}",
                "从 https://www.gyan.dev/ffmpeg/builds/ 下 essentials 包，"
                f"把 bin\\ffmpeg.exe 与 bin\\ffprobe.exe 放到 {cfg.path('paths.bin')}，"
                "或确保 PATH 上可执行")
        return ffmpeg, ffprobe
    res = run_capture([str(ffprobe), "-version"], timeout=20)
    first = (res.stdout or res.stderr or "").splitlines()[0] if (res.stdout or res.stderr) else ""
    if not res.ok:
        rep.add("ffmpeg.exe / ffprobe.exe 可达", FAIL, f"ffprobe -version 返回 {res.returncode}",
                "重新下载静态构建，注意不要只放 ffmpeg.exe 而漏掉 ffprobe.exe")
        return ffmpeg, ffprobe
    configured = str(cfg.get("ffmpeg.bin"))
    on_path = str(ffmpeg) != configured
    note = f"{first}"
    if on_path:
        note += f"（config 指定 {configured} 不存在，已回退 PATH：{ffmpeg}）"
    rep.add("ffmpeg.exe / ffprobe.exe 可达", OK if not on_path else WARN, note,
            "" if not on_path else f"建议把 ffmpeg.exe/ffprobe.exe 复制到 {cfg.path('paths.bin')}，"
                                   "避免 PATH 变化后失效")
    return ffmpeg, ffprobe


def check_long_paths(rep: Report) -> None:
    if os.name != "nt":
        rep.add("长路径已启用", OK, "非 Windows，跳过")
        return
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\FileSystem") as k:
            val, _ = winreg.QueryValueEx(k, "LongPathsEnabled")
        if int(val) == 1:
            rep.add("长路径已启用（LongPathsEnabled=1）", OK, "1")
        else:
            rep.add("长路径已启用（LongPathsEnabled=1）", FAIL, f"LongPathsEnabled={val}",
                    "管理员 PowerShell：Set-ItemProperty -Path "
                    "'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\FileSystem' "
                    "-Name LongPathsEnabled -Value 1 -Type DWord")
    except OSError as exc:
        rep.add("长路径已启用（LongPathsEnabled=1）", FAIL, str(exc),
                "管理员 PowerShell 设置 LongPathsEnabled=1 后重启")


def check_media_root(rep: Report, cfg: Config) -> None:
    root = Path(str(cfg.media_root))
    if not root.exists():
        rep.add("media_root 存在且可读", FAIL, f"{root} 不存在",
                "在 UI 或 config.yaml 中指定真实存在的素材目录"
                "（规格默认 E:\\，本项目实测素材在 E:\\晚上）")
        return
    txt = str(root).replace("/", "\\")
    if txt.startswith("\\\\"):
        rep.add("media_root 非 UNC", FAIL, f"{root} 是 UNC 网络盘",
                "拒绝索引 UNC 路径（速度慢 100 倍 + explorer 选中失效 + stat 不稳定），"
                "请先 robocopy 到本地盘")
        return
    if "onedrive" in txt.lower():
        rep.add("media_root 非 OneDrive", FAIL, f"{root} 位于 OneDrive 下",
                "OneDrive 云占位符文件会「看似在本地、读时触发下载或直接 0 字节」，"
                "请换到非 OneDrive 的本地目录")
        return
    if not os.access(root, os.R_OK):
        rep.add("media_root 存在且可读", FAIL, f"{root} 不可读", "检查目录 ACL")
        return
    n = 0
    try:
        with os.scandir(root) as it:
            for _ in it:
                n += 1
                if n >= 3:
                    break
    except OSError as exc:
        rep.add("media_root 存在且可读", FAIL, f"{root} 无法列目录：{exc}", "检查权限")
        return
    rep.add("media_root 存在且可读", OK, f"{root}（可列出目录）")
    if os.path.abspath(txt) in (os.path.abspath("E:\\"), os.path.abspath("D:\\"),
                                os.path.abspath("C:\\")):
        rep.add("media_root 不是整个盘根", WARN,
                f"当前为 {root}（整个盘根目录）",
                "规格建议：在 UI 首次启动时提示指定具体子目录（如 E:\\晚上），"
                "可大幅提升扫描效率并减少误报")


def check_project_root(rep: Report, cfg: Config) -> None:
    root = cfg.root
    if not root.exists():
        rep.add("project_root 可写", FAIL, f"{root} 不存在", "创建该目录")
        return
    try:
        root.mkdir(parents=True, exist_ok=True)
        for sub in ("logs", "data", "tmp"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        probe = root / "tmp" / f".write_probe_{session_id()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        rep.add("project_root 可写可创建子目录", OK, str(root))
    except OSError as exc:
        rep.add("project_root 可写可创建子目录", FAIL, f"{root}：{exc}",
                "确认当前用户对该目录有 FullControl（icacls 检查）")


def check_nvme(rep: Report, cfg: Config) -> None:
    try:
        created = cfg.ensure_dirs()
    except OSError as exc:
        rep.add("nvme/thumbs/frames/db/tmp 可写可删", FAIL, str(exc),
                "确认 nvme_root 所在盘可写，且属主不是 root/其他用户")
        return
    nvme = cfg.path("paths.nvme_root")
    try:
        probe = nvme / f".probe_{session_id()}"
        probe.write_bytes(b"x")
        probe.unlink()
    except OSError as exc:
        rep.add("nvme/thumbs/frames/db/tmp 可写可删", FAIL, f"{nvme}：{exc}",
                "icacls <nvme_root> /grant %USERNAME%:(OI)(CI)F")
        return
    rep.add("nvme/thumbs/frames/db/tmp 可写可删", OK,
            f"{nvme}（新建 {len(created)} 个子目录）")


def check_space(rep: Report, cfg: Config) -> None:
    import shutil

    need = float(cfg.get("storage.free_space_min_gb", 30))
    for label, p in (("nvme_root", cfg.path("paths.nvme_root")), ("project_root", cfg.root)):
        try:
            free = shutil.disk_usage(str(p.anchor or p)).free / 1024 ** 3
        except OSError as exc:
            rep.add(f"剩余空间 >= {need:.0f}GB（{label}）", FAIL, str(exc), "检查盘符")
            continue
        total_gb = shutil.disk_usage(str(p.anchor or p)).total / 1024 ** 3
        if free < need:
            rep.add(f"剩余空间 >= {need:.0f}GB（{label}）", FAIL,
                    f"{free:.1f} GB 可用 / 共 {total_gb:.0f} GB",
                    "清理空间或把 nvme_root 移到更大的分区")
        else:
            rep.add(f"剩余空间 >= {need:.0f}GB（{label}）", OK,
                    f"{free:.1f} GB 可用 / 共 {total_gb:.0f} GB")


def check_sqlite_wal(rep: Report, cfg: Config) -> None:
    from src import db as D

    db_path = cfg.db_path
    try:
        conn = D.connect(db_path, cfg=cfg)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
    except sqlite3.Error as exc:
        rep.add("sqlite WAL 可用", FAIL, f"{db_path}：{exc}",
                "确认目录可写；不要在只读介质上建库")
        return
    if str(mode).lower() == "wal":
        rep.add("sqlite WAL 可用", OK, f"{db_path.name} journal_mode={mode}")
    else:
        rep.add("sqlite WAL 可用", FAIL, f"journal_mode={mode}",
                "WAL 需要可写目录（网络盘/FAT 上可能失败），换到 NTFS 本地盘")


def _our_app_is_serving(port: int, timeout: float = 2.0) -> bool:
    """该端口上是不是「我们自己的服务」在跑。

    第二次双击 start.bat 时端口当然被占，但那不是故障，而是「已经在运行」。
    用 Streamlit 健康检查端点区分这两种情况，避免把「重复启动」误报成自检失败。
    """
    import urllib.error
    import urllib.request

    for path in ("/_stcore/health", "/collections"):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return False


def check_ports(rep: Report, cfg: Config) -> None:
    """用 socket().bind 探测端口占用（Windows 无 ss/lsof）。

    占用分两种，必须区分，否则「重复双击」会被误报成启动失败：
      * 本系统自己的服务在响应 → WARN + 提示界面已在运行（不算失败）
      * 其它程序占用           → FAIL + 给出 netstat 排查命令
    """
    targets = [
        ("Streamlit UI", int(cfg.get("ui.port", 8501))),
        ("静态服务", int(cfg.get("ui.static_port", 18501))),
    ]
    for label, port in targets:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            s.close()
            rep.add(f"端口 {port} 可用（{label}）", OK, "bind 成功")
            continue
        except OSError as exc:
            s.close()

        if _our_app_is_serving(port):
            rep.add(f"端口 {port} 已在使用（{label}）", WARN,
                    "该端口上已有本系统的服务在响应（界面应该已经开着）",
                    f"无需处理：直接访问 http://127.0.0.1:{cfg.get('ui.port', 8501)}\n"
                    "如需重启，先关闭已有的 start.bat 窗口")
        else:
            rep.add(f"端口 {port} 可用（{label}）", FAIL,
                    f"bind 失败：{exc}",
                    f"netstat -ano | findstr :{port}  找到占用进程后 taskkill /PID <pid> /F，"
                    f"或修改 config.yaml")

    qurl = str(cfg.get("qdrant.url", ""))
    if "6333" in qurl or "6334" in qurl:
        rep.add("Qdrant 端口未被 Windows 保留", WARN, qurl,
                "Windows Hyper-V 保留段（本机实测 6313-6412 被保留）会拒绝 bind。"
                "改用 7333：config.yaml 的 qdrant.url 与 docker run 的 -p 127.0.0.1:7333:6333")


def check_qdrant(rep: Report, cfg: Config) -> None:
    url = str(cfg.get("qdrant.url"))
    try:
        import urllib.request

        with urllib.request.urlopen(url.rstrip("/") + "/collections", timeout=8) as r:
            body = r.read(200).decode("utf-8", "replace")
        rep.add("Qdrant 容器可连", OK, f"{url} → {body[:80]}")
    except Exception as exc:  # noqa: BLE001
        rep.add("Qdrant 容器可连", FAIL, f"{url} → {exc}",
                "docker start qdrant\n或：docker run -d --name qdrant "
                "-p 127.0.0.1:7333:6333 -p 127.0.0.1:7334:6334 "
                "-v qdrant_storage:/qdrant/storage -v qdrant_snapshots:/qdrant/snapshots "
                "-e QDRANT__TELEMETRY_DISABLED=true --restart unless-stopped qdrant/qdrant\n"
                "验证：curl http://127.0.0.1:7333/collections")


def verify_onnx_structure(
    path: Path,
    *,
    expect_out_dim: Optional[int] = None,
    expect_outputs_at_least: int = 0,
) -> Tuple[bool, str]:
    """只读 ONNX 图结构做完整性判定（比看体积可靠得多）。

    - expect_out_dim：要求「最后一个输出」的末维等于该值（如 ArcFace 512）
    - expect_outputs_at_least：要求输出个数不少于该值（如 SCRFD 的 9 个多尺度输出）
    """
    try:
        import onnx
    except ImportError:
        return True, "未安装 onnx，跳过结构校验"
    try:
        m = onnx.load(str(path), load_external_data=False)
    except Exception as exc:  # noqa: BLE001
        return False, f"ONNX 解析失败：{exc}"

    outs = m.graph.output
    n_out = len(outs)
    if expect_outputs_at_least and n_out < expect_outputs_at_least:
        return False, f"输出数 {n_out} < 期望的 {expect_outputs_at_least}（文件可能被裁剪）"

    if expect_out_dim is not None and outs:
        last = outs[-1]
        dims = [d.dim_value for d in last.type.tensor_type.shape.dim]
        if not dims or dims[-1] != expect_out_dim:
            return False, f"末输出 {last.name} 维度 {dims}，期望末维 = {expect_out_dim}"
        return True, f"{path.name}：{n_out} 个输出，末维 = {expect_out_dim} ✓"

    nodes = len(m.graph.node)
    params = 0
    for init in m.graph.initializer:
        n = 1
        for d in init.dims:
            n *= int(d)
        params += n
    return True, f"{path.name}：{n_out} 个输出 / {nodes} 节点 / {params / 1e6:.2f}M 参数"


def check_weights(rep: Report, cfg: Config) -> None:
    # --- Qwen3-VL-Embedding-2B --- #
    qwen = Path(str(cfg.get("semantic.model_path")))
    missing: List[str] = []
    if not qwen.exists():
        rep.add("权重离线可用（Qwen3-VL-Embedding-2B）", FAIL, f"{qwen} 不存在",
                f"把 Qwen3-VL-Embedding-2B 整个目录放到 {qwen}（禁止无声下载 ~4.3GB）")
    else:
        for name in ("config.json", "tokenizer.json"):
            if not (qwen / name).exists():
                missing.append(name)
        safet = sorted(qwen.glob("*.safetensors"))
        if not safet:
            missing.append("*.safetensors")
        total = 0
        for s in safet:
            sz = s.stat().st_size
            total += sz
            if sz < 1 << 20:
                missing.append(f"{s.name} 仅 {sz} 字节（疑似 LFS 指针，需重下）")
        if total and total < 3 * (1 << 30):
            missing.append(f"safetensors 合计仅 {human_bytes(total)}（应约 4.3GB）")
        if missing:
            rep.add("权重离线可用（Qwen3-VL-Embedding-2B）", FAIL,
                    f"{qwen}：{'；'.join(missing)}",
                    "重新下载完整权重；检查是否被 LFS 指针替代")
        else:
            rep.add("权重离线可用（Qwen3-VL-Embedding-2B）", OK,
                    f"{qwen}（safetensors {human_bytes(total)}）")

    # --- buffalo_l --- #
    # 注意：规格第十一章写的体积阈值（det_10g>100MB、2d106det>10MB）是**错误的**。
    # 官方 buffalo_l 实际构成（本机 2026-10 实测，并经 ONNX 图结构 + 真实推理核验）：
    #   det_10g.onnx   16.1MB  4.23M params  SCRFD，9 个输出（3 尺度 × score/bbox/kps）
    #   w600k_r50.onnx 166.3MB 43.59M params ArcFace，输出 [1,512]
    #   2d106det.onnx  4.8MB   1.25M params  输出 [1,212]（106*2）
    #   1k3d68.onnx    137.0MB 35.89M params 输出 [1,3309]
    #   genderage.onnx 1.26MB  0.33M params
    # 因此这里改成「体积下限只用于抓 LFS 指针/截断」，真正的完整性靠图结构与
    # 输出维度判定（见 verify_weights_present）。
    det = Path(str(cfg.get("faces.det_onnx")))
    rec = Path(str(cfg.get("faces.rec_onnx")))
    lmk = Path(str(cfg.get("faces.landmark_2d_onnx")))
    # (path, name, 最小体积应仅为「排除几 KB 的 LFS 指针」)
    req = [(det, "det_10g.onnx", 4 << 20),
           (rec, "w600k_r50.onnx", 100 << 20),
           (lmk, "2d106det.onnx", 1 << 20)]
    bad: List[str] = []
    for p, name, min_size in req:
        if not p.exists():
            bad.append(f"缺失 {name}")
            continue
        sz = p.stat().st_size
        if sz < min_size:
            bad.append(f"{name} 体积异常 {human_bytes(sz)}（疑似 LFS 指针/下载残缺，应 > {human_bytes(min_size)}）")
    extras = {"1k3d68.onnx": "3D68 附带", "genderage.onnx": "性别年龄，不使用但别删"}
    notes = [f"{n}({d})" for n, d in extras.items() if (det.parent / n).exists()]
    if bad:
        rep.add("权重离线可用（insightface buffalo_l）", FAIL,
                f"{det.parent}：{'；'.join(bad)}",
                "从 https://github.com/deepinsight/insightface/releases 下载 buffalo_l.zip "
                "（约 326MB）解压到该目录")
    else:
        rep.add("权重离线可用（insightface buffalo_l）", OK,
                f"{det.parent}（3 个必需 onnx 齐备，"
                f"det={human_bytes(det.stat().st_size)} rec={human_bytes(rec.stat().st_size)}"
                + (f"，附带 {', '.join(notes)}" if notes else "") + "）")

    # 结构级校验：只读 ONNX 图，不跑推理（快，且能抓到「文件在但内容不对」）
    if not bad and all(p.exists() for p, _n, _s in req):
        struct_ok, struct_msg = verify_onnx_structure(rec, expect_out_dim=512)
        rep.add("人脸识别模型输出维度 = 512", struct_ok, struct_msg,
                "" if struct_ok else "换过模型包会导致向量空间不兼容，整个库需重编码")
        det_ok, det_msg = verify_onnx_structure(det, expect_outputs_at_least=9)
        rep.add("人脸检测模型结构（SCRFD 多尺度输出）", det_ok, det_msg, "")

    # 实际推理探针：确认 provider 真的生效（抓静默回退）
    if not bad and all(p.exists() for p, _n, _s in req):
        try:
            # 必须先让 PyTorch 的 CUDA/cuDNN DLL 进入搜索路径，否则必然回退 CPU
            ok_pre, pre_msg = preload_ort_cuda()
            rep.add("onnxruntime 复用 torch 的 CUDA/cuDNN DLL", OK if ok_pre else WARN,
                    pre_msg,
                    "" if ok_pre else
                    "torch 与 onnxruntime-gpu 必须都是 CUDA 12.x 构建；"
                    "onnxruntime-gpu>=1.21 需要 CUDA 12.8，与本机 torch cu126 不兼容，"
                    "请装 1.20.2")

            import onnxruntime as ort

            providers = [p for p in ("CUDAExecutionProvider",)
                         if p in ort.get_available_providers()]
            so = ort.SessionOptions()
            so.log_severity_level = 3
            sess = ort.InferenceSession(str(rec), sess_options=so,
                                        providers=providers or ["CPUExecutionProvider"])
            actual = sess.get_providers()
            rep.add("人脸模型实际推理 provider",
                    OK if "CUDAExecutionProvider" in actual else WARN,
                    f"实际 providers={actual}",
                    "" if "CUDAExecutionProvider" in actual else
                    "会话回退到 CPU（静默回退）。inference 会慢 10~30 倍。"
                    "修复：pip install onnxruntime-gpu==1.20.2（CUDA 12.x 构建）")
        except Exception as exc:  # noqa: BLE001
            rep.add("人脸模型实际推理 provider", WARN, f"探针失败：{exc}",
                    "确认 onnx 文件完整；此检查非阻塞")


def check_inode(rep: Report, cfg: Config) -> None:
    from src.manifest import probe_inode_stability

    root = Path(str(cfg.media_root))
    if not root.exists():
        rep.add("库所在盘有稳定 inode", WARN, "media_root 不存在，跳过", "")
        return
    probe = probe_inode_stability(root, cfg)
    if probe.stable:
        rep.add("库所在盘有稳定 inode", OK, probe.reason)
    else:
        rep.add("库所在盘有稳定 inode", WARN, probe.reason,
                "降级唯一键为 (path_hash, size, mtime_rounded_1s)；"
                "UI 会常驻 banner 提示「该磁盘不支持 inode，文件移动会被视为新增」")


def check_defender(rep: Report, cfg: Config) -> None:
    """[警告] Defender 排除项未配置 → 打印 Add-MpPreference 命令。"""
    if os.name != "nt":
        return
    paths = [str(cfg.path("paths.nvme_root")), str(cfg.root / ".venv"),
             str(cfg.root / "tmp")]
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        p = cfg.path("paths.bin") / name
        if p.exists():
            paths.append(str(p))
    listed = "，".join(paths)
    try:
        res = run_capture(["powershell", "-NoProfile", "-Command",
                           "(Get-MpPreference).ExclusionPath -join ';'"], timeout=25)
        current = (res.stdout or "").strip()
    except Exception:  # noqa: BLE001
        current = ""
    missing = [p for p in paths if p and p.lower() not in current.lower()]
    if missing:
        rep.add("Defender 排除项", WARN, f"未排除 {len(missing)} 项",
                "管理员 PowerShell 执行：\n  Add-MpPreference -ExclusionPath "
                + ",".join(f"'{p}'" for p in paths) + f"\n建议排除：{listed}")
    else:
        rep.add("Defender 排除项", OK, "关键路径已在排除列表")


def check_acls(rep: Report, cfg: Config) -> None:
    """[警告] umask 在 Windows 无意义 → 改为检查目录 ACL 是否继承。"""
    if os.name != "nt":
        rep.add("目录 ACL 继承", OK, "非 Windows，跳过", "")
        return
    res = run_capture(["icacls", str(cfg.path("paths.nvme_root"))], timeout=25)
    out = res.stdout or ""
    inherits = "(OI)" in out and "(CI)" in out
    rep.add("目录 ACL 继承（替代 umask）", OK if inherits else WARN,
            "含 (OI)(CI) 继承标记" if inherits else "未见 (OI)(CI) 继承标记",
            "" if inherits else f"icacls \"{cfg.path('paths.nvme_root')}\" "
                                "/grant %USERNAME%:(OI)(CI)F /T")


def check_deps(rep: Report, cfg: Config) -> None:
    """P0 真正需要的 python 包（缺失会直接阻塞某个阶段）。"""
    required = {
        "yaml": "PyYAML",
        "numpy": "numpy",
        "PIL": "pillow",
        "cv2": "opencv-python-headless",
        "qdrant_client": "qdrant-client",
        "psutil": "psutil",
        "scenedetect": "scenedetect",
        "imagehash": "imagehash",
        "streamlit": "streamlit",
    }
    missing: List[str] = []
    for mod, pkg in required.items():
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001
            missing.append(pkg)
    if missing:
        rep.add("Python 依赖", FAIL, f"缺失 {', '.join(missing)}",
                ".venv\\Scripts\\python.exe -m pip install " + " ".join(missing))
    else:
        rep.add("Python 依赖", OK, f"{len(required)} 个模块可导入")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def run_checks(cfg: Config) -> Report:
    rep = Report()
    LOG.info("会话 %s 启动自检开始（project_root=%s, media_root=%s）",
             session_id(), cfg.root, cfg.media_root)
    check_python(rep)
    check_torch(rep)
    check_onnxruntime(rep)
    check_ffmpeg(rep, cfg)
    check_long_paths(rep)
    check_media_root(rep, cfg)
    check_project_root(rep, cfg)
    check_nvme(rep, cfg)
    check_space(rep, cfg)
    check_sqlite_wal(rep, cfg)
    check_ports(rep, cfg)
    check_qdrant(rep, cfg)
    check_deps(rep, cfg)
    check_weights(rep, cfg)
    check_inode(rep, cfg)
    check_defender(rep, cfg)
    check_acls(rep, cfg)
    return rep


def print_report(rep: Report) -> None:
    icons = {OK: "✅", WARN: "⚠️ ", FAIL: "❌"}
    width = max((len(c.name) for c in rep.checks), default=20)
    print()
    print("=" * 88)
    print(f"  启动自检报告  (session={session_id()})")
    print("=" * 88)
    for c in rep.checks:
        print(f"{icons.get(c.status, '?')} [{c.status:4}] {c.name:<{width}}  {c.detail}")
        if c.fix and c.status != OK:
            for line in c.fix.splitlines():
                print(f"           ↳ {line}")
    print("-" * 88)
    print(f"  通过 {sum(1 for c in rep.checks if c.status == OK)} / "
          f"警告 {len(rep.warned)} / 失败 {len(rep.failed)}")
    print("=" * 88)
    if rep.failed:
        print("\n❌ 存在必须修复的项，已中止。请按上面 ↳ 提示处理后重跑：")
        print("   .venv\\Scripts\\python.exe 00_bootstrap.py\n")
    else:
        print("\n✅ 全部必过项通过，可以开始扫描 / 建库。\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    import json

    p = argparse.ArgumentParser(description="启动自检（规格第十一章）")
    p.add_argument("--config", default=None)
    p.add_argument("--config-overlay", action="append", default=None, metavar="FILE")
    p.add_argument("--project-root", default=None)
    p.add_argument("--dry-run", action="store_true", help="兼容统一 CLI（自检本身不写库）")
    p.add_argument("--shard", default=None, help="兼容统一 CLI（自检不分片）")
    p.add_argument("--json", action="store_true", help="额外输出机器可读 JSON")
    p.add_argument("--no-digest", action="store_true", help="只打印报告，不影响退出码")
    args = p.parse_args(argv)

    cfg = cfg_from_args(args)
    rep = run_checks(cfg)
    print_report(rep)
    if args.json:
        print(json.dumps({"checks": [
            {"name": c.name, "status": c.status, "detail": c.detail, "fix": c.fix}
            for c in rep.checks]}, ensure_ascii=False, indent=2))
    return 1 if rep.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
