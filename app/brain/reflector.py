"""Reflector：评估一步执行结果，决定 done / retry / replan。

设计：
    - 评估三件事：
        1. 工具是否报错 / 返回错误？
        2. 工具结果是否满足步骤 thought 想要的目标？
        3. 是否需要更多步骤（plan 还需扩展）？
    - 两种模式：
        a) heuristic：基于规则的快速判断（默认开启，零 LLM 开销）
        b) llm：调用 LLM 做语义判断（更准确，多一次小模型调用）
    - 输出 Reflection { verdict: "ok"|"retry"|"replan"|"fail", comment: str }
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

from app.brain.llm_client import ChatMessage, LLMClient
from app.brain.plan import Plan, Step

log = logging.getLogger(__name__)


@dataclass
class Reflection:
    verdict: str           # "ok" | "retry" | "replan" | "fail"
    comment: str = ""
    confidence: float = 1.0

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "comment": self.comment,
                "confidence": self.confidence}


# ---------------------------------------------------------------------------
#  Heuristic 评估
# ---------------------------------------------------------------------------

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

    def reflect(self, step: Step) -> Reflection:
        if step.kind == "final":
            # final 步骤只要有 result 就 ok
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


# ---------------------------------------------------------------------------
#  LLM-based Reflector（更准确，但有 LLM 开销）
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
#  统一接口
# ---------------------------------------------------------------------------


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
