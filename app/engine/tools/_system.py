"""系统操作工具：open_website / open_app / system_info / take_screenshot / clipboard / notification。"""
from __future__ import annotations

import json
import os
import subprocess
import webbrowser
from pathlib import Path

from ._core import Tool, ToolRegistry


def _collect_suggestions(query: str, limit: int = 5) -> list[tuple[str, str, float]]:
    """收集「内置映射相似」+「已装应用相似」的 top-N 候选。

    Returns: [(display_name, exe_path, score), ...]，按 score 降序，去重。
    """
    out: dict[str, tuple[str, str, float]] = {}    # path(lowercase) -> (display, path, score)

    def _add(display: str, path: str, score: float) -> None:
        key = path.lower()
        existing = out.get(key)
        if existing is None or score > existing[2]:
            out[key] = (display, path, score)

    # 1) 内置映射相似候选
    try:
        from app.core.app_registry import get_registry
        reg = get_registry()
        # 内置 key 相似候选（这是「名字对名字」，已装应用是「名字对显示名」）
        for display, score in reg.find_similar_in_registry(query, limit=limit * 2):
            # 把 display 反向归一化找到对应路径
            full = reg.resolve(display)
            if full:
                _add(display, full, score)
    except Exception:  # noqa: BLE001
        pass

    # 2) 已装应用扫描相似候选（wait=True 等扫描完成，最多 3 秒）
    #    阈值 0.55：低于这个会返回太多噪声（如 "totallyfake" → Tailscale 50%）
    try:
        from app.core.installed_apps import find_similar_apps
        for app, score in find_similar_apps(
            query, limit=limit * 2, min_ratio=0.55, wait=True, timeout=3.0,
        ):
            _add(app.name, app.path, score)
    except Exception:  # noqa: BLE001
        pass

    # 排序 + 截断
    items = sorted(out.values(), key=lambda x: -x[2])
    return items[:limit]


