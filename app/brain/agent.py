"""Agent 循环：让大模型通过工具调用真正「做事」。

这是一个真正的 ReAct 循环（与 LangChain `create_agent` 语义对齐）：

    1. 流式请求 LLM（带 tools schema）；
    2. 模型可以决定：
       a) 直接给文字回复（闲聊 / 总结）→ 循环结束，文字即为 final answer
       b) 调用工具 → 工具结果作为 ToolMessage 回传，循环回到步骤 1
    3. 重复 1-2 直到：
       a) 模型给出 final answer（不调工具），或
       b) 达到 max_turns 上限，或
       c) 连续多轮纯调工具没给文字 → 强制进入 final 阶段（去掉 tools，让模型必须总结）

这是「真正的智能体」—— 模型自主决定调什么工具、调几次、什么时候给 final answer。
不像 Planner+Executor 模式那样：先规划后执行、模型不再回头参与决策。

产出事件（yield）：
    ("text",  chunk)                       正文增量
    ("tool",  name, args_str, result_str)  一次工具执行完成
    ("meta",  {...})                       内部事件（force_retry / force_final 等）
    ("done",  final_text)                  整个循环结束

危险工具确认：
    对 DANGEROUS_TOOLS 中的工具（如 open_app / open_website），
    执行前会调用 confirm_tool 回调（由调用方注入），返回 False 则跳过执行。

【抗幻觉】三层机制：
    1. force_tool_use（首轮）→ 工具可解决的意图，第一轮带 tool_choice="required"
       服务端不支持时降级为 user-prompt 强制（见 LLMClient）
    2. force_retry（首轮）→ 首轮 force 后模型仍只回文字 → 注入强提示重试一次
    3. force_final（连续多轮纯调工具）→ 去掉 tools，强制模型给出 final answer
       避免「无限调工具不给最终回复」的退化行为
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import AsyncIterator, Callable, Optional

from app.brain.llm_client import LLMClient, detect_action_intent
from app.engine.tools import ToolRegistry

log = logging.getLogger(__name__)

MAX_TURNS = 6

# 需要用户确认的危险工具集合
DANGEROUS_TOOLS = {"open_app", "open_website"}

# 连续多少轮纯调工具（无文字）后强制进入 final 阶段
MAX_CONSECUTIVE_TOOL_ONLY_TURNS = 3

# 回复清洗：<think> 推理块 / 残留标记不进 UI 与 TTS
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_THINK_TAG_RE = re.compile(r"</?(?:think|reasoning)>", re.I)

# 防工具独白泄漏的 system 附加铁律（幂等标记）
_STYLE_RULE = (
    "\n【回复风格铁律】给主人的回复里严禁出现：工具名称（如 open_website/open_app）、"
    "对内部操作的描述（如「我应该使用XX工具」「这是一个URL类型的请求」）、"
    "推理过程、<think> 标记。只输出符合角色人设的一句自然中文回复。"
)


def _sanitize_reply(text: str) -> str:
    t = _THINK_BLOCK_RE.sub("", text or "")
    t = _THINK_TAG_RE.sub("", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


class AgentLoop:
    def __init__(self, client: LLMClient, registry: ToolRegistry,
                 max_turns: int = MAX_TURNS,
                 confirm_tool: Optional[Callable[[str, str], bool]] = None):
        """
        Args:
            client: LLM 客户端
            registry: 工具注册表
            max_turns: 最大循环轮数
            confirm_tool: 危险工具执行前的确认回调，签名为 (tool_name, args_json) -> bool。
                          返回 False 表示用户拒绝执行。为 None 时不确认直接执行。
        """
        self.client = client
        self.registry = registry
        self.max_turns = max_turns
        self.confirm_tool = confirm_tool

    @staticmethod
    def _first_user_text(messages: list[dict]) -> str:
        """取最后一条 user 消息的文本（用于意图判定）。"""
        for m in reversed(messages):
            if m.get("role") == "user" and m.get("content"):
                return str(m["content"])
        return ""

    @staticmethod
    def _format_tool_results(messages: list[dict]) -> str:
        """把最近 N 条 ToolMessage 整理成文本片段，用于「强制 final」时的 user 提示。
        让模型知道前面调过哪些工具、结果是什么，方便总结。
        """
        bits: list[str] = []
        for m in messages[-10:]:
            if m.get("role") == "tool":
                content = (m.get("content") or "")[:200]
                bits.append(f"  - {content}")
        return "\n".join(bits) if bits else "（无）"

    async def _confirm_or_skip(self, name: str, args: str) -> Optional[str]:
        """对危险工具执行确认。返回 None 表示继续执行，返回字符串表示跳过（附带结果文本）。"""
        if name not in DANGEROUS_TOOLS or self.confirm_tool is None:
            return None
        try:
            confirmed = await asyncio.to_thread(self.confirm_tool, name, args)
            if not confirmed:
                return "用户取消了此操作。"
        except Exception as e:  # noqa: BLE001
            log.warning("确认回调异常：%s", e)
            return None  # 回调失败时不阻塞执行
        return None

    async def _execute_tools_parallel(
        self,
        tool_calls: list[dict],
        messages: list[dict],
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[tuple]:
        """并行执行所有工具调用，按原顺序 yield 结果。"""

        async def _exec_one(i: int, tc: dict) -> tuple[int, str, str, str]:
            """在后台线程中执行单个工具，返回 (index, name, args, result)。"""
            name = tc["name"] or f"unknown_{i}"
            args = tc["arguments"] or "{}"

            # 危险工具确认
            skip_result = await self._confirm_or_skip(name, args)
            if skip_result is not None:
                log.info("tool %s(%s) -> SKIPPED", name, args[:120])
                return i, name, args, skip_result

            # 在线程池中执行同步工具函数
            result = await asyncio.to_thread(self.registry.execute, name, args)
            log.info("tool %s(%s) -> %s", name, args[:120], result[:120])
            return i, name, args, result

        # 检查取消
        if cancel_check is not None and cancel_check():
            return

        # 并行执行所有工具
        tasks = [_exec_one(i, tc) for i, tc in enumerate(tool_calls)]
        results = await asyncio.gather(*tasks)

        # 按原始顺序 yield 结果
        for i, name, args, result in sorted(results, key=lambda x: x[0]):
            yield "tool", name, args, result
            messages.append({
                "role": "tool",
                "tool_call_id": tool_calls[i]["id"] or f"call_{i}",
                "content": result[:4000],
            })

    async def run(
        self,
        messages: list[dict],
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[tuple]:
        """真正的 ReAct 循环。

        关键设计：
        - 第 1 轮 force_tool_use（抗幻觉第 1 层）
        - 第 1 轮没调工具 + 有工具意图 → 注入 user-prompt 强制重试（抗幻觉第 2 层）
        - 连续多轮纯调工具无文字 → 去掉 tools 强制 final（抗幻觉第 3 层）
        """
        tools = self.registry.to_openai() if self.registry.names() else None
        final_text = ""
        # 累计「最近连续纯调工具、无文字」的轮数（用于抗幻觉第 3 层）
        consecutive_tool_only = 0

        # 【抗幻觉】第 1 层：意图识别 → 第一轮 force_tool_use
        intent = detect_action_intent(self._first_user_text(messages))
        force_first_turn = intent is not None
        if force_first_turn:
            log.info("AgentLoop: 检测到动作意图 %s，第一轮开启 force_tool_use",
                     intent)

        # 防工具独白泄漏：给 system 追加回复风格铁律（幂等）
        if messages and messages[0].get("role") == "system":
            base = str(messages[0].get("content") or "")
            if "回复风格铁律" not in base:
                messages[0]["content"] = base + _STYLE_RULE

        for turn in range(self.max_turns):
            if cancel_check is not None and cancel_check():
                break
            content_parts: list[str] = []
            tool_calls: list[dict] = []

            # 【抗幻觉】第 3 层：连续多轮纯调工具 → 去掉 tools 强制 final
            # 避免模型「调工具上瘾」一直不总结
            force_final = consecutive_tool_only >= MAX_CONSECUTIVE_TOOL_ONLY_TURNS
            if force_final:
                log.warning(
                    "AgentLoop: 已连续 %d 轮纯调工具无文字，强制进入 final 阶段（去掉 tools）",
                    consecutive_tool_only)
                yield ("meta", {"event": "force_final",
                                "reason": f"连续 {consecutive_tool_only} 轮纯调工具无文字",
                                "turn": turn})

            async for ev, data in self.client.chat_stream_events(
                    messages,
                    tools=None if force_final else tools,  # final 阶段不带 tools
                    cancel_check=cancel_check,
                    force_tool_use=(force_first_turn and turn == 0)):
                if ev == "text":
                    # 只缓存、不上屏：带工具意图的轮次里模型常先输出工具独白
                    # （「我应该使用XX工具…」），这类文本绝不能进聊天窗口/TTS，
                    # 否则多轮独白会拼成一条精神污染回复。确认本轮是最终回复后
                    # 在轮末一次性发出。
                    content_parts.append(data)
                elif ev == "finish":
                    if data.get("content") and not content_parts:
                        # 模型没走流式正文（罕见），兜底拿 finish 里的完整 content
                        content_parts.append(data["content"])
                    tool_calls = data.get("tool_calls") or []

            final_text = _sanitize_reply("".join(content_parts))

            # 【抗幻觉】第 2 层：第 1 轮 + 有工具意图 + 模型没调工具 →
            # 注入强提示重试一次（重试时不再用 force_tool_use，让 user prompt 起作用）
            if (turn == 0 and force_first_turn and not tool_calls
                    and not (cancel_check and cancel_check())):
                log.warning(
                    "AgentLoop: 第一轮 force_tool_use 后模型仍未调工具，"
                    "注入强提示重试一次（意图=%s）", intent)
                yield ("meta", {"event": "force_retry",
                                "reason": "第一轮未调用工具",
                                "intent": intent})
                messages.append({
                    "role": "user",
                    "content": (
                        "[系统提醒] 你刚才只回了文字但**没有调用任何工具**。"
                        f"主人要的是「{intent}」类操作（{self._first_user_text(messages)[:60]}），"
                        "必须调用对应工具（看上面 schema）。请立刻调用，不要再回文字。"
                    ),
                })
                # 重置连续计数（因为这一轮根本没调工具）
                consecutive_tool_only = 0
                continue   # 进入下一轮

            # 没调工具 → 本轮即最终回复：清洗后一次性发 UI / TTS
            if not tool_calls:
                if final_text:
                    yield "text", final_text
                break

            # 追加 assistant 的工具调用消息（OpenAI 格式）
            messages.append({
                "role": "assistant",
                "content": final_text or "",
                "tool_calls": [
                    {
                        "id": tc["id"] or f"call_{i}",
                        "type": "function",
                        "function": {"name": tc["name"],
                                     "arguments": tc["arguments"] or "{}"},
                    }
                    for i, tc in enumerate(tool_calls)
                ],
            })

            # 并行执行所有工具调用
            async for event in self._execute_tools_parallel(
                    tool_calls, messages, cancel_check=cancel_check):
                yield event

            # 统计「纯调工具无文字」连续次数
            if not final_text.strip():
                consecutive_tool_only += 1
            else:
                consecutive_tool_only = 0

            # 如果下一轮要强制 final，提前注入 user 总结指令
            if consecutive_tool_only >= MAX_CONSECUTIVE_TOOL_ONLY_TURNS - 1:
                # 还有一轮机会，先在 user 里给一个软提示
                tool_summary = self._format_tool_results(messages)
                messages.append({
                    "role": "user",
                    "content": (
                        f"[提示] 你已经调用了多个工具，结果如下：\n{tool_summary}\n"
                        "**下一轮请不要再调任何工具**，直接用中文给主人一个完整、自然的总结回复。"
                    ),
                })

        # 兜底：final_text 为空（极端情况，比如最后一轮纯调工具）
        # → 不让 UI 看到空文本，而是从工具结果拼一个简短回复
        if not final_text.strip() and tools:
            tool_results = [
                m.get("content", "") for m in messages
                if m.get("role") == "tool"
            ]
            if tool_results:
                # 取最后一条工具结果作为基础（避免给主人看一长串原始结果）
                last_result = tool_results[-1].strip()
                # 截断到合理长度
                if len(last_result) > 300:
                    last_result = last_result[:300] + "..."
                final_text = _sanitize_reply(last_result)

        yield "done", final_text
