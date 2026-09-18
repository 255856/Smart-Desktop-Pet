"""文件操作工具：list_desktop_files / read_text_file。"""
from __future__ import annotations

import os
from pathlib import Path

from ._core import Tool, ToolRegistry


def register(reg: ToolRegistry) -> None:
    """注册文件类工具。"""

    def list_desktop_files() -> str:
        """列出桌面文件。"""
        desktop = Path(os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"))
        if not desktop.is_dir():
            desktop = Path.home() / "Desktop"
        if not desktop.is_dir():
            return "找不到桌面目录"
        files = sorted(desktop.iterdir(), key=lambda p: p.name)
        if not files:
            return "桌面是空的"
        lines = [f"{'📁' if p.is_dir() else '📄'} {p.name}" for p in files[:50]]
        total = len(files)
        if total > 50:
            lines.append(f"... 还有 {total - 50} 个文件")
        return f"桌面共 {total} 个文件：\n" + "\n".join(lines)

    def read_text_file(path: str) -> str:
        """读取文本文件内容（最多 2000 字符）。"""
        p = Path(path)
        if not p.is_file():
            return f"文件不存在：{path}"
        try:
            raw = p.read_text(encoding="utf-8", errors="replace")
            content = raw[:2000]
            if len(raw) > 2000:
                content += "\n...（已截断，文件较长）"
            return content
        except Exception as e:  # noqa: BLE001
            return f"读取失败：{e}"

    reg.register(Tool(
        name="list_desktop_files",
        description="列出桌面上的文件和文件夹。",
        parameters={"type": "object", "properties": {}},
        fn=list_desktop_files,
    ))
    reg.register(Tool(
        name="read_text_file",
        description="读取文本文件内容（最多 2000 字符）。",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "文件路径"}},
            "required": ["path"],
        },
        fn=read_text_file,
    ))


__all__ = ["register"]