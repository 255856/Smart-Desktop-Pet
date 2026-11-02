"""app/engine/tools/_system.py 的回归测试（Stage 3.5）。

钉死两件事：

1. **模块里只剩一个 register()** —— 以前有两份：:104 的内嵌副本（永远被后面
   :733 那份覆盖，从不执行）和 :733 的活的那份。两份都注册同一批 7 个工具，
   所以删掉死副本**工具集不变**，但少了一份要手工同步的定义。
2. **注册出来的工具对象一字不差** —— 名字 / 描述 / 参数 schema / 函数体
   都跟删死副本之前一致（快照是删除前抓的）。

不需要 API key / 网络 / GUI。
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PATH = ROOT / "app" / "engine" / "tools" / "_system.py"

# 删死副本之前抓的快照（2026-10 的树）：_system 负责注册的 7 个工具
SYSTEM_TOOL_NAMES = (
    "clipboard_copy",
    "list_installed_apps",
    "open_app",
    "open_website",
    "send_notification",
    "system_info",
    "take_screenshot_tool",
)

# name -> (description, parameters, fn 的 module.qualname)
SYSTEM_TOOL_SNAPSHOT: dict[str, tuple[str, dict, str]] = {
    "clipboard_copy": (
        "把文本复制到系统剪贴板，主人可以 Ctrl+V 粘贴。",
        {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要复制的文本"}},
            "required": ["text"],
        },
        "app.engine.tools._system.clipboard_copy",
    ),
    "list_installed_apps": (
        "列出系统中已安装的应用（扫描开始菜单 / 桌面 / App Paths / "
        "Uninstall 注册表 / Program Files）。\n"
        "用法：\n"
        "  - 不传 query：返回所有已装应用（中文名优先，再按名字排序）\n"
        "  - 传 query：按相似度返回 top-N（如 list_installed_apps(query='vscode')）\n"
        "重要：主人说「打开 XX」时，**直接调 open_app 就能开**（它自带模糊匹配），"
        "本工具只在你需要确认真实应用名时用。别因为列表里没看到某个名字就"
        "回答「没装」——同一个程序常有多个快捷方式名（米哈游启动器 / 崩坏：星穹铁道），"
        "而列表默认只显示前 20 条。",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "过滤关键词（可选；空 = 列出全部）"},
                "limit": {"type": "integer", "description": "最多返回多少条（默认 20）"},
            },
        },
        "app.engine.tools._system.list_installed_apps_tool",
    ),
    "open_app": (
        "启动一个 Windows 应用程序。智能查找顺序：\n"
        "(1) 完整路径（如 C:\\\\Program Files\\\\...\\\\app.exe）；\n"
        "(2) 内置映射 + 用户自定义（notepad / 记事本 / QQ / 微信 / VS Code 等）；\n"
        "(3) **系统已安装应用扫描**（按相似度匹配，扫描开始菜单 / 桌面 / "
        "App Paths / Uninstall / Program Files）；\n"
        "(4) 找不到时返回 top 5 相似候选，让你挑一个再开。\n"
        "用户给的 app_name 不必精确——支持中文 / 拼写错误 / 别名，"
        "**直接把主人说的原话传进来即可**（如 app_name='星穹铁道'），"
        "不要因为自己不确定名字就去 list_installed_apps 里查。",
        {
            "type": "object",
            "properties": {"app_name": {"type": "string"}},
            "required": ["app_name"],
        },
        "app.engine.tools._system.open_app",
    ),
    "open_website": (
        "用默认浏览器打开一个网址。自动补全协议头："
        "传 'baidu.com' 或 'www.baidu.com' 都能打开；"
        "传 'xx yy' 会用 DuckDuckGo 搜索。",
        {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        "app.engine.tools._system.open_website",
    ),
    "send_notification": (
        "发送 Windows 系统通知（右下角弹窗）。需要安装 win10toast 库。",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "通知标题"},
                "message": {"type": "string", "description": "通知内容"},
            },
            "required": ["title", "message"],
        },
        "app.engine.tools._system.send_notification",
    ),
    "system_info": (
        "获取系统信息：OS版本、CPU使用率、内存、磁盘、电池电量。",
        {"type": "object", "properties": {}},
        "app.engine.tools._system.system_info",
    ),
    "take_screenshot_tool": (
        "截取当前屏幕截图（返回 base64 编码的 PNG）。用于让你看到屏幕内容。",
        {"type": "object", "properties": {}},
        "app.engine.tools._system.take_screenshot_tool",
    ),
}

# 删死副本之前抓的完整内置工具名快照（65 个；MCP 的 7 个是运行时桥接的，不在内）
FULL_BUILTIN_TOOL_NAMES = (
    "Task", "add_reminder", "calculate", "cancel_countdown",
    "change_pet_emotion", "clear_bubble", "clipboard_copy", "convert_units",
    "countdown", "date_info", "delete_reminder", "edit_file", "feed_self",
    "fetch_url_text", "forget_memory", "generate_audio", "generate_image",
    "generate_video", "get_battery", "get_brightness", "get_clipboard",
    "get_current_time", "get_pet_status", "get_tts_mute_status", "get_volume",
    "get_weather", "glob_files", "grep_files", "kill_process",
    "list_countdowns", "list_desktop_files", "list_installed_apps",
    "list_processes", "list_reminders", "lock_screen", "multi_edit",
    "ocr_image", "open_app", "open_file_explorer", "open_notepad",
    "open_terminal", "open_website", "play_animation", "query_slang",
    "read_file", "read_text_file", "recall_memory", "remember_fact",
    "run_script", "say_to_user", "search_files", "send_notification",
    "send_sticker", "set_bluetooth", "set_brightness", "set_tts_mute_until", "set_volume",
    "set_wifi", "submit_slang", "system_info", "take_screenshot_tool",
    "understand_image", "unmute_tts", "update_self", "use_skill", "web_search",
    "browser",
)


@pytest.fixture(scope="module")
def system_module():
    """app.engine.tools._system（活的那份 register 所在的模块）。"""
    from app.engine.tools import _system
    return _system


@pytest.fixture(scope="module")
def system_registry(system_module):
    """只跑 _system.register 的注册表（7 个工具）。"""
    from app.engine.tools._core import ToolRegistry
    reg = ToolRegistry()
    system_module.register(reg)
    return reg


# --- 1. 模块里只剩一个 register -----------------------------------------


def test_system_module_defines_register_exactly_once():
    """_system.py 里只能有一个模块级 register()（曾经有两份，死的那份永不执行）。"""
    tree = ast.parse(SYSTEM_PATH.read_text("utf-8"))
    registers = [n for n in tree.body
                 if isinstance(n, ast.FunctionDef) and n.name == "register"]
    assert len(registers) == 1, (
        "_system.py 里有 " + str(len(registers)) + " 个 register()，应该只剩 1 个："
        + str([n.lineno for n in registers])
    )


def test_register_has_no_nested_tool_definitions():
    """活的那份 register 只是「把模块级工具注册进去」，不再内嵌一份实现。"""
    tree = ast.parse(SYSTEM_PATH.read_text("utf-8"))
    register = next(n for n in tree.body
                    if isinstance(n, ast.FunctionDef) and n.name == "register")
    nested = [n.name for n in ast.walk(register)
              if isinstance(n, ast.FunctionDef) and n is not register]
    assert nested == [], "register() 里又内嵌了工具实现：" + str(nested)


def test_dead_copy_markers_are_gone():
    """死副本的痕迹（内嵌的 hooks 归一化 / 内嵌工具体）不许留在文件里。"""
    src = SYSTEM_PATH.read_text("utf-8")
    assert "hooks = hooks or {}" not in src, "_system.py 里还有死副本留下的 hooks 归一化"
    # 死副本里的内嵌函数都带这行注释；模块级实现没有
    assert src.count("注册系统级工具（剪贴板、通知、打开网址、打开应用、截图、系统信息）") == 1


# --- 2. 工具集 / 工具对象一字不差 ----------------------------------------


def test_system_registers_exactly_the_seven_tools(system_registry):
    """_system 只注册这 7 个工具（删死副本前后一致）。"""
    assert sorted(system_registry._tools) == sorted(SYSTEM_TOOL_NAMES)


def test_registered_tool_objects_match_snapshot(system_registry):
    """名字 / 描述 / 参数 schema / 函数体 都跟删死副本之前一致（A5）。"""
    got = {}
    for name, tool in system_registry._tools.items():
        got[name] = (
            tool.description,
            tool.parameters,
            tool.fn.__module__ + "." + tool.fn.__qualname__,
        )
    assert got == SYSTEM_TOOL_SNAPSHOT


def test_tool_descriptions_are_not_empty(system_registry):
    """每个工具都得有描述（描述丢了 LLM 就没法选工具）。"""
    for name, tool in system_registry._tools.items():
        assert tool.description.strip(), name + " 没有描述"


def test_tool_description_digest_is_stable(system_registry):
    """描述内容的 sha256 快照（描述被改写会红）。"""
    digests = {
        name: hashlib.sha256(tool.description.encode("utf-8")).hexdigest()[:12]
        for name, tool in system_registry._tools.items()
    }
    expected = {
        name: hashlib.sha256(snap[0].encode("utf-8")).hexdigest()[:12]
        for name, snap in SYSTEM_TOOL_SNAPSHOT.items()
    }
    assert digests == expected


def test_full_registry_tool_names_unchanged():
    """整个注册表的工具名跟删死副本之前一模一样（A4）。"""
    from app.engine.tools import build_default_tools

    class _StubState:
        def get(self, *a, **k):
            return None

        def stats_summary(self) -> str:
            return "stub"

    reg = build_default_tools(state=_StubState(), reminders=None, memory=None,
                              items=None)
    assert sorted(reg._tools) == sorted(FULL_BUILTIN_TOOL_NAMES)
