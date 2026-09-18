"""AgentLoopV2：在原 AgentLoop 之上提供「是否启用 ReAct Plan-Execute-Reflect」的开关。

接口与 AgentLoop 兼容：run(messages, cancel_check) → AsyncIterator[event]。
两种模式：
    - mode="react"（默认）：Planner + Executor + Reflector，多步规划 + 反思
    - mode="single"：原 AgentLoop 行为，单轮最多 N 步工具调用

产出事件保持兼容：
    ("text", chunk) / ("tool", name, args, result) / ("done", text)
新增事件：
    ("plan", plan_dict) / ("reflection", reflection_dict)
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Callable, Optional

from app.brain.agent import AgentLoop as _LegacyAgentLoop
from app.brain.executor import PlanExecutor
from app.brain.llm_client import LLMClient
from app.brain.plan import Plan
from app.brain.planner import Planner
from app.brain.reflector import make_reflector
from app.engine.tools import ToolRegistry

log = logging.getLogger(__name__)


class AgentLoopV2:
    def __init__(
        self,
        client: LLMClient,
        registry: ToolRegistry,
        mode: str = "react",
        max_turns: int = 6,
        confirm_tool: Optional[Callable[[str, str], bool]] = None,
        persona: str = "",
        char_name: str = "桌宠",
        reflector_mode: str = "heuristic",
        max_replans: int = 2,
    ):
        """
        Args:
            mode: "react" | "single"
            max_turns: single 模式下的最多轮数
            confirm_tool: 危险工具确认回调
            persona: 角色人设（planner 用）
            char_name: 角色名（planner 用）
            reflector_mode: "heuristic" | "llm" | "off"
        """
        self.client = client
        self.registry = registry
        self.mode = mode
        self.max_turns = max_turns
        self.confirm_tool = confirm_tool

        # legacy / plan-exec 都构造好
        self._legacy = _LegacyAgentLoop(
            client=client, registry=registry,
            max_turns=max_turns, confirm_tool=confirm_tool,
        )
        if registry is not None and registry.names():
            self.planner = Planner(
                client=client, registry=registry,
                char_name=char_name, persona=persona,
            )
            self.reflector = make_reflector(reflector_mode, client)
            self.executor = PlanExecutor(
                client=client, registry=registry,
                planner=self.planner, reflector=self.reflector,
                max_replans=max_replans, confirm_tool=confirm_tool,
            )
        else:
            self.planner = None
            self.executor = None

    async def run(
        self,
        messages: list[dict],
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[tuple]:
        """主入口。根据 mode 走 legacy / plan-exec。"""
        if self.mode == "single" or self.executor is None:
            async for ev in self._legacy.run(messages, cancel_check=cancel_check):
                yield ev
            return

        # react 模式
        # 1) 提取用户最新一条作为 goal
        goal = ""
        for m in reversed(messages):
            if m.get("role") == "user" and m.get("content"):
                goal = m["content"]
                break
        if not goal:
            goal = "（空目标）"

        # 2) Planner 出 Plan
        try:
            plan: Plan = await self.planner.make_plan(goal, history=messages)
        except Exception as e:  # noqa: BLE001
            log.warning("Planner 失败，降级到 legacy：%s", e)
            async for ev in self._legacy.run(messages, cancel_check=cancel_check):
                yield ev
            return

        # 3) Executor 执行
        try:
            final = ""
            async for ev in self.executor.run(plan, history=messages,
                                              cancel_check=cancel_check):
                # 把 executor 的 text/plan/reflection/tool/done 透传出去
                if ev[0] == "done":
                    final = ev[1] or ""
                yield ev
            # 把 final answer 作为 assistant 正文追加到消息历史里
            messages.append({"role": "assistant", "content": final})
        except Exception as e:  # noqa: BLE001
            log.warning("Executor 失败：%s", e)
            async for ev in self._legacy.run(messages, cancel_check=cancel_check):
                yield ev


def make_agent_loop(
    client: LLMClient,
    registry: ToolRegistry,
    *,
    enable_planning: bool = True,
    persona: str = "",
    char_name: str = "桌宠",
    reflector_mode: str = "heuristic",
    max_turns: int = 6,
    max_replans: int = 2,
    confirm_tool: Optional[Callable[[str, str], bool]] = None,
) -> AgentLoopV2:
    """工厂方法：根据 enable_planning 决定 mode。"""
    return AgentLoopV2(
        client=client,
        registry=registry,
        mode="react" if enable_planning else "single",
        max_turns=max_turns,
        confirm_tool=confirm_tool,
        persona=persona,
        char_name=char_name,
        reflector_mode=reflector_mode,
        max_replans=max_replans,
    )
