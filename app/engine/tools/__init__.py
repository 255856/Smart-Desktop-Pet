"""桌宠工具系统（Function Calling）。

让大模型不只是「说」，还能「做」。所有工具按领域拆分到独立模块：

    _core        Tool / ToolRegistry 核心类型
    _time        get_current_time / get_pet_status / 绝对时间解析
    _reminder    add_reminder / list_reminders / delete_reminder
    _memory      remember_fact / recall_memory / forget_memory
    _pet         feed_self / play_animation / change_pet_emotion / say_to_user
    _system      open_website / open_app / system_info / screenshot / clipboard / notification
    _math        calculate / convert_units / date_info
    _file        list_desktop_files / read_text_file
    _shortcuts   taskmgr / control / settings / explorer / terminal / notepad / calculator
    _search      web_search（主用 Tavily / 降级 DuckDuckGo）

主程序只需调用 build_default_tools(state, reminders, memory, items, hooks) 即可获得
一个完整 ToolRegistry。

fn 签名：fn(**kwargs) -> str（返回给模型的工具结果，尽量是简洁中文/JSON）。
所有 fn 都在聊天 worker 线程同步执行，必须快速返回，不要在里面开 GUI。
"""
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