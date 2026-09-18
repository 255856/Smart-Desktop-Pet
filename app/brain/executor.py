"""PlanExecutor：按 Plan 一步步执行（工具调用或 final），配合 Reflector 决定走向。

对外的事件流（与 AgentLoop 兼容）：
    ("text",  chunk)                       —— 暂无直接正文（plan-exec 不主动说话）
    ("tool",  name, args_str, result_str)  —— 一次工具执行完成
    ("plan",  plan_dict)                   —— 计划已生成 / 修订
    ("reflection", reflection_dict)        —— 每一步的反思
    ("done",  final_text)                  —— 最终答案（来自 final 步骤的 result 或拼接）
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Callable, Optional

from app.brain.plan import Plan, Step, plan_to_compact_text
from app.brain.planner import Planner
from app.brain.reflector import Reflection, make_reflector
from app.brain.llm_client import LLMClient
from app.engine.tools import ToolRegistry

log = logging.getLogger(__name__)

MAX_REPLANS = 2
MAX_RETRIES_PER_STEP = 2


class PlanExecutor:
    """执行 Plan：每步 Executor，反思后决定走向。"""

    def __init__(
        self,
        client: LLMClient,
        registry: ToolRegistry,
        planner: Planner,
        reflector=None,
        max_replans: int = MAX_REPLANS,
        max_retries: int = MAX_RETRIES_PER_STEP,
        confirm_tool: Optional[Callable[[str, str], bool]] = None,
    ):
        self.client = client
        self.registry = registry
        self.planner = planner
        # 默认用启发式反思器（无开销）
        self.reflector = reflector if reflector is not None else make_reflector("heuristic")
        self.max_replans = max_replans
        self.max_retries = max_retries
        self.confirm_tool = confirm_tool

    async def _confirm_or_skip(self, name: str, args: str) -> Optional[str]:
        if name not in {"open_app", "open_website"} or self.confirm_tool is None:
            return None
        try:
            ok = await asyncio.to_thread(self.confirm_tool, name, args)
            if not ok:
                return "用户取消了此操作。"
        except Exception:  # noqa: BLE001
            return None
        return None

    async def _run_step(self, step: Step) -> Reflection:
        """执行一个步骤（tool 或 final）并返回 Reflection。"""
        step.status = "running"
        step.started_at = __import__("time").time()
        try:
            if step.kind == "final":
                # final 步骤：结果由 Planner 直接填好，无需执行
                step.status = "done"
                step.finished_at = __import__("time").time()
                return self.reflector.reflect(step)

            # tool 步骤
            if not step.tool_name:
                step.result = "错误：步骤缺少 tool_name"
                step.status = "failed"
                step.finished_at = __import__("time").time()
                return self.reflector.reflect(step)

            args_str = json.dumps(step.arguments or {}, ensure_ascii=False)
            skip = await self._confirm_or_skip(step.tool_name, args_str)
            if skip is not None:
                step.result = skip
                step.status = "done"
                step.finished_at = __import__("time").time()
                return self.reflector.reflect(step)

            result = await asyncio.to_thread(
                self.registry.execute, step.tool_name, args_str)
            step.result = result
            # 根据结果决定 status
            if result.startswith("错误") or "未安装" in result[:20]:
                step.status = "failed"
            else:
                step.status = "done"
            step.finished_at = __import__("time").time()
            log.info("step %s(%s) → %s", step.tool_name,
                     args_str[:80], (result or "")[:120])
            return self.reflector.reflect(step)
        except Exception as e:  # noqa: BLE001
            step.result = f"异常：{e}"
            step.status = "failed"
            step.finished_at = __import__("time").time()
            log.exception("step 执行异常")
            return self.reflector.reflect(step)

    async def run(
        self,
        plan: Plan,
        history: list[dict] | None = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[tuple]:
        """执行 Plan 并产出事件流。

        并行执行：相同 parallel_group 的 step 会用 asyncio.gather 并行执行
        （适合"读文件 + 查天气"等无依赖步骤）。
        """
        history = history or []
        yield "plan", plan.to_dict()

        replans = 0
        while not plan.is_done:
            if cancel_check is not None and cancel_check():
                break

            step = plan.current_step
            if step is None:
                break

            # ---- 并行组：同 group 的多个 step 用 gather 一起跑 ----
            parallel_steps: list[Step] = [step]
            if step.parallel_group:
                # 收集连续的、同 group 的 step
                i = plan.current + 1
                while i < len(plan.steps) and plan.steps[i].parallel_group == step.parallel_group:
                    parallel_steps.append(plan.steps[i])
                    i += 1

            if len(parallel_steps) > 1:
                # 并行执行
                reflections = await asyncio.gather(
                    *[self._run_step(s) for s in parallel_steps])
            else:
                reflection = await self._run_step(step)
                reflections = [reflection]

            # 处理每个 step 的重试 + 反思
            for s, reflection in zip(parallel_steps, reflections):
                # 多次重试
                tries = 0
                while reflection.verdict == "retry" and tries < self.max_retries:
                    if cancel_check is not None and cancel_check():
                        break
                    tries += 1
                    s.retry_count = tries
                    s.status = "pending"
                    yield "reflection", Reflection(
                        "retry",
                        f"第 {tries} 次重试：{reflection.comment}",
                    ).to_dict()
                    reflection = await self._run_step(s)

                yield "reflection", reflection.to_dict()

                # 工具步骤的 tool 事件（兼容旧 UI）
                if s.kind == "tool" and s.tool_name:
                    yield "tool", s.tool_name, json.dumps(
                        s.arguments, ensure_ascii=False), s.result or ""

                # 决定走向
                if reflection.verdict == "ok":
                    pass   # 正常推进
                elif reflection.verdict in ("retry", "fail"):
                    s.status = "skipped"
                elif reflection.verdict == "replan":
                    if replans >= self.max_replans:
                        log.warning("replan 次数耗尽，终止 plan")
                        plan.final_answer = "抱歉，多次重新规划仍未解决，请换个问法。"
                        break
                    replans += 1
                    plan = await self.planner.replan(plan, history=history)
                    yield "plan", plan.to_dict()
                    break   # replan 后从新 plan 头开始

            # 推进 plan.current（跳过所有并行 step）
            plan.current += len(parallel_steps)

        # 取最终答案
        if plan.final_answer is None:
            last_final = next(
                (s for s in reversed(plan.steps) if s.kind == "final" and s.result),
                None,
            )
            if last_final is not None:
                plan.final_answer = last_final.result
            else:
                bits = [f"{s.tool_name}: {s.result}"
                        for s in plan.steps
                        if s.kind == "tool" and s.status == "done" and s.result]
                plan.final_answer = "\n".join(bits) if bits else "（未产出答案）"

        yield "done", plan.final_answer or ""
