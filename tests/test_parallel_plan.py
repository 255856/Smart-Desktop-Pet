"""测试 PlanExecutor 的 parallel_group 并行能力。"""
import asyncio
import json
import time

from app.brain._legacy.plan import Plan, Step
from app.brain._legacy.executor import PlanExecutor
from app.brain._legacy.planner import Planner
from app.brain._legacy.reflector import HeuristicReflector
from app.engine.tools import Tool, ToolRegistry


class TestParallelSteps:
    def setup_method(self):
        self.reg = ToolRegistry()
        # 三个会 sleep 0.5s 的工具
        for name in ["t1", "t2", "t3"]:
            def make_fn(n=name):
                def fn(**_):
                    time.sleep(0.5)
                    return f"{n}-done"
                return fn
            self.reg.register(Tool(
                name=name, description=name,
                parameters={"type": "object", "properties": {}},
                fn=make_fn(),
            ))

    def _make_plan(self, with_parallel: bool) -> Plan:
        kwargs = {"thought": "do", "arguments": {}}
        if with_parallel:
            steps = [
                Step(id="1", kind="tool", tool_name="t1",
                     parallel_group="g1", **kwargs),
                Step(id="2", kind="tool", tool_name="t2",
                     parallel_group="g1", **kwargs),
                Step(id="3", kind="tool", tool_name="t3",
                     parallel_group="g1", **kwargs),
                Step(id="4", kind="final", thought="r", result="all done"),
            ]
        else:
            steps = [
                Step(id="1", kind="tool", tool_name="t1", **kwargs),
                Step(id="2", kind="tool", tool_name="t2", **kwargs),
                Step(id="3", kind="tool", tool_name="t3", **kwargs),
                Step(id="4", kind="final", thought="r", result="all done"),
            ]
        return Plan(goal="g", steps=steps)

    def _make_executor(self):
        async def fake_plan(goal, history=None):
            return self._make_plan(with_parallel=True)
        planner = Planner(client=None, registry=self.reg, char_name="t")
        planner.make_plan = fake_plan
        return PlanExecutor(client=None, registry=self.reg,
                            planner=planner, reflector=HeuristicReflector())

    def test_parallel_group_runs_concurrently(self):
        """3 个 0.5s 工具并行执行应 ~0.5s，串行应 ~1.5s。"""
        async def drive():
            ex = self._make_executor()
            plan = self._make_plan(with_parallel=True)
            t0 = time.time()
            events = []
            async for ev in ex.run(plan):
                events.append(ev)
                if ev[0] == "done":
                    break
            return time.time() - t0, events
        elapsed, events = asyncio.run(drive())
        # 并行：~0.5s（容许到 1.0s）
        assert elapsed < 1.0, f"并行执行应该 <1.0s，实际 {elapsed:.2f}s"
        # 事件流应该包含 3 个 tool 事件 + 1 个 done
        tool_events = [e for e in events if e[0] == "tool"]
        assert len(tool_events) == 3
        # done 事件有 final answer
        done = next(e for e in events if e[0] == "done")
        assert done[1] == "all done"

    def test_serial_takes_longer(self):
        """3 个 0.5s 工具串行应 ~1.5s。"""
        async def drive():
            plan = self._make_plan(with_parallel=False)
            ex = self._make_executor()   # 复用 executor，但 plan 不带 group
            t0 = time.time()
            events = []
            async for ev in ex.run(plan):
                events.append(ev)
                if ev[0] == "done":
                    break
            return time.time() - t0
        elapsed = asyncio.run(drive())
        # 串行：~1.5s（容许 1.0 ~ 2.5s）
        assert elapsed > 1.0, f"串行执行应该 >1.0s，实际 {elapsed:.2f}s"
