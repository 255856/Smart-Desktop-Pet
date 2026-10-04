"""按文件名 / 关键词在指定目录递归搜索文件。沙箱限定在用户主目录。"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from ._core import Tool, ToolRegistry

log = logging.getLogger(__name__)

# 沙箱根：用户家目录
_HOME = Path.home()


def _is_safe_path(p: Path) -> bool:
    """路径必须在用户家目录下（防止越权）。"""
    try:
        p_resolved = p.resolve()
        return str(p_resolved).startswith(str(_HOME.resolve()))
    except (OSError, ValueError):
        return False


def register(reg: ToolRegistry) -> None:
    """注册文件搜索工具。"""

    def search_files(keyword: str, directory: str = "", limit: int = 50) -> str:
        """在指定目录（默认桌面）递归搜文件名含 keyword 的文件。沙箱限定在家目录。"""
        if not keyword or not keyword.strip():
            return "错误：keyword 不能为空"
        if directory:
            base = Path(directory).expanduser()
        else:
            base = _HOME / "Desktop"
        if not _is_safe_path(base):
            return f"错误：路径必须在用户主目录下（拒绝 {base}）"
        if not base.exists():
            return f"目录不存在：{base}"
        limit = max(1, min(int(limit if limit else 50), 500))
        matches = []
        kw = keyword.lower()
        try:
            for p in base.rglob("*"):
                if not p.is_file():
                    continue
                if kw in p.name.lower():
                    matches.append(p)
                    if len(matches) >= limit:
                        break
        except PermissionError as e:
            return f"无权限扫描：{e}"
        except Exception as e:  # noqa: BLE001
            return f"扫描失败：{e}"
        if not matches:
            return f"在 {base} 未找到文件名含「{keyword}」的文件"
        lines = [f"找到 {len(matches)} 个匹配（最多 {limit}）："]
        for p in matches:
            try:
                size = p.stat().st_size
                size_str = _fmt_size(size)
            except OSError:
                size_str = "?"
            lines.append(f"  {p}  ({size_str})")
        return "\n".join(lines)

    def _fmt_size(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.1f}{unit}"
            n //= 1024
        return f"{n}TB"

    reg.register(Tool(name="search_files",
        description="在指定目录（默认桌面）递归搜文件名含 keyword 的文件。"
                    "沙箱限定在用户主目录下，禁止越权读系统目录。",
        parameters={"type": "object",
                    "properties": {
                        "keyword": {"type": "string",
                                    "description": "文件名子串（大小写不敏感）"},
                        "directory": {"type": "string",
                                      "description": "搜索目录，绝对路径或 ~/xxx，空则桌面"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 500,
                                  "description": "最多返回几条"},
                    },
                    "required": ["keyword"]},
        fn=search_files))


__all__ = ["register"]