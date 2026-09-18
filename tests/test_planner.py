"""Planner / Reflector / Executor / AgentLoopV2 单元测试。

由于 Planner/Reflector 的 LLM 部分不能离线稳定测，
这里只测纯逻辑：JSON 解析、Reflection、Plan 状态机、Executor mock 执行。
"""
import asyncio
import json
import os
import tempfile

import pytest

from app.brain.plan import (
    Plan,
    Step,
    extract_plan_json,
    parse_plan_from_llm,
    plan_to_compact_text,
)


class TestPlanJSON:
    def test_extract_from_codeblock(self):
        text = '思考中\n```json\n{"steps": [{"kind": "final", "answer": "hi"}]}\n```\n完毕'
        obj = extract_plan_json(text)
        assert obj is not None
        assert "steps" in obj

    def test_extract_from_inline_json(self):
        text = '这里有个计划 {"steps":[{"kind":"tool","tool_name":"add_reminder","arguments":{"text":"x"}}]} 后面'
        obj = extract_plan_json(text)
        assert obj is not None
        assert obj["steps"][0]["tool_name"] == "add_reminder"

    def test_extract_returns_none_on_garbage(self):
        assert extract_plan_json("完全没 JSON 的东西") is None
        assert extract_plan_json("") is None

    def test_parse_plan_final_only(self):
        text = '```json\n{"steps":[{"kind":"final","thought":"直接回答","answer":"你好呀"}]}\n```'
        plan = parse_plan_from_llm(text, goal="打招呼")
        assert len(plan.steps) == 1
        assert plan.steps[0].kind == "final"
        assert plan.steps[0].result == "你好呀"

    def test_parse_plan_multi_tool(self):
        text = json.dumps({
            "steps": [
                {"kind": "tool", "thought": "记一下", "tool_name": "remember_fact",
                 "arguments": {"content": "主人喜欢咖啡"}},
                {"kind": "final", "thought": "回复", "answer": "记好啦"},
            ]
        }, ensure_ascii=False)
        plan = parse_plan_from_llm(text, goal="记住咖啡")
        assert len(plan.steps) == 2
        assert plan.steps[0].tool_name == "remember_fact"
        assert plan.steps[1].kind == "final"

    def test_parse_plan_fallback_to_final(self):
        plan = parse_plan_from_llm("纯文本，无 JSON", goal="hi")
        assert len(plan.steps) == 1
        assert plan.steps[0].kind == "final"
        assert "纯文本" in (plan.steps[0].result or "")

    def test_parse_plan_invalid_step_kind_filtered(self):
        text = json.dumps({
            "steps": [
                {"kind": "weird", "thought": "未知类型"},
                {"kind": "tool", "tool_name": "x", "arguments": {}},
            ]
        })
        plan = parse_plan_from_llm(text, goal="x")
        # 第一个 weird 会被默认为 tool（容错），但无 tool_name 也行
        assert len(plan.steps) == 2


class TestPlanState:
    def test_current_step(self):
        plan = Plan(goal="x", steps=[
            Step(id="1", kind="final", result="hi"),
            Step(id="2", kind="final", result="hi2"),
        ])
        assert plan.current_step.id == "1"
        plan.current = 1
        assert plan.current_step.id == "2"

    def test_is_done(self):
        plan = Plan(goal="x", steps=[Step(id="1", kind="final", result="hi")])
        assert plan.is_done is False
        plan.current = 1
        assert plan.is_done is True

    def test_plan_to_compact_text(self):
        plan = Plan(goal="g", steps=[
            Step(id="1", kind="tool", tool_name="t", arguments={"a": 1},
                 thought="do", status="done", result="ok"),
        ])
        s = plan_to_compact_text(plan)
        assert "g" in s
        assert "t(" in s
        assert "ok" in s


class TestReflectorHeuristic:
    def test_final_with_result_ok(self):
        from app.brain.reflector import HeuristicReflector
        r = HeuristicReflector().reflect(
            Step(id="1", kind="final", result="hi"))
        assert r.verdict == "ok"

    def test_final_empty_retry(self):
        from app.brain.reflector import HeuristicReflector
        r = HeuristicReflector().reflect(
            Step(id="1", kind="final", result=""))
        assert r.verdict == "retry"

    def test_tool_error_retry_then_replan(self):
        from app.brain.reflector import HeuristicReflector
        s = Step(id="1", kind="tool", tool_name="x",
                 arguments={}, status="failed", result="错误：xxx", retry_count=0)
        r1 = HeuristicReflector().reflect(s)
        assert r1.verdict == "retry"
        s.retry_count = 3
        r2 = HeuristicReflector().reflect(s)
        assert r2.verdict == "replan"

    def test_tool_success(self):
        from app.brain.reflector import HeuristicReflector
        s = Step(id="1", kind="tool", tool_name="x", arguments={},
                 status="done", result="操作成功")
        r = HeuristicReflector().reflect(s)
        assert r.verdict == "ok"

    def test_make_reflector_modes(self):
        from app.brain.reflector import make_reflector
        assert make_reflector("off") is not None
        assert make_reflector("heuristic") is not None
        assert make_reflector("llm") is not None   # 没传 client 也会回退 heuristic


