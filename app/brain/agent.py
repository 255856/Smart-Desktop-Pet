"""Agent 循环：让大模型通过工具调用真正「做事」。

流程（单次用户消息）：
    1. 流式请求 LLM（带 tools）；
    2. 模型回正文 → 逐 chunk 透传给 UI；
    3. 模型回 tool_calls → 并行执行 ToolRegistry 里的工具（asyncio.gather），
       把结果以 role=tool 消息追加，回到第 1 步（最多 MAX_TURNS 轮）；
    4. 没有工具调用的那轮正文即为最终回复。

产出事件（yield）：
    ("text",  chunk)                       正文增量
    ("tool",  name, args_str, result_str)  一次工具执行完成
    ("done",  final_text)                  整个循环结束

危险工具确认：
    对 DANGEROUS_TOOLS 中的工具（如 open_app / open_website），
    执行前会调用 confirm_tool 回调（由调用方注入），返回 False 则跳过执行。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator, Callable, Optional

from app.brain.llm_client import LLMClient
from app.engine.tools import ToolRegistry

log = logging.getLogger(__name__)

MAX_TURNS = 6

# 需要用户确认的危险工具集合
DANGEROUS_TOOLS = {"open_app", "open_website"}


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
        tools = self.registry.to_openai() if self.registry.names() else None
        final_text = ""

        for _turn in range(self.max_turns):
            if cancel_check is not None and cancel_check():
                break
            content_parts: list[str] = []
            tool_calls: list[dict] = []

            async for ev, data in self.client.chat_stream_events(
                    messages, tools=tools, cancel_check=cancel_check):
                if ev == "text":
                    content_parts.append(data)
                    yield ev, data
                elif ev == "finish":
                    if data.get("content") and not content_parts:
                        # 模型没走流式正文（罕见），兜底拿 finish 里的完整 content
                        content_parts.append(data["content"])
                        yield "text", data["content"]
                    tool_calls = data.get("tool_calls") or []

            final_text = "".join(content_parts)

            if not tool_calls:
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

            # 并行执行所有工具调用（yield 的是 4-tuple: ("tool", name, args, result)）
            async for event in self._execute_tools_parallel(
                    tool_calls, messages, cancel_check=cancel_check):
                yield event
            # 带着工具结果进入下一轮流式请求
        yield "done", final_text
