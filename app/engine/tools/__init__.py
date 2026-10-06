"""桌宠工具系统（Function Calling）。"""
from __future__ import annotations

from typing import Callable, Optional

from ._core import Tool, ToolRegistry, is_safe_url
from . import (
    _time, _reminder, _memory, _pet, _system, _math, _file, _shortcuts, _search,
    _audio, _weather, _timer, _power, _filesearch, _webfetch, _ocr, _runner,
    _tts_mute,
)


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
    _audio.register(reg)
    _weather.register(reg)
    _timer.register(reg)
    _power.register(reg)
    _filesearch.register(reg)
    _webfetch.register(reg)
    _ocr.register(reg)
    _runner.register(reg)
    _tts_mute.register(reg)

    # ---- 接线：把 UI hook 注入到用「全局 setter」而非参数传 hook 的模块 ----
    # 原先 _timer.set_say_hook / _power.set_bubble_clear_hook 全仓只有 tests
    # 调用过，主程序从不注入，导致：
    #   - countdown 到点静默无声（工具却说「到点主人会收到提醒」）
    #   - clear_bubble 永久空转
    #
    # 注意：这里**只接 bubble**（Qt 信号，跨线程 emit 是安全的——Qt 会把槽调用
    # 排队到接收者所在线程）。不要在这里接任何「到点后再触发」的自定义回调：
    # 那种回调会在后台线程里 emit 一个可能已析构的 QObject，实测直接把进程
    # 打成 access violation。「没有提醒通道」这件事由 countdown() 在调用时
    # 同步判断（见 _timer._has_notify_channel）。
    _bubble = hooks.get("bubble")
    if callable(_bubble):
        _timer.set_say_hook(lambda text: _bubble(text))
    # 倒计时到点专用：走闹钟通道而不是普通气泡，UI 层会弹置顶窗 + 响铃 + 播报
    _alarm = hooks.get("alarm")
    if callable(_alarm):
        _timer.set_alarm_hook(_alarm)
    _hide = hooks.get("hide_bubble")
    if callable(_hide):
        _power.set_bubble_clear_hook(_hide)

    return reg


__all__ = ["Tool", "ToolRegistry", "build_default_tools", "is_safe_url"]