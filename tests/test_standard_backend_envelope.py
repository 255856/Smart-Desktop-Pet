# -*- coding: utf-8 -*-
"""标准后端（LangChain）工具结果契约：失败可见 / 统一裁剪 / 单工具超时。

ReAct 路径（app/brain/agent.py + ToolRegistry.execute）早就有这套契约：
    · 失败      → 信封 status=error（界面显示 ✕）
    · 超大结果  → 统一裁剪（trim_tool_result：50000 / 45000 / 2000）
    · 卡死的工具 → 单工具超时（TOOL_CALL_TIMEOUT_SECONDS）+ 整轮预算

标准后端走 LangChain 的 ToolNode，**直接调 tool.fn** → 只有一段裸文本：
    · 工具抛异常 → LangChain 包成 "Error: ..."，界面按前缀判不出失败 → 显示 ✓
    · 超大结果   → 整段塞进上下文，没有统一裁剪
    · 卡死的工具 → 整轮对话跟着卡住

这里锁住 app/brain/langchain_agent.py 新加的适配层
（app/engine/tools/_core.py::wrap_tool_fn）。
"""
from __future__ import annotations

import asyncio
import sys
import time

from app.brain.langchain_agent import convert_tools
from app.engine.tools import Tool, ToolRegistry
from app.engine.tools import _core as tool_core

# QApplication 必须先于任何 QWidget 存在（跟 tests/test_tool_status_truth.py 同款）
from app.core.qt_compat import QApplication
app = QApplication.instance() or QApplication(sys.argv)

from app.ui.chat_window.utils import _tool_status


