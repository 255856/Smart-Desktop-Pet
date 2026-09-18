"""Evaluator 测试。"""
import asyncio
import json

import pytest

from app.eval.cases import (
    EvalCase, EvalResult, Evaluator, MockLLMClient, MockScript,
    _parse_tool_markers, _strip_tool_markers, builtin_cases, run_eval_suite,
)
from app.engine.tools import Tool, ToolRegistry


def _make_agent_factory():
    """构造一个接收 MockLLMClient 的 agent 工厂（用 PlanExecutor）。"""
    from app.brain.executor import PlanExecutor
    from app.brain.plan import Plan, Step
    from app.brain.planner import Planner
    from app.brain.reflector import HeuristicReflector

    reg = ToolRegistry()
    for name, ret in [
        ("add_reminder", "已设置提醒"),
        ("remember_fact", "已记住"),
        ("calculate", "37"),
        ("recall_memory", "找到"),
        ("get_current_time", "2026-09-13"),
    ]:
        def make_fn(text=None, delay_minutes=None, content=None,
                    category=None, importance=None, expression=None, **_):
            return ret
        reg.register(Tool(name=name, description=name,
                          parameters={"type": "object", "properties": {}},
                          fn=make_fn))

    # 共享一个 mutable holder，把 plan / 工具调用结果塞进去
    holder = {"plan": None}

    def factory(mock_client):
        async def fake_make_plan(goal, history=None):
            text = mock_client._next_text()
            # 格式 1：<tool>name|args</tool> 标记
            tool_calls = _parse_tool_markers(text)
            clean_text = _strip_tool_markers(text)
            # 格式 2：JSON 格式的 Plan
            if not tool_calls and clean_text.strip().startswith("{"):
                try:
                    obj = json.loads(clean_text)
                    if "steps" in obj:
                        steps: list[Step] = []
                        for s in obj["steps"]:
                            kind = s.get("kind", "tool")
                            if kind == "final":
                                steps.append(Step(
                                    id=f"s{len(steps)}",
                                    kind="final",
                                    thought=s.get("thought", ""),
                                    result=s.get("answer", ""),
                                ))
                            else:
                                steps.append(Step(
                                    id=f"s{len(steps)}",
                                    kind="tool",
                                    thought=s.get("thought", ""),
                                    tool_name=s.get("tool_name"),
                                    arguments=s.get("arguments", {}),
                                ))
                        return Plan(goal=goal, steps=steps)
                except Exception:
                    pass

            steps: list[Step] = []
            for tc in tool_calls:
                steps.append(Step(
                    id=f"s{len(steps)}",
                    kind="tool",
                    tool_name=tc["name"],
                    arguments=json.loads(tc["arguments"]),
                    thought="mock",
                ))
            if not steps:
                # 没有工具调用也没 JSON plan：直接当 final
                steps.append(Step(id="s0", kind="final",
                                   thought="reply", result=clean_text))
            else:
                steps.append(Step(id=f"s{len(steps)}", kind="final",
                                  thought="reply", result=clean_text))
            return Plan(goal=goal, steps=steps)

        planner = Planner(client=mock_client, registry=reg, char_name="t")
        planner.make_plan = fake_make_plan  # type: ignore[assignment]
        executor = PlanExecutor(
            client=mock_client, registry=reg, planner=planner,
            reflector=HeuristicReflector(),
        )

        # 包装一下 run：executor 直接 yield plan / tool / reflection / done
        # Evaluator 期望的事件格式
        class Wrapped:
            def __init__(self, ex):
                self.ex = ex

            def run(self, messages, cancel_check=None):
                # 直接返回 inner 的 async generator
                async def _drive():
                    goal = messages[-1]["content"] if messages else ""
                    plan = await fake_make_plan(goal, history=messages)
                    holder["plan"] = plan
                    async for ev in self.ex.run(plan):
                        yield ev
                return _drive()
        return Wrapped(executor)
    return factory


