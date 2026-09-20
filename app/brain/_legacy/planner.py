"""Planner：把用户目标拆解为结构化步骤。

使用方式：
    planner = Planner(llm_client, tool_registry, persona)
    plan = await planner.make_plan(user_message, history=[...])

设计要点：
    - 提示词使用 ReAct-style 模板 + JSON 输出约束
    - 拆 plan 时告诉模型可用工具名 + 简短描述
    - 失败/空 plan 自动降级为「final」单步
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from app.brain.llm_client import ChatMessage, LLMClient
from app.engine.tools import ToolRegistry

from app.brain._legacy.plan import Plan, parse_plan_from_llm

log = logging.getLogger(__name__)


_PLAN_SYSTEM_TEMPLATE = """你是「{char_name}」桌宠的 Planner 子模块。
你的任务是把用户的一句话目标拆成 1-6 个可执行的步骤。

【可用工具】（只能从这些里选 tool_name）
{tool_list}

【输出格式】严格只输出一个 JSON 对象，不要任何其他文字。
```json
{{
  "steps": [
    {{"kind": "tool", "thought": "为什么这一步", "tool_name": "add_reminder", "arguments": {{"text": "喝水", "delay_minutes": 30}}}},
    {{"kind": "final", "thought": "整合工具结果给主人", "answer": "已经把提醒设好啦～"}}
  ]
}}
```

【规则】
- 如果只是聊天 / 打招呼 / 闲聊，直接一个 {{"kind":"final","answer":"..."}} 即可，不要强行调工具。
- 工具的 arguments 字段必须是合法 JSON object，参数名严格匹配 schema。
- 步骤之间按依赖顺序排列；前面步骤的结果可以后面步骤引用（用 $step1 之类占位，目前先靠 LLM 自己把前序结果填进 arguments）。
- 最多 6 步；多于 6 步的任务压缩成更粗的粒度。
- 主人最近说过的话：
{history}
"""


class Planner:
    """用 LLM 构造结构化 Plan。"""

    def __init__(self, client: LLMClient, registry: Optional[ToolRegistry],
                 char_name: str = "桌宠", persona: str = ""):
        self.client = client
        self.registry = registry
        self.char_name = char_name
        self.persona = persona

    def _build_system_prompt(self, history: list[dict]) -> str:
        # 工具清单（简短版）
        if self.registry is not None:
            lines = []
            for t in self.registry._tools.values():  # noqa: SLF001
                # 提取 properties key 作为参数提示
                params = t.parameters.get("properties", {}) if isinstance(t.parameters, dict) else {}
                args_hint = ", ".join(params.keys()) if params else "（无参数）"
                # 截断描述
                desc = (t.description or "").strip().replace("\n", " ")
                if len(desc) > 80:
                    desc = desc[:80] + "..."
                lines.append(f"- {t.name}({args_hint}): {desc}")
            tool_list = "\n".join(lines)
        else:
            tool_list = "（无工具可用）"

        # 历史（最近 5 条）
        hist_lines = []
        for h in history[-5:]:
            role = h.get("role", "user")
            content = (h.get("content") or "")[:120]
            hist_lines.append(f"  [{role}] {content}")
        history_text = "\n".join(hist_lines) or "  （无）"

        return _PLAN_SYSTEM_TEMPLATE.format(
            char_name=self.char_name,
            tool_list=tool_list,
            history=history_text,
        )

    async def make_plan(self, user_message: str,
                        history: list[dict] | None = None) -> Plan:
        """调用 LLM 生成 Plan。失败时降级为 single-step final。"""
        history = history or []
        system = self._build_system_prompt(history)
        # 用普通 chat（非 stream）拿到完整 JSON 文本
        old_system = self.client.system_prompt
        try:
            self.client.system_prompt = system
            messages = [ChatMessage(role="user", content=user_message)]
            text = await self.client.chat_once(messages)
        except Exception as e:  # noqa: BLE001
            log.warning("Planner 调用失败：%s", e)
            text = ""
        finally:
            self.client.system_prompt = old_system

        log.debug("Planner 输出：%s", text[:400])
        plan = parse_plan_from_llm(text, goal=user_message)
        log.info("Planner 生成 %d 步（replan=0）", len(plan.steps))
        return plan

    async def replan(self, original: Plan, history: list[dict] | None = None) -> Plan:
        """根据 Reflector 反馈重做 Plan。"""
        from app.brain._legacy.plan import plan_to_compact_text
        feedback = plan_to_compact_text(original, last_n=6)
        history = history or []
        prompt = (
            "原计划在执行中遇到了问题（见下方）。请基于已有进展重新规划，"
            "跳过已成功的步骤，只补新的步骤。\n\n"
            f"【反馈】\n{feedback}\n\n"
            "请输出新的 JSON 计划："
        )
        system = self._build_system_prompt(history)
        old_system = self.client.system_prompt
        try:
            self.client.system_prompt = system
            text = await self.client.chat_once(
                [ChatMessage(role="user", content=prompt)])
        except Exception as e:  # noqa: BLE001
            log.warning("Planner.replan 调用失败：%s", e)
            text = ""
        finally:
            self.client.system_prompt = old_system

        new_plan = parse_plan_from_llm(text, goal=original.goal)
        new_plan.replan_count = original.replan_count + 1
        log.info("Planner.replan 第 %d 次，新计划 %d 步",
                 new_plan.replan_count, len(new_plan.steps))
        return new_plan
