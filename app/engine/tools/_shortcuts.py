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


def register(reg: ToolRegistry) -> None:
    """注册 Windows 快捷启动工具（仅这几个不与 open_app 重复的）。"""

    def open_file_explorer(path: str = "") -> str:
        if path:
            os.startfile(path)
        else:
            os.startfile("explorer.exe")
        return f"已打开文件资源管理器{' (' + path + ')' if path else ''}"

    def open_terminal() -> str:
        subprocess.Popen(["powershell.exe"])
        return "已打开 PowerShell 终端"

    def open_notepad(text: str = "") -> str:
        if text:
            tmp = Path(tempfile.gettempdir()) / "pet_notepad.txt"
            tmp.write_text(text, encoding="utf-8")
            os.startfile(str(tmp))
            return f"已打开记事本（{len(text)} 字符）"
        os.startfile("notepad.exe")
        return "已打开记事本"

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