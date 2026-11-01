# -*- coding: utf-8 -*-
"""**整轮工具预算**（秒）—— 用尽后不再执行新工具，让模型用已有信息收尾。

为什么需要
----------
§app/engine/tools/_core.py§ 里有 §TOOL_PHASE_BUDGET_SECONDS§（120 秒），
但**只有 ReAct 路径真的用它**：§app/brain/agent.py§ 在整轮工具预算用尽时停止追加新工具。
**standard（LangChain）后端的工具是 LangGraph 的 ToolNode 内部调用的**，
我们插不进去 → 那条路上预算形同虚设（一轮工具调用可以无限拖下去）。

这里用 **middleware** 把闸补上：§create_agent§ 已经有 5 个 middleware
（含一个 confirm 拦截器），我们再加一个 §wrap_tool_call§ 闸 ——
预算用尽时**不再执行新工具**，返回一条明确的 ToolMessage，
模型看到后会用已有信息回答主人（而不是继续无限调工具）。

这条路径上工具的**单次**超时与**统一裁剪**已经由
§app/engine/tools/_core.py::wrap_tool_fn§ 负责（2026-10-31）。
"""
from __future__ import annotations

import logging
import time

from app.engine.tools._core import (
    TOOL_PHASE_BUDGET_SECONDS,
    TOOL_RESULT_MAX_CHARS,
)

log = logging.getLogger(__name__)


def _fit_one(text: str, room: int) -> str:
    """把一条工具结果裁到 room 个字符（保头保尾 + 省略标记）。"""
    if room <= 0:
        return ""
    if len(text) <= room:
        return text
    head = max(1, int(room * 0.9))
    tail = max(0, room - head - 30)
    omitted = len(text) - head - tail
    marker = "\n…（中间省略 %d 字）…\n" % omitted
    return text[:head] + marker + (text[-tail:] if tail else "")

class ToolPhaseBudget:
    """整轮工具预算（墙钟秒）。

    - §reset()§：每轮开始时调用（预算按轮算，不按进程算）。
    - §exhausted§：预算用尽后为 True（可观测）。
    - §blocked§：被挡下的工具调用次数。
    """

    def __init__(self, budget_seconds: float = TOOL_PHASE_BUDGET_SECONDS,
                 max_chars: int = TOOL_RESULT_MAX_CHARS) -> None:
        self.budget_seconds = float(budget_seconds or 0)
        # 整轮工具结果的**字符预算**（跟 ReAct 的 fit_tool_payload 同一口径）：
        # standard 后端以前只有单工具裁剪，整轮总量没人管 → 一轮能塞进
        # 几十万字符，把模型上下文顶爆。
        self.max_chars = int(max_chars or 0)
        self.started = time.monotonic()
        self.exhausted = False
        self.blocked = 0
        self.chars_used = 0
        self.chars_trimmed = 0

    @staticmethod
    def _fmt_seconds(v: float) -> str:
        """秒数显示：小于 10 秒要带小数（"0 秒" 看不出是超时还是没设）。"""
        try:
            v = float(v)
        except (TypeError, ValueError):
            return "?"
        return ("%.1f" % v) if v < 10 else ("%.0f" % v)

    def reset(self) -> None:
        """新一轮开始：预算重新计时（字符预算也归零）。"""
        self.started = time.monotonic()
        self.exhausted = False
        self.blocked = 0
        self.chars_used = 0
        self.chars_trimmed = 0

    @property
    def elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.started)

    def remaining(self) -> float:
        if self.budget_seconds <= 0:
            return float("inf")
        return max(0.0, self.budget_seconds - self.elapsed)

    def as_middleware(self):
        """返回 LangChain §wrap_tool_call§ 兼容的中间件实例。"""
        from langchain.agents.middleware import wrap_tool_call
        from langchain_core.messages import ToolMessage

        outer = self

        @wrap_tool_call
        async def _budget_gate(request, handler):
            tc = getattr(request, "tool_call", None) or {}
            tool_name = tc.get("name") if isinstance(tc, dict) else ""
            tool_call_id = tc.get("id") if isinstance(tc, dict) else ""
            if outer.budget_seconds <= 0:
                return await handler(request)          # 没设预算 = 不拦
            if outer.exhausted or outer.remaining() <= 0:
                outer.exhausted = True
                outer.blocked += 1
                log.info("整轮工具预算已用尽（%.0f 秒），挡住 %s",
                         outer.budget_seconds, tool_name)
                return ToolMessage(
                    content=(
                        "失败：整轮工具预算（%s 秒）已用尽，不再执行新工具；"
                        "请**用已有的信息**回答主人，不要再调工具。"
                        % outer._fmt_seconds(outer.budget_seconds)
                    ),
                    tool_call_id=tool_call_id or "",
                    name=tool_name or "",
                )
            result = await handler(request)
            # 整轮工具结果的**字符预算**（跟 ReAct 的 fit_tool_payload 同一口径）：
            # standard 后端以前只有单工具裁剪，整轮总量没人管 —— 一轮能塞进
            # 几十万字符，把模型上下文顶爆。
            content = getattr(result, "content", None)
            if outer.max_chars > 0 and isinstance(content, str):
                room = outer.max_chars - outer.chars_used
                if room <= 0:
                    outer.chars_trimmed += 1
                    return ToolMessage(
                        content=("（整轮工具结果已超字符预算（%d 字），"
                                 "这条不再展开；请用已有信息回答。）"
                                 % outer.max_chars),
                        tool_call_id=tool_call_id or "",
                        name=tool_name or "",
                    )
                if len(content) > room:
                    outer.chars_trimmed += 1
                    content = _fit_one(content, room)
                    try:
                        result.content = content
                    except Exception:  # noqa: BLE001
                        pass
                outer.chars_used += len(content)
            return result

        return _budget_gate


__all__ = ["ToolPhaseBudget"]
