"""系统操作工具：open_website / open_app / system_info / take_screenshot / clipboard / notification。"""
from __future__ import annotations

import json
import os
import subprocess
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ._core import ToolRegistry
from ._decorator import tool

if TYPE_CHECKING:  # pragma: no cover - 仅供类型标注
    from app.core.installed_apps import InstalledApp

# cmd.exe / POSIX shell 的元字符。open_app 的兜底 PATH 搜索不允许出现这些字符，
# 否则会被 shell 当成命令分隔符 / 引号逃逸，形成命令注入
# （app_name 来自 LLM，可经提示词注入构造）。
_SHELL_METACHARS = frozenset('&|<>^"\'%!()\n\r`$*?;#')


def _has_cjk(text: str) -> bool:
    """名字里有没有中日韩字符（用来把中文应用排到英文应用前面）。"""
    return any("\u4e00" <= ch <= "\u9fff" for ch in (text or ""))


# 列表展示顺序：主人自己放的应用（桌面）排最前，其次开始菜单，
# 系统组件（卸载注册表 / Program Files）排最后 —— 它们几乎不会被「打开 XX」命中。
_SOURCE_PRIORITY = {
    "desktop": 0,
    "start_menu": 1,
    "app_paths": 2,
    "uninstall": 3,
    "program_files": 4,
}


def _find_installed_candidates(
    query: str, limit: int = 5,
) -> list[tuple["InstalledApp", float]]:
    """扫描一次系统已装应用，返回相似候选。

    同一次 open_app 里，「解析启动路径」和「列出候选建议」共用这一份结果——
    否则会把系统扫两遍（冷缓存时原本要先阻塞 3 s、再阻塞 3 s）。
    """
    try:
        from app.core.installed_apps import find_similar_apps
        return find_similar_apps(
            query, limit=limit * 2, min_ratio=0.55, wait=True, timeout=3.0,
        )
    except Exception:  # noqa: BLE001
        return []


