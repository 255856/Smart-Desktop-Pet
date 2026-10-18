"""工具解锁:战令式 LLM 工具可见性控制。

读 companion_config.yaml 的 tool_unlock 段,提供:
  - compute_unlocked(level) -> set[str]: 该等级下 LLM 可见的工具名集合
  - level_for_tool(name) -> int | None: 工具所属最低解锁等级 (None = 未配置,默认 SAFE)
  - UNLOCK_REWARDS: dict[int, str]: 等级 → 奖励标题
  - UNLOCK_CELEBRATE: dict[int, str]: 等级 → 祝贺语
  - MCP_DEFAULT_LEVEL: int: MCP 外部工具默认锁的等级
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)


# 单源真相 (从 yaml 加载;若 yaml 未配置则用空 dict,ToolRegistry 退化为全部可见)
TOOL_UNLOCK: dict[int, list[str]] = {}
UNLOCK_REWARDS: dict[int, str] = {}
UNLOCK_CELEBRATE: dict[int, str] = {}
MCP_DEFAULT_LEVEL: int = 8

_LOADED = False


def load_unlock_config(cfg) -> None:
    """从 companion_config 加载 tool_unlock 段。多次调用幂等。"""
    global TOOL_UNLOCK, UNLOCK_REWARDS, UNLOCK_CELEBRATE, MCP_DEFAULT_LEVEL, _LOADED
    if _LOADED:
        return
    block = getattr(cfg, "tool_unlock", None)
    if block is None:
        log.warning("tool_unlock 段未配置,所有工具始终可见 (兼容旧版)")
        _LOADED = True
        return
    levels = getattr(block, "levels", None) or {}
    for lvl, names in levels.items():
        try:
            lvl_int = int(lvl)
        except (TypeError, ValueError):
            continue
        if not isinstance(names, (list, tuple)):
            continue
        # 去重,保留配置顺序
        seen: set[str] = set()
        cleaned: list[str] = []
        for n in names:
            if isinstance(n, str) and n and n not in seen:
                seen.add(n)
                cleaned.append(n)
        if cleaned:
            TOOL_UNLOCK[lvl_int] = cleaned
    rewards = getattr(block, "rewards_title", None) or {}
    for lvl, title in rewards.items():
        try:
            UNLOCK_REWARDS[int(lvl)] = str(title)
        except (TypeError, ValueError):
            continue
    celeb = getattr(block, "celebrate", None) or {}
    for lvl, msg in celeb.items():
        try:
            UNLOCK_CELEBRATE[int(lvl)] = str(msg)
        except (TypeError, ValueError):
            continue
    try:
        MCP_DEFAULT_LEVEL = int(getattr(block, "mcp_default_level", 8) or 8)
    except (TypeError, ValueError):
        MCP_DEFAULT_LEVEL = 8
    _LOADED = True
    log.info("工具解锁配置已加载: %d 级, Lv1 解锁 %d 个, Lv10 解锁 %d 个",
             len(TOOL_UNLOCK),
             len(TOOL_UNLOCK.get(1, [])),
             sum(len(v) for v in TOOL_UNLOCK.values()))


def is_loaded() -> bool:
    return _LOADED


def _flatten_unlocked(max_level: int) -> set[str]:
    """返回 <= max_level 的所有工具集合。"""
    out: set[str] = set()
    for lvl, names in TOOL_UNLOCK.items():
        if lvl <= max_level:
            out.update(names)
    return out


def compute_unlocked(level: int, *, include_mcp: bool = True,
                     mcp_names: Optional[list[str]] = None) -> set[str]:
    """给定等级,返回 LLM 可见的工具名集合。

    include_mcp: True 时把 MCP 工具加入(等级 >= MCP_DEFAULT_LEVEL 才可见)。
    mcp_names: MCP 工具的真实注册名列表(以 mcp_ 前缀开头)。
    """
    out = _flatten_unlocked(level)
    if include_mcp and level >= MCP_DEFAULT_LEVEL:
        for nm in (mcp_names or []):
            out.add(nm)
    return out


def is_mcp_tool(name: str) -> bool:
    """工具名是否是 MCP 外部工具 (MCPClientRegistry.bridge_to 用 mcp_ 前缀)。"""
    return name.startswith("mcp_")


def level_for_tool(name: str) -> Optional[int]:
    """反查工具名对应的最低解锁等级。None 表示未在配置里 (按 SAFE 处理 → Lv1 解锁)。"""
    if is_mcp_tool(name):
        return MCP_DEFAULT_LEVEL
    for lvl in sorted(TOOL_UNLOCK.keys()):
        if name in TOOL_UNLOCK[lvl]:
            return lvl
    return None


def is_unlocked(name: str, level: int) -> bool:
    """检查工具是否在给定等级下对 LLM 可见。"""
    if is_mcp_tool(name):
        return level >= MCP_DEFAULT_LEVEL
    req = level_for_tool(name)
    if req is None:
        return True  # 未配置的默认可见 (Lv1 解锁)
    return level >= req


def split_configured_and_mcp(names: list[str]) -> tuple[set[str], list[str]]:
    """拆分工具名为 (yaml 已配置的, MCP 外部工具列表)。"""
    configured: set[str] = set()
    mcp: list[str] = []
    for n in names:
        if is_mcp_tool(n):
            mcp.append(n)
        else:
            configured.add(n)
    return configured, mcp


__all__ = [
    "TOOL_UNLOCK", "UNLOCK_REWARDS", "UNLOCK_CELEBRATE", "MCP_DEFAULT_LEVEL",
    "load_unlock_config", "is_loaded",
    "compute_unlocked", "level_for_tool", "is_unlocked",
    "is_mcp_tool", "split_configured_and_mcp",
]