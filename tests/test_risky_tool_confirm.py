"""高风险工具 confirm 中间件(HITL 等价)的测试。

覆盖:
- RiskyToolConfirm.as_middleware() 行为
- 第一次调用拦截
- args.__confirmed=True 放行 + 字段剥离
- 显式 confirm() 放行
- 显式 reject() 永久拦截
- 非 risky 工具直通
- on_blocked 回调触发
- 干净 args 在 handler 端真的拿到

约定:用 LangChain 的 ToolCallRequest 包装请求,handler 用 fake handler 标记
「真的执行了吗」并把收到的 tool_call 原样返回,便于断言剥离是否生效。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

from app.brain.hallucination_guard import (
    _CONFIRM_KEY,
    _RISKY_TOOLS,
    RiskyToolConfirm,
    _extract_confirm_flag,
)


def _mk_req(name: str, args: Any, call_id: str = "call_xyz") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": args, "id": call_id},
        tool=None, state={}, runtime=None,
    )


class _FakeHandler:
    """记录调用,返回 'CALLED' 字符串。"""

    def __init__(self):
        self.calls: list[ToolCallRequest] = []

    async def __call__(self, req: ToolCallRequest):
        self.calls.append(req)
        return f"CALLED:{req.tool_call['name']}"


def _run(coro):
    return asyncio.run(coro)


# -------------------- _extract_confirm_flag --------------------

class TestExtractConfirmFlag:
    def test_dict_with_true_confirmed(self):
        clean, confirmed = _extract_confirm_flag({"a": 1, "__confirmed": True})
        assert confirmed is True
        assert clean == {"a": 1}
        assert "__confirmed" not in clean

    def test_dict_without_confirmed(self):
        clean, confirmed = _extract_confirm_flag({"a": 1})
        assert confirmed is False
        assert clean == {"a": 1}

    def test_dict_with_confirmed_false_not_passing(self):
        clean, confirmed = _extract_confirm_flag({"a": 1, "__confirmed": False})
        assert confirmed is False  # 只有严格 True 才算

    def test_dict_with_confirmed_truthy_string_not_passing(self):
        clean, confirmed = _extract_confirm_flag({"__confirmed": "yes"})
        assert confirmed is False  # 字符串不是 True

    def test_non_dict_passes_through(self):
        clean, confirmed = _extract_confirm_flag(["a", "b"])
        assert confirmed is False
        assert clean == ["a", "b"]

    def test_none_passes_through(self):
        clean, confirmed = _extract_confirm_flag(None)
        assert confirmed is False
        assert clean is None


# -------------------- RiskyToolConfirm 中间件行为 --------------------

class TestMiddlewareBlocksRiskyTool:
    def test_first_call_to_lock_screen_blocked(self):
        ctrl = RiskyToolConfirm()
        mw = ctrl.as_middleware()
        req = _mk_req("lock_screen", {})
        handler = _FakeHandler()

        result = _run(mw.awrap_tool_call(req, handler))

        assert isinstance(result, ToolMessage), "拦截后必须返回 ToolMessage"
        assert handler.calls == [], "拦截后 handler 不能被调用"
        parsed = json.loads(result.content)
        assert parsed["status"] == "awaiting_confirmation"
        assert parsed["tool"] == "lock_screen"

    def test_first_call_to_kill_process_blocked(self):
        ctrl = RiskyToolConfirm()
        mw = ctrl.as_middleware()
        req = _mk_req("kill_process", {"pid": 1234})
        handler = _FakeHandler()

        result = _run(mw.awrap_tool_call(req, handler))

        assert isinstance(result, ToolMessage)
        assert handler.calls == []
        parsed = json.loads(result.content)
        assert parsed["tool"] == "kill_process"
        assert parsed["args"]["pid"] == 1234

    def test_non_risky_tool_passes_through(self):
        ctrl = RiskyToolConfirm()
        mw = ctrl.as_middleware()
        req = _mk_req("get_time", {})
        handler = _FakeHandler()

        result = _run(mw.awrap_tool_call(req, handler))

        assert handler.calls == [req]
        assert result == "CALLED:get_time"


class TestMiddlewareAcceptsConfirmedFlag:
    def test_args_with_confirmed_true_passes_through_and_strips_flag(self):
        ctrl = RiskyToolConfirm()
        mw = ctrl.as_middleware()
        # 用一个真实参数确认 handler 收到的是干净 dict
        req = _mk_req("lock_screen", {"__confirmed": True, "reason": "user said yes"})
        handler = _FakeHandler()

        result = _run(mw.awrap_tool_call(req, handler))

        # handler 必须被调用
        assert len(handler.calls) == 1
        passed_args = handler.calls[0].tool_call["args"]
        assert passed_args == {"reason": "user said yes"}
        assert _CONFIRM_KEY not in passed_args
        # 拦截器记录了通过
        assert ctrl.is_confirmed(req.tool_call["id"])
        # 返回 handler 的真实结果
        assert result == "CALLED:lock_screen"

    def test_args_with_confirmed_false_still_blocked(self):
        ctrl = RiskyToolConfirm()
        mw = ctrl.as_middleware()
        req = _mk_req("lock_screen", {"__confirmed": False})
        handler = _FakeHandler()

        result = _run(mw.awrap_tool_call(req, handler))

        assert handler.calls == []
        assert isinstance(result, ToolMessage)


class TestMiddlewareHonorsExplicitConfirm:
    def test_explicit_confirm_lets_subsequent_call_through(self):
        ctrl = RiskyToolConfirm()
        ctrl.confirm("call_pre_confirmed")
        mw = ctrl.as_middleware()
        req = _mk_req("lock_screen", {}, call_id="call_pre_confirmed")
        handler = _FakeHandler()

        result = _run(mw.awrap_tool_call(req, handler))

        assert handler.calls == [req]
        assert result == "CALLED:lock_screen"

    def test_explicit_confirm_does_not_apply_to_different_id(self):
        ctrl = RiskyToolConfirm()
        ctrl.confirm("call_A")
        mw = ctrl.as_middleware()
        req = _mk_req("lock_screen", {}, call_id="call_B")
        handler = _FakeHandler()

        result = _run(mw.awrap_tool_call(req, handler))

        assert handler.calls == []
        assert isinstance(result, ToolMessage)


class TestMiddlewareRejects:
    def test_explicit_reject_blocks_forever(self):
        ctrl = RiskyToolConfirm()
        ctrl.reject("call_x")
        mw = ctrl.as_middleware()
        req = _mk_req("lock_screen", {}, call_id="call_x")
        handler = _FakeHandler()

        result = _run(mw.awrap_tool_call(req, handler))

        assert handler.calls == []
        parsed = json.loads(result.content)
        assert parsed["status"] == "rejected"

    def test_rejected_cannot_be_bypassed_by_confirmed_flag(self):
        """被 reject 之后,即使 args 里塞 __confirmed=True 也照样拦。"""
        ctrl = RiskyToolConfirm()
        ctrl.reject("call_y")
        mw = ctrl.as_middleware()
        req = _mk_req("lock_screen", {"__confirmed": True}, call_id="call_y")
        handler = _FakeHandler()

        result = _run(mw.awrap_tool_call(req, handler))

        assert handler.calls == []
        parsed = json.loads(result.content)
        assert parsed["status"] == "rejected"


class TestMiddlewareOnBlockedCallback:
    def test_callback_fires_on_block(self):
        ctrl = RiskyToolConfirm()
        seen: list = []

        def cb(call_id, name, args):
            seen.append((call_id, name, args))

        ctrl.on_blocked(cb)
        mw = ctrl.as_middleware()
        req = _mk_req("set_wifi", {"enabled": False})
        handler = _FakeHandler()

        _run(mw.awrap_tool_call(req, handler))

        assert seen == [("call_xyz", "set_wifi", {"enabled": False})]

    def test_callback_not_called_when_passing_through(self):
        ctrl = RiskyToolConfirm()
        seen: list = []
        ctrl.on_blocked(lambda *a: seen.append(a))
        mw = ctrl.as_middleware()
        req = _mk_req("get_time", {})
        handler = _FakeHandler()

        _run(mw.awrap_tool_call(req, handler))

        assert seen == []

    def test_callback_exception_does_not_break_middleware(self):
        ctrl = RiskyToolConfirm()

        def bad_cb(*a, **kw):
            raise RuntimeError("boom")

        ctrl.on_blocked(bad_cb)
        mw = ctrl.as_middleware()
        req = _mk_req("lock_screen", {})
        handler = _FakeHandler()

        # 抛异常被吞掉,拦截仍生效
        result = _run(mw.awrap_tool_call(req, handler))

        assert isinstance(result, ToolMessage)
        assert handler.calls == []

    def test_callback_returning_true_passes_through(self):
        """2026-10-15:UI callback 返回 True → 立即放行(不再等模型重试 __confirmed)。"""
        ctrl = RiskyToolConfirm()

        def approve(*a, **kw):
            return True

        ctrl.on_blocked(approve)
        mw = ctrl.as_middleware()
        req = _mk_req("lock_screen", {})
        handler = _FakeHandler()

        result = _run(mw.awrap_tool_call(req, handler))

        # callback 同意 → handler 被调用,不是拦截
        assert result == "CALLED:lock_screen"
        assert len(handler.calls) == 1

    def test_callback_returning_false_keeps_blocked(self):
        """callback 返回 False → 仍然拦截,返回 sentinel。"""
        ctrl = RiskyToolConfirm()

        def reject(*a, **kw):
            return False

        ctrl.on_blocked(reject)
        mw = ctrl.as_middleware()
        req = _mk_req("lock_screen", {})
        handler = _FakeHandler()

        result = _run(mw.awrap_tool_call(req, handler))

        assert isinstance(result, ToolMessage)
        assert handler.calls == []

    def test_no_callback_blocks(self):
        """没注册 callback → 默认拦截(行为和 _RISKY_TOOLS 重定向到模型重试协议一致)。"""
        ctrl = RiskyToolConfirm()
        mw = ctrl.as_middleware()
        req = _mk_req("lock_screen", {})
        handler = _FakeHandler()

        result = _run(mw.awrap_tool_call(req, handler))

        assert isinstance(result, ToolMessage)
        assert handler.calls == []

    def test_multiple_callbacks_any_true_passes(self):
        """多个 callback,任一返回 True → 放行。"""
        ctrl = RiskyToolConfirm()

        def reject_first(*a, **kw):
            return False

        def approve_second(*a, **kw):
            return True

        ctrl.on_blocked(reject_first)
        ctrl.on_blocked(approve_second)
        mw = ctrl.as_middleware()
        req = _mk_req("kill_process", {"pid": 1})
        handler = _FakeHandler()

        result = _run(mw.awrap_tool_call(req, handler))

        assert result == "CALLED:kill_process"
        assert len(handler.calls) == 1


# -------------------- build_hallucination_middlewares 集成 --------------------

class TestBuildReturnsTuple:
    def test_returns_middlewares_and_controller(self):
        from app.brain.hallucination_guard import build_hallucination_middlewares
        mws, ctrl = build_hallucination_middlewares()
        assert isinstance(mws, list)
        assert len(mws) == 3  # orphan_pruner + grounding + logger(confirm 拆出)
        assert isinstance(ctrl, RiskyToolConfirm)

    def test_fresh_build_returns_fresh_controller(self):
        """每次 build 都是新 controller —— 旧 ID 不污染新 session。"""
        from app.brain.hallucination_guard import build_hallucination_middlewares
        _, ctrl1 = build_hallucination_middlewares()
        _, ctrl2 = build_hallucination_middlewares()
        ctrl1.confirm("call_abc")
        assert ctrl2.is_confirmed("call_abc") is False


# -------------------- LangChainAgent 集成 --------------------

class TestLangChainAgentConfirmAPI:
    """LangChainAgent 必须在 __init__ 就把 _confirm_ctrl 建好,
    confirm_tool_call / reject_tool_call / on_confirm_needed 三个公开 API
    在 _ensure_agent 跑之前也能用(UI 早订阅)。"""

    def _make_agent(self):
        from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
        from app.brain.llm_client import LLMConfig
        from app.engine.tools import ToolRegistry

        reg = ToolRegistry()
        agent = LangChainAgent(
            llm_cfg=LLMConfig(),
            registry=reg,
            cfg=LangChainAgentConfig(),
            thread_id="t-test",
            persona="test",
        )
        return agent

    def test_init_creates_confirm_ctrl(self):
        agent = self._make_agent()
        assert agent._confirm_ctrl is not None
        assert isinstance(agent._confirm_ctrl, RiskyToolConfirm)

    def test_confirm_tool_call_records_id(self):
        agent = self._make_agent()
        agent.confirm_tool_call("abc")
        assert agent._confirm_ctrl.is_confirmed("abc")

    def test_reject_tool_call_records_id(self):
        agent = self._make_agent()
        agent.reject_tool_call("xyz")
        assert agent._confirm_ctrl.is_rejected("xyz")

    def test_on_confirm_needed_registers_callback(self):
        agent = self._make_agent()
        seen = []
        agent.on_confirm_needed(lambda *a: seen.append(a))
        # 直接通过 controller 触发一次
        agent._confirm_ctrl._fire_blocked("c1", "lock_screen", {})
        assert seen == [("c1", "lock_screen", {})]

    def test_ensure_agent_middleware_uses_self_controller(self):
        """_ensure_agent 跑完后,middlewares 里 confirm 那个用的 controller
        必须 == self._confirm_ctrl(否则 UI 端 confirm() 不生效)。"""
        import asyncio
        from app.engine.tools import Tool, ToolRegistry

        agent = self._make_agent()
        # 注册一个工具确保 registry 有内容
        agent.registry.register(Tool(name="noop", description="n",
                                     parameters={}, fn=lambda: "ok"))
        original_ctrl = agent._confirm_ctrl
        original_ctrl.confirm("preexisting_id")

        asyncio.run(agent._ensure_agent())

        # 找中间件列表里的 confirm 中间件,跑一次拦截,确认
        # 1) middleware 是用 original_ctrl 构造的 2) preexisting_id 仍生效
        from langgraph.prebuilt.tool_node import ToolCallRequest
        confirm_mw = None
        for m in (agent._middlewares or []):
            name = getattr(m, "name", "")
            if name and "confirm" in name.lower():
                confirm_mw = m
                break
        assert confirm_mw is not None, "确认中间件没挂上"

        # 跑一次 lock_screen（已有并发版）,pre-existing ID 必须放行
        req = ToolCallRequest(
            tool_call={"name": "lock_screen", "args": {}, "id": "preexisting_id"},
            tool=None, state={}, runtime=None,
        )
        called_marker = []

        async def fake_handler(r):
            called_marker.append(r)
            return "OK"

        result = asyncio.run(confirm_mw.awrap_tool_call(req, fake_handler))
        assert result == "OK", f"preexisting confirm 失效: {result}"
        assert called_marker, "preexisting ID 应该直通"

    def test_ensure_agent_middleware_sees_new_confirms(self):
        """__init__ 后调 confirm_tool_call,_ensure_agent 之后该 ID 在中间件里仍然有效。"""
        import asyncio
        from app.engine.tools import Tool
        from langgraph.prebuilt.tool_node import ToolCallRequest

        agent = self._make_agent()
        agent.registry.register(Tool(name="noop", description="n",
                                     parameters={}, fn=lambda: "ok"))
        agent.confirm_tool_call("post_init_id")
        asyncio.run(agent._ensure_agent())

        confirm_mw = None
        for m in (agent._middlewares or []):
            if "confirm" in getattr(m, "name", "").lower():
                confirm_mw = m
                break

        async def handler(r):
            return "CALLED"

        req = ToolCallRequest(
            tool_call={"name": "lock_screen", "args": {}, "id": "post_init_id"},
            tool=None, state={}, runtime=None,
        )
        result = asyncio.run(confirm_mw.awrap_tool_call(req, handler))
        assert result == "CALLED"


# -------------------- 已知 RISKY_TOOLS 集合守卫 --------------------

class TestRiskyToolsSet:
    def test_set_contains_expected_tools(self):
        """回归:锁屏/杀进程/邮件这类绝不能漏。"""
        for name in ("lock_screen", "kill_process", "send_email",
                     "run_script", "send_notification"):
            assert name in _RISKY_TOOLS, f"{name} must be in _RISKY_TOOLS"