def _collect_suggestions(
    query: str,
    limit: int = 5,
    installed_candidates: Optional[list[tuple["InstalledApp", float]]] = None,
) -> list[tuple[str, str, float]]:
    """收集「内置映射相似」+「已装应用相似」的 top-N 候选。

    Returns: [(display_name, exe_path, score), ...]，按 score 降序，去重。

    installed_candidates: 外部已经查好的「已装应用相似候选」，传入则复用，
        不再重复调用 find_similar_apps。
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
            full = reg.resolve(display, installed_candidates=installed_candidates)
            if full:
                _add(display, full, score)
    except Exception:  # noqa: BLE001
        pass

    # 2) 已装应用扫描相似候选（wait=True 等扫描完成，最多 3 秒）
    #    阈值 0.55：低于这个会返回太多噪声（如 "totallyfake" → Tailscale 50%）
    try:
        if installed_candidates is None:
            installed_candidates = _find_installed_candidates(query, limit=limit)
        for app, score in installed_candidates:
            _add(app.name, app.path, score)
    except Exception:  # noqa: BLE001
        pass

    # 排序 + 截断
    items = sorted(out.values(), key=lambda x: -x[2])
    return items[:limit]



@tool(
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
)
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


@tool(
    description=(
        "启动一个 Windows 应用程序。智能查找顺序：\n"
        "(1) 完整路径（如 C:\\\\Program Files\\\\...\\\\app.exe）；\n"
        "(2) 内置映射 + 用户自定义（notepad / 记事本 / QQ / 微信 / VS Code 等）；\n"
        "(3) **系统已安装应用扫描**（按相似度匹配，扫描开始菜单 / 桌面 / "
        "App Paths / Uninstall / Program Files）；\n"
        "(4) 找不到时返回 top 5 相似候选，让你挑一个再开。\n"
        "用户给的 app_name 不必精确——支持中文 / 拼写错误 / 别名，"
        "**直接把主人说的原话传进来即可**（如 app_name='星穹铁道'），"
        "不要因为自己不确定名字就去 list_installed_apps 里查。"
    ),
    parameters={
        "type": "object",
        "properties": {"app_name": {"type": "string"}},
        "required": ["app_name"],
    },
)
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

    # 0) 入参白名单校验：拒绝 shell 元字符。
    #    app_name 来自 LLM，可被提示词注入构造成 "notepad&calc"。
    #    必须放在最前面——否则注册表的模糊匹配会把它解析成 notepad，
    #    静默启动一个用户没要求的应用（既是安全问题也是体验问题）。
    bad = _SHELL_METACHARS & set(name)
    if bad:
        return (
            f"错误：应用名「{name}」含非法字符 {' '.join(sorted(bad))}。\n"
            f"提示：只传应用名或完整路径，不要拼接命令。"
        )

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
    #    先只查内置映射（installed_candidates=[] = 不走已装应用兜底），
    #    这样命中内置时不必等系统扫描。
    registry = get_registry()
    found = registry.resolve(name, installed_candidates=[])
    if found:
        try:
            os.startfile(found)  # noqa: S606
            return f"已启动 {name}（{found}）"
        except Exception as e:  # noqa: BLE001
            return f"错误：启动失败 {found}：{e}"

    # 3b) 内置没命中 → 扫一次已装应用；这一份结果同时喂给
    #     「解析启动路径」和「列出候选建议」，系统只扫一遍。
    candidates = _find_installed_candidates(name, limit=5)
    found = registry.resolve(name, installed_candidates=candidates)
    if found:
        try:
            os.startfile(found)  # noqa: S606
            return f"已启动 {name}（{found}）"
        except Exception as e:  # noqa: BLE001
            return f"错误：启动失败 {found}：{e}"

    # 4) 没找到：返回 top 5 相似候选（内置映射 + 已装应用）
    suggestions = _collect_suggestions(
        name, limit=5, installed_candidates=candidates,
    )
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

    # 5) 完全找不到：兜底「PATH 搜索」（仅 ASCII 短串）
    looks_like_exe = name.replace(" ", "").isascii()
    if looks_like_exe:
        try:
            subprocess.Popen(
                [name],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return (
                f"已尝试启动 {name}（未在已装应用中找到，"
                f"已在系统 PATH 中搜索。"
                f"若未弹出窗口，可能未安装。）"
            )
        except FileNotFoundError:
            return (
                f"未找到应用「{name}」，系统 PATH 里也没有这个程序。\n"
                f"提示：用英文名（如 notepad / mspaint / explorer）、"
                f"或完整路径（如 C:\\Program Files\\...\\app.exe）"
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


@tool(
    name="list_installed_apps",
    description=(
        "列出系统中已安装的应用（扫描开始菜单 / 桌面 / App Paths / "
        "Uninstall 注册表 / Program Files）。\n"
        "用法：\n"
        "  - 不传 query：返回所有已装应用（中文名优先，再按名字排序）\n"
        "  - 传 query：按相似度返回 top-N（如 list_installed_apps(query='vscode')）\n"
        "重要：主人说「打开 XX」时，**直接调 open_app 就能开**（它自带模糊匹配），"
        "本工具只在你需要确认真实应用名时用。别因为列表里没看到某个名字就"
        "回答「没装」——同一个程序常有多个快捷方式名（米哈游启动器 / 崩坏：星穹铁道），"
        "而列表默认只显示前 20 条。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "过滤关键词（可选；空 = 列出全部）"},
            "limit": {"type": "integer", "description": "最多返回多少条（默认 20）"},
        },
    },
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
    # 排序：先按来源（主人自己放的应用优先），组内中文名优先、再按名字。
    #
    # 纯按名字排序的话，前 N 个会被 7-Zip / Accessibility / 「卸载 xxx」
    # 这类系统组件占满 —— 主人说「打开星穹铁道」，LLM 在列表里翻不到
    # 「崩坏：星穹铁道」这个名字就会答「没装」。而桌面 / 开始菜单里的才是
    # 他真正会用工具去打开的东西。
    apps_sorted = sorted(
        apps,
        key=lambda a: (
            _SOURCE_PRIORITY.get(a.source, 9),
            not _has_cjk(a.name),
            a.name.lower(),
        ),
    )
    lines = [f"已装应用（共 {len(apps_sorted)} 个，显示前 {min(limit, len(apps_sorted))} 个）：", ""]
    for app in apps_sorted[:limit]:
        lines.append(f"  {app.name}  [{app.source}]  {app.path}")
    return "\n".join(lines)


@tool(
    description="获取系统信息：OS版本、CPU使用率、内存、磁盘、电池电量。",
    parameters={"type": "object", "properties": {}},
)
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


@tool(
    description="截取当前屏幕截图（返回 base64 编码的 PNG）。用于让桌宠看到用户屏幕内容。",
    parameters={"type": "object", "properties": {}},
)
def take_screenshot_tool() -> str:
    """截取当前屏幕并返回 base64 编码的图片。

    注意：模型需要支持 vision 能力才能分析图片。
    """
    from app.engine.screenshot import screenshot_to_base64
    b64 = screenshot_to_base64()
    if b64 is None:
        return "错误：截图失败，请确保已安装 pyautogui 或 Pillow"
    return f"截图成功（base64，{len(b64)} 字符）"


@tool(
    description="把文本复制到系统剪贴板，主人可以 Ctrl+V 粘贴。",
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string", "description": "要复制的文本"}},
        "required": ["text"],
    },
)
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


@tool(
    description="发送 Windows 系统通知（右下角弹窗）。需要安装 win10toast 库。",
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "通知标题"},
            "message": {"type": "string", "description": "通知内容"},
        },
        "required": ["title", "message"],
    },
)
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


def register(reg: ToolRegistry, *, hooks=None) -> None:
    """注册系统级工具（剪贴板、通知、打开网址、打开应用、截图、系统信息）。"""
    reg.register(open_website)
    reg.register(open_app)
    reg.register(list_installed_apps_tool)
    reg.register(system_info)
    reg.register(take_screenshot_tool)
    reg.register(clipboard_copy)
    reg.register(send_notification)


__all__ = ["register"]