"""Planner / Reflector 数据模型：把用户目标拆成可执行计划。"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class Step:
    id: str
    kind: str                          # "tool" | "final"
    thought: str = ""
    tool_name: Optional[str] = None
    arguments: dict = field(default_factory=dict)
    status: str = "pending"            # pending|running|done|failed|skipped
    result: Optional[str] = None
    reflection: Optional[str] = None
    retry_count: int = 0
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    # 并行标记：同 parallel_group 的 step 可以 gather 并行执行（仅当 group 字段相同）
    parallel_group: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Step":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Plan:
    goal: str
    steps: list[Step] = field(default_factory=list)
    current: int = 0
    final_answer: Optional[str] = None
    replan_count: int = 0
    created_at: float = field(default_factory=time.time)

    @property
    def current_step(self) -> Optional[Step]:
        if 0 <= self.current < len(self.steps):
            return self.steps[self.current]
        return None

    @property
    def is_done(self) -> bool:
        return self.final_answer is not None or self.current >= len(self.steps)

    def to_dict(self) -> dict:
        return {
            "goal": self.goal,
            "steps": [s.to_dict() for s in self.steps],
            "current": self.current,
            "final_answer": self.final_answer,
            "replan_count": self.replan_count,
            "created_at": self.created_at,
        }


#  Plan parsing：从 LLM 输出里抽 JSON


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.DOTALL)


def extract_plan_json(text: str) -> Optional[dict]:
    """从 LLM 输出里抽出 JSON。优先匹配 ```json``` 代码块，否则尝试首个 {…}。"""
    if not text:
        return None
    # 1) ```json ... ```
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 2) 第一个 { 到最后一个 }
    if "{" in text and "}" in text:
        start = text.find("{")
        end = text.rfind("}")
        candidate = text[start:end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
    return None


def parse_plan_from_llm(text: str, goal: str) -> Plan:
    """从 LLM 文本构造 Plan。

    容错策略：
        1. 抽 JSON 成功 → 按 schema 构造 Plan
        2. JSON 抽不到 → 构造一个 final 步骤，把整段文本作为 final_answer 候选
    """
    obj = extract_plan_json(text)
    if obj and isinstance(obj, dict) and "steps" in obj:
        try:
            steps_raw = obj["steps"]
            steps: list[Step] = []
            for s in steps_raw:
                if not isinstance(s, dict):
                    continue
                kind = s.get("kind", "tool")
                if kind not in ("tool", "final"):
                    kind = "tool"
                step = Step(
                    id=s.get("id") or f"s{uuid.uuid4().hex[:6]}",
                    kind=kind,
                    thought=s.get("thought", ""),
                    tool_name=s.get("tool_name") or s.get("name"),
                    arguments=s.get("arguments") or {},
                    status="pending",
                )
                if kind == "final":
                    step.result = s.get("answer", "")
                steps.append(step)
            if steps:
                return Plan(goal=goal, steps=steps)
        except Exception as e:  # noqa: BLE001
            log.warning("parse_plan_from_llm 失败：%s", e)

    # Fallback：构造一个 final 步骤
    return Plan(
        goal=goal,
        steps=[Step(
            id=f"s{uuid.uuid4().hex[:6]}",
            kind="final",
            thought="LLM 没有返回结构化计划，直接给出答案。",
            result=(text or "").strip(),
        )],
    )


def plan_to_compact_text(plan: Plan, last_n: int = 5) -> str:
    """把 Plan 压缩成一段文本，给后续 Reflector / Replan 用。"""
    lines = [f"目标：{plan.goal}", f"步骤总数：{len(plan.steps)}",
             f"已重规划次数：{plan.replan_count}", ""]
    # 只保留最近 last_n 步的详情
    start = max(0, len(plan.steps) - last_n)
    for i, s in enumerate(plan.steps[start:], start=start):
        status_icon = {
            "pending": "⏳", "running": "▶️",
            "done": "✅", "failed": "❌", "skipped": "⏭️",
        }.get(s.status, "?")
        line = f"#{i + 1} {status_icon} [{s.kind}] {s.thought}"
        if s.tool_name:
            args = json.dumps(s.arguments, ensure_ascii=False)[:80]
            line += f" → {s.tool_name}({args})"
        if s.result:
            line += f"\n   结果：{s.result[:200]}"
        if s.reflection:
            line += f"\n   反思：{s.reflection[:200]}"
        lines.append(line)
    return "\n".join(lines)
