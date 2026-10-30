# -*- coding: utf-8 -*-
"""【记忆规则】与「记忆工具永远在工具列表里」的回归用例。

两条不变量：
  1. 【记忆规则】是**常量**字符串：住在稳定通道（system prompt）里，
     不随本轮 query / 时间 / 状态变 —— 它一变，MiniMax 的 8192-token
     前缀缓存第一块就作废（实测缓存命中率 98.9% → 1.5%）。
  2. 记忆三件套**永远**在本轮下发的工具列表里：【记忆规则】是每轮都在的
     行为规则，主人随口说的耐久事实可能出现在任何一轮，
     不能因为本轮意图被裁掉（列表里没有 = 模型调不到）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.brain.agent import AgentLoop, _ALWAYS_TOOL_NAMES  # noqa: E402
from app.brain.brain_controller import MEMORY_RULE_BLOCK  # noqa: E402


# ── 1) 规则是常量 ─────────────────────────────────────────────────────────
def test_memory_rule_block_is_a_constant_string():
    """常量：同一份内容、长度有界、不含每轮变的东西。"""
    from app.brain import brain_controller as bc

    assert isinstance(bc.MEMORY_RULE_BLOCK, str)
    assert bc.MEMORY_RULE_BLOCK is MEMORY_RULE_BLOCK
    # 稳定通道里放的是**同一个常量对象**（不是每轮拼出来的字符串）
    assert bc.MEMORY_RULE_BLOCK == MEMORY_RULE_BLOCK
    # 有界：前缀缓存第一块别被它撑爆（原来 303 字，改动后仍然有界）
    assert len(MEMORY_RULE_BLOCK) < 700, len(MEMORY_RULE_BLOCK)
    # 不许带每轮变的东西（时间戳 / 状态）
    for bad in ("[当前时间:", "【当前时间】", "【你的实时状态】", "星期"):
        assert bad not in MEMORY_RULE_BLOCK, bad


def test_memory_rule_block_keeps_the_three_categories():
    """规则要说清「记什么」：主人讲过的事 / 自己的承诺立场 / 共同经历。"""
    assert "remember_fact" in MEMORY_RULE_BLOCK
    assert "①" in MEMORY_RULE_BLOCK
    assert "②" in MEMORY_RULE_BLOCK
    assert "③" in MEMORY_RULE_BLOCK
    assert "supersedes" in MEMORY_RULE_BLOCK
    # ②必须记（qq-bridge 的 memoryRuleLine 同一口径）
    assert "必须记" in MEMORY_RULE_BLOCK


def test_memory_rule_block_is_byte_identical_across_calls():
    """连续读两次必须逐字节相同（它进 system prompt，变了就掉缓存）。"""
    from app.brain import brain_controller as bc

    a = bc.MEMORY_RULE_BLOCK
    b = bc.MEMORY_RULE_BLOCK
    assert a.encode("utf-8") == b.encode("utf-8")


# ── 2) 记忆工具永远在工具列表里 ───────────────────────────────────────────
def _tool_list(*names):
    return [{"type": "function", "function": {"name": n}} for n in names]


@pytest.mark.parametrize("intent", [
    None, "", "query", "open_app", "add_reminder", "remember_fact",
    "get_weather", "web_search", "aw_query",
])
def test_memory_tools_survive_the_intent_filter(intent):
    """任何意图下，记忆三件套都在本轮工具列表里。"""
    loop = AgentLoop(client=None, registry=None)
    tools = _tool_list(
        "get_current_time", "get_pet_status", "remember_fact",
        "recall_memory", "forget_memory", "open_app", "web_search",
    )
    picked = loop._select_tools(intent, tools)
    names = {t["function"]["name"] for t in picked}
    for want in _ALWAYS_TOOL_NAMES:
        assert want in names, "意图 %r 下 %s 被裁掉了" % (intent, want)


def test_always_tool_names_is_exactly_the_memory_trio():
    assert set(_ALWAYS_TOOL_NAMES) == {"remember_fact", "recall_memory", "forget_memory"}
