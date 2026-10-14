"""LangChain 标准后端（与轻量级后端并存）。"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

# LangChain / LangGraph（标准后端的核心）
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
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


# 内部 Tool → LangChain StructuredTool 的适配器
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


# 主类
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
    # LangChain 1.0+ 第一次 import 慢（含 langchain / langgraph / openai 几个大包），
    # 后台线程里 `_runner` 跑完才能 `self._ready.set()`。旧 5 秒经常不够，会让
    # 后续 `run_coro` 拿到 `self.loop is None` 卡死。30 秒足够冷启动。
    _START_TIMEOUT_S = 30.0

    def __init__(self) -> None:
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._runner_exc: Optional[BaseException] = None   # 记录线程里 import / 设 loop 失败

    def start(self) -> None:
        if self.thread is not None:
            return
        self._ready.clear()
        self._runner_exc = None

        def _runner():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self.loop = loop
                self._ready.set()
                try:
                    loop.run_forever()
                finally:
                    loop.close()
                    self.loop = None
            except BaseException as e:  # noqa: BLE001
                # 记录 import 失败等异常,start() 收到 _ready 超时后能看到
                self._runner_exc = e
                self._ready.set()   # 解锁 start() 的 wait,让 raise 出去
                raise

        self.thread = threading.Thread(target=_runner, daemon=True, name="lc-persistent-loop")
        self.thread.start()
        got_ready = self._ready.wait(timeout=self._START_TIMEOUT_S)
        if not got_ready:
            log.error(
                "LangChain 后台 loop 启动超时（>%.0fs）。可能 LangChain 包导入慢或线程卡死。",
                self._START_TIMEOUT_S,
            )
            # 释放 thread 引用(daemon 线程已泄漏,引用清掉避免下次误判已启动)
            self.thread = None
            raise RuntimeError(
                f"LangChain 后台 loop 启动超时（>{self._START_TIMEOUT_S:.0f}s）"
            )
        if self._runner_exc is not None:
            log.error("LangChain 后台 loop 启动失败: %r", self._runner_exc)
            # runner 已经 raise 出去,thread 会自然结束,但还是清引用保持一致
            self.thread = None
            raise RuntimeError(f"LangChain 后台 loop 启动失败: {self._runner_exc!r}")

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
        if self.loop is None:
            # start() 之后仍 None → 启动失败,抛错而不是传给 run_coroutine_threadsafe
            raise RuntimeError("LangChain 持久 loop 不可用，请查看上面 ERROR 日志")
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
        cwd: Optional[str] = None,
        extra_docs_paths: Optional[list] = None,
        session_id: Optional[str] = None,
        session_store: Optional[Any] = None,
    ):
        self.llm_cfg = llm_cfg
        self.registry = registry
        self.persona = persona
        self.cfg = cfg or LangChainAgentConfig()
        self.cancel_check = cancel_check
        # 会话绑定:优先用 session_id 从 session_store 拿 thread_id。
        # 这样会话列表 / 续接在 store 层管理,agent 内部只认 thread_id。
        self._session_id: Optional[str] = session_id
        self._session_store = session_store
        if session_id is not None:
            if session_store is None:
                raise ValueError("session_id given without session_store")
            info = session_store.get(session_id)
            if info is None:
                raise ValueError(
                    f"SessionStore: session_id={session_id!r} not found"
                )
            self.thread_id = info.thread_id
        else:
            self.thread_id = thread_id
        # 项目级指令:从 cwd 向上找 AGENTS.md / CLAUDE.md,加上主程序注入的 extra_paths
        self._cwd = cwd
        self._extra_docs_paths: list = list(extra_docs_paths or [])
        # 缓存最近一次加载的项目指令(避免每次 _ensure_agent 重读)
        self._cached_project_docs: str = ""
        self._agent: Optional[Any] = None
        self._middlewares: Optional[list] = None  # 给测试/调试用
        # RiskyToolConfirm 控制器 —— 立刻建好,这样 confirm_tool_call /
        # reject_tool_call / on_confirm_needed 在 _ensure_agent() 跑之前
        # 也能用(UI 可能在 agent 第一次构建前就订阅回调)。
        try:
            from app.brain.hallucination_guard import RiskyToolConfirm
            self._confirm_ctrl: Optional[RiskyToolConfirm] = RiskyToolConfirm()
        except Exception as e:  # noqa: BLE001
            log.warning("RiskyToolConfirm 初始化失败: %s", e)
            self._confirm_ctrl = None
        self._checkpointer_cm: Optional[Any] = None
        self._checkpointer: Optional[Any] = None
        self._model: Optional[Any] = None
        # 启动持久 loop（懒初始化）。失败时清掉类级 _LOOP,
        # 避免一次启动失败后整个进程里所有 LangChainAgent 都跟着挂掉。
        try:
            self._get_loop()
        except Exception:
            # 启动失败,把 _LOOP 重置,下次再 _get_loop() 重新尝试
            LangChainAgent._LOOP = None
            raise

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

    # -------------------- 高风险工具 confirm(HITL) --------------------
    def confirm_tool_call(self, tool_call_id: str) -> None:
        """主程序 / UI 在确认弹窗点「确认」后调用。

        把该 tool_call_id 加进确认集合。后续该 ID 的重试会直接放行
        (无需走 __confirmed=True 协议)。
        """
        if self._confirm_ctrl is not None:
            try:
                self._confirm_ctrl.confirm(tool_call_id)
            except Exception as e:  # noqa: BLE001
                log.warning("confirm_tool_call 失败: %s", e)

    def reject_tool_call(self, tool_call_id: str) -> None:
        """主程序 / UI 拒绝后调用 —— 该 ID 永远不通过。"""
        if self._confirm_ctrl is not None:
            try:
                self._confirm_ctrl.reject(tool_call_id)
            except Exception as e:  # noqa: BLE001
                log.warning("reject_tool_call 失败: %s", e)

    def on_confirm_needed(self, callback) -> None:
        """注册一个拦截回调 callback(tool_call_id, tool_name, args)。

        触发时机:每次高风险工具被拦截(任何 astream 轮次里都可能触发)。
        主程序通常在这里弹 confirm dialog,等用户点完按钮再调用 confirm_tool_call
        / reject_tool_call 推进 agent。
        """
        if self._confirm_ctrl is not None:
            try:
                self._confirm_ctrl.on_blocked(callback)
            except Exception as e:  # noqa: BLE001
                log.warning("on_confirm_needed 失败: %s", e)

    # -------------------- Session 同步 --------------------
    def touch_session(self, *, message_count: Optional[int] = None,
                      title: Optional[str] = None) -> bool:
        """更新当前 session 的 last_used_at(可选同步 message_count / title)。

        主程序每完成一轮对话就调一次,让「会话列表」按最近活跃度排序。
        """
        if self._session_store is None or self._session_id is None:
            return False
        try:
            return self._session_store.touch(
                self._session_id, message_count=message_count, title=title,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("touch_session 失败: %s", e)
            return False

    async def _ensure_agent(self) -> None:
        """异步构建 agent（model + tools + checkpointer）。首次调用时构建，后续复用。

        PR: 不在 _release_checkpointer 里关 httpx client 了——关闭 client 会让 LangChain
        内部缓存的 client 引用下次报 "client has been closed"。保留 _model 让下次重建走
        fast-path（只重建 graph，httpx client 复用）。

        2026-10-14: 接入 hallucination_guard middleware（动态 prompt 防幻觉 +
        高风险工具执行前打 WARNING），对齐参考项目 graphs.py 的 middleware 模式。
        """
        if self._agent is not None:
            return
        if self._model is None:
            self._model = self._build_model()
        tools = convert_tools(self.registry)
        if self._checkpointer is None and self.cfg.enable_checkpointer:
            await self._build_checkpointer()
        checkpointer = self._checkpointer

        # 抗幻觉 middleware（dynamic_prompt + risky tool logger + confirm interceptor）
        try:
            from app.brain.hallucination_guard import build_hallucination_middlewares
            std_mws, _new_ctrl = build_hallucination_middlewares()
            # _new_ctrl 是 build 临时构造的,丢弃。confirm 中间件单独用
            # self._confirm_ctrl.as_middleware() 生成,这样跨 astream 的
            # confirm/reject 状态能持续生效。
            middlewares = list(std_mws)
            if self._confirm_ctrl is not None:
                middlewares.append(self._confirm_ctrl.as_middleware())
        except Exception as e:  # noqa: BLE001
            log.warning("HallucinationGuard 加载失败,降级无 middleware: %s", e)
            middlewares = []

        # system_prompt = persona + 项目级指令(AGENTS.md / CLAUDE.md)
        # dynamic_prompt middleware 会在这个基础上再加反幻觉 / 时间 / confirm 协议
        try:
            from app.brain.project_docs import load_project_docs
            self._cached_project_docs = load_project_docs(
                cwd=self._cwd, extra_paths=self._extra_docs_paths,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("ProjectDocs 加载失败,降级无项目指令: %s", e)
            self._cached_project_docs = ""
        base_sp = self.persona or "你是一个有用的桌面助手。"
        if self._cached_project_docs:
            base_sp = base_sp + self._cached_project_docs

        self._agent = create_agent(
            model=self._model,
            tools=tools,
            system_prompt=base_sp,
            checkpointer=checkpointer,
            middleware=middlewares,
        )
        # 把中间件列表挂在 agent 上,供测试 / 调试取用。
        # CompiledStateGraph 不暴露 .middleware,所以我们手动留一份。
        self._middlewares = middlewares
        log.info("LangChainAgent ready: tools=%d, checkpointer=%s, middlewares=%d",
                 len(tools), "on" if checkpointer else "off", len(middlewares))

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

    async def run(
        self,
        messages: list[dict],
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[tuple]:
        """事件协议。

        生命周期：
            1) await _ensure_agent() —— 构建 model + tools + checkpointer
            2) astream —— 流式产出 text/tool 事件
            3) yield done —— 全部结束
            4) finally —— await _release_checkpointer() 关闭 SQLite 连接

        2026-10-14: 加「checkpoint 救援」—— MiniMax 报 2013 'tool call result
        does not follow tool call' 时,怀疑是 checkpoint 里残留了
        schema 失配的 ToolMessage;清掉本 thread 的 checkpoint 后重试一次。
        """
        await self._ensure_agent()
        cancel = cancel_check or self.cancel_check

        history = self._to_lc_history(messages[:-1] if messages else [])
        last_user = ""
        if messages and messages[-1].get("role") == "user":
            last_user = messages[-1]["content"]

        input_payload: dict[str, Any] = {"messages": [*history, HumanMessage(content=last_user)]}
        config = {"configurable": {"thread_id": self.thread_id}}

        try:
            # 最多重试一次:MiniMax 2013 → 清 ckpt → 重跑
            recovered = False
            for attempt in range(2):
                did_recover = False
                async for ev in self._astream_with_retry(
                    input_payload, config, cancel, recovered_after_retry=recovered,
                ):
                    yield ev
                    if ev[0] == "_recovered_retry":
                        # 清掉 ckpt 后请求再调一次;清在事件触发时已完成
                        did_recover = True
                if not did_recover:
                    break
                # 已经因为 2013 触发重试:把 recovered 标 True,第二次还会再跑
                # 一遍同样的逻辑(但这次如果再失败就不会再重试)
                recovered = True
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("LangChainAgent 失败")
            yield ("error", str(e))

    async def _astream_with_retry(self, input_payload, config, cancel,
                                    recovered_after_retry: bool = False):
        """单次 astream 跑,失败时区分是否 2013。

        抛出事件:
          - ("text", ...) / ("tool", ...) / ("done", ...) —— 正常输出
          - ("error", ...)                       —— 终止
          - ("_recovered_retry",)                —— 2013 已清 ckpt,让外层再跑一次
        """
        final_text_parts: list[str] = []
        try:
            async for chunk, _meta in self._agent.astream(input_payload, config=config,
                                                          stream_mode="messages"):
                if cancel and cancel():
                    yield ("error", "cancelled")
                    return
                if isinstance(chunk, (AIMessageChunk, AIMessage)):
                    text = chunk.content
                    if isinstance(text, list):
                        text = "".join(p.get("text", "") if isinstance(p, dict) else str(p)
                                       for p in text)
                    text = sanitize_text(text, is_final=False)
                    if text:
                        final_text_parts.append(text)
                        yield ("text", text)
                elif isinstance(chunk, ToolMessage):
                    name = chunk.name or ""
                    result = chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                    # 高风险工具 confirm 拦截 sentinel:ToolMessage 内容是 JSON,
                    # status="awaiting_confirmation" / "rejected"。识别后除了
                    # yield tool 事件让 UI 正常显示,还要 yield confirm_needed
                    # 让上层有机会弹 confirm dialog + 调 confirm_tool_call。
                    try:
                        import json as _json
                        parsed = _json.loads(result)
                        if isinstance(parsed, dict) and parsed.get("status") in (
                            "awaiting_confirmation", "rejected",
                        ):
                            tc_id = getattr(chunk, "tool_call_id", "") or ""
                            args_view = parsed.get("args") or {}
                            yield ("tool", name, "", result)
                            yield ("confirm_needed", tc_id, name, args_view)
                            continue
                    except (ValueError, TypeError):
                        pass  # 普通工具结果,走默认路径
                    yield ("tool", name, "", result)
            yield ("done", "".join(final_text_parts))
            return
        except Exception as e:
            msg = str(e)
            if "2013" in msg and "tool call result does not follow tool call" in msg:
                if recovered_after_retry:
                    # 已经重试过一次还 2013 → 不再救
                    log.warning("LangChainAgent: 2013 重试后仍失败")
                    yield ("error", msg)
                    return
                # 清 ckpt;成功后告诉外层再跑一次
                if self._clear_thread_checkpoint():
                    log.warning(
                        "LangChainAgent: 检测到 2013,清掉 thread=%s 的 checkpoint,准备重试",
                        self.thread_id,
                    )
                    yield ("_recovered_retry",)
                    return
                # 没接 checkpointer 之类,清不掉 → 当 error
                yield ("error", "checkpoint 2013 且无法清理")
                return
            raise  # 非 2013 → 让外层 try 接住

    def _clear_thread_checkpoint(self) -> bool:
        """清掉 self.thread_id 在 checkpoint DB 里的所有记录。

        2026-10-14: 当 MiniMax 2013 (tool call result does not follow tool call)
        触发,怀疑 checkpoint 残留了 schema 失配的 ToolMessage,清掉再试。
        """
        try:
            if not self.cfg.enable_checkpointer or not self.cfg.checkpoint_db:
                return False
            from pathlib import Path
            import sqlite3
            db_path = Path(self.cfg.checkpoint_db)
            if not db_path.is_file():
                return False
            conn = sqlite3.connect(str(db_path))
            try:
                cur = conn.execute(
                    "DELETE FROM checkpoints WHERE thread_id = ?",
                    (self.thread_id,),
                )
                ckpt_n = cur.rowcount
                cur = conn.execute(
                    "DELETE FROM writes WHERE thread_id = ?",
                    (self.thread_id,),
                )
                write_n = cur.rowcount
                conn.commit()
                log.info(
                    "LangChainAgent: 清掉 thread=%s 的 checkpoint(ckpt=%d, write=%d)",
                    self.thread_id, ckpt_n, write_n,
                )
                return ckpt_n + write_n > 0
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("清 checkpoint 失败:%s", e)
            return False

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