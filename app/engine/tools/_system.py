"""系统操作工具：open_website / open_app / system_info / take_screenshot / clipboard / notification。"""
from __future__ import annotations

import json
import os
import subprocess
import webbrowser
from pathlib import Path

from ._core import Tool, ToolRegistry


def register(reg: ToolRegistry, *, hooks=None) -> None:
    """注册系统级工具（剪贴板、通知、打开网址、打开应用、截图、系统信息）。"""
    hooks = hooks or {}

    def open_website(url: str) -> str:
        url = url.strip()
        # 严格校验：只允许 http/https
        if not url.startswith(("http://", "https://")):
            return f"错误：只允许 http:// 或 https:// 开头的网址，收到：{url}"
        webbrowser.open(url)
        return f"已在浏览器打开 {url}"

    def open_app(app_name: str) -> str:
        """启动一个 Windows 应用程序。

        查找顺序：
            1. 完整路径 → 直接 os.startfile
            2. 注册表映射（内置 + 用户自定义 JSON）
            3. Program Files 目录搜索
            4. cmd /c start 兜底
        """
        from app.core.app_registry import get_registry
        name = app_name.strip()
        p = Path(name)
        if p.is_file():
            os.startfile(str(p))  # noqa: S606
            return f"已启动 {p.name}"
        registry = get_registry()
        found = registry.resolve(name)
        if found:
            os.startfile(found)  # noqa: S606
            return f"已启动 {name}（{found}）"
        try:
            subprocess.Popen(["cmd.exe", "/c", "start", "", name])
            return f"已尝试启动 {name}（若没弹出窗口可能未安装，可在 data/app_registry.json 中添加路径映射）"
        except Exception as e:  # noqa: BLE001
            return f"错误：无法启动「{name}」：{e}"

    def system_info() -> str:
        """获取系统信息：CPU、内存、磁盘、电量、OS。"""
        import platform
        info = {
            "os": platform.system() + " " + platform.release(),
            "python": platform.python_version(),
            "cpu": platform.processor() or platform.machine(),
            "platform": platform.platform(),
        }
        try:
            import psutil
            info["cpu_percent"] = f"{psutil.cpu_percent(interval=0.1):.1f}%"
            mem = psutil.virtual_memory()
            info["ram_total"] = f"{mem.total / 1024**3:.1f} GB"
            info["ram_used"] = f"{mem.used / 1024**3:.1f} GB"
            info["ram_percent"] = f"{mem.percent:.1f}%"
            disk = psutil.disk_usage("C:\\")
            info["disk_total"] = f"{disk.total / 1024**3:.1f} GB"
            info["disk_free"] = f"{disk.free / 1024**3:.1f} GB"
            info["disk_percent"] = f"{disk.percent:.1f}%"
            try:
                bat = psutil.sensors_battery()
                if bat:
                    info["battery"] = f"{bat.percent:.0f}%{'(充电中)' if bat.power_plug else '(未充电)'}"
            except Exception:  # noqa: BLE001
                pass
        except ImportError:
            info["cpu_percent"] = "未安装 psutil"
            info["ram"] = "未安装 psutil"
            info["disk"] = "未安装 psutil"
            info["battery"] = "未安装 psutil"
        return json.dumps(info, ensure_ascii=False)

    def take_screenshot_tool() -> str:
        """截取当前屏幕并返回 base64 编码的图片。

        注意：模型需要支持 vision 能力才能分析图片。
        """
        from app.engine.screenshot import screenshot_to_base64
        b64 = screenshot_to_base64()
        if b64 is None:
            return "错误：截图失败，请确保已安装 pyautogui 或 Pillow"
        return f"截图成功（base64，{len(b64)} 字符）"

    def clipboard_copy(text: str) -> str:
        """把文本复制到系统剪贴板。"""
        try:
            from app.core.qt_compat import QApplication
            app = QApplication.instance()
            if app is None:
                return "错误：无 GUI 应用实例"
            app.clipboard().setText(text)
            return f"已复制到剪贴板（{len(text)} 字符）"
        except Exception as e:  # noqa: BLE001
            return f"错误：{e}"

    def send_notification(title: str, message: str) -> str:
        """发送 Windows 系统通知。"""
        try:
            import win10toast
            toast = win10toast.ToastNotifier()
            toast.show_toast(title, message, duration=5, threaded=True)
            return "通知已发送"
        except ImportError:
            return "未安装 win10toast，无法发送系统通知（pip install win10toast）"
        except Exception as e:  # noqa: BLE001
            return f"错误：{e}"

    reg.register(Tool(
        name="open_website",
        description="用默认浏览器打开一个网址。",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        fn=open_website,
    ))
    reg.register(Tool(
        name="open_app",
        description="启动一个 Windows 应用程序。支持中文名/英文名/完整路径。路径映射可通过 data/app_registry.json 自定义。",
        parameters={
            "type": "object",
            "properties": {"app_name": {"type": "string"}},
            "required": ["app_name"],
        },
        fn=open_app,
    ))
    reg.register(Tool(
        name="system_info",
        description="获取系统信息：OS版本、CPU使用率、内存、磁盘、电池电量。",
        parameters={"type": "object", "properties": {}},
        fn=system_info,
    ))
    reg.register(Tool(
        name="take_screenshot",
        description="截取当前屏幕截图（返回 base64 编码的 PNG）。用于让桌宠看到用户屏幕内容。",
        parameters={"type": "object", "properties": {}},
        fn=take_screenshot_tool,
    ))
    reg.register(Tool(
        name="clipboard_copy",
        description="把文本复制到系统剪贴板，主人可以 Ctrl+V 粘贴。",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要复制的文本"}},
            "required": ["text"],
        },
        fn=clipboard_copy,
    ))
    reg.register(Tool(
        name="send_notification",
        description="发送 Windows 系统通知（右下角弹窗）。需要安装 win10toast 库。",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "通知标题"},
                "message": {"type": "string", "description": "通知内容"},
            },
            "required": ["title", "message"],
        },
        fn=send_notification,
    ))


__all__ = ["register"]