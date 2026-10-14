"""LangChain 标准后端的抗幻觉 middleware（对齐参考项目 graphs.py 的动态 prompt 模式）。

设计原则：
  - 不复用手写 ReAct 的反幻觉 3 层（那是产品的核心）；而是给标准后端加一个
    「基线」：每次模型调用前注入「不知道就说不知道」「工具失败就承认失败」
    等硬规则，降低标准后端编造的倾向。
  - 风险工具提醒：对 lock_screen / kill_process / set_wifi / set_bluetooth
    / send_notification / run_script 等高风险工具，在执行前打醒（目前
    是 log.warning，后续接 HumanInTheLoopMiddleware 可升级为真中断）。

参考：
  E:\\Python study\\agent\\app\\graphs.py:212-247
    @dynamic_prompt
    def dynamic_prompt_func(request):
        if request.state.get("authenticated"):
            return "..."
        return "..."
"""
from __future__ import annotations

import datetime
import logging
from typing import Any

log = logging.getLogger(__name__)


# 高风险工具：执行前必须由用户在场确认（目前只 log.warning，真正的人机协同
# 留作后续 HumanInTheLoopMiddleware 接入）
_RISKY_TOOLS = frozenset({
    "lock_screen", "kill_process", "set_wifi", "set_bluetooth",
    "send_notification", "run_script", "set_volume", "set_brightness",
    "send_email",   # 参考项目示例：邮件类也需 HITL
})


_ANTI_HALLUCINATION_SUFFIX = """

【防幻觉基线】（不要破例外）
- 没看到的图片不要描述；没收到的数据不要编造。
- 不知道主人最近做了什么就说不知道，不要猜。
- 工具返回失败、报错、错误的，**必须如实告知主人失败**，不要装作成功。
- 调用工具前，先看清实参。调用后，根据返回值告诉主人结果。
- 如果已记住主人说过某事，可以引用；如果仅仅是「可能」，说「我不确定」。
"""


def grounding_dynamic_prompt(request) -> str:
    """参考项目 graphs.py 的 dynamic_prompt_func：根据状态调整 system_prompt。

    这里在原 persona 后面拼接一段防幻觉基线 + 当前时间。
    主要用途：
      - 强制模型在每次调用里都看到防幻觉规则（不仅仅在开始一次性 system_prompt 里看）
      - 在调用中提供「现在是什么时间」让模型错不了时间类问题
    """
    base = getattr(request, "system_prompt", None) or ""
    if not base:
        base = "你是一个有用的桌面助手。"
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    weekday = "一二三四五六日"[datetime.datetime.now().weekday()]
    return base + _ANTI_HALLUCINATION_SUFFIX + f"\n\n[当前时间:{now} 星期{weekday}]"


def _log_risky_tool_call(tool_name: str, args: Any) -> None:
    """高风险工具执行前打醒。后续接入 HumanInTheLoopMiddleware 可走真中断。"""
    log.warning(
        "HallucinationGuard: 危险工具被调用 %r args=%r —— 默认推跟主程序需要在上下文里看到明确用户请求才能执行",
        tool_name, args,
    )


def build_hallucination_middlewares():
    """构造一组 middleware 供 LangChainAgent._ensure_agent 接入。

    Returns:
        list: 可以传给 create_agent(middleware=...) 的 middleware 列表。
    """
    from langchain.agents.middleware import dynamic_prompt, wrap_tool_call

    # 1) dynamic_prompt: 每次调用都重新生成 system_prompt,加上防幻觉基线 + 当前时间
    grounding = dynamic_prompt(grounding_dynamic_prompt)

    # 2) wrap_tool_call: 高风险工具执行前打 WARNING 日志
    @wrap_tool_call
    async def risky_tool_logger(request, handler):
        name = getattr(request, "tool_call", None)
        tool_name = getattr(name, "name", None) if name else None
        args = getattr(name, "args", None) if name else None
        if tool_name in _RISKY_TOOLS:
            _log_risky_tool_call(tool_name, args)
        return await handler(request)

    return [grounding, risky_tool_logger]


__all__ = ["build_hallucination_middlewares", "_RISKY_TOOLS"]