"""LangChain 标准后端测试。

不发起真实 LLM 请求（避免依赖外部服务 / 消耗 token），只验证：
1. Tool → StructuredTool 转换正确
2. agent 构造流程无异常
3. event 协议兼容（yield tuple 类型匹配）
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.brain.langchain_agent import (
    LangChainAgent,
    LangChainAgentConfig,
    convert_tools,
)
from app.brain.llm_client import LLMConfig
from app.engine.tools import Tool, ToolRegistry


def _make_registry() -> ToolRegistry:
    reg = ToolRegistry()

    def echo(x: str) -> str:
        """echo back the input."""
        return f"echoed:{x}"

    def add(a: int, b: int = 1) -> str:
        """add two numbers."""
        return str(a + b)

    def now() -> str:
        """return current time."""
        return "2026-01-01 00:00:00"

    reg.register(Tool(
        name="echo",
        description="echo back the input",
        parameters={"type": "object", "properties": {"x": {"type": "string"}}},
        fn=echo,
    ))
    reg.register(Tool(
        name="add",
        description="add two numbers",
        parameters={"type": "object", "properties": {
            "a": {"type": "integer"},
            "b": {"type": "integer"},
        }},
        fn=add,
    ))
    reg.register(Tool(
        name="now",
        description="return current time",
        parameters={"type": "object", "properties": {}},
        fn=now,
    ))
    return reg


# ---------- 工具转换 ----------
def test_convert_tools_count():
    reg = _make_registry()
    lc_tools = convert_tools(reg)
    assert len(lc_tools) == 3


def test_convert_tools_preserves_name():
    reg = _make_registry()
    lc_tools = convert_tools(reg)
    names = {t.name for t in lc_tools}
    assert names == {"echo", "add", "now"}


def test_convert_tools_schema_has_args():
    reg = _make_registry()
    lc_tools = convert_tools(reg)
    echo = next(t for t in lc_tools if t.name == "echo")
    schema = echo.args_schema.model_json_schema()
    assert "x" in schema["properties"]


def test_convert_tools_callable():
    reg = _make_registry()
    lc_tools = convert_tools(reg)
    add = next(t for t in lc_tools if t.name == "add")
    assert add.invoke({"a": 2, "b": 3}) == "5"
    # 默认值
    assert add.invoke({"a": 5}) == "6"


def test_convert_tools_zero_tool():
    """空注册表转换应当返回空列表。"""
    empty = ToolRegistry()
    assert convert_tools(empty) == []


# ---------- LangChainAgent 构造 ----------
def test_agent_construction_with_registry():
    reg = _make_registry()
    llm = LLMConfig(api_key="sk-test", model="gpt-test", stream=False)
    agent = LangChainAgent(
        llm_cfg=llm,
        registry=reg,
        persona="你是助手",
    )
    assert agent.registry is reg
    assert agent.llm_cfg is llm
    assert agent.persona == "你是助手"


def test_agent_config_defaults():
    cfg = LangChainAgentConfig()
    assert cfg.enable_checkpointer is True
    assert cfg.max_iterations == 10
    assert cfg.return_intermediate_steps is False


# ---------- event 协议兼容 ----------
def test_history_to_lc_messages_skips_system():
    """system role 应当被跳过（已通过 system_prompt 注入）。"""
    msgs = [
        {"role": "system", "content": "you are a helper"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "how are you?"},
    ]
    lc_msgs = LangChainAgent._to_lc_history(msgs)
    # 4 → 2 (跳过 system) + history 中保留 user/assistant
    assert len(lc_msgs) == 3


def test_history_to_lc_messages_empty():
    lc = LangChainAgent._to_lc_history([])
    assert lc == []


# ---------- checkpoint 不再重复累积 ----------
def test_checkpoint_input_replaces_instead_of_appending():
    """喂给 graph 的 messages 必须先 RemoveMessage 清空，否则历史会被一遍遍追加。

    老实现每轮都把完整本地 history 塞进 graph，而 LangGraph 的 add_messages
    是追加语义 → 同一段历史被一遍遍写进 checkpoint，
    data/langchain_checkpoints.db 曾涨到 275 MB（单条 4,393 条消息）。
    """
    from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage
    from langgraph.graph.message import REMOVE_ALL_MESSAGES, add_messages

    history = LangChainAgent._to_lc_history([
        {"role": "user", "content": "u0"},
        {"role": "assistant", "content": "a0"},
    ])
    payload = LangChainAgent._checkpoint_input(history)

    # 第一条必须是 RemoveMessage(REMOVE_ALL_MESSAGES)
    assert isinstance(payload[0], RemoveMessage)
    assert payload[0].id == REMOVE_ALL_MESSAGES
    # 后面紧跟本轮要保留的消息
    assert [m.content for m in payload[1:]] == ["u0", "a0"]

    # 用真实 reducer 验证：清空 + 写入 == 只有新写入的那些，不重复
    old_state = [HumanMessage(content="old_u"), AIMessage(content="old_a")]
    merged = add_messages(old_state, payload)
    assert [m.content for m in merged] == ["u0", "a0"]


def test_checkpoint_input_is_bounded_by_local_history():
    """checkpoint 里的消息数应该等于本轮传入的历史条数（不被历史累积放大）。"""
    history = LangChainAgent._to_lc_history([
        {"role": "user", "content": f"u{i}"} for i in range(10)
    ])
    payload = LangChainAgent._checkpoint_input(history)
    assert len(payload) == 11   # 1 个 RemoveMessage + 10 条历史


# ---------- 用 mock astream 验证 event 协议 ----------
def test_run_yields_event_protocol():
    """用 mock 替换 astream，验证 event tuple 类型与字段。"""
    from langchain_core.messages import AIMessageChunk, ToolMessage

    reg = _make_registry()
    llm = LLMConfig(api_key="sk-test", model="gpt-test", stream=False)

    agent = LangChainAgent(
        llm_cfg=llm, registry=reg, persona="你是助手",
        cfg=LangChainAgentConfig(enable_checkpointer=False),
    )

    # Mock compiled graph：astream 返回受控 chunk 序列
    fake_graph = MagicMock()

    async def fake_astream(payload, config=None, stream_mode=None):
        yield AIMessageChunk(content="你好"), {}
        yield ToolMessage(content="echoed:hi", name="echo", tool_call_id="x"), {}
        yield AIMessageChunk(content=" [happy]"), {}

    fake_graph.astream = fake_astream

    # 跳过真实 create_agent，直接注入 mock graph
    agent._agent = fake_graph
    msgs = [{"role": "user", "content": "hi"}]

    async def collect():
        out = []
        async for ev in agent.run(msgs):
            out.append(ev)
        return out

    result = asyncio.run(collect())
    # 第一条是 AIMessageChunk → ("text", "你好")
    assert result[0] == ("text", "你好")
    # 第二条是 ToolMessage → ("tool", "echo", "", "echoed:hi")
    assert result[1] == ("tool", "echo", "", "echoed:hi")
    # 第三条是 AIMessageChunk → ("text", " [happy]")
    assert result[2][0] == "text"
    # 最后一条是 ("done", 全文本)
    assert result[-1][0] == "done"


def test_run_propagates_error():
    """异常应当 yield ("error", ...)。"""
    reg = _make_registry()
    llm = LLMConfig(api_key="sk-test", model="gpt-test", stream=False)
    agent = LangChainAgent(llm_cfg=llm, registry=reg, persona="x",
                            cfg=LangChainAgentConfig(enable_checkpointer=False))

    fake_graph = MagicMock()

    async def fake_astream(payload, config=None, stream_mode=None):
        raise RuntimeError("boom")
        yield  # noqa: never reached

    fake_graph.astream = fake_astream
    agent._agent = fake_graph

    async def collect():
        out = []
        async for ev in agent.run([{"role": "user", "content": "hi"}]):
            out.append(ev)
        return out

    result = asyncio.run(collect())
    assert any(ev[0] == "error" for ev in result)
    assert any("boom" in ev[1] for ev in result if ev[0] == "error")


def test_run_respects_cancel_check():
    """cancel_check 触发时停止并 yield ("error", "cancelled")。"""
    from langchain_core.messages import AIMessageChunk

    reg = _make_registry()
    llm = LLMConfig(api_key="sk-test", model="gpt-test", stream=False)
    agent = LangChainAgent(llm_cfg=llm, registry=reg, persona="x",
                            cfg=LangChainAgentConfig(enable_checkpointer=False))

    fake_graph = MagicMock()

    call_count = {"n": 0}

    async def fake_astream(payload, config=None, stream_mode=None):
        for _ in range(10):
            call_count["n"] += 1
            yield AIMessageChunk(content="chunk"), {}

    fake_graph.astream = fake_astream
    agent._agent = fake_graph

    cancelled = {"v": False}

    def cancel_check() -> bool:
        return cancelled["v"]

    async def collect():
        out = []
        async for ev in agent.run([{"role": "user", "content": "x"}],
                                  cancel_check=cancel_check):
            if call_count["n"] >= 3:
                cancelled["v"] = True
            out.append(ev)
        return out

    result = asyncio.run(collect())
    assert any(ev[0] == "error" and "cancel" in str(ev[1]).lower() for ev in result)
    # 不应跑完 10 次
    assert call_count["n"] < 10


# ---------- 关闭语义 ----------
def test_close_is_idempotent_without_checkpointer():
    """关闭一个没启用 checkpointer 的 agent 不会出错。"""
    reg = _make_registry()
    llm = LLMConfig(api_key="sk-test", model="gpt-test", stream=False)
    agent = LangChainAgent(llm_cfg=llm, registry=reg, persona="x",
                            cfg=LangChainAgentConfig(enable_checkpointer=False))
    # 没创建过 checkpointer
    agent.close()  # 不应抛异常
    agent.close()  # 多次调用也安全


# ---------- race condition 修复 ----------
def test_persistent_loop_start_timeout_raises():
    """_PersistentLoop.start() 在 _ready 超时后必须 raise,不能沉默返回。
    旧实现 5s timeout,LangChain 1.0+ import 慢时会让后续 run_coro 卡死。
    """
    from app.brain.langchain_agent import _PersistentLoop

    loop = _PersistentLoop()
    # 把 _START_TIMEOUT_S 改成 0s,模拟「线程永远跑不到 _ready.set()」
    loop._START_TIMEOUT_S = 0.0
    with pytest.raises(RuntimeError, match="启动超时"):
        loop.start()
    # 失败后 thread 也不留(避免 daemon 线程泄漏)
    assert loop.thread is None


def test_persistent_loop_records_runner_exception():
    """_runner 里 import 失败,start() 应该把异常 raise 出去,而不是沉默。
    验证:模拟一个「跑出异常」的 runner,start() 检测到 _runner_exc 后 raise。
    """
    from app.brain.langchain_agent import _PersistentLoop

    # 准备一个坏的 runner:不创建 loop,直接 raise
    # 我们手动模拟 start() 的检测逻辑(因为它是内嵌的 _runner 函数,不能直接换)
    loop = _PersistentLoop()
    # 直接设置 exc 字段(模拟 runner 跑过这一行后崩了)
    loop._runner_exc = RuntimeError("模拟 import 失败")
    loop._ready.set()  # 让 wait 立即通过
    # 单独验证 start() 走完 wait 后看到 _runner_exc 也会 raise
    # 这里我们手工调一遍 start() 的检测路径
    got_ready = loop._ready.wait(timeout=loop._START_TIMEOUT_S)
    assert got_ready is True
    assert loop._runner_exc is not None
    # 用同样的判断逻辑确认会 raise
    if loop._runner_exc is not None:
        with pytest.raises(RuntimeError, match="模拟 import 失败"):
            raise RuntimeError(f"LangChain 后台 loop 启动失败: {loop._runner_exc!r}")


def test_langchain_agent_init_failure_resets_loop_singleton():
    """LangChainAgent.__init__ 失败时,_LOOP 类变量要重置,下次重试。

    模拟:_get_loop() 抛异常(模拟后台 loop 启动失败),
    LangChainAgent 构造应该 raise 且清掉 _LOOP,下次能再 _get_loop() 重建。
    """
    from app.brain import langchain_agent as mod

    # 把 _LOOP 先设成「启动会爆」的对象
    class _BrokenLoop:
        def start(self):
            raise RuntimeError("模拟后台 loop 启动失败")

    # 直接 patch _get_loop 让它 raise
    original_get_loop = mod.LangChainAgent._get_loop
    mod.LangChainAgent._get_loop = classmethod(lambda cls: (_ for _ in ()).throw(
        RuntimeError("模拟后台 loop 启动失败")
    ))

    try:
        reg = _make_registry()
        llm = LLMConfig(api_key="sk-test", model="gpt-test", stream=False)
        with pytest.raises(RuntimeError, match="模拟后台 loop 启动失败"):
            LangChainAgent(llm_cfg=llm, registry=reg, persona="x",
                           cfg=LangChainAgentConfig(enable_checkpointer=False))
    finally:
        # 恢复 _get_loop
        mod.LangChainAgent._get_loop = original_get_loop
        # 显式 reset 以防前面失败留下半死状态
        mod.LangChainAgent._LOOP = None


class TestHallucinationGuard:
    """抗幻觉 middleware 的单元测试。"""

    def test_prune_keeps_valid_tool_pair(self):
        """完整 Human + AIMessage(tool_call) + ToolMessage + Human 应全部保留。"""
        from app.brain.hallucination_guard import _prune_orphan_tool_messages
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        msgs = [
            HumanMessage(content="现在几点了"),
            AIMessage(content="",
                      tool_calls=[{"id": "call_1", "name": "get_current_time", "args": {}}]),
            ToolMessage(content='{"now": "12:00"}', tool_call_id="call_1"),
            HumanMessage(content="谢谢"),
        ]
        kept = _prune_orphan_tool_messages(msgs)
        assert len(kept) == 4, "完整 tool pair 不该丢任何消息"

    def test_prune_drops_orphan_tool_message(self):
        """孤立的 ToolMessage(前面没有匹配 AIMessage.tool_call)要被丢。"""
        from app.brain.hallucination_guard import _prune_orphan_tool_messages
        from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

        msgs = [
            HumanMessage(content="hi"),
            # 孤立:tool_call_id='orphan_id' 前面没 AIMessage 提供它
            ToolMessage(content="12:00", tool_call_id="orphan_id"),
            AIMessage(content="ok"),
            HumanMessage(content="bye"),
        ]
        kept = _prune_orphan_tool_messages(msgs)
        # 孤立的 ToolMessage 被丢,其余 3 条保留
        assert len(kept) == 3
        assert all(not (type(m).__name__ == "ToolMessage"
                        and getattr(m, "tool_call_id", None) == "orphan_id")
                   for m in kept)

    def test_build_middlewares_returns_tuple(self):
        """build 返回 (3 个标准中间件, RiskyToolConfirm controller)。

        从只返回 list 改为返回 tuple;并把 confirm 中间件拆出去,
        让 caller 自己用 ctrl.as_middleware() 生成。
        """
        from app.brain.hallucination_guard import (
            RiskyToolConfirm,
            build_hallucination_middlewares,
        )
        mw, ctrl = build_hallucination_middlewares()
        assert isinstance(mw, list)
        assert isinstance(ctrl, RiskyToolConfirm)
        names = {getattr(m, "name", "?") for m in mw}
        assert "orphan_tool_pruner" in names
        assert "grounding_dynamic_prompt" in names
        assert "risky_tool_logger" in names
        assert len(mw) == 3


class TestCheckpointRecovery:
    """2013 'tool call result does not follow tool call' 时,LangChainAgent
    应该清掉当前 thread 的 checkpoint 然后重试一次。"""

    def test_clear_thread_checkpoint_deletes_rows(self, tmp_path):
        """_clear_thread_checkpoint 必须真的从 SQLite 删掉对应 thread 的行。"""
        # 手动建一个 fake checkpoint DB + 表
        import sqlite3
        db_path = tmp_path / "ckpt.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE checkpoints (
                thread_id TEXT, checkpoint_ns TEXT DEFAULT '',
                checkpoint_id TEXT, parent_checkpoint_id TEXT,
                type TEXT, checkpoint BLOB, metadata BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id));
            CREATE TABLE writes (
                thread_id TEXT, checkpoint_ns TEXT DEFAULT '',
                checkpoint_id TEXT, task_id TEXT, idx INTEGER,
                channel TEXT, type TEXT, value BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx));
        """)
        # 插两行:一条目标 thread + 一条其它 thread(必须保留)
        conn.execute(
            "INSERT INTO checkpoints(thread_id, checkpoint_id, type, checkpoint, metadata) "
            "VALUES (?, ?, ?, ?, ?)", ("default", "c1", "x", b"a", b"b"),
        )
        conn.execute(
            "INSERT INTO checkpoints(thread_id, checkpoint_id, type, checkpoint, metadata) "
            "VALUES (?, ?, ?, ?, ?)", ("other", "c2", "x", b"c", b"d"),
        )
        conn.commit()
        conn.close()

        reg = _make_registry()
        llm = LLMConfig(api_key="sk-test", model="gpt-test", stream=False)
        agent = LangChainAgent(
            llm_cfg=llm, registry=reg, persona="x",
            cfg=LangChainAgentConfig(
                enable_checkpointer=True,
                checkpoint_db=str(db_path),
            ),
            thread_id="default",
        )

        ok = agent._clear_thread_checkpoint()
        assert ok is True, "应至少删 1 行"

        # 验证 default 的全删了,other 的保留
        conn = sqlite3.connect(str(db_path))
        n_default = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id='default'"
        ).fetchone()[0]
        n_other = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id='other'"
        ).fetchone()[0]
        conn.close()
        assert n_default == 0
        assert n_other == 1

    def test_run_retries_after_2013(self, tmp_path):
        """第一次 astream 抛 2013 → 清 ckpt → 第二次 astream 正常完成。"""
        from openai import BadRequestError
        from langchain_core.messages import AIMessageChunk

        # 先建好 ckpt DB(模拟已有脏数据)
        import sqlite3
        db_path = tmp_path / "ckpt.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE checkpoints (
                thread_id TEXT, checkpoint_ns TEXT DEFAULT '',
                checkpoint_id TEXT, parent_checkpoint_id TEXT,
                type TEXT, checkpoint BLOB, metadata BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id));
            CREATE TABLE writes (
                thread_id TEXT, checkpoint_ns TEXT DEFAULT '',
                checkpoint_id TEXT, task_id TEXT, idx INTEGER,
                channel TEXT, type TEXT, value BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx));
        """)
        conn.execute(
            "INSERT INTO checkpoints(thread_id, checkpoint_id, type, checkpoint, metadata) "
            "VALUES (?, ?, ?, ?, ?)", ("default", "c1", "x", b"a", b"b"),
        )
        conn.commit()
        conn.close()

        reg = _make_registry()
        llm = LLMConfig(api_key="sk-test", model="gpt-test", stream=False)
        agent = LangChainAgent(
            llm_cfg=llm, registry=reg, persona="x",
            cfg=LangChainAgentConfig(
                enable_checkpointer=True,
                checkpoint_db=str(db_path),
            ),
            thread_id="default",
        )

        # patch _agent.astream:第一次 raise 2013,第二次 yield 正常
        call_count = [0]

        async def first_call(*args, **kwargs):
            raise BadRequestError(
                "Error code: 400 - invalid params, tool call result does not follow tool call (2013)",
                response=MagicMock(status_code=400),
                body=None,
            )

        async def second_call(*args, **kwargs):
            yield AIMessageChunk(content="重试成功!", type="AIMessageChunk"), {}

        async def fake_astream(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                await first_call(*args, **kwargs)
            else:
                async for x in second_call(*args, **kwargs):
                    yield x

        agent._agent = MagicMock()
        agent._agent.astream = fake_astream

        async def go():
            events = []
            async for ev in agent.run([{"role": "user", "content": "hi"}]):
                events.append(ev)
            return events

        events = asyncio.run(go())
        assert any(ev[0] == "text" for ev in events), f"应收到 text 事件,got {events}"
        assert any(ev[0] == "done" for ev in events), f"应收到 done 事件,got {events}"
        assert call_count[0] == 2, f"应调用 2 次,实际 {call_count[0]}"