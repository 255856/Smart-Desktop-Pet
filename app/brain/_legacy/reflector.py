"""Reflector：评估一步执行结果，决定 done / retry / replan。"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

from app.brain.llm_client import ChatMessage, LLMClient, detect_action_intent

from app.brain._legacy.plan import Plan, Step

log = logging.getLogger(__name__)


@dataclass
class Reflection:
    verdict: str           # "ok" | "retry" | "replan" | "fail"
    comment: str = ""
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "comment": self.comment,
                "confidence": self.confidence}


#  Heuristic 评估

_ERROR_KEYWORDS = ("错误", "失败", "Error", "error", "Exception", "exception",
                   "找不到", "无法", "未知工具", "未配置")


def _looks_like_error(result: str) -> bool:
    if not result:
        return True
    r = result.strip()
    if len(r) > 32:
        return False
    return any(k in r for k in _ERROR_KEYWORDS)


class HeuristicReflector:
    """基于规则快速评估，零 LLM 开销。"""

    def __init__(self, plan_goal: str = "") -> None:
        """可注入 plan_goal 用于「意图-行为一致性」反幻觉检查。
        PlanExecutor 在每次 reflect() 之前用 plan.goal 注入。
        """
        self.plan_goal = plan_goal or ""
        # 缓存意图判定结果（一个 plan 只判定一次）
        self._cached_intent = (
            detect_action_intent(self.plan_goal) if self.plan_goal else None
        )

    def set_plan_goal(self, goal: str) -> None:
        """PlanExecutor 在切换 plan 时调用。"""
        self.plan_goal = goal or ""
        self._cached_intent = (
            detect_action_intent(self.plan_goal) if self.plan_goal else None
        )

    def reflect(self, step: Step) -> Reflection:
        # ---- 【抗幻觉】意图-行为一致性检查 ----
        # 用户原始目标需要工具动作，但 final 步骤只回文字（模型没调工具）
        if step.kind == "final" and self._cached_intent:
            # 看看整个 plan 里到底有没有 tool 步骤被执行过
            # （HeuristicReflector 单步评估看不到全局，所以由 PlanExecutor 在外层
            #  检查；这里先打旗，executor 那边补判）
            # 但 single-step final（无前置 tool）就一定是幻觉
            if step.result and step.result.strip():
                # 让 PlanExecutor 复核（它知道整个 plan 的 tool 步骤计数）
                # 暂时返回 ok 让流程继续；executor 在 _run_step 之后会
                # 用 _check_hallucination_full_plan() 拦截
                pass

        if step.kind == "final":
            # final 步骤只要有 result 就 ok（Executor 层会有二次拦截）
            if step.result and step.result.strip():
                return Reflection("ok", "final 步骤有结果")
            return Reflection("retry", "final 步骤结果为空", confidence=0.7)

        # tool 步骤
        if step.status == "failed":
            if step.retry_count < 2:
                return Reflection("retry", f"工具执行失败：{step.result[:120]}")
            return Reflection("replan", f"重试 {step.retry_count} 次仍失败", confidence=0.8)

        if _looks_like_error(step.result or ""):
            if step.retry_count < 2:
                return Reflection("retry",
                                  f"工具返回疑似错误：{(step.result or '')[:120]}")
            return Reflection("replan",
                              f"重试 {step.retry_count} 次仍返回错误", confidence=0.7)

        return Reflection("ok", "工具执行成功")

    def detect_plan_hallucination(self, plan: Plan) -> Optional[Reflection]:
        """【抗幻觉】跨步检查：plan 里没有任何成功执行的 tool 步骤，
        但用户的 goal 明确需要工具动作 → 整体是幻觉。

        返回 None 表示没问题；返回 Reflection("replan", ...) 表示
        Executor 应该把这个 plan 判失败，触发 Planner.replan()。
        """
        if not self._cached_intent:
            return None   # 用户意图不明确，不强制
        # 统计真正成功执行过的 tool 步骤
        ok_tools = [s for s in plan.steps
                    if s.kind == "tool" and s.status == "done" and s.result]
        if ok_tools:
            return None   # 至少有一个工具真调了 → 不算幻觉
        # 用户意图需要工具 + plan 里一个 tool 都没成功 → 幻觉
        log.warning(
            "HeuristicReflector: 意图=%s 但 plan 中没有成功执行的 tool 步骤 → 判定为幻觉",
            self._cached_intent)
        return Reflection(
            "replan",
            f"用户意图是「{self._cached_intent}」类工具动作，但整个 plan 没有成功执行任何 tool。"
            "请重新规划：在 steps 里加入对应工具调用（kind='tool'），不要直接给 final 文字答复。",
            confidence=0.9,
        )


#  LLM-based Reflector（更准确，但有 LLM 开销）


_REFLECT_SYSTEM = """你是「Reflector」评估模块。给定一个步骤的目标（thought）、它调用的工具和工具返回，
判断该步骤是否成功完成。

输出严格 JSON：
{"verdict": "ok"|"retry"|"replan", "comment": "一句话解释", "confidence": 0-1}

- ok：工具结果满足 thought 的目标
- retry：工具失败 / 返回错误，参数可能有问题，再试一次（最多 2 次）
- replan：思路本身不对，需要 Planner 重新设计
"""


class LLMReflector:
    def __init__(self, client: LLMClient):
        self.client = client

    async def reflect(self, step: Step) -> Reflection:
        # 简单用例：直接 fallback 到 heuristic
        if not step.tool_name and not step.result:
            return Reflection("fail", "LLM 反思无信息可用")

        args_preview = json.dumps(step.arguments, ensure_ascii=False)[:200]
        prompt = (
            f"【步骤目标】{step.thought}\n"
            f"【工具调用】{step.tool_name}({args_preview})\n"
            f"【工具返回】{(step.result or '')[:600]}\n"
            "请评估。"
        )
        old_system = self.client.system_prompt
        try:
            self.client.system_prompt = _REFLECT_SYSTEM
            text = await self.client.chat_once(
                [ChatMessage(role="user", content=prompt)])
        except Exception as e:  # noqa: BLE001
            log.warning("LLM 反思失败：%s", e)
            return HeuristicReflector().reflect(step)
        finally:
            self.client.system_prompt = old_system

        obj = _extract_json(text)
        if not obj:
            return HeuristicReflector().reflect(step)
        verdict = obj.get("verdict", "ok")
        if verdict not in ("ok", "retry", "replan", "fail"):
            verdict = "ok"
        return Reflection(
            verdict=verdict,
            comment=str(obj.get("comment", ""))[:200],
            confidence=float(obj.get("confidence", 0.7)),
        )


def _extract_json(text: str) -> Optional[dict]:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


#  统一接口


def make_reflector(mode: str = "heuristic", llm_client: Optional[LLMClient] = None):
    """mode: "heuristic" | "llm" | "off"."""
    mode = (mode or "heuristic").lower()
    if mode == "off":
        class _AlwaysOK:
            def reflect(self, step: Step) -> Reflection:
                return Reflection("ok", "反思已关闭")
        return _AlwaysOK()
    if mode == "llm" and llm_client is not None:
        return LLMReflector(llm_client)
    return HeuristicReflector()
