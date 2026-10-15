"""按文件名 / 内容在指定目录递归搜索文件。沙箱限定在用户主目录。"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from ._core import ToolRegistry, is_under_any
from ._decorator import tool

log = logging.getLogger(__name__)

# 沙箱根：用户家目录 + 主程序可追加的「项目目录」
# 设计：默认只允许家目录内访问(防越权读系统目录);主程序(chat_window 启动时)
# 可以调 set_extra_safe_roots() 把项目目录加进去(例如 E:\study\desktop-pet)。
# 这些额外根和家目录一起走 is_under_any() 校验,不在白名单里的路径一律拒绝。
_HOME = Path.home()
_extra_safe_roots: list[Path] = []


def set_extra_safe_roots(paths: list) -> None:
    """主程序在 chat_window 启动时调用,把项目目录加进沙箱白名单。

    Args:
        paths: 额外的允许根目录路径列表。每个会被 resolve() 后判定,
               路径不存在/无法 resolve 时记 WARNING 但不抛(避免启动失败)。
    """
    global _extra_safe_roots
    out: list[Path] = []
    for p in paths or []:
        try:
            out.append(Path(p).resolve())
        except (OSError, RuntimeError, ValueError) as e:
            log.warning("set_extra_safe_roots: 跳过 %s (%s)", p, e)
    _extra_safe_roots = out


def get_extra_safe_roots() -> list[Path]:
    return list(_extra_safe_roots)


# 递归扫描的硬上限：rglob 会一路走进整个目录树，若不设限，
# 在 Desktop 下挂了大项目 / node_modules 的机器上可能跑好几分钟。
# 工具在 agent 里是 asyncio.to_thread 同步执行，一个卡住的工具会
# 阻塞整轮对话（用户看到桌宠一直「思考中」且无法取消），所以必须有时间预算。
_MAX_VISIT = 20000        # 最多遍历多少个条目
_TIME_BUDGET_S = 8.0      # 最多花多少秒

# 单文件读取上限（grep 跳过的最大字节数；超大的二进制文件直接跳过，
# 不然 OpenCV 这种几十 MB 的视频转码缓存会把一次 grep 拖死）。
_MAX_FILE_BYTES = 2 * 1024 * 1024   # 2 MiB


def _is_safe_path(p: Path) -> bool:
    """路径必须在用户家目录或「主程序加的项目目录」下（防止越权）。

    用 relative_to 做路径语义比较；str.startswith 的前缀比较会让
    C:\\Users\\alice-backup 通过 C:\\Users\\alice 的检查。
    """
    return is_under_any(p, [_HOME] + list(_extra_safe_roots))


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n //= 1024
    return f"{n}TB"


def _matches_include(name: str, include: str) -> bool:
    """文件名 glob 匹配：支持 `*.py` / `foo*.txt` 这类简单模式。

    用 fnmatch 而非完整 glob 语义；足够覆盖 99% 场景，
    且行为可预测（`**` 这种递归语义各家库不一致）。
    """
    import fnmatch
    return fnmatch.fnmatch(name, include)


def _iter_text_files(base: Path, include: str):
    """遍历 base 下文件，按 include 过滤，跳过大文件 / 二进制。

    这是 generator，把「超时 / 超量截断」逻辑集中在调用方。
    """
    visited = 0
    deadline = time.monotonic() + _TIME_BUDGET_S
    truncated = False
    for p in base.rglob("*"):
        visited += 1
        if visited > _MAX_VISIT or time.monotonic() > deadline:
            truncated = True
            break
        if not p.is_file():
            continue
        if include and not _matches_include(p.name, include):
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if size > _MAX_FILE_BYTES:
            continue
        yield p, visited, truncated


def register(reg: ToolRegistry) -> None:
    """注册文件搜索工具。"""

    @tool(
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
    )
    def search_files(keyword: str, directory: str = "", limit: int = 50) -> str:
        """在指定目录（默认桌面）递归搜文件名含 keyword 的文件。沙箱限定在家目录。"""
        if not keyword or not keyword.strip():
            return "错误：keyword 不能为空"
        if directory:
            base = Path(directory).expanduser()
        else:
            base = _HOME / "Desktop"
        if not _is_safe_path(base):
            return (
                f"错误：路径不在沙箱内（拒绝 {base}）。"
                f"默认只允许用户主目录（{_HOME}），"
                f"可在 config.yaml 加 llm.project_dirs 把项目目录放行。"
            )
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

    @tool(
        description="在指定目录下递归按正则匹配文件内容，输出 file:行号:内容 形式。"
                    "沙箱限定在用户主目录下；超过 2 MiB 的文件自动跳过。"
                    "适合按「符号 / 字符串 / 代码片段」定位文件，比逐个 read_file 快得多。",
        parameters={"type": "object",
                    "properties": {
                        "pattern": {"type": "string",
                                    "description": "正则表达式（Python re 语法）"},
                        "directory": {"type": "string",
                                      "description": "搜索目录，空则桌面"},
                        "include": {"type": "string",
                                    "description": "文件名 glob 过滤，如 *.py / *.txt；空表示所有"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 1000,
                                        "description": "最多返回多少条匹配行"},
                        "case_insensitive": {"type": "boolean",
                                             "description": "是否大小写不敏感（默认 False）"},
                    },
                    "required": ["pattern"]},
    )
    def grep_files(pattern: str, directory: str = "", include: str = "",
                   max_results: int = 100, case_insensitive: bool = False) -> str:
        """按正则递归搜索文件内容。"""
        if not pattern or not pattern.strip():
            return "错误：pattern 不能为空"
        try:
            flags = re.MULTILINE
            if case_insensitive:
                flags |= re.IGNORECASE
            rx = re.compile(pattern, flags)
        except re.error as e:
            return f"错误：正则表达式非法：{e}"
        if directory:
            base = Path(directory).expanduser()
        else:
            base = _HOME / "Desktop"
        if not _is_safe_path(base):
            return (
                f"错误：路径不在沙箱内（拒绝 {base}）。"
                f"默认只允许用户主目录（{_HOME}），"
                f"可在 config.yaml 加 llm.project_dirs 把项目目录放行。"
            )
        if not base.exists():
            return f"目录不存在：{base}"
        max_results = max(1, min(int(max_results if max_results else 100), 1000))

        hits: list[str] = []
        files_scanned = 0
        files_with_hit = 0
        truncated = False
        scan_truncated = False

        try:
            for p, visited, scan_trunc in _iter_text_files(base, include):
                if scan_trunc:
                    scan_truncated = True
                files_scanned += 1
                try:
                    # errors='replace' 防 latin-1 / gbk 编码文件名在桌面里误伤；
                    # grep 看的是内容，编码异常的文件可以整个跳过。
                    text = p.read_text(encoding="utf-8", errors="replace")
                except (OSError, UnicodeError):
                    continue
                matched_this_file = False
                for i, line in enumerate(text.splitlines(), start=1):
                    if rx.search(line):
                        hits.append(f"{p}:{i}:{line}")
                        matched_this_file = True
                        if len(hits) >= max_results:
                            truncated = True
                            break
                if matched_this_file:
                    files_with_hit += 1
                if truncated:
                    break
        except PermissionError as e:
            return f"错误：无权限扫描：{e}"
        except Exception as e:  # noqa: BLE001
            return f"错误：扫描失败：{e}"

        if not hits:
            scope = f"包含「{include}」的文件" if include else "所有文件"
            if scan_truncated or truncated:
                return (f"错误：在 {base} 已扫描 {files_scanned} 个 {scope} 后达到时间/数量上限，"
                        f"未找到匹配「{pattern}」的内容（结果不完整，不代表真的没有）。"
                        f"建议指定更具体的 directory 或 include。")
            return f"在 {base} 的 {scope} 中未找到匹配「{pattern}」的内容"

        header = f"找到 {len(hits)} 处匹配（涉及 {files_with_hit} 个文件，扫描 {files_scanned} 个）："
        footer_parts = []
        if truncated:
            footer_parts.append(f"（已达 max_results={max_results} 上限，结果可能不完整，"
                                f"建议缩小 directory 或加 include 过滤）")
        if scan_truncated:
            footer_parts.append(f"（扫描过程中达到 _MAX_VISIT={_MAX_VISIT} 或 {_TIME_BUDGET_S}s 时间上限，"
                                f"部分目录未遍历）")
        body = "\n".join(hits)
        if footer_parts:
            return f"{header}\n{body}\n" + "\n".join(footer_parts)
        return f"{header}\n{body}"

    reg.register(search_files)
    reg.register(grep_files)

    @tool(
        description="按 glob 路径模式（如 src/**/*.py）在指定目录下查找文件，按修改时间倒序。"
                    "沙箱限定在用户主目录下。比 search_files 更精确（用模式而非子串）。",
        parameters={"type": "object",
                    "properties": {
                        "pattern": {"type": "string",
                                    "description": "glob 模式（支持 ** 递归，**/*.py 这种）"},
                        "directory": {"type": "string",
                                      "description": "搜索根目录，绝对路径或 ~/xxx；空则桌面"},
                        "max_results": {"type": "integer", "minimum": 1, "maximum": 2000,
                                        "description": "最多返回多少个文件"},
                        "include_dirs": {"type": "boolean",
                                         "description": "是否包含目录（默认仅文件）"},
                    },
                    "required": ["pattern"]},
    )
    def glob_files(pattern: str, directory: str = "", max_results: int = 200,
                   include_dirs: bool = False) -> str:
        """按 glob 模式递归匹配文件路径。"""
        if not pattern or not pattern.strip():
            return "错误：pattern 不能为空"
        if directory:
            base = Path(directory).expanduser()
        else:
            base = _HOME / "Desktop"
        if not _is_safe_path(base):
            return (
                f"错误：路径不在沙箱内（拒绝 {base}）。"
                f"默认只允许用户主目录（{_HOME}），"
                f"可在 config.yaml 加 llm.project_dirs 把项目目录放行。"
            )
        if not base.exists():
            return f"目录不存在：{base}"
        max_results = max(1, min(int(max_results if max_results else 200), 2000))

        # glob 模式里用户写正斜杠很正常（"src/**/*.py"），但 pathlib 在 Windows 上
        # 严格按 OS 分隔符来；统一替换成 OS 的分隔符，行为可预测。
        import os as _os
        norm_pattern = pattern.replace("/", _os.sep)

        visited = 0
        deadline = time.monotonic() + _TIME_BUDGET_S
        truncated = False
        matches: list[tuple[Path, float]] = []
        try:
            for p in base.glob(norm_pattern):
                visited += 1
                if visited > _MAX_VISIT or time.monotonic() > deadline:
                    truncated = True
                    break
                if not include_dirs and not p.is_file():
                    continue
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    mtime = 0.0
                matches.append((p, mtime))
                if len(matches) >= max_results * 4:
                    # 多收一些再按 mtime 排序截断，避免「遍历到的第一个 pattern
                    # 命中都是旧文件、没截到的反而新」这种反直觉。
                    pass
        except (NotImplementedError, OSError) as e:
            return f"错误：glob 失败：{e}"
        except Exception as e:  # noqa: BLE001
            return f"错误：扫描失败：{e}"

        if not matches:
            if truncated:
                return (f"错误：在 {base} 用「{pattern}」扫描了 {visited} 个条目后达到时间/数量上限，"
                        f"未找到匹配文件（结果不完整，不代表真的没有）。"
                        f"建议指定更具体的 directory。")
            return f"在 {base} 用「{pattern}」未找到匹配文件"

        matches.sort(key=lambda t: t[1], reverse=True)  # mtime desc
        if len(matches) > max_results:
            truncated = True
            matches = matches[:max_results]

        lines = [f"找到 {len(matches)} 个匹配（按修改时间倒序，最多 {max_results}）："]
        for p, _ in matches:
            try:
                size = p.stat().st_size
                size_str = _fmt_size(size)
            except OSError:
                size_str = "?"
            kind = "/" if p.is_dir() else ""
            lines.append(f"  {p}{kind}  ({size_str})")
        if truncated:
            lines.append(f"（扫描/结果达到上限，部分匹配目录未遍历或被截断，建议缩小 directory）")
        return "\n".join(lines)

    reg.register(glob_files)


__all__ = ["register"]