class TestExecutor:
    def _make_registry(self):
        from app.engine.tools import Tool, ToolRegistry
        reg = ToolRegistry()
        reg.register(Tool(
            name="mock_ok",
            description="总是返回 ok 的 mock 工具",
            parameters={"type": "object", "properties": {}},
            fn=lambda: "mock-ok-result",
        ))
        reg.register(Tool(
            name="mock_err",
            description="总是报错的 mock 工具",
            parameters={"type": "object", "properties": {}},
            fn=lambda: (_ for _ in ()).throw(RuntimeError("mock boom")),
        ))
        return reg

    def test_run_tool_success(self):
        from app.brain.executor import PlanExecutor
        from app.brain.planner import Planner
        from app.engine.tools import ToolRegistry

        reg = self._make_registry()

        # Mock client 不调 LLM：Planner.make_plan 用 mock client 也行，
        # 这里直接构造 Plan 跳过 Planner
        async def fake_make_plan(goal, history=None):
            return Plan(goal=goal, steps=[
                Step(id="s1", kind="tool", tool_name="mock_ok",
                     arguments={}, thought="做某事"),
            ])
        planner = Planner(client=None, registry=reg, char_name="test")
        planner.make_plan = fake_make_plan  # type: ignore[assignment]

        executor = PlanExecutor(
            client=None, registry=reg, planner=planner,
            reflector=None,  # 用默认 heuristic
        )

        async def drive():
            events = []
            plan = await planner.make_plan("goal")
            async for ev in executor.run(plan):
                events.append(ev)
            return events

        events = asyncio.run(drive())
        kinds = [e[0] for e in events]
        assert "plan" in kinds
        assert "reflection" in kinds
        assert "tool" in kinds
        assert "done" in kinds
        # 工具结果应透传
        tool_evt = next(e for e in events if e[0] == "tool")
        assert tool_evt[1] == "mock_ok"
        assert tool_evt[3] == "mock-ok-result"

    def test_run_final_step(self):
        from app.brain.executor import PlanExecutor
        from app.brain.planner import Planner

        reg = self._make_registry()

        async def fake_make_plan(goal, history=None):
            return Plan(goal=goal, steps=[
                Step(id="s1", kind="final", result="你好主人"),
            ])
        planner = Planner(client=None, registry=reg, char_name="t")
        planner.make_plan = fake_make_plan  # type: ignore[assignment]
        executor = PlanExecutor(client=None, registry=reg, planner=planner)

        async def drive():
            events = []
            plan = await planner.make_plan("g")
            async for ev in executor.run(plan):
                events.append(ev)
            return events

        events = asyncio.run(drive())
        done = next(e for e in events if e[0] == "done")
        assert done[1] == "你好主人"

    def test_run_retry_then_success(self):
        from app.brain.executor import PlanExecutor
        from app.brain.planner import Planner
        from app.brain.reflector import HeuristicReflector

        reg = self._make_registry()

        # 写一个前两次失败、第三次成功的工具
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("transient")
            return "ok"

        from app.engine.tools import Tool, ToolRegistry
        reg2 = ToolRegistry()
        reg2.register(Tool(
            name="flaky", description="flaky",
            parameters={"type": "object", "properties": {}},
            fn=flaky,
        ))

        async def fake_make_plan(goal, history=None):
            return Plan(goal=goal, steps=[
                Step(id="s1", kind="tool", tool_name="flaky",
                     arguments={}, thought="try"),
            ])
        planner = Planner(client=None, registry=reg2, char_name="t")
        planner.make_plan = fake_make_plan  # type: ignore[assignment]
        executor = PlanExecutor(
            client=None, registry=reg2, planner=planner,
            reflector=HeuristicReflector(),
            max_retries=3,
        )

        async def drive():
            events = []
            plan = await planner.make_plan("g")
            async for ev in executor.run(plan):
                events.append(ev)
            return events

        events = asyncio.run(drive())
        # 应至少有 2 次 reflection（retry + ok）
        reflections = [e for e in events if e[0] == "reflection"]
        assert len(reflections) >= 2
        # 最终 done
        assert any(e[0] == "done" for e in events)


class TestAgentLoopV2Factory:
    def test_make_agent_loop_default(self):
        from app.brain.agent_v2 import make_agent_loop
        from app.brain.llm_client import LLMClient
        from app.brain.plan import Plan
        from app.engine.tools import Tool, ToolRegistry
        reg = ToolRegistry()
        reg.register(Tool(name="x", description="x",
                          parameters={"type": "object", "properties": {}},
                          fn=lambda: "ok"))
        client = LLMClient.__new__(LLMClient)   # 不真正初始化 httpx
        client.cfg = None
        client.system_prompt = ""
        loop = make_agent_loop(client, reg, enable_planning=True)
        assert loop.mode == "react"
        assert loop.executor is not None

    def test_make_agent_loop_single_mode(self):
        from app.brain.agent_v2 import make_agent_loop
        from app.brain.llm_client import LLMClient
        from app.engine.tools import Tool, ToolRegistry
        reg = ToolRegistry()
        client = LLMClient.__new__(LLMClient)
        client.cfg = None
        client.system_prompt = ""
        loop = make_agent_loop(client, reg, enable_planning=False)
        assert loop.mode == "single"
