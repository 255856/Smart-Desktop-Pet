"""按文件名 / 关键词在指定目录递归搜索文件。沙箱限定在用户主目录。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from ._core import Tool, ToolRegistry, is_under_any

log = logging.getLogger(__name__)

# 沙箱根：用户家目录
_HOME = Path.home()

# 递归扫描的硬上限：rglob 会一路走进整个目录树，若不设限，
# 在 Desktop 下挂了大项目 / node_modules 的机器上可能跑好几分钟。
# 工具在 agent 里是 asyncio.to_thread 同步执行，一个卡住的工具会
# 阻塞整轮对话（用户看到桌宠一直「思考中」且无法取消），所以必须有时间预算。
_MAX_VISIT = 20000        # 最多遍历多少个条目
_TIME_BUDGET_S = 8.0      # 最多花多少秒


def _is_safe_path(p: Path) -> bool:
    """路径必须在用户家目录下（防止越权）。

    用 relative_to 做路径语义比较；str.startswith 的前缀比较会让
    C:\\Users\\alice-backup 通过 C:\\Users\\alice 的检查。
    """
    return is_under_any(p, [_HOME])


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
        visited = 0
        deadline = time.monotonic() + _TIME_BUDGET_S
        truncated = False
        try:
            for p in base.rglob("*"):
                visited += 1
                if visited > _MAX_VISIT or time.monotonic() > deadline:
                    truncated = True
                    break
                if not p.is_file():
                    continue
                if kw in p.name.lower():
                    matches.append(p)
                    if len(matches) >= limit:
                        break
        except PermissionError as e:
            return f"错误：无权限扫描：{e}"
        except Exception as e:  # noqa: BLE001
            return f"错误：扫描失败：{e}"
        if not matches:
            if truncated:
                return (f"错误：在 {base} 扫描了 {visited} 个条目后达到时间/数量上限，"
                        f"未找到文件名含「{keyword}」的文件（结果不完整，"
                        f"不代表真的没有）。建议指定更具体的 directory 缩小范围。")
            return f"在 {base} 未找到文件名含「{keyword}」的文件"
        lines = [f"找到 {len(matches)} 个匹配（最多 {limit}）："]
        for p in matches:
            try:
                size = p.stat().st_size
                size_str = _fmt_size(size)
            except OSError:
                size_str = "?"
            lines.append(f"  {p}  ({size_str})")
        if truncated:
            lines.append(f"（只扫描了前 {visited} 个条目就达到上限，结果可能不完整。"
                         f"建议指定更具体的 directory）")
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