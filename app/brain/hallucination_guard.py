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


def _prune_orphan_tool_messages(messages):
    """清掉孤立的 ToolMessage(其 tool_call_id 在前面的 AIMessage.tool_calls 里找不到)。

    2026-10-14: 实测修复 ——MiniMax API 报 error 2013:
      'invalid params, tool call result does not follow tool call'
    原因:checkpointer 把上一会话的 ToolMessage/History 持久化了,但对应的
    AIMessage.tool_call_id 在新 session 已经被剥离(LangChain schema 变化、
    模型切换、或 astream 中途被打断)。这种孤立的 ToolMessage 仍然保留在
    checkpointer 里,下次 astream 把它和新 HumanMessage 一起发到模型,
    模型看到「tool result 找不到对应的 tool call」就 400 了。

    修法:每次 before_model 走一遍 messages,丢掉任何「前面没有匹配
    AIMessage.tool_calls 的 ToolMessage」,让模型只看到完整的 tool pair。

    Returns:
        list: 过滤后的 messages(原 list 不变)。
    """
    kept = []
    # 累计到目前为止所有「合法的 tool_call_id」
    valid_ids: set[str] = set()
    for m in messages:
        # 收集 AIMessage 提供的 tool_call_id
        if hasattr(m, "tool_calls"):
            for tc in (m.tool_calls or []):
                tc_id = (tc.get("id") if isinstance(tc, dict) else None) or getattr(tc, "id", None)
                if tc_id:
                    valid_ids.add(tc_id)
        # 过滤 ToolMessage
        if type(m).__name__ == "ToolMessage":
            tcid = getattr(m, "tool_call_id", None)
            if tcid and tcid in valid_ids:
                kept.append(m)
            else:
                log.warning(
                    "HallucinationGuard: 丢弃孤立 ToolMessage tool_call_id=%s "
                    "(前面没有匹配的 AIMessage.tool_call,推测 checkpoint 残留)",
                    tcid,
                )
            continue
        kept.append(m)
    return kept


def build_hallucination_middlewares():
    """构造一组 middleware 供 LangChainAgent._ensure_agent 接入。

    Returns:
        list: 可以传给 create_agent(middleware=...) 的 middleware 列表。
    """
    from langchain.agents.middleware import AgentMiddleware, dynamic_prompt, wrap_tool_call

    # 0) before_model 防御:清掉孤立 ToolMessage(防 MiniMax 'tool call result does not follow tool call' 2013)
    class _OrphanToolPruner(AgentMiddleware):
        """LangChain middleware: 在每次模型调用前过滤 messages。

        用类而不是装饰器,因为 `before_model` 钩子在 AgentMiddleware 上更直接。
        """
        name = "orphan_tool_pruner"
        def before_model(self, state, runtime):  # noqa: D401
            try:
                msgs = state.get("messages") or []
                if msgs:
                    pruned = _prune_orphan_tool_messages(msgs)
                    if len(pruned) != len(msgs):
                        # 替换 state.messages(AgentState 是 MessagesState 派生)
                        return {"messages": pruned}
            except Exception as e:  # noqa: BLE001
                log.debug("OrphanToolPruner: %s", e)
            return None

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

    return [_OrphanToolPruner(), grounding, risky_tool_logger]


__all__ = ["build_hallucination_middlewares", "_RISKY_TOOLS", "_prune_orphan_tool_messages"]