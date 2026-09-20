"""Sub-agent 多智能体框架。

设计：
    - BaseAgent：抽象基类（system_prompt + 工具子集 + 专属 LLMClient）
    - LifeAgent：默认桌宠 Agent（关心主人 + 桌宠自身状态 + 提醒 + 记忆）
    - ResearchAgent：检索 / 问答 / 计算
    - CodeAgent：写代码 / 运行代码（隔离环境）
    - Orchestrator：根据用户输入分派到合适 agent；agent 可调其他 agent（白名单）

启动方式（在 main.py 注入）：
    orchestrator = Orchestrator(llm_client, ...)
    orchestrator.register(LifeAgent(...))
    orchestrator.register(ResearchAgent(...))
    orchestrator.register(CodeAgent(...))
    reply = await orchestrator.dispatch(user_msg, history)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

from app.brain.llm_client import ChatMessage, LLMClient
from app.engine.tools import Tool, ToolRegistry

log = logging.getLogger(__name__)


@dataclass
class AgentContext:
    """调用 sub-agent 时携带的上下文。"""
    user_message: str
    history: list[dict] = field(default_factory=list)
    parent_agent: Optional[str] = None     # 调用方 agent 名（用于循环防护）
    call_depth: int = 0                    # 调用深度


@dataclass
class AgentResult:
    """Sub-agent 调用的返回。"""
    text: str
    agent_name: str
    tool_calls: int = 0
    success: bool = True
    error: str = ""


class BaseAgent:
    """所有 sub-agent 的基类。"""

    name: str = "base"
    description: str = ""
    # 哪些 agent 可以调用本 agent（名字列表）
    allowed_callers: list[str] = []

    def __init__(self, llm_client: LLMClient, tool_registry: ToolRegistry,
                 allowed_tools: list[str] | None = None,
                 persona_suffix: str = ""):
        """
        Args:
            llm_client: 共享 LLM 客户端（每次调用前改 system_prompt）
            tool_registry: 共享工具注册表（agent 只用自己白名单的子集）
            allowed_tools: 本 agent 允许的工具名（None = 用类属性 / 全部）
            persona_suffix: 追加到 system prompt 的专家人设
        """
        self.client = llm_client
        self.registry = tool_registry
        if allowed_tools is not None:
            self.allowed_tools = allowed_tools
        else:
            # 沿用子类类属性（如 LifeAgent.allowed_tools），都没有就 None = 全部
            cls_attr = getattr(type(self), "allowed_tools", None)
            self.allowed_tools = list(cls_attr) if cls_attr else None
        self.persona_suffix = persona_suffix

    def _tools_for_agent(self) -> list[dict]:
        """本 agent 可用的 OpenAI tools 字段。"""
        all_tools = self.registry.to_openai() if self.registry else []
        if self.allowed_tools is None:
            return all_tools
        allow = set(self.allowed_tools)
        return [t for t in all_tools
                if t["function"]["name"] in allow]

    def system_prompt(self, ctx: AgentContext) -> str:
        """构造本 agent 的 system prompt。"""
        return self.persona_suffix

    async def run(self, ctx: AgentContext) -> AgentResult:
        """执行一次 sub-agent 调用。

        默认实现：直接 LLM chat + 工具调用（最多 4 轮）。
        子类可重写以定制行为（如 CodeAgent 可能执行代码）。
        """
        messages = list(ctx.history) + [
            {"role": "user", "content": ctx.user_message},
        ]
        old_system = self.client.system_prompt
        self.client.system_prompt = self.system_prompt(ctx)
        try:
            tool_calls_count = 0
            full_text = ""
            tools = self._tools_for_agent() or None
            for _ in range(4):
                tool_calls: list[dict] = []
                # 优先用 chat_stream_events（支持工具调用增量），否则降级到 chat_once
                if hasattr(self.client, "chat_stream_events"):
                    parts: list[str] = []
                    async for ev, data in self.client.chat_stream_events(
                            messages, tools=tools):
                        if ev == "text":
                            parts.append(data)
                        elif ev == "finish":
                            tool_calls = data.get("tool_calls") or []
                    full_text = "".join(parts)
                else:
                    full_text = await self.client.chat_once([
                        ChatMessage(role=m["role"], content=m.get("content") or "")
                        for m in messages
                    ])

                if not tool_calls:
                    break

                # 追加 assistant 消息
                messages.append({
                    "role": "assistant", "content": full_text or "",
                    "tool_calls": [
                        {"id": tc["id"] or f"c{i}",
                         "type": "function",
                         "function": {"name": tc["name"],
                                      "arguments": tc["arguments"] or "{}"}}
                        for i, tc in enumerate(tool_calls)
                    ],
                })
                # 并行执行
                async def run_one(tc):
                    name = tc["name"]
                    args = tc["arguments"] or "{}"
                    result = await asyncio.to_thread(
                        self.registry.execute, name, args)
                    return tc, result

                results = await asyncio.gather(
                    *[run_one(tc) for tc in tool_calls])
                for tc, result in results:
                    tool_calls_count += 1
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"] or "c",
                        "content": (result or "")[:4000],
                    })

            # 不在此处做中文角色化清洗：SubAgent 是多专家框架（CodeAgent 可能输出代码 /
            # 英文路径），且主显示出口 chat_window._on_done 会统一 sanitize；
            # delegate_to_agent 的工具结果回给主 agent 后也会再经最终清洗。
            return AgentResult(text=full_text, agent_name=self.name,
                               tool_calls=tool_calls_count,
                               success=True)
        except Exception as e:  # noqa: BLE001
            log.exception("Sub-agent %s 出错", self.name)
            return AgentResult(text="", agent_name=self.name,
                               success=False, error=str(e))
        finally:
            self.client.system_prompt = old_system


# ---------------------------------------------------------------------------
#  内置专家 agents
# ---------------------------------------------------------------------------


class LifeAgent(BaseAgent):
    """生活 / 情感 Agent：聊天、提醒、记忆、桌宠状态。"""
    name = "life"
    description = "日常生活陪伴：聊天、设置提醒、记忆主人偏好、播报桌宠状态。"
    allowed_tools = [
        "add_reminder", "list_reminders", "delete_reminder",
        "remember_fact", "recall_memory", "forget_memory",
        "get_pet_status", "get_current_time",
        "say_to_user", "play_animation", "change_pet_emotion",
    ]

    def system_prompt(self, ctx: AgentContext) -> str:
        return (
            "你是「生活助手」子智能体，专注日常陪伴与情感交流。\n"
            "擅长：设置提醒、记忆主人偏好、关心主人状态、自然聊天。\n"
            "限制：不要执行代码、不要做联网搜索（请转交给其他专家）。\n"
            "回复简洁、温暖，最后保留一个 [emotion] 标签。\n"
            + (self.persona_suffix or "")
        )


class ResearchAgent(BaseAgent):
    """研究 / 检索 Agent：通用问答、计算、查时间。"""
    name = "research"
    description = "知识问答、数学计算、单位换算、时间查询。"

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allowed_tools", [
            "get_current_time", "date_info", "system_info",
            "calculate", "convert_units", "recall_memory",
        ])
        super().__init__(*args, **kwargs)

    def system_prompt(self, ctx: AgentContext) -> str:
        return (
            "你是「研究助手」子智能体，专注知识问答与计算。\n"
            "擅长：数学计算、单位换算、时间日期、知识问答（结合记忆）。\n"
            "限制：不要执行代码、不要操作本机程序（转交其他专家）。\n"
            "回答要精确、有依据，必要时列步骤。\n"
            + (self.persona_suffix or "")
        )


class CodeAgent(BaseAgent):
    """代码 Agent：读写文件 / 查进程 / 系统操作。"""
    name = "code"
    description = "代码 / 文件 / 系统操作 Agent。"

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("allowed_tools", [
            "list_desktop_files", "read_text_file",
            "open_file_explorer", "open_terminal", "open_notepad",
            "open_task_manager", "system_info",
        ])
        super().__init__(*args, **kwargs)

    def system_prompt(self, ctx: AgentContext) -> str:
        return (
            "你是「代码助手」子智能体，专注代码与文件操作。\n"
            "擅长：读写文件、打开 IDE / 终端、查进程与系统信息。\n"
            "限制：写文件前要谨慎，能用 read 就不要 write。\n"
            + (self.persona_suffix or "")
        )


# ---------------------------------------------------------------------------
#  Orchestrator
# ---------------------------------------------------------------------------


@dataclass
class OrchestratorConfig:
    """编排器配置。"""
    enable_dispatch: bool = True        # False = 所有消息都走 life
    max_call_depth: int = 2             # 防止 agent 互相调用无限递归


# 分派关键词（用于 rule-based 分派，LLM-based 也可）
_DISPATCH_RULES = {
    "life": [
        "提醒", "记一下", "记住", "忘了", "寂寞", "聊天", "心情",
        "状态", "健康", "饿", "渴", "困", "累", "想", "陪我",
    ],
    "research": [
        "计算", "等于", "多少", "怎么", "为什么", "什么是", "解释",
        "单位", "换算", "农历", "今天", "星期", "日期",
    ],
    "code": [
        "代码", "文件", "桌面", "进程", "内存", "cpu", "打开",
        "记事本", "终端", "资源管理器", "task", "ide", "vscode",
    ],
}


class Orchestrator:
    """根据用户消息分派到合适的 sub-agent。"""

    def __init__(self, llm_client: LLMClient,
                 config: Optional[OrchestratorConfig] = None):
        self.client = llm_client
        self.config = config or OrchestratorConfig()
        self._agents: dict[str, BaseAgent] = {}

    def register(self, agent: BaseAgent) -> None:
        self._agents[agent.name] = agent
        log.info("Orchestrator 注册 agent: %s (tools=%s)",
                 agent.name, agent.allowed_tools or "all")

    def names(self) -> list[str]:
        return list(self._agents.keys())

    def get(self, name: str) -> Optional[BaseAgent]:
        return self._agents.get(name)

    def dispatch_by_rule(self, user_message: str) -> str:
        """基于关键词的简单分派（默认 life）。"""
        if not self.config.enable_dispatch or not self._agents:
            return "life"
        text = user_message.lower()
        scores: dict[str, int] = {n: 0 for n in self._agents}
        for agent_name, keywords in _DISPATCH_RULES.items():
            if agent_name not in scores:
                continue
            for kw in keywords:
                if kw.lower() in text:
                    scores[agent_name] += 1
        # 取最高分
        best = max(scores.items(), key=lambda x: x[1])
        if best[1] == 0 or best[0] not in self._agents:
            return "life"
        return best[0]

    async def dispatch_by_llm(self, user_message: str,
                              history: list[dict] | None = None) -> str:
        """让 LLM 选 agent（更准，慢一点）。"""
        if not self._agents:
            return "life"
        agent_desc = "\n".join(
            f"- {n}: {a.description}" for n, a in self._agents.items())
        prompt = (
            "根据主人的消息，选一个最合适的子智能体处理。\n"
            "只输出 agent 名字（" + " | ".join(self._agents.keys()) + "），"
            "不要其他文字。\n\n"
            f"【可用 agent】\n{agent_desc}\n\n"
            f"【主人的消息】{user_message}"
        )
        try:
            old = self.client.system_prompt
            self.client.system_prompt = "你是路由器，只输出一个 agent 名。"
            text = await self.client.chat_once(
                [ChatMessage(role="user", content=prompt)])
            self.client.system_prompt = old
        except Exception:
            return "life"
        # 匹配
        for name in self._agents:
            if name in text:
                return name
        return "life"

    async def dispatch(self, user_message: str, history: list[dict] | None = None,
                       prefer: str = "auto") -> AgentResult:
        """分派并执行。prefer: "auto" | "rule" | agent_name。"""
        history = history or []
        if prefer == "auto":
            agent_name = await self.dispatch_by_llm(user_message, history)
        elif prefer == "rule":
            agent_name = self.dispatch_by_rule(user_message)
        else:
            agent_name = prefer
        agent = self._agents.get(agent_name) or self._agents.get("life")
        if agent is None:
            return AgentResult(text="（无可用 agent）", agent_name="?",
                               success=False)
        ctx = AgentContext(user_message=user_message, history=history)
        log.info("Orchestrator → %s (msg=%r)", agent.name,
                 user_message[:60])
        return await agent.run(ctx)


# ---------------------------------------------------------------------------
#  暴露为工具：让主桌宠 Agent 把任务分给 sub-agents
# ---------------------------------------------------------------------------


def make_dispatch_tool(orchestrator: Orchestrator) -> Tool:
    """让桌宠主 Agent 可以调 `delegate_to_agent` 把任务分给专家。"""

    def delegate_to_agent(agent_name: str, task: str) -> str:
        agent = orchestrator.get(agent_name)
        if agent is None:
            return f"错误：未知 agent '{agent_name}'，可用：{orchestrator.names()}"
        # 同步 wrapper：起一个新 loop 跑
        async def drive():
            ctx = AgentContext(user_message=task)
            return await agent.run(ctx)
        try:
            loop = asyncio.new_event_loop()
            try:
                result = loop.run_until_complete(drive())
            finally:
                loop.close()
        except Exception as e:  # noqa: BLE001
            return f"错误：分派失败：{e}"
        return (result.text or "（无输出）")[:3000]

    return Tool(
        name="delegate_to_agent",
        description=(
            "把任务分派给一个专家子智能体。可选："
            + "、".join(orchestrator.names())
            + "。例如问计算 / 查日期用 research，问代码 / 文件用 code，"
              "日常陪伴 / 提醒 / 记忆用 life。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent_name": {"type": "string", "enum": orchestrator.names()},
                "task": {"type": "string", "description": "任务描述"},
            },
            "required": ["agent_name", "task"],
        },
        fn=delegate_to_agent,
    )
