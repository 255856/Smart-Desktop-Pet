"""LangChain 标准后端（与轻量级后端并存）。"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Optional

# LangChain / LangGraph（标准后端的核心）
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)
from langchain_core.tools import StructuredTool

from app.brain.llm_client import (
    LLMConfig,
    build_usage_event,
    normalize_usage,
    sanitize_text,
)
from app.brain.token_ledger import next_request_index, record_usage
from app.brain.tool_schema import tool_to_args_schema
from app.brain.prompt_layer import (build_dynamic_block, build_system_prompt,
                                    build_tool_handbook, load_style_guide)
from app.engine.tools import Tool, ToolRegistry

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 重量级依赖延迟导入
#
# create_agent / ChatOpenAI / AsyncSqliteSaver 三个 import 实测：
#   langchain_openai                1.84 s
#   langchain.agents (create_agent) 1.00 s
#   langgraph.checkpoint.sqlite.aio 0.01 s
#
# 而它们只在**真的要用 standard 后端**时才需要。聊天窗原来在模块级 import 了
# 本模块 → 每次启动桌宠（哪怕用 lightweight 后端）都要白付这 ~1.1~1.8 秒。
#
# 现在改成「在用到的地方 import」：模块本身只留轻量的 langchain_core.messages
# （isinstance 判断 / 构造消息要用）。真正用到的三个函数里各自 import，
# 第一次调用之后 Python 会缓存模块，第二次起就没有额外开销了。
#
# 但 create_agent 以前是**模块级属性**，测试和手动验证脚本会用
# mock.patch("app.brain.langchain_agent.create_agent") 拦截 agent 创建。
# 为了不破坏这个约定，这里用 PEP 562 的模块级 __getattr__ 把这几个重依赖
# 按需暴露成模块属性：模块导入时不会真加载 langchain（启动不受影响），
# 第一次真正访问该属性时才导入并缓存进模块命名空间。
# ---------------------------------------------------------------------------

_LAZY_ATTRS = {
    "create_agent": ("langchain.agents", "create_agent"),
    "ChatOpenAI": ("langchain_openai", "ChatOpenAI"),
    "AsyncSqliteSaver": ("langgraph.checkpoint.sqlite.aio", "AsyncSqliteSaver"),
}


def __getattr__(name: str):
    """PEP 562：重依赖按需暴露成模块属性（保持旧的 mock.patch 约定）。"""
    target = _LAZY_ATTRS.get(name)
    if target is None:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}")
    import importlib
    mod_name, attr = target
    try:
        value = getattr(importlib.import_module(mod_name), attr)
    except ImportError as e:  # pragma: no cover - 环境没装 langchain
        raise ImportError(
            "请先安装 langchain>=1.0: pip install langchain langchain-openai "
            "langgraph langgraph-checkpoint-sqlite"
        ) from e
    # 缓存进模块命名空间：后续访问直接命中，不再走 __getattr__
    globals()[name] = value
    return value


# 派生 session_id 前缀（chat_window 按人设派生："char:<人设名>:main"）。
# 这种 id 不登记在 SessionStore 里，直接当 thread_id 用；其余未知 id 照旧报错。
_DERIVED_SESSION_PREFIX = "char:"


def _text_fingerprint(text: str) -> str:
    """文本指纹（sha1 前 16 位）—— 判断「记忆块变了没」。

    Stage 2.8：standard 后端的 agent 只构建一次，记忆块被烘进 system prompt，
    于是会话中写入的新事实永远进不了 prompt（主人说「记住我明天要交房租」，
    桌宠下一轮照样当没听见）。共享层（prompt_layer）每次都重算记忆块，
    **内容**是活的；过期的是 agent 实例。

    这里用记忆块的指纹做廉价变更检测：变了才重建 agent（保住编译好的图 +
    checkpointer 状态），没变就原样复用 —— 不是每轮重建。
    """
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


# 内部 Tool → LangChain StructuredTool 的适配器
def _internal_tool_to_langchain(tool: Tool) -> StructuredTool:
    """把桌宠的 Tool(name, description, parameters, fn) 包装为 LangChain StructuredTool。

    args_schema 必须来自**手写 JSON Schema**（tool.parameters），不能靠
    from_function 从 fn 签名反推：反推会把属性描述 / enum / min-max 全丢掉，
    还会把 run_script 的 args 改名成 v__args（撞 BaseTool.args），
    两种调用方式都失败。转换见 app/brain/tool_schema.py。
    """
    return StructuredTool.from_function(
        func=tool.fn,
        name=tool.name,
        description=tool.description,
        args_schema=tool_to_args_schema(tool),
        parse_docstring=False,
    )


def convert_tools(registry: ToolRegistry) -> list[StructuredTool]:
    """批量转换 ToolRegistry → LangChain tool 列表。"""
    return [_internal_tool_to_langchain(t) for t in registry._tools.values()]


def convert_tools_for_level(registry: ToolRegistry, level: int) -> list[StructuredTool]:
    """按等级过滤后转换 ToolRegistry → LangChain tool 列表。"""
    return [_internal_tool_to_langchain(t) for t in registry.tools_for_level(level)]


# 主类
# LangChainAgentConfig 住在轻量模块 app/brain/langchain_config.py，
# 这里再导出一次，老的 `from app.brain.langchain_agent import LangChainAgentConfig`
# 写法继续可用（见本文件末尾 __all__）。
from app.brain.langchain_config import LangChainAgentConfig  # noqa: E402


class _PersistentLoop:
    """管理一个专属后台线程 + 持久 event loop，专门跑 LangChain 的 astream。

    所有 LangChain 资源（httpx client、AsyncSqliteSaver）都在这个 loop 里创建，
    永不切换 loop → 彻底避免 "Event loop is closed" 问题。
    """
    # 后台线程冷启动超时（LangChain 1.0+ 第一次 import 慢）
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
            try:  # 记录 import 失败等异常，start() 收到 _ready 超时后能看到
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
            self.thread = None
            raise RuntimeError(
                f"LangChain 后台 loop 启动超时（>{self._START_TIMEOUT_S:.0f}s）"
            )
        if self._runner_exc is not None:
            log.error("LangChain 后台 loop 启动失败: %r", self._runner_exc)
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


# ---------------------------------------------------------------------------
# 台账：一次模型调用 = 一条台账行
# ---------------------------------------------------------------------------
# 台账主键 = (session_id, request_index, backend, retry)。request_index 必须
# **按模型调用**分配 —— 一个回合里模型可能被调用多次（模型调工具 → 工具结果 →
# 再调模型），每次都是一次独立的计费请求。
#
# 轻量后端按「每次 provider HTTP 请求」分配（正确）。标准后端以前按「整个
# astream」分配 —— 一个回合里的多次模型调用共用同一个 request_index，台账按
# 主键折叠（后到覆盖先到），于是**只有最后一次调用的用量留下来，前几次的
# token 被静默丢掉**：工具调用越多，少算得越多。


def _model_call_key(meta: Any) -> Optional[tuple]:
    """从 LangGraph 的 stream metadata 里取「这是第几次模型调用」。

    ``langgraph_step`` 在一次 super-step 内恒定、跨 super-step 递增，而一次
    模型调用恰好对应一个 model super-step（工具节点占另一个 step）。
    拿不到它 → 返回 None，调用方退回「一次 usage 事件 = 一次模型调用」。
    """
    if not meta:
        return None
    try:
        step = meta.get("langgraph_step")
    except Exception:  # noqa: BLE001
        return None
    if step is None:
        return None
    try:
        return ("step", int(step))
    except Exception:  # noqa: BLE001
        return ("step", str(step))


class _ModelCallUsageTracker:
    """把「一个回合里的 N 次模型调用」分别记账。

    同一次模型调用里 provider 重复上报 usage 时共用同一个 request_index ——
    台账按主键折叠（后到覆盖先到），不会被算成两次计费。
    """

    __slots__ = ("_key", "_request_index", "_fallback_seq")

    def __init__(self) -> None:
        self._key: Optional[tuple] = None
        self._request_index: Optional[int] = None
        self._fallback_seq = 0

    def request_index(self, meta: Any) -> int:
        """本次 usage 事件所属模型调用的 request_index（同一次模型调用内恒定）。"""
        key = _model_call_key(meta)
        if key is None:
            # 没有 LangGraph 元数据 → 只能按「一次 usage 事件 = 一次模型调用」
            self._fallback_seq += 1
            key = ("usage-event", self._fallback_seq)
        if key != self._key:
            self._key = key
            self._request_index = next_request_index()
        return self._request_index


class LangChainAgent:
    """LangChain 1.0+ create_agent 实现的桌宠 Agent。

    所有 LangChain 资源（ChatOpenAI、AsyncSqliteSaver、CompiledStateGraph）
    都跑在一个**持久后台 loop** 里（_PersistentLoop），跨调用不重建。
    避免 asyncio.run() 每次创建新 loop 导致的 "Event loop is closed" 问题。
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
        skills_dir: Optional[str] = None,
        auto_load_skills: Optional[list] = None,
        memory_store: Optional[Any] = None,
        level_getter: Optional[Callable[[], int]] = None,
    ):
        self.llm_cfg = llm_cfg
        self.registry = registry
        self.persona = persona
        self.cfg = cfg or LangChainAgentConfig()
        # 工具解锁:返回当前桌宠等级 int,LLM 工具列表按等级过滤,未解锁拦截
        self.level_getter = level_getter
        self.cancel_check = cancel_check
        # 长期记忆（记忆 sidecar 客户端），跨 session 共享；注入时调 memory.system_block()
        self.memory_store = memory_store
        # 会话绑定：优先用 session_id 从 session_store 拿 thread_id
        self._session_id: Optional[str] = session_id
        self._session_store = session_store
        if session_id is not None:
            info = (session_store.get(session_id)
                    if session_store is not None else None)
            if info is not None:
                # store 里真实存在的会话 → 用它登记的 thread_id
                # （checkpoint 才落在同一条线上）
                self.thread_id = info.thread_id
            elif session_store is None:
                raise ValueError("session_id given without session_store")
            elif session_id.startswith(_DERIVED_SESSION_PREFIX):
                # v31: 派生 session_id（"char:<人设>:main"）本来就不在 store 里 ——
                # 直接把它当 thread_id：切人设 = 换 session_id = 换 checkpoint 分文件。
                # 注意别把 store 查询整个绕开：真·未知 session_id 必须照旧报错。
                self.thread_id = session_id
            else:
                raise ValueError(
                    f"SessionStore: session_id={session_id!r} not found"
                )
        else:
            self.thread_id = thread_id
        # 项目级指令：从 cwd 向上找 AGENTS.md / CLAUDE.md + 主程序注入的 extra_paths
        # Stage 1.1：加载结果进共享层（app/brain/prompt_layer.build_dynamic_block），
        # 这里仍然负责「从哪儿读」—— 两个后端必须读到**同一份**内容。
        self._cwd = cwd
        self._extra_docs_paths: list = list(extra_docs_paths or [])
        # 缓存最近一次加载的项目指令 / skills（测试与调试要看这两个属性）
        self._cached_project_docs: str = ""
        self._skills_dir = skills_dir
        self._auto_load_skills: list = list(auto_load_skills or [])
        self._cached_skills: str = ""
        self._agent: Optional[Any] = None
        # Stage 2.8：当前 agent 里烘进去的「记忆块指纹」—— 变了才重建 agent
        self._agent_memory_sig: Optional[str] = None
        self._middlewares: Optional[list] = None  # 给测试/调试用
        # RiskyToolConfirm 控制器：立刻建好，UI 在 agent 构建前可订阅回调
        try:
            from app.brain.hallucination_guard import RiskyToolConfirm
            self._confirm_ctrl: Optional[RiskyToolConfirm] = RiskyToolConfirm()
        except Exception as e:  # noqa: BLE001
            log.warning("RiskyToolConfirm 初始化失败: %s", e)
            self._confirm_ctrl = None
        # post-LLM 拦截器：状态差时改写 preemptive 问文案
        try:
            from app.brain.hallucination_guard import PostLLMStateGuard
            self._post_llm_guard: Optional[PostLLMStateGuard] = PostLLMStateGuard()
        except Exception as e:  # noqa: BLE001
            log.warning("PostLLMStateGuard 初始化失败: %s", e)
            self._post_llm_guard = None
        self._checkpointer_cm: Optional[Any] = None
        self._checkpointer: Optional[Any] = None
        self._model: Optional[Any] = None
        # 启动持久 loop（懒初始化）。失败时清掉类级 _LOOP
        try:
            self._get_loop()
        except Exception:
            LangChainAgent._LOOP = None
            raise

        # 装上 Task 工具的 subagent factory，让主 agent 能派生子 agent
        try:
            from app.engine.tools import _subagent
            _subagent.set_subagent_factory(self._make_subagent_factory())
        except Exception as e:  # noqa: BLE001
            log.warning("Task 工具 subagent factory 装失败: %s", e)

    def _build_subagent_registry(self) -> ToolRegistry:
        """给子 agent 一个「安全」工具集：继承主 agent 的工具，去掉 Task 和高风险工具。

        返回新的 ToolRegistry（不修改 self.registry）。
        """
        from app.brain.hallucination_guard import _RISKY_TOOLS
        sub_reg = ToolRegistry()
        try:
            for name, tool in self.registry._tools.items():
                if name == "Task":
                    continue   # 禁止递归
                if name in _RISKY_TOOLS:
                    continue   # 危险工具子 agent 不该有
                sub_reg.register(tool)
        except Exception as e:  # noqa: BLE001
            log.warning("_build_subagent_registry 失败,降级空 registry: %s", e)
        return sub_reg

    def _make_subagent_factory(self):
        """构造 Task 工具用的 subagent factory。

        Factory 收到 (task_str, kwargs),开一个新 LangChainAgent,跑一次完整
        对话,把所有 text 拼起来返回。
        同步实现:用 ThreadPoolExecutor + asyncio.run 在子线程里跑(主 agent
        本身已经在 event loop 里,不能 asyncio.run 嵌套)。
        """
        import asyncio
        import concurrent.futures
        import uuid as _uuid

        parent_self = self

        def factory(task_str: str, kwargs: dict) -> str:
            agent_type = kwargs.get("agent_type", "general")
            sub = LangChainAgent(
                llm_cfg=parent_self.llm_cfg,
                registry=parent_self._build_subagent_registry(),
                persona=parent_self.persona,
                cfg=parent_self.cfg,
                thread_id=f"sub-{_uuid.uuid4().hex[:10]}",
                cwd=parent_self._cwd,
                extra_docs_paths=parent_self._extra_docs_paths,
            )
            # 子 agent 不用重新装 subagent factory(已经被本 factory 占用,
            # 递归风险已经通过 _build_subagent_registry 去掉 Task 工具消除)。
            try:
                _subagent_module = __import__(
                    "app.engine.tools._subagent",
                    fromlist=["set_subagent_factory"],
                )
                _subagent_module.set_subagent_factory(None)
            except Exception:
                pass

            async def _run():
                events: list[str] = []
                try:
                    async for ev in sub.run(
                        [{"role": "user", "content": task_str}],
                    ):
                        if ev[0] == "text":
                            events.append(ev[1])
                        elif ev[0] == "error":
                            return f"[sub-agent error] {ev[1]}"
                        elif ev[0] == "done":
                            final = ev[1] if ev[1] else "".join(events)
                            return final
                    return "".join(events)
                finally:
                    # 释放 checkpointer 连接（避免 SQLite 句柄泄漏）
                    try:
                        await sub._release_checkpointer()
                    except Exception:
                        pass

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(asyncio.run, _run())
                try:
                    return future.result(timeout=300)   # 5 分钟 cap
                except concurrent.futures.TimeoutError:
                    return "错误: sub-agent 超时(5 分钟)"
                except Exception as e:  # noqa: BLE001
                    log.exception("sub-agent 失败")
                    return f"错误: sub-agent 失败: {e}"

        return factory

    def _build_model(self) -> "ChatOpenAI":
        """OpenAI 兼容协议（DeepSeek / minimax / Ollama / 任何 base_url）。"""
        # 延迟 import：langchain_openai 实测 1.84 s（见文件头说明）
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as e:  # pragma: no cover
            raise ImportError("langchain-openai 未安装") from e
        kwargs = dict(
            base_url=self.llm_cfg.base_url,
            api_key=self.llm_cfg.api_key or "EMPTY",     # Ollama 接受任意非空字符串
            model=self.llm_cfg.model,
            temperature=self.llm_cfg.temperature,
            max_tokens=self.llm_cfg.max_tokens,
            timeout=self.llm_cfg.timeout,
            # 流式开关跟设置面板走 —— 之前硬编码 True，面板上那个
            # 「启用流式输出」勾选框点了毫无反应。
            streaming=bool(getattr(self.llm_cfg, "stream", True)),
            # v31: 打开 usage 流 — LangChain ChatOpenAI 1.0+ 用 stream_usage 控制。
            # 没开的话 LangGraph messages 流的 chunk.usage_metadata 全是 None,
            # 标准后端 chat_window 状态栏 token 永远 0。OpenAI / MiniMax / DeepSeek
            # 都能返 usage, 老 ollama 不支持时这字段保持 None 不影响。
            stream_usage=True,
        )
        # 推理强度：OpenAI o1/o3、部分推理模型支持。没设或为空就不传
        effort = getattr(self.llm_cfg, "reasoning_effort", None)
        if effort:
            kwargs["reasoning_effort"] = effort
        return ChatOpenAI(**kwargs)

    async def _build_checkpointer(self):
        """构建 AsyncSqliteSaver 实例（已 aenter，可直接给 create_agent 用）。

        这个 await 必须在持久 loop 里跑，所以 _ensure_agent 整体必须通过 run_sync
        在后台 loop 里执行。
        """
        if not self.cfg.enable_checkpointer:
            return None
        Path(self.cfg.checkpoint_db).parent.mkdir(parents=True, exist_ok=True)
        # 延迟 import：langgraph.checkpoint.sqlite.aio 见文件头说明
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        except ImportError as e:  # pragma: no cover
            raise ImportError("langgraph-checkpoint-sqlite 未安装") from e
        # AsyncSqliteSaver.from_conn_string 是 @asynccontextmanager
        self._checkpointer_cm = AsyncSqliteSaver.from_conn_string(self.cfg.checkpoint_db)
        self._checkpointer = await self._checkpointer_cm.__aenter__()
        return self._checkpointer

    async def _release_checkpointer(self) -> None:
        """关闭 AsyncSqliteSaver；httpx client 不主动关（让 GC 处理）。"""
        if self._checkpointer_cm is not None:
            try:
                await self._checkpointer_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                log.exception("AsyncSqliteSaver close failed")
            self._checkpointer_cm = None
            self._checkpointer = None

        # 保留 _model 不变（不再重建 → 后续 _ensure_agent 走 fast-path 复用）
        self._agent = None
        # agent 没了，记忆块指纹也一并清掉（下次必然重建）
        self._agent_memory_sig = None

    async def _prune_bloated_checkpoint(self, max_bytes: int = 512 * 1024) -> None:
        """清掉「历史遗留」的臃肿 checkpoint 行。

        背景：老实现每轮都把完整本地 history 塞进 graph，而 add_messages 是
        追加语义 → 同一段历史被一遍遍写进 checkpoint。
        实测 data/langchain_checkpoints.db 涨到 **275 MB**（510 条 checkpoint，
        单条最大 1.26 MB / 4,393 条消息，同一句「帮我锁屏」重复 213 次）。

        现在改成 RemoveMessage 覆盖式写入（见 _checkpoint_input），新写的
        checkpoint 很小。但**历史遗留的那些大行还在**（saver 每次 aput 是
        插新行，不是覆盖旧行），所以这里在构建 agent 时检查一下：
        本 thread 最新一条 checkpoint 超过 max_bytes 就把本 thread 的历史行清掉。

        清掉是安全的 —— 下一轮会用本地 history 把状态重新种回去
        （_checkpoint_input 里带 RemoveMessage，状态本来就是覆盖式的）。
        """
        if self._checkpointer is None or not self.thread_id:
            return
        cfg = {"configurable": {"thread_id": self.thread_id}}
        try:
            tup = await self._checkpointer.aget_tuple(cfg)
        except Exception as e:  # noqa: BLE001
            log.debug("读 checkpoint 失败，跳过清理: %s", e)
            return
        if tup is None:
            return
        try:
            blob = getattr(tup, "checkpoint", None)
            size = len(blob) if isinstance(blob, (bytes, bytearray)) else 0
            if not size:
                return
            if size <= max_bytes:
                return
            await self._checkpointer.adelete_thread(self.thread_id)
            log.info(
                "LangChainAgent: thread=%s 的 checkpoint 过大（%d 字节），"
                "已清理历史行（下一轮会用本地 history 重新种状态）",
                self.thread_id, size,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("清理臃肿 checkpoint 失败: %s", e)

    def close(self) -> None:
        """同步 close（向后兼容 QThread 信号回调路径，实际关闭在 _release_checkpointer 里跑）。

        Qt 信号回调在主线程、没有 event loop，所以这里只是清标志位；
        真正的关闭由 chat_window 的 _AgentWorker 在 drive() 协程里 await _release_checkpointer() 完成。
        """

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

        Stage 2.8：agent 只构建一次 ⇒ 记忆块被烘死在 system prompt 里，
        会话中写入的新事实永远看不见。现在按**记忆块指纹**判断要不要重建：
          · 记忆没变 → 直接复用现有 agent（不重建，编译好的图 + checkpointer 状态都留着）
          · 记忆变了 → 重建 agent，把新的记忆块烘进去
        """
        # Stage 2.1 + 记忆通道搬家：记忆块**不再**进 system prompt（它在易变
        # 通道，每轮按 query 变）⇒ 指纹必须**恒定**，否则每轮都会重建 agent
        # （编译好的图 + checkpointer 状态每轮丢掉）。记忆内容对 agent
        # 完全不可见 ⇒ 指纹恒定 ⇒ agent 只建一次。
        mem_sig = self._memory_signature()
        if self._agent is not None and (self._agent_memory_sig is None
                                       or mem_sig == self._agent_memory_sig):
            # 记忆没变 —— 不重建（重建会丢掉编译好的图和 checkpointer 状态）
            # _agent_memory_sig 为 None = 这个 agent **不是我们建的**
            # （外部注入的替身 / 测试假图）—— 尊重调用方，别偷偷换掉。
            return
        if self._model is None:
            self._model = self._build_model()
        tools = convert_tools_for_level(self.registry, self._current_level())
        if self._checkpointer is None and self.cfg.enable_checkpointer:
            await self._build_checkpointer()
        checkpointer = self._checkpointer
        # 历史遗留的臃肿 checkpoint 清理（详见 _prune_bloated_checkpoint）
        await self._prune_bloated_checkpoint()

        # 抗幻觉 middleware（dynamic_prompt + risky tool logger + confirm interceptor）
        try:
            from app.brain.hallucination_guard import build_hallucination_middlewares
            std_mws, _new_ctrl = build_hallucination_middlewares()
            # _new_ctrl 是 build 临时构造的，丢弃。confirm 中间件单独用
            # self._confirm_ctrl.as_middleware() 生成，跨 astream 的 confirm/reject 状态持续生效
            middlewares = list(std_mws)
            if self._confirm_ctrl is not None:
                middlewares.append(self._confirm_ctrl.as_middleware())
            # post-LLM 拦截器：状态差时改写 preemptive 问文案，加在最后
            if getattr(self, "_post_llm_guard", None) is not None:
                middlewares.append(self._post_llm_guard.as_middleware())
        except Exception as e:  # noqa: BLE001
            log.warning("HallucinationGuard 加载失败,降级无 middleware: %s", e)
            middlewares = []

        # Stage 1.1：最终 system 字符串由**共享层**拼（app/brain/prompt_layer.py）——
        # ReAct 后端走同一套函数、同一批输入 ⇒ 两个后端逐字节相同。
        #   人设 + 会话上下文（SKILL / 长期记忆 / 可用工具列表）= self.persona
        #   项目文档 / skills / 风格指南 / 工具手册 / 常量段 = 共享层
        #   防幻觉基线 + confirm 协议：共享层拼一次，grounding middleware 只做幂等透传
        #   （当前时间**不在** system prompt 里 —— 见 run() 里的说明）
        base_sp = self.assemble_system_prompt()

        # 延迟 import：langchain.agents 实测 1.00 s（见文件头说明）。
        # 先看模块属性（测试可能用 mock.patch 替换过），没有再走 __getattr__ 懒加载。
        create_agent = globals().get("create_agent")
        if create_agent is None:
            create_agent = __getattr__("create_agent")
        self._agent = create_agent(
            model=self._model,
            tools=tools,
            system_prompt=base_sp,
            checkpointer=checkpointer,
            middleware=middlewares,
        )
        # 记住这次烘进去的记忆块指纹（下次变了才重建）
        self._agent_memory_sig = mem_sig
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

    @staticmethod
    def _checkpoint_input(lc_msgs: list) -> list:
        """构造喂给 graph 的 messages：**先清空** checkpoint 里的旧消息，再放本轮要留的。

        为什么不能直接把完整历史塞进去：
        LangGraph 的 add_messages reducer 是**追加**语义 —— 传进去的消息会接到
        checkpoint 状态后面。原来每轮都把完整本地 history 塞进去，于是同一段历史
        被一遍遍写进 checkpoint：
          - data/langchain_checkpoints.db 实测涨到 275 MB（510 条 checkpoint）
          - 单条 checkpoint 4,393 条消息，同一句「帮我锁屏」重复 213 次
          - 模型每轮都要重读这堆重复内容（上下文窗口被撑爆）

        现在用 RemoveMessage(REMOVE_ALL_MESSAGES) 先把 checkpoint 的消息清空，
        再放本轮真正要保留的（= 已被 _trim_history 裁过的本地历史 + 本轮新增）：
          - 模型看到的上下文 = 本地裁剪后的历史，和设计意图一致（不再是重复膨胀版）
          - checkpoint 大小有界，不会随轮数无限增长
          - 每轮写入量恒定，不再 O(n²)

        RemoveMessage 是 langchain>=1.0 / langgraph 的标准做法：
        add_messages 遇到它会清空已有消息，后续消息照常追加。
        """
        try:
            # 延迟 import：langgraph.graph.message（见文件头说明）
            from langgraph.graph.message import REMOVE_ALL_MESSAGES
            return [RemoveMessage(id=REMOVE_ALL_MESSAGES), *lc_msgs]
        except Exception as e:  # noqa: BLE001
            log.warning("构造 checkpoint 输入失败，退回追加模式: %s", e)
            return lc_msgs

    async def _prefetch_aw_results(self, user_text: str) -> list:
        """同步调 AW 工具，返 LangChain messages 序列，插入历史模拟 LLM 自己调过。

        只对能直接从文本推断的 intent 子集做预调：
          - 「我刚才在干嘛 / 最近看了什么 / 过去X小时」→ aw_get_recent_activities
          - 「现在 / 当前 / 焦点窗口在哪个」→ aw_get_current_window
          - 「最近看了哪些网站」→ aw_get_browser_tabs
        """
        t = user_text or ""
        wants_activities = any(kw in t for kw in (
            "刚才在干嘛", "刚才看了什么", "刚才用了什么", "刚才浏览",
            "刚才做什么", "过去", "最近", "一小时", "半小时", "我做了什么"))
        wants_current = any(kw in t for kw in (
            "当前", "现在", "正在用", "焦点"))
        wants_browser = any(kw in t for kw in ("网页", "网站", "浏览器 tab", "逛了哪些"))

        results: list = []
        # 仅跑主程序一条，不并行堆——简单优先，出错不动 LLM 主流程
        if wants_activities:
            tool_msg = await self._call_aw("aw_get_recent_activities", {"minutes": 30, "limit": 15})
            if tool_msg:
                results.extend(tool_msg)
        if wants_current:
            tool_msg = await self._call_aw("aw_get_current_window", {})
            if tool_msg:
                results.extend(tool_msg)
        if wants_browser:
            tool_msg = await self._call_aw("aw_get_browser_tabs", {"minutes": 60, "limit": 10})
            if tool_msg:
                results.extend(tool_msg)
        return results

    async def _call_aw(self, tool_name: str, args: dict) -> list:
        """直接调 aw_* 工具，同步返 LangChain message 序列。

        用 app.integrations.activitywatch 调（不走 MCP，同步快），失败返 [] 不影响流程。
        """
        import uuid as _uuid
        try:
            from app.integrations.activitywatch import (
                get_recent_activities_summary,
                get_current_window_one_liner,
            )
        except Exception as e:
            log.debug("_call_aw import failed: %s", e)
            return []
        try:
            # ActivityWatch 客户端是同步 urllib（内部 3 次阻塞 urlopen，
            # 超时 2+4+6 秒）。这里是 async 函数，直接调会把这个持久 loop
            # 整个卡住 —— 该 loop 上所有 agent 操作/工具调用/流式输出一起等。
            # 丢到线程池里跑，loop 不被阻塞。
            if tool_name == "aw_get_recent_activities":
                content = await asyncio.to_thread(
                    get_recent_activities_summary,
                    minutes=int(args.get("minutes", 30)),
                    limit=int(args.get("limit", 15)),
                )
            elif tool_name == "aw_get_current_window":
                content = await asyncio.to_thread(get_current_window_one_liner)
            else:
                return []
        except Exception as e:
            log.debug("_call_aw call failed for %s: %s", tool_name, e)
            return []
        if content is None:
            content = f"(aw-server 未运行或 watcher 没装)"
        tool_call_id = f"prefetch_{_uuid.uuid4().hex[:10]}"
        # 创建对应 oai function name（ToOpenAI 用的命名: mcp_<server>_<tool>）
        oai_name = f"mcp_aw_{tool_name}"
        ai_msg = AIMessage(
            content="",
            tool_calls=[{
                "id": tool_call_id,
                "name": oai_name,
                "args": args,
            }],
        )
        tool_msg = ToolMessage(content=content, tool_call_id=tool_call_id)
        return [ai_msg, tool_msg]

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

        含「checkpoint 救援」：报 2013 'tool call result does not follow tool call' 时，
        清掉本 thread 的 checkpoint 后重试一次。
        """
        # 国产模型默认忽略 tool_choice 字段。命中 aw_query 等专用 intent 时，
        # 直接走 app.integrations.activitywatch 同步调工具，把 ToolMessage + AI tool_call
        # 拼进 messages，让 LLM 看到完整「问 → 调 → 结果」三段式
        intent = None
        injected_tool_msgs: list = []
        if messages and messages[-1].get("role") == "user":
            last_user_for_intent = messages[-1].get("content", "") or ""
        else:
            last_user_for_intent = ""
        try:
            from app.brain.llm_client import detect_action_intent
            intent = detect_action_intent(last_user_for_intent) if last_user_for_intent else None
        except Exception:
            intent = None

        # 命中 aw_query：同步调工具，把结果作为 AIMessage(tool_calls) + ToolMessage(content) 注入历史
        if intent == "aw_query" and last_user_for_intent:
            tool_results = await self._prefetch_aw_results(last_user_for_intent)
            if tool_results:
                injected_tool_msgs = tool_results
                log.info("LangChainAgent.run: aw_query 预调工具, 注入 %d 条 tool_call+tool_msg",
                         len(injected_tool_msgs) // 2)

        await self._ensure_agent()
        cancel = cancel_check or self.cancel_check

        # 本轮易变上下文（当前时间 / 桌宠实时状态）作为**独立的 user 消息**
        # 插在主人本轮发言前面。**不能**进 system prompt —— system message
        # 落在 MiniMax 的前缀缓存（最长公共前缀）块里，块里每轮变一次（时间戳！）
        # 整块作废（实测缓存命中率 98.9% → 1.5%）。
        # chat_window 已经插过的话这里是幂等 no-op；直接调 LangChainAgent 的
        # 路径（例如 task 子 agent）由这里兜底补上时间。
        from app.brain.llm_client import inject_dynamic_context
        # 本轮主人说的话 = 召回长期记忆的 query（易变通道里带上「你记得的事」）
        query = ""
        for m in reversed(messages or []):
            role = (m.get("role") if isinstance(m, dict)
                    else getattr(m, "role", ""))
            if role == "user":
                content = (m.get("content") if isinstance(m, dict)
                           else getattr(m, "content", ""))
                query = content if isinstance(content, str) else ""
                break
        messages = inject_dynamic_context(
            messages, state=getattr(self, "_companion_state", None),
            query=query)

        history = self._to_lc_history(messages[:-1] if messages else [])
        last_user = ""
        if messages and messages[-1].get("role") == "user":
            last_user = messages[-1]["content"]

        # 把注入的工具序列插在 user 消息前面，模拟 LLM 之前调过工具
        lc_msgs: list = [*history]
        lc_msgs.extend(injected_tool_msgs)
        lc_msgs.append(HumanMessage(content=last_user))

        input_payload: dict[str, Any] = {"messages": self._checkpoint_input(lc_msgs)}
        config = {"configurable": {"thread_id": self.thread_id}}

        try:
            # 最多重试一次：清 ckpt → 重跑
            recovered = False
            for attempt in range(2):
                did_recover = False
                async for ev in self._astream_with_retry(
                    input_payload, config, cancel, recovered_after_retry=recovered,
                ):
                    yield ev
                    if ev[0] == "_recovered_retry":
                        # 清掉 ckpt 后请求再调一次，清在事件触发时已完成
                        did_recover = True
                if not did_recover:
                    break
                # 已经触发重试：把 recovered 标 True
                recovered = True
            # 任务级 outcome：走完所有 yield 没异常 → success
            self._finish_run_with_outcome(success=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("LangChainAgent 失败")
            yield ("error", str(e))
            self._finish_run_with_outcome(success=False)

    # -------------------- 桌宠状态反馈闭环接入 --------------------
    # 任务结果（success / failure / rejected）反向更新 PetState。
    # 应用方式：主程序调 set_companion_state(state)，内部所有 task 调用都
    # 走同一个 agent 实例，set 一次永久生效（除非外部显式调用 set_companion_state(None) 解除）。

    def assemble_system_prompt(self, persona: str | None = None) -> str:
        """standard 后端的 system prompt 组装路径（唯一入口，Stage 1.1）。

        与 ReAct 后端（agent.AgentLoop.assemble_system_prompt）用**同一套**共享层
        函数、**同一批**输入 ⇒ 两个后端发给 API 的 system 字符串逐字节相同
        （见 tests/test_backend_parity.py）。

        - persona: 人设（+ 上游注入的会话上下文：SKILL / 可用工具列表）
        - 项目文档 / skills：走**本 agent 自己的**路径与 store
          （cwd / extra_docs_paths / skills_dir / auto_load_skills），
          跟 HEAD 的行为一致 —— 共享层负责「拼在哪、什么顺序」。

        ⚠️ **长期记忆块不在 system prompt 里**（Stage 2.1 + 记忆通道搬家）：
        记忆块按**本轮 query** 召回 ⇒ 每轮都变。放 system prompt 会打掉
        MiniMax 的前缀缓存（最长公共前缀）（98.9% → 1.5%），也会让 Stage 2.8
        的 agent 指纹每轮变（每轮重建 agent，编译好的图 + checkpointer
        状态全丢）。它现在走**易变通道**：build_turn_context(query=...) →
        独立的 user 消息（见 inject_dynamic_context）。
        """
        if persona is None:
            persona = self.persona or "你是一个有用的桌面助手。"
        return build_system_prompt(
            persona=persona,
            style_guide=load_style_guide(),
            tool_handbook=build_tool_handbook(
                registry=self.registry, level=self._current_level()),
            dynamic=build_dynamic_block(
                project_docs=self._load_project_docs(),
                skills=self._load_skills(),
            ),
        )

    def _load_project_docs(self) -> str:
        """项目级指令（AGENTS.md / CLAUDE.md）+ 额外注入的指令文件。

        没给 cwd 时从**项目根**往上找（与 ReAct 后端同一位置 ⇒ 两边一致）。
        """
        try:
            from app.brain.project_docs import load_project_docs
            from app.brain.prompt_layer import app_root
            self._cached_project_docs = load_project_docs(
                cwd=self._cwd or app_root(),
                extra_paths=self._extra_docs_paths,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("ProjectDocs 加载失败,降级无项目指令: %s", e)
            self._cached_project_docs = ""
        return self._cached_project_docs

    def _load_skills(self) -> str:
        """skills 目录里的行为说明（没给 skills_dir → 项目根 / skills）。

        **只注入显式点名的 skill**（auto_load_skills）。
        以前这里是 [self._auto_load_skills or None] —— 而 load_skills 的
        names=None 语义是「把目录下**全部** skill 都加载」。auto_load_skills
        为空（默认值）时也就跟着变成「全开」：skills/poem.md（任何问题都用
        七言绝句回答）被塞进 system prompt，把人设整个劫持了，模型还把
        max_tokens 预算全花在写诗上，主人看得见的回复被截成空。
        现在：空列表 ⇒ 一个都不注入；显式列出来的照旧注入。
        """
        try:
            from app.brain.skills import load_skills
            from app.brain.prompt_layer import app_root
            skills_dir = self._skills_dir or (app_root() / "skills")
            if not Path(skills_dir).is_dir():
                self._cached_skills = ""
            else:
                self._cached_skills = load_skills(
                    skills_dir, list(self._auto_load_skills),
                )
        except Exception as e:  # noqa: BLE001
            log.warning("Skills 加载失败,降级无 skills: %s", e)
            self._cached_skills = ""
        return self._cached_skills

    def _memory_signature(self) -> str:
        """记忆库指纹（**恒定**：不随召回内容变）。

        Stage 2.1 + 记忆通道搬家：记忆块不再进 system prompt ⇒ 记忆内容
        对 agent 不可见 ⇒ 指纹恒定 ⇒ agent 只构建一次（不因写记忆而重建）。
        """
        return "memory-in-volatile-channel"

    def _memory_block(self, query: str = "") -> str:
        """长期记忆块：本 agent 的 memory_store 优先；没有就回落会话默认。

        query: 本轮主人说的话（按这句话召回）。记忆块走**易变通道**
        （inject_dynamic_context → 独立 user 消息），不进 system prompt。
        """
        if self.memory_store is not None:
            try:
                if hasattr(self.memory_store, "system_block"):
                    return self.memory_store.system_block(query) or ""
                return ""
            except Exception as e:  # noqa: BLE001
                log.warning("memory_store.system_block 失败,跳过: %s", e)
                return ""
        from app.brain.prompt_layer import get_memory_block
        return get_memory_block(query)

    def _current_level(self) -> int:
        """读当前桌宠等级,无 level_getter 时按 Lv10 处理 (全部可见)。"""
        if self.level_getter is None:
            return 10
        try:
            lvl = int(self.level_getter())
        except Exception:  # noqa: BLE001
            return 10
        return max(1, min(10, lvl))

    def set_companion_state(self, pet_state: "Optional[Any]") -> None:
        """注入 / 解除桌宠 PetState。

        同步给 RiskyToolConfirm / PostLLMStateGuard 和 _companion_registry，
        pet_state=None 解除同步（主线程关闭 / 测试 cleanup）。
        """
        self._companion_state = pet_state
        # 同步给 RiskyToolConfirm（中间件查表路径）
        if getattr(self, "_confirm_ctrl", None) is not None:
            try:
                self._confirm_ctrl.set_companion_state(pet_state)
            except Exception as e:  # noqa: BLE001
                log.warning("LangChainAgent.set_companion_state 同步 _confirm_ctrl 失败: %s", e)
        # 同步给 PostLLMStateGuard
        if getattr(self, "_post_llm_guard", None) is not None:
            try:
                self._post_llm_guard.set_companion_state(pet_state)
            except Exception as e:  # noqa: BLE001
                log.warning("LangChainAgent.set_companion_state 同步 _post_llm_guard 失败: %s", e)
        try:
            from app.brain._companion_registry import (
                set_state_for_thread, clear_state_for_thread,
            )
            if pet_state is None:
                clear_state_for_thread(self.thread_id)
            else:
                set_state_for_thread(self.thread_id, pet_state)
        except Exception as e:  # noqa: BLE001
            log.warning("LangChainAgent.set_companion_state 写 registry 失败: %s", e)

    def _fire_outcome(self, outcome: str, tool_name: str = "",
                      risk: int = 0) -> None:
        """任务结果反向更新 PetState（如果注入了 state）。

        出错静默（PetState 缺失字段、数值越界等都不应炸 agent 主循环）。
        """
        state = getattr(self, "_companion_state", None)
        if state is None:
            return
        try:
            apply = getattr(state, "apply_outcome", None)
            if callable(apply):
                apply(outcome, tool_name, risk_level=risk)
            else:
                log.warning(
                    "LangChainAgent: state %r 没 apply_outcome 方法, 跳过",
                    type(state).__name__,
                )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "LangChainAgent: _fire_outcome(%s) 失败: %s", outcome, e,
            )

    def _finish_run_with_outcome(self, success: bool) -> None:
        """在 run() 结尾调，通知 PetState 一次 outcome（整体任务结果）。

        走完所有 yield 没异常 → success；走中抛异常 → failure。
        """
        outcome = "success" if success else "failure"
        try:
            self._fire_outcome(outcome)
        except Exception:  # noqa: BLE001
            pass

    # -------------------- Plan mode --------------------
    # /plan <request> 走这个方法。语义：
    #   - 跟 run() 用同一个 agent、同一个 thread（共享 checkpoint）
    #   - 但追加「只能输出 markdown 计划、不能调任何工具」指令
    #   - 不写 checkpoint 的 user/assistant（避免污染历史）
    # 事件协议跟 run() 一致：("text", chunk), ("done", final), ("error", ...)。
    #
    # 设计：直接在最后一条 user 上挂 plan-mode 提示，不另建 agent / 不用不同
    # system_prompt，避免 plan 上下文跟执行上下文分裂。
    # 计划输出走同 thread 的 checkpoint，用 config["metadata"] 标记 plan-mode。

    _PLAN_MODE_PROMPT = (
        "\n\n[系统:你现在处于 plan mode(规划模式)。请遵守:]\n"
        "1. 不要调用任何工具。只输出一份 markdown 格式的执行计划。\n"
        "2. 计划结构:\n"
        "   - 简要复述主人的诉求\n"
        "   - 编号列出每一步(标题 + 一句话说明预期产出)\n"
        "   - 标注每步会用到的工具 / 信息\n"
        "   - 标注潜在风险或主人需要决策的岔路口\n"
        "3. 不要执行、不要「为确认而预演」、不要「询问要不要这样做」。\n"
        "4. 计划末尾写明「是否需要主人确认?」—— 默认需要。\n"
    )

    async def plan(
        self,
        messages: list[dict],
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[tuple]:
        """以 plan mode 跑一次对话,只产出 markdown 计划,不调工具。

        复用 self._agent / self.thread_id / checkpointer。事件协议:
            ("text", chunk) / ("done", final) / ("error", msg)
        不会 yield ("tool", ...) / ("confirm_needed", ...)(理论上 plan 模式不调工具)。
        """
        await self._ensure_agent()
        cancel = cancel_check or self.cancel_check

        # 取历史 + 拼接 plan-mode 提示到 user 消息末尾
        history = self._to_lc_history(messages[:-1] if messages else [])
        last_user = ""
        if messages and messages[-1].get("role") == "user":
            raw = messages[-1]["content"]
            # multimodal（列表）时只往 text 部分追加，别破坏图片
            if isinstance(raw, list):
                new_parts = []
                for p in raw:
                    if isinstance(p, dict) and p.get("type") == "text":
                        new_parts.append({**p, "text": (p.get("text") or "") + self._PLAN_MODE_PROMPT})
                    else:
                        new_parts.append(p)
                last_user = new_parts
            else:
                last_user = (raw or "") + self._PLAN_MODE_PROMPT

        input_payload: dict[str, Any] = {
            "messages": [*history, HumanMessage(content=last_user)]
        }
        config = {
            "configurable": {"thread_id": self.thread_id},
            "metadata": {"plan_mode": "1"},
        }

        # plan 模式简化流程：不跑 checkpoint 救援，只 stream 文本
        final_text_parts: list[str] = []
        # 台账主键的一部分: **一次模型调用 = 一个 request_index**。
        # 一个回合里模型可能被调用多次 (模型调工具 -> 工具结果 -> 再调模型),
        # 每次都是一次独立的计费请求 —— 按 astream 分配会让后面那次调用
        # 按同一个主键覆盖前面那次, 前几次调用的 token 被静默丢掉。
        # 同一次模型调用里 usage 被 yield 多次时共用同一个 request_index
        # -> 台账按 key 折叠, 不会重复计数。
        usage_calls = _ModelCallUsageTracker()
        try:
            async for chunk, _meta in self._agent.astream(
                input_payload, config=config, stream_mode="messages",
            ):
                if cancel and cancel():
                    yield ("error", "cancelled")
                    return
                # v31: 抓 LLM API usage 字典 — AIMessageChunk 有 usage_metadata 字段
                # (LangChain 1.0+), 走 messages 模式时只在最后一个 chunk 带, 直接 yield
                # meta 事件 (跟 AgentLoop 路径一致, chat_window._on_meta 接收累加)
                if isinstance(chunk, (AIMessageChunk, AIMessage)):
                    usage_meta = getattr(chunk, "usage_metadata", None)
                    if usage_meta:
                        log.debug(
                            "langchain agent.run: chunk type=%s, usage_meta=%r",
                            type(chunk).__name__, usage_meta)
                        try:
                            # v31: 归一化 —— LangChain 的 usage_metadata 里缓存信息
                            # 在 input_token_details (cache_read / cache_creation),
                            # 跟 OpenAI 原始字段名不一样, 必须走同一个 normaliser。
                            usage = normalize_usage(usage_meta)
                            record_usage(usage, backend="standard",
                                         model=getattr(self.llm_cfg, "model", ""),
                                         session_id=getattr(self, "_session_id", "") or "",
                                         request_index=usage_calls.request_index(_meta))
                            yield build_usage_event(usage)
                        except Exception:
                            pass
                    text = chunk.content
                    if isinstance(text, list):
                        text = "".join(p.get("text", "") if isinstance(p, dict) else str(p)
                                       for p in text)
                    text = sanitize_text(text, is_final=False)
                    if text:
                        final_text_parts.append(text)
                        yield ("text", text)
                elif isinstance(chunk, ToolMessage):
                    # plan 模式理论上不该走到这里——万一模型还是调了工具，忽略
                    log.warning(
                        "plan_mode: 模型在 plan 阶段调了工具 %r —— 忽略(plan 模式不允许)",
                        chunk.name,
                    )
            yield ("done", "".join(final_text_parts))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("plan_mode 失败")
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
        # 台账主键的一部分: **一次模型调用 = 一个 request_index**。
        # 一个回合里模型可能被调用多次 (模型调工具 -> 工具结果 -> 再调模型),
        # 每次都是一次独立的计费请求 —— 按 astream 分配会让后面那次调用
        # 按同一个主键覆盖前面那次, 前几次调用的 token 被静默丢掉。
        # 同一次模型调用里 usage 被 yield 多次时共用同一个 request_index
        # -> 台账按 key 折叠, 不会重复计数。
        usage_calls = _ModelCallUsageTracker()
        try:
            async for chunk, _meta in self._agent.astream(input_payload, config=config,
                                                          stream_mode="messages"):
                if cancel and cancel():
                    yield ("error", "cancelled")
                    return
                if isinstance(chunk, (AIMessageChunk, AIMessage)):
                    # v31: 抓 LLM API usage — AIMessageChunk 末尾带 usage_metadata 字段
                    # (LangChain 1.0+ 在 stream_usage=True 时返)。chat_window._on_meta
                    # 累加到 self._accum_tokens, 状态栏精确显示。
                    usage_meta = getattr(chunk, "usage_metadata", None)
                    if usage_meta:
                        # v31: 归一化 + 落台账; 事件形状跟轻量后端完全一致
                        # (多了 cache_hit / cache_miss / cache_write 三个桶)。
                        usage = normalize_usage(usage_meta)
                        record_usage(usage, backend="standard",
                                     model=getattr(self.llm_cfg, "model", ""),
                                     session_id=getattr(self, "_session_id", "") or "",
                                     request_index=usage_calls.request_index(_meta))
                        yield build_usage_event(usage)
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

        当报 2013 (tool call result does not follow tool call) 时清掉再试。
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

    def plan_sync(self, messages: list[dict],
                  cancel_check: Optional[Callable[[], bool]] = None,
                  timeout: float = 300.0) -> list[tuple]:
        """同步版 plan():给 _PlanWorker (QThread) 调用 —— 在持久 loop 里跑 async plan(),
        返回所有 (text|done|error, ...) 事件。返回 list 而非 generator,
        跟 run_sync 保持一致,worker 直接 for 迭代即可。
        """
        async def collect() -> list[tuple]:
            out: list[tuple] = []
            async for ev in self.plan(messages, cancel_check=cancel_check):
                out.append(ev)
            return out
        return LangChainAgent._get_loop().run_coro(collect(), timeout=timeout)

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