def _reg(tool: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(tool)
    return reg


def _lc_tool(reg: ToolRegistry, name: str):
    return next(t for t in convert_tools(reg) if t.name == name)


# ---------------------------------------------------------------------------
# A1 · 失败必须**看得见**（界面显示 ✕，不是 ✓）
# ---------------------------------------------------------------------------

def test_standard_tool_exception_is_reported_as_failure():
    """工具抛异常 → 结果带「失败：」前缀 → 界面判 fail（✕）。"""
    def boom():
        raise RuntimeError("磁盘炸了")

    reg = _reg(Tool(name="boom", description="总是失败",
                    parameters={"type": "object", "properties": {}}, fn=boom))
    out = _lc_tool(reg, "boom").invoke({})
    assert out.startswith("失败：")
    assert "磁盘炸了" in out
    assert _tool_status(out, "") == "fail"


def test_standard_failure_is_visible_where_it_used_to_be_ok():
    """旧行为对照：LangChain 把异常包成 "Error: ..." → 界面判 ok（✓，谎报成功）。"""
    legacy = "Error: RuntimeError('磁盘炸了')\n Please fix your mistakes."
    assert _tool_status(legacy, "") == "ok"      # 旧路径：失败被显示成 ✓
    assert _tool_status("失败：工具 boom 执行出错：磁盘炸了", "") == "fail"


def test_standard_tool_failure_text_gets_failure_prefix():
    """工具自己报失败（错误：/失败：前缀）→ 前缀保留，界面判 fail。"""
    reg = _reg(Tool(name="bad", description="会失败",
                    parameters={"type": "object", "properties": {}},
                    fn=lambda: "错误：找不到目标"))
    out = _lc_tool(reg, "bad").invoke({})
    assert out.startswith("错误：")
    assert _tool_status(out, "") == "fail"


# ---------------------------------------------------------------------------
# A2 · 超大结果按统一裁剪器裁（与 ReAct 同款）
# ---------------------------------------------------------------------------

def test_standard_huge_result_is_trimmed_like_react_path():
    """标准后端裁出来的文本 == ReAct 路径（ToolRegistry.execute）裁出来的。"""
    big = "A" * 60000
    reg = _reg(Tool(name="big", description="返回大结果",
                    parameters={"type": "object", "properties": {}},
                    fn=lambda: big))
    lc_out = _lc_tool(reg, "big").invoke({})
    react = reg.execute("big", {})
    assert react.truncated is True
    assert lc_out == react.data
    assert len(lc_out) <= tool_core.TOOL_RESULT_MAX_CHARS + 200
    assert "中间省略" in lc_out


def test_standard_small_result_is_untouched():
    reg = _reg(Tool(name="echo", description="回显",
                    parameters={"type": "object", "properties": {
                        "x": {"type": "string"}}},
                    fn=lambda x: "echoed:" + x))
    out = _lc_tool(reg, "echo").invoke({"x": "hi"})
    assert out == "echoed:hi"
    assert _tool_status(out, "") == "ok"


# ---------------------------------------------------------------------------
# A3 · 卡死的工具在单工具超时处被砍断（整轮不卡住）
# ---------------------------------------------------------------------------

def test_standard_hanging_tool_is_cut_off(monkeypatch):
    monkeypatch.setattr(tool_core, "TOOL_CALL_TIMEOUT_SECONDS", 0.3)

    def slow():
        time.sleep(30)
        return "done"

    reg = _reg(Tool(name="slow", description="卡死",
                    parameters={"type": "object", "properties": {}}, fn=slow))
    t0 = time.monotonic()
    out = _lc_tool(reg, "slow").invoke({})
    elapsed = time.monotonic() - t0
    assert elapsed < 5, "工具超时后应该立刻返回，不能把整轮拖住"
    assert out.startswith("失败：")
    assert "超时" in out
    assert _tool_status(out, "") == "fail"


def test_standard_async_tool_call_also_guarded(monkeypatch):
    """标准后端走的是 ToolNode 的异步调用（ainvoke）—— 同样受超时保护。"""
    monkeypatch.setattr(tool_core, "TOOL_CALL_TIMEOUT_SECONDS", 0.3)

    def slow():
        time.sleep(30)
        return "done"

    reg = _reg(Tool(name="slow", description="卡死",
                    parameters={"type": "object", "properties": {}}, fn=slow))
    tool = _lc_tool(reg, "slow")
    t0 = time.monotonic()
    out = asyncio.run(tool.ainvoke({}))
    elapsed = time.monotonic() - t0
    assert elapsed < 5
    assert out.startswith("失败：")
    assert "超时" in out


# ---------------------------------------------------------------------------
# A4 · ReAct 路径原样不动 + schema / 注册表不动
# ---------------------------------------------------------------------------

def test_react_path_envelope_unchanged():
    """ReAct 路径仍返回结构化信封（本次改动没碰它）。"""
    reg = _reg(Tool(name="echo", description="回显",
                    parameters={"type": "object", "properties": {
                        "x": {"type": "string"}}},
                    fn=lambda x: "echoed:" + x))
    env = reg.execute("echo", {"x": "hi"})
    assert isinstance(env, tool_core.ToolResult)
    assert env.status == "ok"
    assert env.data == "echoed:hi"
    assert env.truncated is False


def test_react_path_failure_envelope_unchanged():
    reg = _reg(Tool(name="boom", description="总是失败",
                    parameters={"type": "object", "properties": {}},
                    fn=lambda: (_ for _ in ()).throw(RuntimeError("炸了"))))
    env = reg.execute("boom", {})
    assert env.status == "error"
    assert env.error_code == "tool_error"
    assert env.data.startswith("错误：")


def test_standard_tool_schema_and_name_unchanged():
    """工具 schema / 名字 / 描述没被适配层改动。"""
    params = {"type": "object",
              "properties": {"x": {"type": "string", "description": "输入"}},
              "required": ["x"]}
    reg = _reg(Tool(name="echo", description="回显", parameters=params,
                    fn=lambda x: "echoed:" + x))
    tool = _lc_tool(reg, "echo")
    assert tool.name == "echo"
    assert tool.description == "回显"
    schema = tool.args_schema.model_json_schema()
    assert "x" in schema["properties"]
    assert schema["properties"]["x"]["type"] == "string"


# ---------------------------------------------------------------------------
# A1（补）· LangChain 自己判失败（参数校验不过等）也要带「失败：」前缀
# ---------------------------------------------------------------------------

def test_standard_langchain_level_error_is_reported_as_failure():
    """ToolMessage.status=error（LangChain 判失败）→ 内容带前缀 → 界面 ✕。"""
    from unittest.mock import MagicMock
    from langchain_core.messages import ToolMessage

    from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
    from app.brain.llm_client import LLMConfig

    reg = _reg(Tool(name="echo", description="回显",
                    parameters={"type": "object", "properties": {
                        "x": {"type": "string"}}},
                    fn=lambda x: "echoed:" + x))
    agent = LangChainAgent(
        llm_cfg=LLMConfig(api_key="sk-test", model="gpt-test", stream=False),
        registry=reg, persona="x",
        cfg=LangChainAgentConfig(enable_checkpointer=False),
    )

    fake_graph = MagicMock()

    async def fake_astream(payload, config=None, stream_mode=None):
        yield ToolMessage(content="Error: 1 validation error for echo",
                          name="echo", tool_call_id="x", status="error"), {}

    fake_graph.astream = fake_astream
    agent._agent = fake_graph

    async def collect():
        out = []
        async for ev in agent.run([{"role": "user", "content": "hi"}]):
            out.append(ev)
        return out

    events = asyncio.run(collect())
    tool_ev = next(ev for ev in events if ev[0] == "tool")
    assert tool_ev[3].startswith("失败：")
    assert _tool_status(tool_ev[3], "") == "fail"


class TestToolPhaseBudget:
    """整轮工具预算：ReAct 路径早有（agent.py），standard 路径靠 middleware 补上。"""

    def test_budget_runs_out_and_resets(self):
        from app.brain.tool_budget_middleware import ToolPhaseBudget

        b = ToolPhaseBudget(budget_seconds=0.01)
        time.sleep(0.05)
        assert b.remaining() <= 0
        b.exhausted = True
        b.blocked = 7
        b.reset()
        assert b.exhausted is False
        assert b.blocked == 0
        assert b.remaining() > 0

    def test_zero_budget_means_no_limit(self):
        from app.brain.tool_budget_middleware import ToolPhaseBudget

        b = ToolPhaseBudget(budget_seconds=0)
        assert b.remaining() == float("inf")

    def test_middleware_blocks_the_call_when_exhausted(self):
        import asyncio
        from app.brain.tool_budget_middleware import ToolPhaseBudget

        b = ToolPhaseBudget(budget_seconds=0.01)
        time.sleep(0.03)
        mw = b.as_middleware()
        called = []

        class _Req:
            tool_call = {"name": "web_search", "id": "call_1", "args": {}}

        async def _handler(request):
            called.append(1)
            return "真跑了"

        async def _run():
            # wrap_tool_call 装饰后拿到的是 LangChain 的 middleware 协议对象，
            # 真正的钩子是 awrap_tool_call（bind 后的 async 函数）。
            fn = getattr(mw, "awrap_tool_call", None)
            assert fn is not None, "拿不到中间件的 awrap_tool_call 钩子"
            return await fn(_Req(), _handler)

        out = asyncio.run(_run())
        assert out is not None, "预算用尽时中间件必须给出一个 ToolMessage"
        assert not called, "预算用尽后不该再执行工具"
        assert b.exhausted is True
        assert b.blocked == 1
        assert "预算" in str(getattr(out, "content", out))
    print("[OK] 整轮工具预算：standard 路径也生效了")
