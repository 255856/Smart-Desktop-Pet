"""桌宠工具系统（Function Calling）。"""
from __future__ import annotations

from typing import Callable, Optional

from ._core import Tool, ToolRegistry
from . import _time, _reminder, _memory, _pet, _system, _math, _file, _shortcuts, _search


def build_default_tools(
    *,
    state,
    reminders,
    memory,
    items: Optional[object] = None,
    hooks: Optional[dict[str, Callable[[str], None]]] = None,
    tavily_api_key: Optional[str] = None,
) -> ToolRegistry:
    """用各子系统组装默认工具集。

    hooks: 可选回调，key 取 "bubble"（桌宠冒泡）/"animation"（播动画），
           由主程序提供（内部用 Qt 信号转回 UI 线程）。
    tavily_api_key: 若非空，web_search 主用 Tavily；否则降级 DuckDuckGo
                    (建议与 llm.api_key 解耦，但优先用 TAVILY_API_KEY 环境变量)
    """
    hooks = hooks or {}
    reg = ToolRegistry()

    # 每个模块暴露 register(reg, ...)，负责把自己组的所有 Tool 注册进去
    _time.register(reg, state=state)
    _reminder.register(reg, reminders=reminders)
    _memory.register(reg, memory=memory)
    _pet.register(reg, state=state, items=items, hooks=hooks)
    _system.register(reg, hooks=hooks)
    _math.register(reg)
    _file.register(reg)
    _shortcuts.register(reg)
    _search.register(reg, tavily_api_key=tavily_api_key)

    return reg


__all__ = ["Tool", "ToolRegistry", "build_default_tools"]