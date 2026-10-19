"""Streamlit 首次运行准备 + 端口占用守卫。

start.bat 在拉起 Streamlit 之前调用本脚本，解决两个会让人以为「双击启动不了」的问题：

1. **首次运行会停在 Email 提示上**
   Streamlit 在 %USERPROFILE%\\.streamlit\\credentials.toml 不存在时会交互式索要邮箱。
   双击启动时如果没人回答，界面就起不来（或被误认为卡死）。
   本脚本幂等地写入 {"general":{"email":""}}，跳过该提示。

2. **重复双击会因端口冲突失败**
   静态服务绑的是固定端口（config.yaml 的 ui.static_port），第二个实例必然 bind 失败。
   与其报一堆错误，不如检测到 8501 已被监听时直接提示「已在运行」并打开浏览器。

退出码：
  0  可以继续启动 Streamlit
  3  检测到已经在运行（start.bat 应直接打开浏览器后退出）
  1  准备失败（打印原因）
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def ensure_streamlit_credentials() -> tuple[bool, str]:
    """幂等创建 ~/.streamlit/credentials.toml，避免首次运行的交互式邮箱提示。"""
    cred = Path.home() / ".streamlit" / "credentials.toml"
    if cred.exists():
        return True, f"已存在：{cred}"
    try:
        cred.parent.mkdir(parents=True, exist_ok=True)
        # TOML 无 null，未提供邮箱就写空串（Streamlit 官方也是这么存）
        cred.write_text('[general]\nemail = ""\n', encoding="utf-8")
        return True, f"已创建（跳过首次运行邮箱提示）：{cred}"
    except OSError as exc:
        return False, f"无法写入 {cred}：{exc}"


def port_listening(host: str, port: int) -> bool:
    """判断端口是否已被监听（Windows 无 ss/lsof，用 connect 探测）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.5)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


def main() -> int:
    ok, msg = ensure_streamlit_credentials()
    print(f"[准备] Streamlit 凭据：{msg}")
    if not ok:
        return 1

    # 读取配置里的端口（失败则用默认值）
    host, ui_port, static_port = "127.0.0.1", 8501, 18501
    try:
        from src.common import load_config

        cfg = load_config()
        host = str(cfg.get("ui.host", host))
        ui_port = int(cfg.get("ui.port", ui_port))
        static_port = int(cfg.get("ui.static_port", static_port))
    except Exception as exc:  # noqa: BLE001 - 配置读不到不该阻塞启动
        print(f"[准备] 读取 config.yaml 失败，使用默认端口：{exc}")

    if port_listening(host, ui_port):
        print(f"[准备] 检测到 {host}:{ui_port} 已在监听 —— 界面似乎已经在运行。")
        print(f"       直接打开：http://{host}:{ui_port}")
        return 3

    if port_listening(host, static_port):
        print(f"[准备] 警告：静态服务端口 {host}:{static_port} 已被占用。")
        print(f"       UI 能起来，但 ▶ 预览可能失效。")
        print(f"       如确认没有其它实例，请修改 config.yaml 的 ui.static_port。")

    print(f"[准备] 端口就绪：UI={ui_port} 静态={static_port}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