def register(reg: ToolRegistry, *, hooks=None) -> None:
    """注册系统级工具（剪贴板、通知、打开网址、打开应用、截图、系统信息）。"""
    hooks = hooks or {}

    def open_website(url: str) -> str:
        """打开网址。自动补全协议头；多种容错。"""
        url = (url or "").strip()
        if not url:
            return "错误：网址为空"
        # 自动补全协议头：用户给「www.baidu.com」「baidu.com」也能开
        # 已带协议的：原样（http / https / file / ftp 都接受，但只有 http/https
        # 实际能成功打开——其余的也原样传给 webbrowser，让它决定）
        if not url.startswith(("http://", "https://", "file://", "ftp://")):
            # 包含「.」且没有空格时按域名补 https；否则当作搜索词走 DuckDuckGo
            if " " not in url and "." in url:
                url = "https://" + url
            else:
                from urllib.parse import quote
                url = "https://duckduckgo.com/?q=" + quote(url)
        # 非 http/https 警告（webbrowser 通常只能打开这两个）
        warning = ""
        if url.startswith(("ftp://", "file://")):
            warning = "（注意：浏览器可能打不开此协议）"
        try:
            ok = webbrowser.open(url)
            if not ok:
                return f"错误：浏览器打开失败（可能没有默认浏览器），URL={url}"
        except Exception as e:  # noqa: BLE001
            return f"错误：打开网址失败：{e}（URL={url}）"
        return f"已在浏览器打开 {url}{warning}"

    def open_app(app_name: str) -> str:
        """启动一个 Windows 应用程序。

        查找顺序：
            1. 空名直接报错（避免弹 cmd 窗口）
            2. 完整路径（绝对 / 相对）→ is_file 后 os.startfile
            3. 路径看起来像路径但文件不存在 → 直接报错（不要走兜底闪 cmd）
            4. 内置注册表映射（内置 + 用户自定义 JSON）→ resolve → os.startfile
               内置找不到时降级到「系统已装应用扫描」（按相似度排序）
            5. **没找到** → 列出 top 5 相似候选 + 操作建议（让用户挑）

        返回值：
            - 成功："已启动 <名称>（<路径>）"
            - 失败："错误：..." 或 "未找到应用 ... + 候选列表"
        """
        from app.core.app_registry import get_registry

        name = (app_name or "").strip()
        if not name:
            return "错误：应用名为空。用法：open_app(app_name='notepad')"

        # 1) 完整路径（绝对 / 相对）
        p = Path(name)
        if p.is_file():
            try:
                os.startfile(str(p))  # noqa: S606
                return f"已启动 {p.name}（{p}）"
            except Exception as e:  # noqa: BLE001
                return f"错误：启动文件失败 {p}：{e}"

        # 2) 看起来是路径（带盘符/分隔符/扩展名）但文件不存在 → 直接报错
        looks_like_path = (
            len(name) >= 2 and name[1] == ":"                       # 盘符
            or "\\" in name or "/" in name                         # 路径分隔符
            or name.lower().endswith((".exe", ".bat", ".cmd", ".lnk", ".msc"))
        )
        if looks_like_path:
            return (
                f"错误：路径不存在或不是文件：{name}\n"
                f"提示：用 Path('{name}').exists() 检查路径"
            )

        # 3) 内置注册表（+ 已装应用扫描兜底）
        registry = get_registry()
        found = registry.resolve(name)
        if found:
            try:
                os.startfile(found)  # noqa: S606
                return f"已启动 {name}（{found}）"
            except Exception as e:  # noqa: BLE001
                return f"错误：启动失败 {found}：{e}"

        # 4) 没找到：返回 top 5 相似候选（内置映射 + 已装应用）
        suggestions = _collect_suggestions(name, limit=5)
        if suggestions:
            lines = [
                f"未找到应用「{name}」。你可能想打开以下应用之一：",
                "",
            ]
            for i, (display, path, score) in enumerate(suggestions, 1):
                lines.append(
                    f"  {i}. {display}  ({score:.0%} 相似)  [路径: {path}]"
                )
            lines.append("")
            lines.append(
                "要打开上面任一应用，请重新调用 open_app 用准确的名称（如："
                f"open_app(app_name='{suggestions[0][0]}')）。"
            )
            return "\n".join(lines)

        # 5) 完全找不到：兜底「Shell PATH 搜索」（仅 ASCII 短串）
        looks_like_exe = name.replace(" ", "").isascii()
        if looks_like_exe:
            try:
                subprocess.Popen(
                    [name],
                    shell=True,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return (
                    f"已尝试启动 {name}（未在已装应用中找到，"
                    f"已让 Windows Shell 在 PATH 中搜索。"
                    f"若未弹出窗口，可能未安装。）"
                )
            except Exception as e:  # noqa: BLE001
                return f"错误：启动 {name} 失败：{e}"

        # 6) 中文 / 多字短语没有任何候选：清晰报错
        return (
            f"未找到应用「{name}」。\n"
            f"提示：\n"
            f"  1. 用英文名（如 notepad / mspaint / explorer）\n"
            f"  2. 给完整路径（如 C:\\\\Program Files\\\\...\\\\app.exe）\n"
            f"  3. 在 data/app_registry.json 里添加自定义映射"
        )

    def list_installed_apps_tool(query: str = "", limit: int = 20) -> str:
        """列出系统中已安装应用（按 query 过滤；空 query 返回前 N 个）。

        给 LLM 用来在用户说「打开 XX」时先发现真实应用名。
        """
        try:
            from app.core.installed_apps import list_installed_apps, find_similar_apps
        except ImportError as e:  # noqa: BLE001
            return f"错误：installed_apps 模块导入失败：{e}"
        if query and query.strip():
            # 阻塞等扫描完成（最多 5 秒）—— 首次调用扫描可能还没完
            candidates = find_similar_apps(
                query.strip(), limit=limit, min_ratio=0.3,
                wait=True, timeout=5.0,
            )
            if not candidates:
                return f"未找到与「{query}」相似的已装应用（系统可能确实没装）。"
            lines = [f"与「{query}」相似的已装应用（按相似度降序）：", ""]
            for app, score in candidates:
                lines.append(f"  {app.name}  ({score:.0%})  [{app.path}]")
            return "\n".join(lines)
        apps = list_installed_apps(wait=True, timeout=5.0)
        if not apps:
            return "已装应用列表为空（扫描还没完成或系统未安装第三方应用）。"
        # 按名字排序
        apps_sorted = sorted(apps, key=lambda a: a.name.lower())
        lines = [f"已装应用（共 {len(apps_sorted)} 个，显示前 {min(limit, len(apps_sorted))} 个）：", ""]
        for app in apps_sorted[:limit]:
            lines.append(f"  {app.name}  [{app.source}]  {app.path}")
        return "\n".join(lines)

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
        description=(
            "用默认浏览器打开一个网址。自动补全协议头："
            "传 'baidu.com' 或 'www.baidu.com' 都能打开；"
            "传 'xx yy' 会用 DuckDuckGo 搜索。"
        ),
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        fn=open_website,
    ))
    reg.register(Tool(
        name="open_app",
        description=(
            "启动一个 Windows 应用程序。智能查找顺序：\n"
            "(1) 完整路径（如 C:\\\\Program Files\\\\...\\\\app.exe）；\n"
            "(2) 内置映射 + 用户自定义（notepad / 记事本 / QQ / 微信 / VS Code 等）；\n"
            "(3) **系统已安装应用扫描**（按相似度匹配，扫描开始菜单 / 桌面 / "
            "App Paths / Uninstall / Program Files）；\n"
            "(4) 找不到时返回 top 5 相似候选，让你挑一个再开。\n"
            "用户给的 app_name 不必精确——支持中文 / 拼写错误 / 别名。"
        ),
        parameters={
            "type": "object",
            "properties": {"app_name": {"type": "string"}},
            "required": ["app_name"],
        },
        fn=open_app,
    ))
    reg.register(Tool(
        name="list_installed_apps",
        description=(
            "列出系统中已安装的应用（扫描开始菜单 / 桌面 / App Paths / "
            "Uninstall 注册表 / Program Files）。\n"
            "用法：\n"
            "  - 不传 query：返回所有已装应用（按名字排序）\n"
            "  - 传 query：按相似度返回 top-N（如 list_installed_apps(query='vscode')）\n"
            "主人说「打开 XX」但你不确定 XX 的真实应用名时，先用这个工具列一下，"
            "再调 open_app 用准确的名称。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "过滤关键词（可选；空 = 列出全部）"},
                "limit": {"type": "integer", "description": "最多返回多少条（默认 20）"},
            },
        },
        fn=list_installed_apps_tool,
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