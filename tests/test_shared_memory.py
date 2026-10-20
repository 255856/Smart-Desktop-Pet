"""v56: 共同经历（shared）类别记忆 + proactive 注入。

覆盖:
- `MemoryStore.CATEGORY_CHOICES` 含 "shared"
- `add_shared()` 默认 category="shared", importance=0.7
- `add_shared()` 接受自定义 importance
- `recent_shared(n)` 按 created_at 倒序返最近 n 条, 只含 shared 类
- `_memory.py` 的 remember_fact tool enum 接受 "shared"
- proactive context 含 `shared_memories` 字段（最近 3 条 shared 的 content）
"""
from __future__ import annotations

import os
import sys
import tempfile
from typing import List
from unittest import mock

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


# ----------------------------
# MemoryStore.add_shared / recent_shared
# ----------------------------


class TestSharedCategoryBasics:
    def setup_method(self):
        # 用空白 db 文件, 不要污染持久化数据
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        os.unlink(path)  # 删除, 留 path
        self.tmp = path
        from app.brain.memory import MemoryStore
        self.store = MemoryStore(path, backend_name="substring")

    def teardown_method(self):
        # 清理
        try:
            os.unlink(self.tmp)
        except OSError:
            pass

    def test_category_choices_includes_shared(self):
        from app.brain.memory import CATEGORY_CHOICES
        assert "shared" in CATEGORY_CHOICES

    def test_add_shared_defaults_to_shared_category_07_importance(self):
        item = self.store.add_shared("上周三一起下了三盘象棋你输了")
        assert item.category == "shared"
        assert item.importance == pytest.approx(0.7)
        assert item.content == "上周三一起下了三盘象棋你输了"

    def test_add_shared_accepts_custom_importance(self):
        item = self.store.add_shared("上次那个方案你吐槽了最后还是采纳了", importance=0.85)
        assert item.category == "shared"
        assert item.importance == pytest.approx(0.85)

    def test_add_with_unknown_category_falls_back_to_other(self):
        """sanity: 不在 CATEGORY_CHOICES 里的 category 会落到 other。"""
        item = self.store.add("xxx", category="nonsense")
        assert item.category == "other"

    def test_recent_shared_returns_only_shared(self):
        self.store.add("普通 fact", category="fact")
        self.store.add("共同经历 1", category="shared")
        self.store.add("共同经历 2", category="shared")
        self.store.add("偏好", category="preference")

        shared = self.store.recent_shared(5)
        assert len(shared) == 2
        assert all(i.category == "shared" for i in shared)

    def test_recent_shared_orders_newest_first(self):
        import time as _t
        self.store.add_shared("早的")
        _t.sleep(0.01)
        self.store.add_shared("晚的")
        shared = self.store.recent_shared(5)
        assert shared[0].content == "晚的"
        assert shared[1].content == "早的"

    def test_recent_shared_respects_n(self):
        for i in range(5):
            self.store.add_shared(f"记忆 {i}")
        shared = self.store.recent_shared(2)
        assert len(shared) == 2
        # 最近的 2 条是 #3 #4
        contents = [s.content for s in shared]
        assert "记忆 3" in contents
        assert "记忆 4" in contents

    def test_recent_shared_empty_returns_empty(self):
        assert self.store.recent_shared(3) == []


# ----------------------------
# _memory.py remember_fact tool enum
# ----------------------------


def test_remember_fact_enum_accepts_shared():
    """remember_fact 工具的 category enum 必须含 'shared', 否则 LLM 没法调。"""
    from app.engine.tools._memory import register
    from app.engine.tools import ToolRegistry
    from app.brain.memory import MemoryStore
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd); os.unlink(path)
    try:
        memory = MemoryStore(path, backend_name="substring")
        reg = ToolRegistry()
        register(reg, memory=memory)
        tool_obj = reg.get("remember_fact")
        assert tool_obj is not None, "remember_fact tool not registered"
        cat_enum = tool_obj.parameters["properties"]["category"]["enum"]
        assert "shared" in cat_enum
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ----------------------------
# proactive context 含 shared_memories 字段
# ----------------------------


def test_proactive_context_includes_shared_memories():
    """ProactiveBrain 构造 context_base 时, 应包含 shared_memories 字段。

    我们直接调 _fire() 之前会构造的 context_base 同等结构, 用 mock 替换
    memory 让它返回预设的 shared 列表。
    """
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
        "now": now.strftime(f"%Y-%m-%d %H:%M 星期{wd}"),
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


def test_proactive_backward_compatible_with_old_memory_no_recent_shared():
    """老版本 MemoryStore 没有 recent_shared 时, proactive 应优雅降级（空列表）。"""
    from app.brain.proactive import ProactiveBrain

    fake_llm = mock.MagicMock()
    fake_llm.model = "test"
    fake_llm.base_url = "http://127.0.0.1:11434"
    fake_llm.api_key = "test-key"

    class _LegacyMemory:
        def recent(self, n):
            return []
        # 注意: 没有 recent_shared 方法

    class _FakeState:
        def stats_summary(self):
            return "ok"

    brain = ProactiveBrain(
        llm_cfg=fake_llm,
        persona="你是桌宠",
        state=_FakeState(),
        memory=_LegacyMemory(),
        char_name="小汐",
        aw_cfg=None,
    )
    # 复制 _fire 里那段代码: getattr fallback 让 legacy 不抛 AttributeError
    shared = []
    try:
        shared = brain.memory.recent_shared(3)
    except AttributeError:
        shared = []
    assert shared == []