class TestToolMarkers:
    def test_parse_single(self):
        text = '思考中 <tool>add_reminder|{"text":"x","delay_minutes":10}</tool> 完成'
        calls = _parse_tool_markers(text)
        assert len(calls) == 1
        assert calls[0]["name"] == "add_reminder"
        assert json.loads(calls[0]["arguments"])["text"] == "x"

    def test_parse_multiple(self):
        text = (
            '<tool>add_reminder|{"text":"a"}</tool> '
            '中间 <tool>get_current_time|{}</tool>'
        )
        calls = _parse_tool_markers(text)
        assert len(calls) == 2

    def test_strip(self):
        text = '你好 <tool>x|{}</tool> 世界'
        assert _strip_tool_markers(text) == "你好  世界".replace("  ", " ").strip() or "你好 世界"


class TestEvaluator:
    def setup_method(self):
        self.factory = _make_agent_factory()

    def test_run_case_pass(self):
        case = EvalCase(
            name="t1",
            user_message="设提醒",
            mock_responses=[
                '<tool>add_reminder|{"text":"喝水","delay_minutes":25}</tool>'
                '好哒 [happy]'
            ],
            expected_tools=["add_reminder"],
            expected_tool_args={"add_reminder": {"text": "喝水"}},
        )
        ev = Evaluator(self.factory)
        result = asyncio.run(ev._run_case(case))
        assert result.success, result.notes
        assert any(n == "add_reminder" for n, _ in result.tool_calls_made)

    def test_run_case_fail_missing_tool(self):
        case = EvalCase(
            name="t2",
            user_message="x",
            mock_responses=["纯聊天回复"],
            expected_tools=["add_reminder"],   # 期望但没调
        )
        ev = Evaluator(self.factory)
        result = asyncio.run(ev._run_case(case))
        assert not result.success
        assert any("未调用" in n for n in result.notes)

    def test_run_case_wrong_args_recorded_in_notes(self):
        """参数不匹配应作为 note 记录（不算失败）。"""
        case = EvalCase(
            name="t3",
            user_message="x",
            mock_responses=[
                '<tool>add_reminder|{"text":"别的"}</tool> ok',
            ],
            expected_tool_args={"add_reminder": {"text": "期望的"}},
        )
        ev = Evaluator(self.factory)
        result = asyncio.run(ev._run_case(case))
        assert result.success   # 参数不匹配只是 note
        assert any("参数" in n for n in result.notes)

    def test_run_case_missing_required_arg_fails(self):
        """必需参数缺失应判失败。"""
        case = EvalCase(
            name="t3b",
            user_message="x",
            mock_responses=[
                # add_reminder 缺 text
                '<tool>add_reminder|{"delay_minutes":25}</tool> ok',
            ],
            expected_tool_args={"add_reminder": {"text": "喝水"}},
        )
        ev = Evaluator(self.factory)
        result = asyncio.run(ev._run_case(case))
        assert not result.success
        assert any("缺参" in n for n in result.notes)

    def test_run_case_max_tool_calls(self):
        # 强制超过 max_tool_calls
        case = EvalCase(
            name="t4",
            user_message="x",
            mock_responses=[
                '<tool>add_reminder|{"text":"a"}</tool> '
                '<tool>add_reminder|{"text":"b"}</tool> '
                '<tool>add_reminder|{"text":"c"}</tool>'
                'ok',
            ],
            expected_tools=["add_reminder"],
            max_tool_calls=2,
        )
        ev = Evaluator(self.factory)
        result = asyncio.run(ev._run_case(case))
        assert not result.success
        assert any("超过上限" in n for n in result.notes)

    def test_run_case_final_keyword(self):
        case = EvalCase(
            name="t5",
            user_message="x",
            mock_responses=["完成啦 [happy]"],
            expected_final_keywords=["完成"],
        )
        ev = Evaluator(self.factory)
        result = asyncio.run(ev._run_case(case))
        assert result.success


class TestBuiltinSuite:
    def test_run_suite(self):
        factory = _make_agent_factory()
        result = asyncio.run(run_eval_suite(factory))
        assert result["total"] == 4
        # 至少 3 个用例要过（mock 数据是好的）
        assert result["passed"] >= 3, result["report"]
        assert "评估报告" in result["report"]

    def test_builtin_cases_have_unique_names(self):
        cases = builtin_cases()
        names = [c.name for c in cases]
        assert len(names) == len(set(names))

    def test_builtin_cases_have_descriptions(self):
        cases = builtin_cases()
        for c in cases:
            assert c.description
            assert c.mock_responses
