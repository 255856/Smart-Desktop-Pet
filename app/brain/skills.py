"""Skills 系统 —— 让用户能以「加载行为说明」的方式增强 agent 能力。

对比 AGENTS.md (project_docs.py):
  - AGENTS.md 是「项目级硬规则」(始终生效,优先级高于默认行为)
  - Skill 是「可选行为包」(按名字加载,临时提升特定能力)

文件布局:
  <skills_dir>/
    code-review.md      ← 名字 code-review,内容是 markdown 行为说明
    test-writing.md
    explain-error.md
    ...

调用方式:
  - list_skills() -> 所有可用 skill 名字(按文件名前缀)
  - load_skill(name) -> 单个 skill 的内容(包装成 system prompt 片段)
  - load_skills(names=None) -> 多个 skill 内容拼起来
    (names=None 加载所有)
  - LangChainAgent 接入:启动时加载「默认 skills」(auto_load_names),
    每次消息按需自动追加 /skill <name> 触发的 skill

缓存:跟 project_docs 一样,按 (path, mtime) 缓存。
size cap:同 project_docs,64 KiB / 文件,32k 字符 / 解码后。

跟 project_docs 的关系:
  system_prompt = persona + project_docs (AGENTS.md) + skills + 反幻觉规则
  顺序很重要:persona 是身份,project_docs 是项目硬规则,skills 是行为补丁,
  反幻觉规则最后压底。任何一层都可以被主人显式对话覆盖(模型自己判断优先级)。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)


# 单文件 cap(与 project_docs 对齐)
_MAX_FILE_BYTES = 64 * 1024
_MAX_CHARS = 32_000

# 文件名 → skill 名字的转换规则:code-review.md -> "code-review"
_NAME_FROM_FILENAME = re.compile(r"^[a-zA-Z0-9_\-\.]+$")

# 缓存
_CACHE: dict[tuple[str, float], str] = {}


def _skill_name_from_path(p: Path) -> str:
    """code-review.md -> code-review"""
    return p.stem


def _read_with_cap(path: Path) -> str | None:
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
                "Skills: %s 太大 (%d bytes),截断到 %d",
                path, size, _MAX_FILE_BYTES,
            )
            with path.open("rb") as f:
                raw = f.read(_MAX_FILE_BYTES)
            text = raw.decode("utf-8", errors="replace")
            text += f"\n\n[... 已截断,文件原始 {size} 字节 ...]"
        else:
            text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        log.warning("Skills: 读 %s 失败: %s", path, e)
        return None
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS] + f"\n\n[... 已截断到 {_MAX_CHARS} 字符 ...]"
    _CACHE[key] = text
    return text


def _validate_name(name: str) -> bool:
    """skill 名字必须只含字母数字 / 下划线 / 连字符 / 点。"""
    return bool(name) and bool(_NAME_FROM_FILENAME.match(name))


def list_skills(skills_dir: Path | str) -> list[str]:
    """列出 skills_dir 下所有 .md 文件对应的 skill 名(按字母序)。"""
    d = Path(skills_dir)
    if not d.is_dir():
        return []
    out: list[str] = []
    for p in sorted(d.glob("*.md")):
        n = _skill_name_from_path(p)
        if _validate_name(n):
            out.append(n)
    return out


def skill_description(skills_dir: Path | str, name: str) -> str:
    """技能的**一句话说明**（给提示词里的技能清单用）。

    取文件里第一行 `# Skill: xxx` 之后的第一段非空文字；
    超过 200 字就截断。读不到就返回空串。
    """
    if not _validate_name(name):
        return ""
    p = Path(skills_dir) / (name + ".md")
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    lines = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#") or s.startswith(">") or s.startswith("---"):
            continue
        lines.append(s)
        if len(" ".join(lines)) > 200:
            break
    return " ".join(lines)[:200].strip()


def load_skill(skills_dir: Path | str, name: str) -> str | None:
    """按名字加载单个 skill 的内容(markdown 原文,加一点头标)。"""
    if not _validate_name(name):
        log.warning("Skills: 非法名字 %r", name)
        return None
    p = Path(skills_dir) / f"{name}.md"
    text = _read_with_cap(p)
    if text is None:
        return None
    # 加 header 让模型能识别这是「行为补丁」
    return f"[Skill: {name}]\n{text}\n[/Skill]"


def load_skills(
    skills_dir: Path | str,
    names: Optional[Iterable[str]] = None,
) -> str:
    """加载多个 skill,拼成一段 system prompt 片段。

    names 为 None:加载目录下全部 skill(数量大时不推荐,留给「全开」场景)。
    否则按给定顺序加载,缺失的跳过 + log.warning。

    返回:拼接好的字符串;空目录 / 全部缺失返回空串。
    """
    if names is None:
        names = list_skills(skills_dir)
    sections: list[str] = []
    for n in names:
        content = load_skill(skills_dir, n)
        if content is None:
            log.warning("Skills: %s 不存在或读失败,跳过", n)
            continue
        sections.append(content)
    if not sections:
        return ""
    header = (
        "\n\n[以下是主人激活的 Skills(行为说明)。"
        "激活的 skill 优先级等同于主人明确指令,但低于对话中的即时纠正。"
        "模型应当遵守 skill 描述的行为规范,但如果主人明确说「不用这个 skill 了」,"
        "立即让该 skill 失效。]\n"
    )
    body = "\n\n".join(sections)
    return header + body + "\n[Skills 结束。]\n"


def clear_cache() -> None:
    _CACHE.clear()


__all__ = [
    "list_skills",
    "load_skill",
    "load_skills",
    "clear_cache",
]