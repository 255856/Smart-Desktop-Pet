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
MCP_SERVER_LEVELS: dict = {}
MCP_DEFAULT_LEVEL: int = 8

_LOADED = False


def load_unlock_config(cfg=None) -> None:
    global MCP_SERVER_LEVELS
    """加载 tool_unlock 配置。多次调用幂等。

    cfg: 传入的 companion_config 对象。若为 None 或没有 tool_unlock 字段
        (比如误传了主配置 config.yaml 的 Config),自动回落到
        app.core.companion_config 的 singleton —— 那里才是 tool_unlock 的家。
    """
    global TOOL_UNLOCK, UNLOCK_REWARDS, UNLOCK_CELEBRATE, MCP_DEFAULT_LEVEL, _LOADED
    if _LOADED:
        return
    block = getattr(cfg, "tool_unlock", None)
    if block is None:
        # cfg 不带 tool_unlock(传了主配置 / None)→ 回落到 companion_config
        try:
            from app.core.companion_config import companion_config as _cc
            block = getattr(_cc, "tool_unlock", None)
        except Exception as e:  # noqa: BLE001
            log.warning("工具解锁: companion_config 读取失败 %s, 全部工具可见", e)
            block = None
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
    # 每个 MCP server 可以单独定级：playwright 的 25 个工具 schema 就有
    # 9.5k token，放高一点，平时不进 prompt（要用时主人自己调高）。
    _lv = getattr(block, "mcp_levels", None) or {}
    try:
        MCP_SERVER_LEVELS = {str(k): int(v) for k, v in dict(_lv).items()}
    except (TypeError, ValueError, AttributeError):
        MCP_SERVER_LEVELS = {}
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


def short_mcp_name(name: str) -> str:
    """MCP 注册名 → UI 显示名，去掉重复的 server 段。

    bridge_to 里拼的是 f"mcp_{server}_{tool.name}"，而外部 tool.name 本身
    常已带 server 前缀（mcp_aw_aw_status ← server=aw, name=aw_status），
    直接显示会出现 aw 重复两次。这里去掉重复段，只用于展示。
    """
    if not name.startswith("mcp_"):
        return name
    parts = name.split("_", 2)
    if len(parts) < 3:
        return name
    server, tool_name = parts[1], parts[2]
    if tool_name == server or tool_name.startswith(f"{server}_"):
        tool_name = tool_name[len(server) + 1:]
    return f"{server}_{tool_name}"


def mcp_server_of(name: str) -> Optional[str]:
    """从 mcp_<server>_<tool> 里取出 server 名。"""
    if not name.startswith("mcp_"):
        return None
    rest = name[4:]
    if "_" not in rest:
        return None
    return rest.split("_", 1)[0]


def level_for_tool(name: str) -> Optional[int]:
    """反查工具名对应的最低解锁等级。None 表示未在配置里 (按 SAFE 处理 → Lv1 解锁)。"""
    if is_mcp_tool(name):
        srv = mcp_server_of(name)
        if srv and srv in MCP_SERVER_LEVELS:
            return MCP_SERVER_LEVELS[srv]
        return MCP_DEFAULT_LEVEL
    for lvl in sorted(TOOL_UNLOCK.keys()):
        if name in TOOL_UNLOCK[lvl]:
            return lvl
    return None


def is_unlocked(name: str, level: int) -> bool:
    """检查工具是否在给定等级下对 LLM 可见。"""
    if is_mcp_tool(name):
        req = level_for_tool(name)
        return level >= (req if req is not None else MCP_DEFAULT_LEVEL)
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
    "is_mcp_tool", "short_mcp_name", "split_configured_and_mcp",
]