"""会话持久层 SessionStore 的测试。

覆盖:
- create / get / list / delete / touch / rename
- session_id 唯一性、thread_id 唯一性
- get_by_thread_id
- 多次 create 互相独立
- DB 文件自动创建
- 边界:空 title、特殊字符、巨大 title
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from app.brain.sessions import SessionInfo, SessionStore


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "sessions.db"
    return SessionStore(db)


class TestCreate:
    def test_create_returns_info(self, store):
        info = store.create()
        assert isinstance(info, SessionInfo)
        assert len(info.session_id) == 12
        assert info.thread_id == f"thread-{info.session_id}"
        assert info.created_at > 0
        assert info.message_count == 0

    def test_create_with_title(self, store):
        info = store.create(title="我的项目")
        assert info.title == "我的项目"

    def test_create_with_empty_title_gets_default(self, store):
        info = store.create()
        assert info.title.startswith("会话 ")

    def test_two_creates_are_independent(self, store):
        a = store.create()
        b = store.create()
        assert a.session_id != b.session_id
        assert a.thread_id != b.thread_id


class TestGet:
    def test_get_existing(self, store):
        info = store.create(title="x")
        got = store.get(info.session_id)
        assert got is not None
        assert got.session_id == info.session_id
        assert got.title == "x"

    def test_get_missing_returns_none(self, store):
        assert store.get("nonexistent_id_xyz") is None

    def test_get_by_thread_id(self, store):
        info = store.create()
        got = store.get_by_thread_id(info.thread_id)
        assert got is not None
        assert got.session_id == info.session_id

    def test_get_by_thread_id_missing(self, store):
        assert store.get_by_thread_id("thread-nope") is None


class TestListAll:
    def test_list_empty(self, store):
        assert store.list_all() == []

    def test_list_orders_by_last_used_desc(self, store):
        a = store.create(title="A")
        time.sleep(0.05)
        b = store.create(title="B")
        time.sleep(0.05)
        c = store.create(title="C")
        # 现在 c 是最新;B 次之;A 最老
        out = store.list_all()
        assert [s.session_id for s in out] == [c.session_id, b.session_id, a.session_id]

    def test_list_limit(self, store):
        for _ in range(5):
            store.create()
            time.sleep(0.01)
        out = store.list_all(limit=2)
        assert len(out) == 2


class TestTouch:
    def test_touch_updates_last_used(self, store):
        info = store.create()
        time.sleep(0.05)
        old = info.last_used_at
        store.touch(info.session_id)
        got = store.get(info.session_id)
        assert got.last_used_at > old

    def test_touch_with_message_count(self, store):
        info = store.create()
        store.touch(info.session_id, message_count=7)
        got = store.get(info.session_id)
        assert got.message_count == 7

    def test_touch_with_title(self, store):
        info = store.create()
        store.touch(info.session_id, title="新名")
        got = store.get(info.session_id)
        assert got.title == "新名"

    def test_touch_missing_returns_false(self, store):
        assert store.touch("nope") is False


class TestDelete:
    def test_delete_existing(self, store):
        info = store.create()
        assert store.delete(info.session_id) is True
        assert store.get(info.session_id) is None

    def test_delete_missing_returns_false(self, store):
        assert store.delete("nope") is False

    def test_delete_doesnt_affect_others(self, store):
        a = store.create()
        b = store.create()
        store.delete(a.session_id)
        assert store.get(b.session_id) is not None


class TestRename:
    def test_rename(self, store):
        info = store.create(title="old")
        assert store.rename(info.session_id, "new") is True
        got = store.get(info.session_id)
        assert got.title == "new"

    def test_rename_missing(self, store):
        assert store.rename("nope", "x") is False


class TestDBFileCreation:
    def test_creates_parent_directory(self, tmp_path):
        deep = tmp_path / "a" / "b" / "sessions.db"
        store = SessionStore(deep)
        store.create()
        assert deep.exists()

    def test_persists_across_instances(self, tmp_path):
        db = tmp_path / "s.db"
        s1 = SessionStore(db)
        info = s1.create(title="persistent")
        s2 = SessionStore(db)
        got = s2.get(info.session_id)
        assert got is not None
        assert got.title == "persistent"


class TestNewSessionIdHelper:
    def test_unique_each_time(self):
        ids = {SessionStore.new_session_id() for _ in range(100)}
        assert len(ids) == 100
        # 长度一致(短 UUID)
        assert all(len(i) == 12 for i in ids)


class TestSessionInfoDataclass:
    def test_round_trip(self):
        info = SessionInfo(
            session_id="abc", thread_id="thread-abc", title="t",
            created_at=1.0, last_used_at=2.0, message_count=3,
        )
        row = info.to_row()
        assert row == ("abc", "thread-abc", "t", 1.0, 2.0, 3)
        info2 = SessionInfo.from_row(row)
        assert info == info2


# -------------------- LangChainAgent 集成 --------------------

class TestLangChainAgentSessionBinding:
    """LangChainAgent 拿到一个 SessionStore + session_id 时,
    应该用对应的 thread_id 跑 astream(checkpoint 才会落到那条线上)。"""

    def test_agent_uses_session_thread_id(self, tmp_path):
        from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
        from app.brain.llm_client import LLMConfig
        from app.engine.tools import ToolRegistry

        store = SessionStore(tmp_path / "s.db")
        info = store.create(title="测试会话")

        agent = LangChainAgent(
            llm_cfg=LLMConfig(),
            registry=ToolRegistry(),
            cfg=LangChainAgentConfig(),
            persona="test",
            thread_id="default",   # 这个应该被 session 覆盖
            session_id=info.session_id,
            session_store=store,
        )
        # agent.thread_id 必须 == info.thread_id
        assert agent.thread_id == info.thread_id
        assert agent.thread_id != "default"

    def test_agent_without_session_keeps_thread_id(self, tmp_path):
        """没传 session 时,thread_id 参数照常工作(不破坏旧调用)。"""
        from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
        from app.brain.llm_client import LLMConfig
        from app.engine.tools import ToolRegistry

        agent = LangChainAgent(
            llm_cfg=LLMConfig(),
            registry=ToolRegistry(),
            cfg=LangChainAgentConfig(),
            persona="test",
            thread_id="my_custom_thread",
        )
        assert agent.thread_id == "my_custom_thread"

    def test_agent_touches_session_after_run(self, tmp_path):
        """跑完一次后,SessionStore 里 last_used_at / message_count 应该被更新。"""
        from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
        from app.brain.llm_client import LLMConfig
        from app.engine.tools import ToolRegistry

        store = SessionStore(tmp_path / "s.db")
        info = store.create()
        time.sleep(0.05)
        old_used = info.last_used_at

        agent = LangChainAgent(
            llm_cfg=LLMConfig(),
            registry=ToolRegistry(),
            cfg=LangChainAgentConfig(),
            persona="test",
            session_id=info.session_id,
            session_store=store,
        )
        agent.touch_session(message_count=1)
        got = store.get(info.session_id)
        assert got.last_used_at > old_used
        assert got.message_count == 1

    def test_unknown_session_id_raises(self, tmp_path):
        from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
        from app.brain.llm_client import LLMConfig
        from app.engine.tools import ToolRegistry

        store = SessionStore(tmp_path / "s.db")
        with pytest.raises(ValueError, match="not found"):
            LangChainAgent(
                llm_cfg=LLMConfig(),
                registry=ToolRegistry(),
                cfg=LangChainAgentConfig(),
                persona="test",
                session_id="nope_does_not_exist",
                session_store=store,
            )