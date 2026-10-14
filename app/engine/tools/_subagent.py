"""Task 工具 —— 让主 agent 派生子 agent 执行隔离的子任务。

设计:
  - 这是一个普通的 Tool,模型可以直接调用,跟 lock_screen / send_email 同级别。
  - 真正派生子 agent 的逻辑由「subagent factory」提供,工厂由 LangChainAgent
    在 __init__ 里装上(set_subagent_factory)。
  - 模块级单例状态:同时只有一个 factory 生效(桌宠实际只有一个 agent 活着)。
    跟 _timer.set_say_hook 一样的套路。

为什么不把 factory 写死在工具里?
  - 工具模块不应该反向依赖 LangChainAgent(会循环 import)
  - 测试要换 factory 时不用碰工具实现

工作流(主 agent 视角):
  1) 主 agent 觉得「这块太碎,扔给子 agent」,调 Task(task="查找 Y")
  2) 工具把 task 传给 factory,factory 起一个新 LangChainAgent(独立 thread_id),
     跑一次完整对话,收集所有 text,返回拼好的字符串
  3) 工具把字符串当 ToolMessage 返回给主 agent
  4) 主 agent 继续它的下一步决策

子 agent 默认约束:
  - 用同一份 llm_cfg(同模型)
  - 用同一份 persona(同角色设定)
  - 但有自己的 thread_id(独立 checkpoint,不污染主对话)
  - 默认禁用高风险工具(子 agent 跑锁屏/杀进程必须弹两次确认,体验差)
  - 默认带搜索类工具(grep_files / glob_files / search_files)

事件协议:factory 必须返回字符串。如果想流式回报子 agent 进度,主程序
可以订阅 _TaskState.on_subagent_event 回调(目前未实现,留口子)。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from ._core import ToolRegistry
from ._decorator import tool

log = logging.getLogger(__name__)


# Factory 签名:接收 (task_str, kwargs),返回最终结果字符串。
#   kwargs: {"agent_type": str} 等
#   返回:str —— 子 agent 的最终回复;若失败,返回 "错误: ..." 风格的纯文本
SubAgentFactory = Callable[[str, dict], str]


class _TaskState:
    """模块级 Task 工具状态(单例)。"""
    factory: Optional[SubAgentFactory] = None


def set_subagent_factory(factory: Optional[SubAgentFactory]) -> None:
    """主 agent 在 __init__ 里调用,装上自己的 factory。"""
    _TaskState.factory = factory


def get_subagent_factory() -> Optional[SubAgentFactory]:
    return _TaskState.factory


def register(reg: ToolRegistry) -> None:
    """注册 Task 工具到 ToolRegistry。"""

    @tool(
        name="Task",
        description="派生子 agent 跑一个隔离的小任务,返回最终文字结果。"
                    "子 agent 拥有独立的上下文,不会污染主对话。"
                    "适合『帮我搜一下 X 然后总结』『这段代码什么意思』这类"
                    "不想开新 thread 也不值得主 agent 一直 context 的事。",
        parameters={"type": "object",
                    "properties": {
                        "task": {"type": "string",
                                 "description": "交给子 agent 完成的任务描述"},
                        "agent_type": {"type": "string",
                                       "description": "子 agent 类型(general / explore / ...)"
                                                     "目前只是语义标签,实际行为相同,留口子给后续细分"},
                    },
                    "required": ["task"]},
    )
    def task(task: str, agent_type: str = "general") -> str:
        """派生子 agent 执行 task,返回结果字符串。"""
        if not task or not task.strip():
            return "错误：task 不能为空"
        f = _TaskState.factory
        if f is None:
            return ("错误：Task 工具未配置 subagent factory。"
                    "主程序没启用 subagent(单 agent 模式)。")
        try:
            return f(task, {"agent_type": agent_type})
        except Exception as e:  # noqa: BLE001
            log.exception("Task 工具调用失败")
            return f"错误：sub-agent 抛异常: {e}"

    reg.register(task)


__all__ = [
    "set_subagent_factory",
    "get_subagent_factory",
    "SubAgentFactory",
]