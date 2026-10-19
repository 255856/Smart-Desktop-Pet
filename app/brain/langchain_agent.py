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


# 派生 session_id 前缀（chat_window 按人设派生："char:<人设名>:main"）。
# 这种 id 不登记在 SessionStore 里，直接当 thread_id 用；其余未知 id 照旧报错。
_DERIVED_SESSION_PREFIX = "char:"


# 内部 Tool → LangChain StructuredTool 的适配器
def _internal_tool_to_langchain(tool: Tool) -> StructuredTool:
    """把桌宠的 Tool(name, description, parameters, fn) 包装为 LangChain StructuredTool。

    StructuredTool.from_function 会从 fn 的 type hints + 默认值自动生成 args_schema。
    """
    return StructuredTool.from_function(
        func=tool.fn,
        name=tool.name,
        description=tool.description,
        parse_docstring=False,
    )


def convert_tools(registry: ToolRegistry) -> list[StructuredTool]:
    """批量转换 ToolRegistry → LangChain tool 列表。"""
    return [_internal_tool_to_langchain(t) for t in registry._tools.values()]


def convert_tools_for_level(registry: ToolRegistry, level: int) -> list[StructuredTool]:
    """按等级过滤后转换 ToolRegistry → LangChain tool 列表。"""
    return [_internal_tool_to_langchain(t) for t in registry.tools_for_level(level)]


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
        # 长期记忆（MemoryStore），跨 session 共享；system_prompt 注入时调 memory.system_block()
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
        self._cwd = cwd
        self._extra_docs_paths: list = list(extra_docs_paths or [])
        # 缓存最近一次加载的项目指令（避免每次 _ensure_agent 重读）
        self._cached_project_docs: str = ""
        self._skills_dir = skills_dir
        self._auto_load_skills: list = list(auto_load_skills or [])
        self._cached_skills: str = ""
        self._agent: Optional[Any] = None
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

    def _build_model(self) -> ChatOpenAI:
        """OpenAI 兼容协议（DeepSeek / minimax / Ollama / 任何 base_url）。"""
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
        """
        if self._agent is not None:
            return
        if self._model is None:
            self._model = self._build_model()
        tools = convert_tools_for_level(self.registry, self._current_level())
        if self._checkpointer is None and self.cfg.enable_checkpointer:
            await self._build_checkpointer()
        checkpointer = self._checkpointer

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

        # system_prompt = persona + 项目级指令（AGENTS.md / CLAUDE.md） + skills
        # dynamic_prompt middleware 会在这个基础上再加反幻觉 / 时间 / confirm 协议
        try:
            from app.brain.project_docs import load_project_docs
            self._cached_project_docs = load_project_docs(
                cwd=self._cwd, extra_paths=self._extra_docs_paths,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("ProjectDocs 加载失败,降级无项目指令: %s", e)
            self._cached_project_docs = ""
        try:
            from app.brain.skills import load_skills
            if self._skills_dir is not None:
                self._cached_skills = load_skills(
                    self._skills_dir, self._auto_load_skills or None,
                )
            else:
                self._cached_skills = ""
        except Exception as e:  # noqa: BLE001
            log.warning("Skills 加载失败,降级无 skills: %s", e)
            self._cached_skills = ""
        base_sp = self.persona or "你是一个有用的桌面助手。"
        if self._cached_project_docs:
            base_sp = base_sp + self._cached_project_docs
        if self._cached_skills:
            base_sp = base_sp + self._cached_skills
        # 长期记忆(跨对话 / 跨 session 共享)—— 拼到 system_prompt
        try:
            if self.memory_store is not None and hasattr(self.memory_store, "system_block"):
                mem_block = self.memory_store.system_block()
                if mem_block:
                    base_sp = base_sp + "\n\n" + mem_block
        except Exception as e:  # noqa: BLE001
            log.warning("MemoryStore.system_block 失败,跳过: %s", e)

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
            if tool_name == "aw_get_recent_activities":
                content = get_recent_activities_summary(
                    minutes=int(args.get("minutes", 30)),
                    limit=int(args.get("limit", 15)),
                )
            elif tool_name == "aw_get_current_window":
                content = get_current_window_one_liner()
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

        history = self._to_lc_history(messages[:-1] if messages else [])
        last_user = ""
        if messages and messages[-1].get("role") == "user":
            last_user = messages[-1]["content"]

        # 把注入的工具序列插在 user 消息前面，模拟 LLM 之前调过工具
        lc_msgs: list = [*history]
        lc_msgs.extend(injected_tool_msgs)
        lc_msgs.append(HumanMessage(content=last_user))

        input_payload: dict[str, Any] = {"messages": lc_msgs}
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
                            yield ("meta", {"event": "usage",
                                            "prompt_tokens": int(usage_meta.get("input_tokens", 0) or 0),
                                            "completion_tokens": int(usage_meta.get("output_tokens", 0) or 0),
                                            "total_tokens": int(usage_meta.get("total_tokens", 0) or 0),
                                            "got_real": True})
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
                        yield ("meta", {"event": "usage",
                                        "prompt_tokens": int(usage_meta.get("input_tokens", 0) or 0),
                                        "completion_tokens": int(usage_meta.get("output_tokens", 0) or 0),
                                        "total_tokens": int(usage_meta.get("total_tokens", 0) or 0),
                                        "got_real": True})
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