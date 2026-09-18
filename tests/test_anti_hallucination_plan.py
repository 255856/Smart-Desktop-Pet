"""【抗幻觉】PlanExecutor 跨步检测幻觉 → replan。

测试场景：
    用户说「帮我打开 QQ」→ Planner 生成的 plan 只有 final 步骤（模型幻觉）→
    HeuristicReflector.detect_plan_hallucination 应该返回 Reflection("replan")
    → PlanExecutor 触发 replan。
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".local-packages"))
sys.path.insert(0, str(ROOT))


class TestPlanExecutorHallucinationDetection:
    """PlanExecutor 检测「意图是工具但 plan 里没真调工具」→ 触发 replan。"""

    def _make_registry(self):
        from app.engine.tools import Tool, ToolRegistry
        reg = ToolRegistry()

        def mock_open_app(app_name: str) -> str:
            return f"已启动 {app_name}"

        reg.register(Tool(
            name="open_app", description="open app",
            parameters={"type": "object",
                        "properties": {"app_name": {"type": "string"}}},
            fn=mock_open_app,
        ))
        return reg

    def test_hallucination_detection_triggers_replan(self):
        """plan 只有 final 步骤 + 意图是工具 → 触发 replan。"""
        from app.brain.plan import Plan, Step
        from app.brain.reflector import HeuristicReflector
        from app.brain.executor import PlanExecutor
        from app.brain.planner import Planner

        reg = self._make_registry()

        # 模拟 Planner 第一次返回只有 final 的 plan（幻觉）
        # 第二次返回有 tool 步骤的 plan（重规划成功）
        call_count = [0]

        async def fake_make_plan(goal, history=None):
            call_count[0] += 1
            if call_count[0] == 1:
                # 第 1 次：模型幻觉，只给 final
                return Plan(goal=goal, steps=[
                    Step(id="s1", kind="final",
                         thought="直接回答", result="已打开 QQ 啦~ [happy]"),
                ])
            else:
                # replan：模型学乖了，加了 tool 步骤
                return Plan(goal=goal, steps=[
                    Step(id="s1", kind="tool", tool_name="open_app",
                         arguments={"app_name": "QQ"}, thought="调用 open_app"),
                    Step(id="s2", kind="final",
                         thought="整合", result="QQ 已经打开啦~ [happy]"),
                ])

        async def fake_replan(plan, history=None):
            return await fake_make_plan(plan.goal, history)

        planner = Planner(client=None, registry=reg, char_name="test")
        planner.make_plan = fake_make_plan  # type: ignore[assignment]
        planner.replan = fake_replan  # type: ignore[assignment]

        executor = PlanExecutor(
            client=None, registry=reg, planner=planner,
            reflector=None,    # 默认 heuristic（构造时会注入 plan_goal）
            max_replans=3,
        )

        async def drive():
            events = []
            plan = await planner.make_plan("帮我打开 QQ")
            async for ev in executor.run(plan):
                events.append(ev)
            return events

        events = asyncio.run(drive())
        # 第 1 次 plan 跑完后应该触发 replan（因为整个 plan 没真调工具）
        kinds = [e[0] for e in events]
        # 至少有 2 个 plan 事件（原 plan + replan）
        assert kinds.count("plan") >= 2, (
            f"应触发 replan，事件流 = {kinds[:20]}..."
        )
        # 应该有 reflection 事件标记 anti_hallucination 触发
        reflections = [e for e in events if e[0] == "reflection"]
        assert any(r[1].get("reason") == "anti_hallucination" for r in reflections), (
            f"应触发 anti_hallucination reflection，reflections = {reflections}"
        )
        # 最终应该看到 tool 事件（replan 后真调了工具）
        tool_events = [e for e in events if e[0] == "tool"]
        assert tool_events, "replan 后应该真调工具"
        assert tool_events[0][1] == "open_app"

    def test_no_replan_when_tool_was_actually_called(self):
        """plan 里有 tool 步骤且真调成功 → 不触发 anti_hallucination replan。"""
        from app.brain.plan import Plan, Step
        from app.brain.executor import PlanExecutor
        from app.brain.planner import Planner

        reg = self._make_registry()

        async def fake_make_plan(goal, history=None):
            return Plan(goal=goal, steps=[
                Step(id="s1", kind="tool", tool_name="open_app",
                     arguments={"app_name": "QQ"}, thought="open it"),
                Step(id="s2", kind="final", result="打开啦~"),
            ])

        planner = Planner(client=None, registry=reg, char_name="test")
        planner.make_plan = fake_make_plan  # type: ignore[assignment]
        planner.replan = AsyncMock()  # type: ignore[assignment]

        executor = PlanExecutor(
            client=None, registry=reg, planner=planner,
            reflector=None,
        )

        async def drive():
            events = []
            plan = await planner.make_plan("帮我打开 QQ")
            async for ev in executor.run(plan):
                events.append(ev)
            return events

        events = asyncio.run(drive())
        # 不应触发 replan（plan.replan 未被调用）
        planner.replan.assert_not_called()
        # 只应该有 1 个 plan 事件
        kinds = [e[0] for e in events]
        assert kinds.count("plan") == 1
        # 应该有 tool 事件
        tool_events = [e for e in events if e[0] == "tool"]
        assert tool_events
        # 不应该有 anti_hallucination reflection
        reflections = [e for e in events if e[0] == "reflection"]
        assert not any(r[1].get("reason") == "anti_hallucination"
                       for r in reflections)

    def test_no_replan_for_chitchat_intent(self):
        """意图不是工具动作（闲聊）→ 即使 plan 只有 final 也不触发 anti_hallucination。"""
        from app.brain.plan import Plan, Step
        from app.brain.executor import PlanExecutor
        from app.brain.planner import Planner

        reg = self._make_registry()

        async def fake_make_plan(goal, history=None):
            return Plan(goal=goal, steps=[
                Step(id="s1", kind="final",
                     thought="闲聊回复", result="好呀主人~"),
            ])

        planner = Planner(client=None, registry=reg, char_name="test")
        planner.make_plan = fake_make_plan  # type: ignore[assignment]
        planner.replan = AsyncMock()  # type: ignore[assignment]

        executor = PlanExecutor(
            client=None, registry=reg, planner=planner,
            reflector=None,
        )

        async def drive():
            events = []
            plan = await planner.make_plan("陪我聊聊天")
            async for ev in executor.run(plan):
                events.append(ev)
            return events

        events = asyncio.run(drive())
        planner.replan.assert_not_called()
        # 不应该有 anti_hallucination reflection
        reflections = [e for e in events if e[0] == "reflection"]
        assert not any(r[1].get("reason") == "anti_hallucination"
                       for r in reflections)
