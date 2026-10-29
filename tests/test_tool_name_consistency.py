"""工具名一致性回归锁：agent 里的三张名字表必须指向**真实注册**的工具名。

背景：app/brain/agent.py 里有三张按工具名索引的表
（_TOOL_PRETTY / _INTENT_TOOL_NAMES / _INTENT_BLOCKED_TOOLS）。
它们以前混着一批早就不存在的名字（take_screenshot / calculator /
get_system_info / memory_search / web_deep_search / set_timer / set_alarm /
add_todo），后果是：
  - 「空总结兜底」永远走不到对应分支；
  - 意图裁剪把真工具筛掉了（query 意图里写的是 take_screenshot，
    真名是 take_screenshot_tool）→ 截图工具在 query 意图下**根本不下发**。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.brain.agent import (  # noqa: E402
    _INTENT_BLOCKED_TOOLS, _INTENT_TOOL_NAMES, _TOOL_PRETTY,
)
from app.engine.tools import build_default_tools  # noqa: E402


class _State:
    def get(self, *a, **k):
        return None


@pytest.fixture(scope="module")
def registry_names() -> set[str]:
    """默认工具集（61 个内置工具）的真名集合。"""
    from app.core.companion_config import companion_config as cc

    reg = build_default_tools(state=_State(), reminders=None, memory=None,
                               cfg=cc)
    return set(reg.names())


#: 宿主侧工具（不在内置注册表里，但确实存在）—— 必须显式登记，不能悄悄漏
HOST_SIDE_TOOLS: dict[str, str] = {
    "mcp_aw_aw_status": "MCP server=aw 的外部工具（按 mcp_aw_ 前缀登记）",
    "mcp_aw_aw_get_current_window": "MCP server=aw 的外部工具",
    "mcp_aw_aw_get_recent_activities": "MCP server=aw 的外部工具",
    "mcp_aw_aw_get_browser_tabs": "MCP server=aw 的外部工具",
}


def _names_in_tables() -> set[str]:
    names: set[str] = set(_TOOL_PRETTY)
    for tools in _INTENT_TOOL_NAMES.values():
        names |= set(tools)
    for tools in _INTENT_BLOCKED_TOOLS.values():
        names |= set(tools)
    return names


def test_every_table_tool_name_exists_in_registry(registry_names):
    """三张表里的每个工具名都必须在注册表里（或在 HOST_SIDE_TOOLS 里登记）。"""
    unknown = sorted(n for n in _names_in_tables()
                     if n not in registry_names
                     and n not in HOST_SIDE_TOOLS)
    assert unknown == [], f"这些名字既不在注册表、也没登记为宿主侧工具：{unknown}"


def test_intent_whitelist_entries_all_exist(registry_names):
    """意图映射里的每个名字都要能落到真工具上（否则意图裁剪是死路径）。"""
    for intent, tools in _INTENT_TOOL_NAMES.items():
        for name in tools:
            assert (name in registry_names
                    or name in HOST_SIDE_TOOLS), f"{intent} 里的 {name} 不存在"


def test_screenshot_tool_is_reachable_in_query_intent(registry_names):
    """回归：query 意图必须能选中截图工具（真名 take_screenshot_tool）。"""
    assert "take_screenshot_tool" in registry_names
    assert "take_screenshot_tool" in _INTENT_TOOL_NAMES["query"], (
        "query 意图里写的是 take_screenshot（不存在）→ 截图工具永远选不中")
    assert "take_screenshot" not in _INTENT_TOOL_NAMES["query"]


def test_tool_pretty_keys_are_real_tools(registry_names):
    """_TOOL_PRETTY 的键必须是真名（否则空总结兜底走不到对应分支）。"""
    unknown = sorted(n for n in _TOOL_PRETTY if n not in registry_names)
    assert unknown == [], unknown
