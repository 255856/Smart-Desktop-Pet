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
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

# LangChain / LangGraph（标准后端的核心）
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
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
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
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


class _PersistentLoop:
    """管理一个专属后台线程 + 持久 event loop，专门跑 LangChain 的 astream。

    关键：所有 LangChain 资源（httpx client、AsyncSqliteSaver）都在这个 loop 里创建，
    永不切换 loop → 彻底避免 "Event loop is closed" 问题。
    """
    def __init__(self) -> None:
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self.thread is not None:
            return
        self._ready.clear()
        def _runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self.loop = loop
            self._ready.set()
            try:
                loop.run_forever()
            finally:
                loop.close()
                self.loop = None
        self.thread = threading.Thread(target=_runner, daemon=True, name="lc-persistent-loop")
        self.thread.start()
        self._ready.wait(timeout=5)

    def stop(self) -> None:
        if self.loop is not None:
            try:
                self.loop.call_soon_threadsafe(self.loop.stop)
            except Exception:  # noqa: BLE001
                pass
        if self.thread is not None:
            self.thread.join(timeout=2)
            self.thread = None

    def run_coro(self, coro: Any, timeout: Optional[float] = None) -> Any:
        if self.loop is None:
            self.start()
        assert self.loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)


class LangChainAgent:
    """LangChain 1.0+ create_agent 实现的桌宠 Agent。

    关键：所有 LangChain 资源（ChatOpenAI、AsyncSqliteSaver、CompiledStateGraph）
    都跑在一个**持久后台 loop** 里（_PersistentLoop），跨调用不重建。
    这避免了 asyncio.run() 每次创建新 loop 导致的 "Event loop is closed" 问题。
    """

    # 类级别的持久 loop（懒初始化，避免循环引用）
    _LOOP: Optional["_PersistentLoop"] = None

    @classmethod
    def _get_loop(cls) -> "_PersistentLoop":
        if cls._LOOP is None:
            cls._LOOP = _PersistentLoop()
            cls._LOOP.start()
        return cls._LOOP

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
        self._model: Optional[Any] = None
        # 启动持久 loop（懒初始化）
        self._get_loop()

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
    async def _build_checkpointer(self):
        """构建 AsyncSqliteSaver 实例（已 aenter，可直接给 create_agent 用）。

        关键：这个 await 必须在持久 loop 里跑，所以 _ensure_agent 整体必须通过 run_sync
        在后台 loop 里执行。chat_window 的 _AgentWorker.run() 会调用 run_sync。
        """
        if not self.cfg.enable_checkpointer:
            return None
        Path(self.cfg.checkpoint_db).parent.mkdir(parents=True, exist_ok=True)
        # AsyncSqliteSaver.from_conn_string 是 @asynccontextmanager，
        # 内部会 await aiosqlite.connect + yield saver；await __aenter__ 拿到 saver 实例
        self._checkpointer_cm = AsyncSqliteSaver.from_conn_string(self.cfg.checkpoint_db)
        self._checkpointer = await self._checkpointer_cm.__aenter__()
        return self._checkpointer

    async def _release_checkpointer(self) -> None:
        """关闭 AsyncSqliteSaver；httpx client 不主动关（让 GC 处理）。

        之前实验过 await client.close() 会导致下次调用报 "client has been closed"
        —— LangChain 内部可能缓存了 client 引用，关闭后下次重建仍报错的根因不明。
        关掉这段后 "Event loop is closed" 还是会偶发（GC 时机），但频率大幅降低。
        """
        if self._checkpointer_cm is not None:
            try:
                await self._checkpointer_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                log.exception("AsyncSqliteSaver close failed")
            self._checkpointer_cm = None
            self._checkpointer = None

        # 注意：保留 _model 不变（不再重建 → 后续 _ensure_agent 走 fast-path 复用）
        self._agent = None

    def close(self) -> None:
        """同步 close（向后兼容 QThread 信号回调路径，实际关闭在 _release_checkpointer 里跑）。

        Qt 信号回调在主线程、没有 event loop，所以这里只是清标志位；
        真正的关闭由 chat_window 的 _AgentWorker 在 drive() 协程里 await _release_checkpointer() 完成。
        """
        # Qt 信号回调路径：仅做标记，真正释放由 worker 协程做
        # 不抛异常以保证 _on_done / _on_failed 不会中断 UI

    # ---------- 构建 agent ----------
    async def _ensure_agent(self) -> None:
        """异步构建 agent（model + tools + checkpointer）。首次调用时构建，后续复用。

        PR: 不在 _release_checkpointer 里关 httpx client 了——关闭 client 会让 LangChain
        内部缓存的 client 引用下次报 "client has been closed"。保留 _model 让下次重建走
        fast-path（只重建 graph，httpx client 复用）。
        """
        if self._agent is not None:
            return
        if self._model is None:
            self._model = self._build_model()
        tools = convert_tools(self.registry)
        if self._checkpointer is None and self.cfg.enable_checkpointer:
            await self._build_checkpointer()
        checkpointer = self._checkpointer

        # system_prompt 直接用人设原文（LLMClient 的 sanitize 已在此层处理）
        self._agent = create_agent(
            model=self._model,
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
        """与 AgentLoopV2 兼容的事件协议。

        生命周期：
            1) await _ensure_agent() —— 构建 model + tools + checkpointer
            2) astream —— 流式产出 text/tool 事件
            3) yield done —— 全部结束
            4) finally —— await _release_checkpointer() 关闭 SQLite 连接
        """
        await self._ensure_agent()
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
                # 同时处理增量 (AIMessageChunk) 和完整消息 (AIMessage)：
                # 有些模型/服务端组合下 LangGraph 会一次返回最终聚合的 AIMessage
                if isinstance(chunk, (AIMessageChunk, AIMessage)):
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
        finally:
            # 释放 checkpointer 连接；下次 _ensure_agent() 会重建
            await self._release_checkpointer()

    def run_sync(self, messages: list[dict],
                 cancel_check: Optional[Callable[[], bool]] = None,
                 timeout: float = 300.0) -> list[tuple]:
        """同步版 run：在持久 loop 里跑 async run()，返回所有事件。

        给 _AgentWorker (QThread) 调用 —— 避免在 QThread 里再开新 loop 导致 "Event loop is closed"。
        """
        async def collect():
            out: list[tuple] = []
            async for ev in self.run(messages, cancel_check=cancel_check):
                out.append(ev)
            return out
        return LangChainAgent._get_loop().run_coro(collect(), timeout=timeout)


__all__ = ["LangChainAgent", "LangChainAgentConfig", "convert_tools"]