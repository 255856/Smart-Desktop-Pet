"""Windows 快捷启动工具：任务管理器/控制面板/设置/资源管理器/终端/记事本/计算器。"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from ._core import Tool, ToolRegistry


def register(reg: ToolRegistry) -> None:
    """注册 Windows 快捷启动工具。"""

    def open_task_manager() -> str:
        subprocess.Popen(["taskmgr.exe"])
        return "已打开任务管理器"

    def open_control_panel() -> str:
        os.startfile("control")
        return "已打开控制面板"

    def open_windows_settings() -> str:
        os.startfile("ms-settings:")
        return "已打开 Windows 设置"

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

    def open_calculator() -> str:
        os.startfile("calc.exe")
        return "已打开计算器"

    reg.register(Tool(name="open_task_manager", description="打开任务管理器。",
        parameters={"type": "object", "properties": {}}, fn=open_task_manager))
    reg.register(Tool(name="open_control_panel", description="打开 Windows 控制面板。",
        parameters={"type": "object", "properties": {}}, fn=open_control_panel))
    reg.register(Tool(name="open_windows_settings", description="打开 Windows 设置。",
        parameters={"type": "object", "properties": {}}, fn=open_windows_settings))
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
    reg.register(Tool(name="open_calculator", description="打开计算器。",
        parameters={"type": "object", "properties": {}}, fn=open_calculator))


__all__ = ["register"]