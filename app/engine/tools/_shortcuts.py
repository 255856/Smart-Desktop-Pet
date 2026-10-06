"""Windows 快捷启动：仅注册『打开资源管理器 / 终端 / 记事本 / 控制面板 / 设置』，
其余内置应用（计算器 / 任务管理器 / QQ / Chrome ...）走通用 open_app(app_name=...)，
内置注册表（app/core/app_registry.py）会负责解析 + 启动。
避免重复工具让 LLM 在多个候选项里挑错。"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from ._core import Tool, ToolRegistry

# 和 _check_explorable 里的 p.resolve() 保持同一形态，
# 否则家目录本身是符号链接时会误判成「家目录之外」。
try:
    _HOME = Path.home().resolve()
except OSError:  # pragma: no cover - 极端环境
    _HOME = Path.home()

# 这些扩展名 os.startfile 等于「执行」，不能让模型给的任意路径直通。
_EXECUTABLE_SUFFIXES = frozenset({
    ".exe", ".bat", ".cmd", ".com", ".scr", ".msi", ".msp",
    ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta",
    ".lnk", ".url", ".pif", ".reg",
})


def _check_explorable(raw: str) -> tuple[Path | None, str]:
    """校验要交给 os.startfile 的目录路径。

    原实现对模型给的任意路径直接 os.startfile，既没有家目录守卫，也没有扩展名
    限制——传个 .exe/.bat/.lnk 就等于直接执行，而同仓 _file.py / _filesearch.py /
    _runner.py 全都有沙箱，唯独这里没有。
    """
    p = Path(raw).expanduser()
    try:
        p = p.resolve()
    except OSError:
        return None, f"错误：路径无法解析：{raw}"
    # 沙箱：只看家目录以内（用 relative_to，不用 str.startswith 免前缀绕过）
    try:
        p.relative_to(_HOME)
    except ValueError:
        return None, (f"错误：安全策略拒绝，只能打开用户主目录下的位置。"
                      f"{p} 在家目录之外。")
    if p.suffix.lower() in _EXECUTABLE_SUFFIXES:
        return None, (f"错误：{p.name} 是可执行/快捷方式类型，"
                      f"不能用「打开文件夹」的方式启动它（那等于直接运行程序）。")
    if not p.exists():
        return None, f"错误：路径不存在：{p}"
    if not p.is_dir():
        return None, f"错误：不是文件夹：{p}"
    return p, ""


def register(reg: ToolRegistry) -> None:
    """注册 Windows 快捷启动工具（仅这几个不与 open_app 重复的）。"""

    def open_file_explorer(path: str = "") -> str:
        if not (path or "").strip():
            try:
                os.startfile("explorer.exe")
            except OSError as e:
                return f"错误：无法打开文件资源管理器（{type(e).__name__}）"
            return "已打开文件资源管理器"
        p, err = _check_explorable(path)
        if err:
            return err
        try:
            os.startfile(str(p))
        except OSError as e:
            return f"错误：无法打开 {p}（{type(e).__name__}）"
        return f"已用文件资源管理器打开 {p}"

    def open_terminal() -> str:
        try:
            proc = subprocess.Popen(["powershell.exe"])
        except OSError as e:
            return f"错误：无法启动 PowerShell（{type(e).__name__}）"
        return f"已打开 PowerShell 终端（pid={proc.pid}）"

    def open_notepad(text: str = "") -> str:
        if not (text or "").strip():
            try:
                os.startfile("notepad.exe")
            except OSError as e:
                return f"错误：无法打开记事本（{type(e).__name__}）"
            return "已打开记事本"
        # 原来写死 pet_notepad.txt，会静默覆盖主人上次的内容
        try:
            with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".txt", prefix="pet_notepad_",
                    delete=False, encoding="utf-8") as fh:
                fh.write(text)
                tmp = Path(fh.name)
        except OSError as e:
            return f"错误：无法写入临时文件（{type(e).__name__}）"
        try:
            os.startfile(str(tmp))
        except OSError as e:
            return f"错误：已写入 {tmp}，但无法打开记事本（{type(e).__name__}）"
        return f"已打开记事本（{len(text)} 字符，临时文件 {tmp.name}）"

    reg.register(Tool(name="open_file_explorer",
        description="打开文件资源管理器，可指定目录。",
        parameters={"type": "object",
                    "properties": {"path": {"type": "string", "description": "目录路径，为空则打开默认"}}},
        fn=open_file_explorer))
    reg.register(Tool(name="open_terminal", description="打开 PowerShell 终端。",
        parameters={"type": "object", "properties": {}}, fn=open_terminal))
    reg.register(Tool(name="open_notepad", description="打开记事本，可选初始文本。",
        parameters={"type": "object",
                    "properties": {"text": {"type": "string", "description": "初始文本，为空则打开空白"}}},
        fn=open_notepad))


__all__ = ["register"]