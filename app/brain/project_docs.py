"""AGENTS.md / CLAUDE.md 自动加载(对齐 Claude Code 的项目级指令机制)。

行为:
  - 给定 cwd,从 cwd 开始向上走到 HOME,寻找 AGENTS.md 或 CLAUDE.md。
  - 找到的第一个(最近 cwd 的)会被读取。
  - 同时支持显式 extra_paths 参数(允许主程序注入额外指令源)。
  - 每个文件 size cap(默认 64 KiB),超大文件截断并打 WARNING。
  - 用 (path, mtime) 做缓存 —— 文件没变就不重读。
  - 返回的字符串可以直接拼到 system_prompt,带清晰的「项目指令」分隔符,
    让模型能看到这是「项目配置」而非人设。

文件优先级:
  - 在同一目录下,AGENTS.md 优先于 CLAUDE.md
    (前者是跨 agent 通用约定,后者是 Claude Code 专属 —— 桌宠不应该锁死 vendor)
  - cwd 离 HOME 越近的目录,优先级越高(更「本地」的指令)

调用方:
  - LangChainAgent.__init__ 增加 cwd 参数(可选)与 extra_docs_paths(可选)
  - grounding_dynamic_prompt 把 load_project_docs() 的结果拼在 persona 之后、
    反幻觉规则之前
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


# 默认文件名(按优先级)
_DEFAULT_NAMES = ("AGENTS.md", "CLAUDE.md")

# 单文件 size cap。超过就截断,避免几十 MB 的指令文件一次塞进 system prompt
# 把 context 窗口吃光。
_MAX_FILE_BYTES = 64 * 1024   # 64 KiB

# 单文件字符截断(utf-8 解码后),防止 half-character / surrogate 等问题
_MAX_CHARS = 32_000

# 模块级缓存,key = (path, mtime) → content(str)
_CACHE: dict[tuple[str, float], str] = {}


def _read_with_cap(path: Path) -> str | None:
    """读一个文件,size cap 后返回字符串。失败返回 None。

    缓存基于 (path, mtime),避免反复 IO。
    """
    try:
        st = path.stat()
    except OSError:
        return None
    key = (str(path.resolve()), st.st_mtime)
    if key in _CACHE:
        return _CACHE[key]
    try:
        size = st.st_size
        if size > _MAX_FILE_BYTES:
            log.warning(
                "ProjectDocs: %s 太大 (%d bytes),截断到 %d",
                path, size, _MAX_FILE_BYTES,
            )
            with path.open("rb") as f:
                raw = f.read(_MAX_FILE_BYTES)
            text = raw.decode("utf-8", errors="replace")
            text += f"\n\n[... 已截断,文件原始 {size} 字节,只读前 {len(raw)} 字节 ...]"
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.warning("ProjectDocs: 读 %s 失败: %s", path, e)
        return None
    if len(text) > _MAX_CHARS:
        log.warning(
            "ProjectDocs: %s 解码后字符数 %d,截断到 %d",
            path, len(text), _MAX_CHARS,
        )
        text = text[:_MAX_CHARS] + f"\n\n[... 已截断到 { _MAX_CHARS} 字符 ...]"
    _CACHE[key] = text
    return text


def _find_in_ancestors(cwd: Path, stop: Path | None = None) -> Path | None:
    """从 cwd 向上走到 stop(默认 HOME),找第一个 AGENTS.md / CLAUDE.md。

    stop 默认用 Path.home();走到 stop 或文件系统根就停。
    """
    if stop is None:
        try:
            stop = Path.home()
        except RuntimeError:
            stop = None
    cur = cwd.resolve()
    while True:
        for name in _DEFAULT_NAMES:
            p = cur / name
            if p.is_file():
                return p
        if cur.parent == cur:
            return None   # 文件系统根
        if stop is not None and cur == stop.resolve():
            return None
        cur = cur.parent


def load_project_docs(
    cwd: Path | str | None = None,
    extra_paths: list[Path | str] | None = None,
    stop_at: Path | str | None = None,
) -> str:
    """加载项目级指令文档,返回拼接好的字符串(可直接拼到 system_prompt)。

    Args:
        cwd: 当前工作目录。从这里向上找 AGENTS.md / CLAUDE.md。
             None 表示不自动找(只读 extra_paths)。
        extra_paths: 额外的指令文件路径列表(主程序注入)。
        stop_at: 向上查找的停止目录。None = HOME。

    Returns:
        拼接好的字符串;没找到任何文件时返回空串。
        格式:
            [项目指令 — <path1>]
            <content1>

            [项目指令 — <path2>]
            <content2>
    """
    sections: list[str] = []
    seen: set[str] = set()   # 防重复(extra_paths 跟自动找到的可能撞)

    # 1) 自动从 cwd 向上找
    if cwd is not None:
        cwd_p = Path(cwd) if not isinstance(cwd, Path) else cwd
        stop_p = Path(stop_at) if stop_at and not isinstance(stop_at, Path) else stop_at
        try:
            found = _find_in_ancestors(cwd_p, stop=stop_p)
        except Exception as e:  # noqa: BLE001
            log.warning("ProjectDocs: 查找 cwd 失败: %s", e)
            found = None
        if found is not None:
            text = _read_with_cap(found)
            if text:
                key = str(found.resolve())
                seen.add(key)
                sections.append(f"[项目指令 — {found}]\n{text}")

    # 2) extra_paths
    if extra_paths:
        for p in extra_paths:
            pp = Path(p) if not isinstance(p, Path) else p
            try:
                pp_resolved = str(pp.resolve())
            except OSError:
                continue
            if pp_resolved in seen:
                continue
            if not pp.is_file():
                log.warning("ProjectDocs: extra path 不存在或不是文件: %s", pp)
                continue
            text = _read_with_cap(pp)
            if text:
                seen.add(pp_resolved)
                sections.append(f"[项目指令 — {pp}]\n{text}")

    if not sections:
        return ""
    body = "\n\n".join(sections)
    # 头部标头让模型能识别这是「项目级配置」,不是主程序人设的一部分
    header = (
        "\n\n[以下是主人配置的项目级指令,优先级高于默认行为,但低于主人直接对话;"
        "遵循主人明确指令优先,遵循项目指令次之,自由发挥兜底。]\n"
    )
    return header + body + "\n[项目指令结束。 ]\n"


def clear_cache() -> None:
    """清缓存(主程序在 reload 配置时调用,或测试 teardown)。"""
    _CACHE.clear()


__all__ = ["load_project_docs", "clear_cache"]