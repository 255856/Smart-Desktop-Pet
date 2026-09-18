"""LangChain 标准后端（与轻量级后端并存）。

设计目标：
- 用 `langchain.agents.create_agent` 替代手写 ReAct（参考 `E:\\Python study\\agent`）
- 用 `SqliteSaver` 替代自研 TraceRecorder 的部分功能
- 保持与 `AgentLoopV2` **相同的事件协议**，ChatWindow 无需任何改动：
    ("text", chunk)        流式文字
    ("tool", name, args, result)    工具调用
    ("done", text)         最终回复
    ("error", exc)         出错

启用方式：
    yaml:
      brain:
        backend: standard     # "lightweight" / "standard"

依赖：langchain>=1.0, langgraph-checkpoint-sqlite, langchain-openai
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

# LangChain / LangGraph（标准后端的核心）
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import StructuredTool

try:
    from langchain.agents import create_agent
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "请先安装 langchain>=1.0: pip install langchain langchain-openai langgraph langgraph-checkpoint-sqlite"
    ) from e

try:
    from langchain_openai import ChatOpenAI
except ImportError as e:  # pragma: no cover
    raise ImportError("langchain-openai 未安装") from e

try:
    from langgraph.checkpoint.sqlite import SqliteSaver
except ImportError as e:  # pragma: no cover
    raise ImportError("langgraph-checkpoint-sqlite 未安装") from e

from app.brain.llm_client import LLMConfig, sanitize_text
from app.engine.tools import Tool, ToolRegistry

log = logging.getLogger(__name__)


# =====================================================================
# 内部 Tool → LangChain StructuredTool 的适配器
# =====================================================================
def _internal_tool_to_langchain(tool: Tool) -> StructuredTool:
    """把桌宠的 Tool(name, description, parameters, fn) 包装为 LangChain StructuredTool。

    StructuredTool.from_function 会从 fn 的 type hints + 默认值自动生成 args_schema，
    这正好和我们 `app/engine/tools/*` 里 `def add_reminder(text, delay_minutes=None, ...)` 的写法对齐。
    """
    return StructuredTool.from_function(
        func=tool.fn,
        name=tool.name,
        description=tool.description,
        parse_docstring=False,       # 我们用 desc 字段，不解析 docstring
    )


def convert_tools(registry: ToolRegistry) -> list[StructuredTool]:
    """批量转换 ToolRegistry → LangChain tool 列表。"""
    return [_internal_tool_to_langchain(t) for t in registry._tools.values()]


# =====================================================================
# 主类
# =====================================================================
@dataclass
class LangChainAgentConfig:
    """LangChain 后端运行参数（从 brain.langchain 段读取）。"""
    enable_checkpointer: bool = True
    checkpoint_db: str = "data/langchain_checkpoints.db"
    max_iterations: int = 10
    return_intermediate_steps: bool = False


class LangChainAgent:
    """LangChain 1.0+ create_agent 实现的桌宠 Agent。"""

    def __init__(
        self,
        llm_cfg: LLMConfig,
        registry: ToolRegistry,
        persona: str = "",
        cfg: Optional[LangChainAgentConfig] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        thread_id: str = "default",
    ):
        self.llm_cfg = llm_cfg
        self.registry = registry
        self.persona = persona
        self.cfg = cfg or LangChainAgentConfig()
        self.cancel_check = cancel_check
        self.thread_id = thread_id
        self._agent: Optional[Any] = None
        self._checkpointer_cm: Optional[Any] = None
        self._checkpointer: Optional[Any] = None

    # ---------- 构建 LLM ----------
    def _build_model(self) -> ChatOpenAI:
        """OpenAI 兼容协议（DeepSeek / minimax / Ollama / 任何 base_url）。"""
        return ChatOpenAI(
            base_url=self.llm_cfg.base_url,
            api_key=self.llm_cfg.api_key or "EMPTY",     # Ollama 接受任意非空字符串
            model=self.llm_cfg.model,
            temperature=self.llm_cfg.temperature,
            max_tokens=self.llm_cfg.max_tokens,
            timeout=self.llm_cfg.timeout,
            streaming=True,
        )

    # ---------- 构建 checkpointer（可选）----------
    def _build_checkpointer(self):
        if not self.cfg.enable_checkpointer:
            return None
        Path(self.cfg.checkpoint_db).parent.mkdir(parents=True, exist_ok=True)
        # SqliteSaver.from_conn_string 是新版 API
        self._checkpointer_cm = SqliteSaver.from_conn_string(self.cfg.checkpoint_db)
        self._checkpointer = self._checkpointer_cm.__enter__()
        return self._checkpointer

    def close(self) -> None:
        """关闭 checkpointer 释放 SQLite 连接。"""
        if self._checkpointer_cm is not None:
            try:
                self._checkpointer_cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                log.exception("SqliteSaver close failed")

    # ---------- 构建 agent ----------
    def _ensure_agent(self) -> None:
        if self._agent is not None:
            return
        model = self._build_model()
        tools = convert_tools(self.registry)
        checkpointer = self._build_checkpointer()

        # system_prompt 直接用人设原文（LLMClient 的 sanitize 已在此层处理）
        self._agent = create_agent(
            model=model,
            tools=tools,
            system_prompt=self.persona or "你是一个有用的桌面助手。",
            checkpointer=checkpointer,
        )
        log.info("LangChainAgent ready: tools=%d, checkpointer=%s",
                 len(tools), "on" if checkpointer else "off")

    # ---------- 历史消息转换 ----------
    @staticmethod
    def _to_lc_history(messages: list[dict]) -> list:
        """把 [{role, content}, ...] 转成 LangChain messages（跳过 system——已经作为 system_prompt）。"""
        out = []
        for m in messages:
            role = m.get("role")
            content = m.get("content", "")
            if role == "user":
                out.append(HumanMessage(content=content))
            elif role == "assistant":
                out.append(AIMessage(content=content))
            # system / tool: 跳过
        return out

    # ---------- 主入口 ----------
    async def run(
        self,
        messages: list[dict],
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[tuple]:
        """与 AgentLoopV2 兼容的事件协议。"""
        self._ensure_agent()
        cancel = cancel_check or self.cancel_check

        # 历史（不含最新 user——直接作为 input）
        history = self._to_lc_history(messages[:-1] if messages else [])
        last_user = ""
        if messages and messages[-1].get("role") == "user":
            last_user = messages[-1]["content"]

        input_payload: dict[str, Any] = {"messages": [*history, HumanMessage(content=last_user)]}
        config = {"configurable": {"thread_id": self.thread_id}}

        final_text_parts: list[str] = []
        try:
            # astream(stream_mode="messages") → (chunk, metadata)
            # chunk 可能是 AIMessageChunk / ToolMessage / HumanMessage 等
            async for chunk, _meta in self._agent.astream(input_payload, config=config,
                                                          stream_mode="messages"):
                if cancel and cancel():
                    yield ("error", "cancelled")
                    return
                if isinstance(chunk, AIMessageChunk):
                    text = chunk.content
                    if isinstance(text, list):
                        # 多模态场景：拼成纯文本
                        text = "".join(p.get("text", "") if isinstance(p, dict) else str(p)
                                       for p in text)
                    text = sanitize_text(text)
                    if text:
                        final_text_parts.append(text)
                        yield ("text", text)
                elif isinstance(chunk, ToolMessage):
                    # 工具执行结果
                    name = chunk.name or ""
                    result = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                    yield ("tool", name, "", result)
            yield ("done", "".join(final_text_parts))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("LangChainAgent 失败")
            yield ("error", str(e))


__all__ = ["LangChainAgent", "LangChainAgentConfig", "convert_tools"]