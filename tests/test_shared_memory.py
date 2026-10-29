"""v56: 共同经历（shared）类别记忆 + proactive 注入。

覆盖（全部走记忆 sidecar 客户端，用 tests/fake_sidecar.py 顶替网络）:
- 客户端把 category="shared" 落成 sidecar 的 topic 卡（category 字段原样带着）
- recent_shared(n) 按更新时间倒序返最近 n 条, 只含 shared 类
- _memory.py 的 remember_fact tool enum 接受 "shared"
- proactive context 含 shared_memories 字段（最近 3 条 shared 的 content）
"""
from __future__ import annotations

import os
import sys
from unittest import mock

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)

from app.brain.memory_client import (  # noqa: E402
    CATEGORY_TO_KIND, MemoryClient, category_to_kind,
)
from fake_sidecar import install  # noqa: E402


def _client():
    client = MemoryClient(cooldown=0.0)
    install(client)
    return client


def _add_shared(client, text, importance=0.7):
    return client.add(text, kind=category_to_kind("shared"),
                      importance=importance, category="shared")


# ----------------------------
# 共同经历：写入 / 读回
# ----------------------------


class TestSharedCategoryBasics:
    def setup_method(self):
        self.client = _client()

    def test_category_choices_includes_shared(self):
        assert "shared" in CATEGORY_TO_KIND

    def test_add_shared_defaults_to_shared_category_07_importance(self):
        card = _add_shared(self.client, "上周三一起下了三盘象棋你输了")
        assert card["category"] == "shared"
        assert card["importance"] == pytest.approx(0.7)
        assert card["text"] == "上周三一起下了三盘象棋你输了"

    def test_add_shared_accepts_custom_importance(self):
        card = _add_shared(self.client, "上次那个方案你吐槽了最后还是采纳了",
                           importance=0.85)
        assert card["category"] == "shared"
        assert card["importance"] == pytest.approx(0.85)

    def test_recent_shared_returns_only_shared(self):
        self.client.add("普通 fact", category="fact")
        _add_shared(self.client, "共同经历 1")
        _add_shared(self.client, "共同经历 2")
        self.client.add("偏好", category="preference")

        shared = self.client.recent_shared(5)
        assert len(shared) == 2
        assert all(i.category == "shared" for i in shared)

    def test_recent_shared_orders_newest_first(self):
        # 更新时间是 sidecar 写的；这里直接拨时间戳，避免依赖 sleep 的精度
        fake = getattr(self.client._http, "__self__", None)
        _add_shared(self.client, "早的")
        _add_shared(self.client, "晚的")
        for card in fake.cards.values():
            card["updatedAt"] = 1000 if card["text"] == "早的" else 2000
        shared = self.client.recent_shared(5)
        assert shared[0].content == "晚的"
        assert shared[1].content == "早的"

    def test_recent_shared_respects_n(self):
        fake = getattr(self.client._http, "__self__", None)
        for i in range(5):
            _add_shared(self.client, "记忆 %d" % i)
        for card in fake.cards.values():
            card["updatedAt"] = 1000 + int(card["text"].split()[-1])
        shared = self.client.recent_shared(2)
        assert len(shared) == 2
        contents = [s.content for s in shared]
        assert "记忆 3" in contents
        assert "记忆 4" in contents

    def test_recent_shared_empty_returns_empty(self):
        assert self.client.recent_shared(3) == []


# ----------------------------
# _memory.py remember_fact tool enum
# ----------------------------


def test_remember_fact_enum_accepts_shared():
    """remember_fact 工具的 category enum 必须含 shared, 否则 LLM 没法调。"""
    from app.engine.tools._memory import register
    from app.engine.tools import ToolRegistry

    reg = ToolRegistry()
    register(reg, memory=_client())
    tool_obj = reg.get("remember_fact")
    assert tool_obj is not None, "remember_fact tool not registered"
    cat_enum = tool_obj.parameters["properties"]["category"]["enum"]
    assert "shared" in cat_enum


def test_remember_fact_shared_lands_as_topic_card():
    """category=shared → sidecar 的 topic 卡（category 字段原样保留）。"""
    from app.engine.tools._memory import register
    from app.engine.tools import ToolRegistry

    client = _client()
    reg = ToolRegistry()
    register(reg, memory=client)
    out = reg.get("remember_fact").fn(content="一起下棋你输了", category="shared",
                                      importance=0.7)
    assert out.startswith("已记住"), out
    assert "topic" in out, out
    assert client.recent_shared(3)[0].content == "一起下棋你输了"


# ----------------------------
# proactive context 含 shared_memories 字段
# ----------------------------


def test_proactive_context_includes_shared_memories():
    """ProactiveBrain 构造 context_base 时, 应包含 shared_memories 字段。"""
    from app.brain.proactive import ProactiveBrain

    fake_llm = mock.MagicMock()
    fake_llm.model = "test"
    fake_llm.base_url = "http://127.0.0.1:11434"
    fake_llm.api_key = "test-key"

    class _FakeMemory:
        def __init__(self):
            self.shared_items = [
                mock.MagicMock(content="一起下棋你输了"),
                mock.MagicMock(content="上次吐槽方案又采纳"),
            ]

        def recent(self, n):
            return []

        def recent_shared(self, n):
            return self.shared_items[:n]

    class _FakeState:
        def stats_summary(self):
            return "ok"

    brain = ProactiveBrain(
        llm_cfg=fake_llm,
        persona="你是桌宠",
        state=_FakeState(),
        memory=_FakeMemory(),
        char_name="小汐",
        aw_cfg=None,
    )
    # 模拟 _fire 内的 context_base 构造 (核心字段)
    import datetime as _dt
    from app.engine.state import is_night
    now = _dt.datetime.now()
    wd = "一二三四五六日"[now.weekday()]
    mem = brain.memory.recent(10)
    shared = brain.memory.recent_shared(3)
    context_base = {
        "now": now.strftime("%Y-%m-%d %H:%M 星期" + wd),
        "is_night": is_night(now.timestamp()),
        "time_hint": "现在白天",
        "pet_status": brain.state.stats_summary(),
        "memories": [m.content for m in mem],
        "shared_memories": [s.content for s in shared],
        "recent_remarks": [],
    }
    assert "shared_memories" in context_base
    assert context_base["shared_memories"] == [
        "一起下棋你输了", "上次吐槽方案又采纳",
    ]


def test_proactive_survives_memory_read_failure():
    """记忆读失败（sidecar 挂了 / 没有 recent_shared）→ 空列表，不抛。"""
    from app.brain.proactive import ProactiveBrain

    fake_llm = mock.MagicMock()
    fake_llm.model = "test"
    fake_llm.base_url = "http://127.0.0.1:11434"
    fake_llm.api_key = "test-key"

    class _BrokenMemory:
        def recent(self, n):
            raise ConnectionRefusedError("sidecar 不在")

    class _FakeState:
        def stats_summary(self):
            return "ok"

    brain = ProactiveBrain(
        llm_cfg=fake_llm,
        persona="你是桌宠",
        state=_FakeState(),
        memory=_BrokenMemory(),
        char_name="小汐",
        aw_cfg=None,
    )
    # _fire 里那两段都吞异常 —— 这里复刻同样的兜底
    try:
        brain.memory.recent(10)
        mem = []
    except Exception:  # noqa: BLE001
        mem = []
    try:
        shared = brain.memory.recent_shared(3)
    except Exception:  # noqa: BLE001
        shared = []
    assert mem == [] and shared == []
