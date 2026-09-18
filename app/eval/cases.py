"""Agent 评估（Evaluator）。

设计：
    - EvalCase：单条评估用例（输入 + 期望 + 评分规则）
    - Evaluator：用 mock LLM 客户端跑 AgentLoop / PlanExecutor，检查行为
    - 评分维度：
        * tool_called: 是否调了指定工具
        * tool_args_match: 工具参数是否包含期望字段
        * tool_count_le: 工具调用次数不超过 N
        * final_text_match: 最终答案包含某些关键字
        * success: 是否成功（无异常）

输出：
    - 控制台报告
    - Markdown 报告（data/eval_report.md）
    - CI 友好：pytest tests/eval/ -m eval

注意：
    - 这是「行为级」评估（不依赖真实 LLM），可离线稳定跑。
    - 真实模型质量评估需要带 LLM API key 跑 e2e，不在本模块。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Mock LLM 客户端（按脚本回放）
# ---------------------------------------------------------------------------


@dataclass
class MockScript:
    """一组预设的 LLM 回复（按调用顺序消费）。"""
    responses: list[str]
    system_prompts_seen: list[str] = field(default_factory=list)


class MockLLMClient:
    """按脚本回放 LLM 回复。

    每调一次 chat_stream_events 就从 script 里 pop 一条文本作为流输出。
    支持检测工具调用标记：文本里写 `<tool>tool_name|args_json</tool>` 表示该轮触发工具调用。
    """

    def __init__(self, script: MockScript):
        self.script = script
        self.system_prompt = ""
        self.call_count = 0
        self.cfg = type("C", (), {"api_key": "mock"})()

    async def chat_once(self, messages):
        self.script.system_prompts_seen.append(self.system_prompt)
        return self._next_text()

    async def chat_stream_events(self, messages, tools=None, cancel_check=None):
        self.call_count += 1
        self.script.system_prompts_seen.append(self.system_prompt)
        text = self._next_text()
        tool_calls = _parse_tool_markers(text)
        # 把标记去掉再 yield
        clean = _strip_tool_markers(text)
        if clean:
            yield ("text", clean)
        yield ("finish", {
            "reason": "tool_calls" if tool_calls else "stop",
            "tool_calls": tool_calls,
            "content": clean,
        })

    def _next_text(self) -> str:
        if self.script.responses:
            return self.script.responses.pop(0)
        return ""


_TOOL_MARKER_RE = re.compile(
    r"<tool>([a-zA-Z_][\w]*)\|(\{.*?\})</tool>", re.DOTALL)


def _parse_tool_markers(text: str) -> list[dict]:
    out: list[dict] = []
    for i, m in enumerate(_TOOL_MARKER_RE.finditer(text)):
        out.append({
            "id": f"mock_call_{i}",
            "name": m.group(1),
            "arguments": m.group(2),
        })
    return out


def _strip_tool_markers(text: str) -> str:
    return _TOOL_MARKER_RE.sub("", text).strip()


# ---------------------------------------------------------------------------
#  EvalCase / EvalReport
# ---------------------------------------------------------------------------


@dataclass
class EvalCase:
    """单条评估用例。"""
    name: str
    user_message: str
    mock_responses: list[str]               # mock LLM 输出（按顺序）
    expected_tools: list[str] = field(default_factory=list)
    expected_tool_args: dict = field(default_factory=dict)
    # 期望：tools[name] 的 args 必须包含 expected_tool_args[name] 的所有字段
    expected_final_keywords: list[str] = field(default_factory=list)
    max_tool_calls: int = 6
    description: str = ""

    @property
    def is_async(self) -> bool:
        return False


@dataclass
class EvalResult:
    """单条用例的评估结果。"""
    case_name: str
    success: bool
    tool_calls_made: list[tuple[str, dict]] = field(default_factory=list)
    final_text: str = ""
    error: str = ""
    duration_ms: int = 0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "case_name": self.case_name,
            "success": self.success,
            "tool_calls": [{"name": n, "args": a} for n, a in self.tool_calls_made],
            "final_text": self.final_text[:300],
            "error": self.error,
            "duration_ms": self.duration_ms,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------------
#  Evaluator 主体
# ---------------------------------------------------------------------------


class Evaluator:
    """运行一组 EvalCase，输出报告。"""

    def __init__(self, agent_factory: Callable[[MockLLMClient], object]):
        """
        Args:
            agent_factory: 接受 mock_client，返回一个 AgentLoop/PlanExecutor-like 对象，
                           该对象必须有 `run(messages)` 异步生成器。
        """
        self.factory = agent_factory

    async def _run_case(self, case: EvalCase) -> EvalResult:
        script = MockScript(responses=list(case.mock_responses))
        client = MockLLMClient(script)
        agent = self.factory(client)
        result = EvalResult(case_name=case.name, success=False)
        t0 = time.time()
        try:
            messages = [{"role": "user", "content": case.user_message}]
            tool_calls_seen: list[tuple[str, dict]] = []
            final_text_parts: list[str] = []
            # agent.run(messages) 应该返回 async iterator
            agen = agent.run(messages)
            if hasattr(agen, "__aiter__"):
                iterator = agen
            else:
                # 是 coroutine：await 后再拿 iterator
                iterator = await agen
            async for ev in iterator:
                kind = ev[0]
                if kind == "tool":
                    name, args_str = ev[1], ev[2]
                    try:
                        args = json.loads(args_str) if isinstance(args_str, str) else args_str
                    except Exception:
                        args = {}
                    tool_calls_seen.append((name, args))
                elif kind == "text":
                    final_text_parts.append(ev[1])
                elif kind == "done":
                    # executor/agent 可能把最终答案放在 done 事件里
                    if len(ev) > 1 and ev[1]:
                        final_text_parts.append(ev[1])
            result.tool_calls_made = tool_calls_seen
            result.final_text = "".join(final_text_parts)
            result.duration_ms = int((time.time() - t0) * 1000)

            # ---- 评分 ----
            notes: list[str] = []
            ok = True
            # 1. 必调工具检查
            called = {n for n, _ in tool_calls_seen}
            for must in case.expected_tools:
                if must not in called:
                    ok = False
                    notes.append(f"未调用期望工具 {must}")
            # 2. 工具参数检查
            for tool_name, expected_args in case.expected_tool_args.items():
                actual = next((a for n, a in tool_calls_seen if n == tool_name), None)
                if actual is None:
                    ok = False
                    notes.append(f"工具 {tool_name} 未调用（参数检查失败）")
                    continue
                for k, v in expected_args.items():
                    if k not in actual:
                        ok = False
                        notes.append(f"工具 {tool_name} 缺参 {k}")
                    elif str(actual[k]) != str(v):
                        notes.append(
                            f"工具 {tool_name} 参数 {k}={actual[k]}（期望 {v}）")
            # 3. 工具调用次数上限
            if len(tool_calls_seen) > case.max_tool_calls:
                ok = False
                notes.append(
                    f"工具调用 {len(tool_calls_seen)} 超过上限 {case.max_tool_calls}")
            # 4. final 关键字
            for kw in case.expected_final_keywords:
                if kw not in result.final_text:
                    ok = False
                    notes.append(f"最终答案缺关键字 {kw!r}")

            result.notes = notes
            result.success = ok and not result.error
        except Exception as e:  # noqa: BLE001
            log.exception("EvalCase %s 异常", case.name)
            result.error = str(e)
            result.success = False
        return result

    async def run_all(self, cases: list[EvalCase]) -> list[EvalResult]:
        results: list[EvalResult] = []
        for case in cases:
            r = await self._run_case(case)
            log.info("EvalCase %s: %s", case.name, "PASS" if r.success else "FAIL")
            results.append(r)
        return results

    def report(self, results: list[EvalResult]) -> str:
        """生成 Markdown 报告。"""
        total = len(results)
        passed = sum(1 for r in results if r.success)
        rate = passed / total if total else 0
        avg_ms = sum(r.duration_ms for r in results) / total if total else 0

        lines = [
            "# Agent 评估报告",
            "",
            f"- 用例总数: **{total}**",
            f"- 通过: **{passed}**",
            f"- 成功率: **{rate:.0%}**",
            f"- 平均耗时: **{avg_ms:.0f} ms**",
            "",
            "## 用例结果",
            "",
            "| 用例 | 状态 | 工具调用 | 耗时 | 备注 |",
            "|------|------|----------|------|------|",
        ]
        for r in results:
            status = "✅" if r.success else "❌"
            tools = ", ".join(f"{n}({json.dumps(a, ensure_ascii=False)[:40]})"
                              for n, a in r.tool_calls_made) or "—"
            notes = "; ".join(r.notes) if r.notes else ""
            lines.append(
                f"| {r.case_name} | {status} | {tools} | {r.duration_ms}ms | {notes[:80]} |"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
#  内置 EvalCase 集
# ---------------------------------------------------------------------------


def builtin_cases() -> list[EvalCase]:
    """内置评测用例（用 mock 脚本驱动）。"""
    return [
        EvalCase(
            name="add_reminder_basic",
            user_message="25 分钟后提醒我喝水",
            # Planner 输出 plan：先调 add_reminder，再 final
            mock_responses=[
                # Planner 调用
                json.dumps({"steps": [
                    {"kind": "tool", "thought": "设置提醒",
                     "tool_name": "add_reminder",
                     "arguments": {"text": "喝水", "delay_minutes": 25}},
                    {"kind": "final", "thought": "回复",
                     "answer": "好哒，25 分钟后叫你喝水 [happy]"},
                ]}, ensure_ascii=False),
            ],
            expected_tools=["add_reminder"],
            expected_tool_args={"add_reminder": {"text": "喝水", "delay_minutes": 25}},
            expected_final_keywords=["提醒"],
            description="设置一个 25 分钟后的喝水提醒",
        ),
        EvalCase(
            name="remember_fact",
            user_message="记住我不吃香菜",
            mock_responses=[
                json.dumps({"steps": [
                    {"kind": "tool", "thought": "记到记忆",
                     "tool_name": "remember_fact",
                     "arguments": {"content": "主人不吃香菜",
                                   "category": "preference",
                                   "importance": 0.9}},
                    {"kind": "final", "thought": "回复",
                     "answer": "记住了！以后避开香菜 [happy]"},
                ]}, ensure_ascii=False),
            ],
            expected_tools=["remember_fact"],
            expected_tool_args={
                "remember_fact": {"category": "preference", "importance": 0.9}},
            description="记住一个偏好",
        ),
        EvalCase(
            name="chat_no_tool",
            user_message="今天有点累",
            mock_responses=[
                json.dumps({"steps": [
                    {"kind": "final", "thought": "纯聊天",
                     "answer": "辛苦啦，休息一下～ [sad]"},
                ]}, ensure_ascii=False),
            ],
            expected_tools=[],   # 不期望调任何工具
            max_tool_calls=0,
            expected_final_keywords=["辛苦"],
            description="纯聊天不应调工具",
        ),
        EvalCase(
            name="calculate",
            user_message="根号 144 加 25 等于多少",
            mock_responses=[
                json.dumps({"steps": [
                    {"kind": "tool", "thought": "用计算器",
                     "tool_name": "calculate",
                     "arguments": {"expression": "sqrt(144)+25"}},
                    {"kind": "final", "thought": "回复",
                     "answer": "是 37 哦～ [happy]"},
                ]}, ensure_ascii=False),
            ],
            expected_tools=["calculate"],
            expected_tool_args={"calculate": {"expression": "sqrt(144)+25"}},
            description="数学计算",
        ),
    ]


# ---------------------------------------------------------------------------
#  入口：跑评估并写报告
# ---------------------------------------------------------------------------


async def run_eval_suite(agent_factory, output_path: Optional[str] = None) -> dict:
    """跑内置用例 + 返回统计。"""
    ev = Evaluator(agent_factory)
    cases = builtin_cases()
    results = await ev.run_all(cases)
    report = ev.report(results)
    if output_path:
        Path(output_path).write_text(report, encoding="utf-8")
        log.info("评估报告已写入 %s", output_path)
    return {
        "total": len(results),
        "passed": sum(1 for r in results if r.success),
        "report": report,
        "results": [r.to_dict() for r in results],
    }
