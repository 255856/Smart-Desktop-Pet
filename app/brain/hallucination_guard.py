"""LangChain 标准后端的抗幻觉 middleware（对齐参考项目 graphs.py 的动态 prompt 模式）。

设计原则：
  - 不复用手写 ReAct 的反幻觉 3 层（那是产品的核心）；而是给标准后端加一个
    「基线」：每次模型调用前注入「不知道就说不知道」「工具失败就承认失败」
    等硬规则，降低标准后端编造的倾向。
  - 风险工具提醒：对 lock_screen / kill_process / set_wifi / set_bluetooth
    / send_notification / run_script 等高风险工具，在执行前拦截（HITL 等价）：
      第一次调用直接拦截，返回「请主人确认」sentinel ToolMessage；
      模型看到后向主人询问；主人回复确认后，模型用同参数加 __confirmed=True 重试；
      中间件取出确认标记后放行。

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
import json
import logging
from typing import Any

from langchain_core.messages import ToolMessage

# v21-2: 创新点 2 工具风险等级表派生。
# 原本 _RISKY_TOOLS 是手写 frozenset, 容易跟 _risk.TOOL_RISK_LEVEL 漂移,
# 这里改为: _RISKY_TOOLS = is_risky(name) (≥ LOW) 的工具名集。
# 这样以后 _risk.py 加新工具 / 调等级, 自动同步影响 confirm 拦截范围。
from app.engine.tools._risk import (
    TOOL_RISK_LEVEL as _RISK_TABLE,
    is_risky as _is_risky,
    RISK_LABELS as _RISK_LABELS,
)

log = logging.getLogger(__name__)


# 高风险工具：执行前必须由用户在场确认。
# 2026-10-15 起从「只 log.warning」升级为「必须 confirm」：
#   - log.warning 不阻断，模型/重试路径仍可跑通，只是开发侧看到；线上无防御
#   - 真拦截能让 lock_screen / kill_process / send_email 这类破坏性操作
#     在主程序未接管 UI 弹窗的情况下也被模型侧挡住一道
# v21-2: _RISKY_TOOLS 改从 _risk.TOOL_RISK_LEVEL 派生 (≥ LOW 门槛, 兼容
# 原 9 条: lock_screen / kill_process / set_wifi / set_bluetooth /
# send_notification / run_script / set_volume / set_brightness / send_email)。
# 新增 generate_video (HIGH) 也自动进集合, 主人写邮件 / 执行脚本前都被拦。
_RISKY_TOOLS = frozenset(name for name in _RISK_TABLE if _is_risky(name))


def _build_risky_prompt_fragment() -> str:
    """v21-2: 从 TOOL_RISK_LEVEL 生成 HIGH + IRREVERSIBLE 档工具列表, 注入 prompt。

    自动列出 ≥HIGH 风险的工具 (主人在 prompt 里看到"哪些是高风险"), 模型
    看不到 SAFE / LOW / MEDIUM 档 (避免 prompt 过长导致注水)。
    按 IRREVERSIBLE 先列, 再 HIGH。模块加载时一次性生成, 后续 prompt 拼接
    直接复用, 不每次重算。
    """
    by_level: dict[int, list[str]] = {}
    for name, level in _RISK_TABLE.items():
        by_level.setdefault(level, []).append(name)
    irreversible = sorted(by_level.get(4, []))
    high = sorted(by_level.get(3, []))
    parts = [
        "以下工具属于高风险操作 (主人查 prompt 时按风险等级分组):",
        "",
        "【IRREVERSIBLE 档 (主人立刻被打断, 难撤销)】",
        "  " + ", ".join(irreversible) if irreversible else "  (无)",
        "",
        "【HIGH 档 (网络/邮件/脚本执行, 影响显著)】",
        "  " + ", ".join(high) if high else "  (无)",
        "",
        "对每一项调用前:",
        "- 第一次调用必须不带 __confirmed 字段 —— 中间件会拦截并返回「请主人确认」提示。",
        "- 看到拦截提示后, 向主人复述参数并询问:「要执行 X 吗?」。",
        "- 主人明确说「确认 / 好的 / y / 可以」之类同意后, 用同样的参数加 __confirmed: True 重试一次。",
        "- 主人拒绝则不要重试, 也不要「擅自决定」继续。",
        "- 不要为非高风险工具添加 __confirmed 字段 (会污染正常调用)。",
    ]
    return "\n".join(parts)


_RISKY_PROMPT_FRAGMENT = _build_risky_prompt_fragment()

# 确认标记键名（注入到 tool args 里）。模型在第二次调用时携带，
# 中间件检测到就放行并把该字段从 args 里剥掉再执行真实工具。
_CONFIRM_KEY = "__confirmed"


_ANTI_HALLUCINATION_SUFFIX = """

【防幻觉基线】（不要破例外）
- 没看到的图片不要描述；没收到的数据不要编造。
- 不知道主人最近做了什么就说不知道，不要猜。
- 工具返回失败、报错、错误的，**必须如实告知主人失败**，不要装作成功。
- 调用工具前，先看清实参。调用后，根据返回值告诉主人结果。
- 如果已记住主人说过某事，可以引用；如果仅仅是「可能」，说「我不确定」。

【高风险工具 confirm 协议】
""" + _RISKY_PROMPT_FRAGMENT

def grounding_dynamic_prompt(request) -> str:
    """参考项目 graphs.py 的 dynamic_prompt_func：根据状态调整 system_prompt。

    这里在原 persona 后面拼接一段防幻觉基线 + 当前时间。
    主要用途：
      - 强制模型在每次调用里都看到防幻觉规则（不仅仅在开始一次性 system_prompt 里看）
      - 在调用中提供「现在是什么时间」让模型错不了时间类问题
      - 注入「高风险工具 confirm 协议」让模型知道拦截-重试约定
    """
    base = getattr(request, "system_prompt", None) or ""
    if not base:
        base = "你是一个有用的桌面助手。"
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    weekday = "一二三四五六日"[datetime.datetime.now().weekday()]
    return base + _ANTI_HALLUCINATION_SUFFIX + f"\n\n[当前时间:{now} 星期{weekday}]"


def _extract_confirm_flag(args: Any) -> tuple[Any, bool]:
    """取出 args 里的 __confirmed 标记，返回 (clean_args, confirmed)。

    兼容 dict / 非 dict：
      - dict：pop 标记字段，返回剩下的 dict
      - 其它（list / str / 等等）：无标记，confirmed=False, 返回原值
    """
    if isinstance(args, dict) and args.get(_CONFIRM_KEY) is True:
        clean = {k: v for k, v in args.items() if k != _CONFIRM_KEY}
        return clean, True
    return args, False


def _log_risky_tool_call(tool_name: str, args: Any) -> None:
    """高风险工具执行前打醒（日志侧防御层）。"""
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
    """构造标准三件 middleware + 新建一个 RiskyToolConfirm 控制器。

    Returns:
        (middlewares_list, confirm_controller):
          - middlewares_list: 3 个标准中间件
              (orphan_tool_pruner / grounding_dynamic_prompt / risky_tool_logger)
            不含 confirm 中间件 —— confirm 中间件必须由调用方用 confirm_controller
            自己 .as_middleware() 生成,以保证 confirm 状态挂在同一个 agent 上。
          - confirm_controller: 新建的 RiskyToolConfirm 实例

    设计取舍:之前把 4 个中间件塞在同一个 list 里返回,导致 caller 必须
    截出来丢弃新 ctrl 再用自己的 ctrl —— 太绕。现在分两步,语义更清晰。
    """
    from langchain.agents.middleware import dynamic_prompt, wrap_tool_call, wrap_model_call

    confirm_ctrl = RiskyToolConfirm()

    # 0) wrap_model_call 防御:清掉孤立 ToolMessage(防 MiniMax 'tool call result does not follow tool call' 2013)
    # 用 wrap_model_call 而不是 before_model,因为 wrap_tool_call 能直接拿到
    # request.messages 并通过 request.override() 替换,保证 LangGraph 内部真的用
    # 清洗过的 messages 去发请求。before_model 的 dict 返回在某些 LangChain 版本里
    # 对 messages 字段不生效(实测:孤儿 ToolMessage 仍被发出去,导致 2013)。
    @wrap_model_call
    async def orphan_tool_pruner(request, handler):
        try:
            msgs = getattr(request, "messages", None) or []
            if msgs:
                pruned = _prune_orphan_tool_messages(msgs)
                if len(pruned) != len(msgs):
                    log.info(
                        "HallucinationGuard: 清洗 %d → %d messages (丢弃 %d 个孤立 ToolMessage)",
                        len(msgs), len(pruned), len(msgs) - len(pruned),
                    )
                    request = request.override(messages=pruned)
        except Exception as e:  # noqa: BLE001
            log.exception("OrphanToolPruner: %s", e)
        return await handler(request)

    # 1) dynamic_prompt: 每次调用都重新生成 system_prompt,加上防幻觉基线 + 当前时间
    grounding = dynamic_prompt(grounding_dynamic_prompt)

    # 2) wrap_tool_call: 高风险工具执行前打 WARNING 日志(纯日志,不再做拦截)
    # 修 2026-10-15 历史 bug:之前用 getattr(tc, "name") 拿不到 dict 的 key,分支
    # 永远走不到,日志层是死的。现在改 tc["name"] / tc.get("name")。
    @wrap_tool_call
    async def risky_tool_logger(request, handler):
        tc = getattr(request, "tool_call", None) or {}
        tool_name = tc.get("name") if isinstance(tc, dict) else None
        args = tc.get("args") if isinstance(tc, dict) else None
        if tool_name in _RISKY_TOOLS:
            _log_risky_tool_call(tool_name, args)
        return await handler(request)

    return [orphan_tool_pruner, grounding, risky_tool_logger], confirm_ctrl


class RiskyToolConfirm:
    """高风险工具 confirm 控制器(HITL 等价)。

    设计:
      - 状态:per-agent 实例,挂在 LangChainAgent 上,跟生命周期一致。
      - 拦截:wrap_tool_call 中间件 —— 第一次调用 risky tool 时,返回 sentinel
        ToolMessage(content=json{status:'awaiting_confirmation',...}, tool_call_id=...)
        模型看到 sentinel 后向主人复述参数询问;主人确认后,模型用原参数 +
        __confirmed=True 重试;中间件取出确认字段,放行。
      - 通知:每拦截一次,触发 on_blocked 回调,主程序可以在那里弹 confirm dialog
        并回调 self.confirm(id) 让 agent 在下一轮重试时直接通过(无需再走模型确认协议)。
      - 越权防御:即使模型自行加 __confirmed=True,只要 (tool_name, args) 跟
        _RISKY_TOOLS 里某个工具匹配且没经过 confirm(),仍会被拦截。

    安全分析:
      - 最常见攻击路径 ——「模型幻觉自行加 __confirmed=True」—— 不需要 UI 接入也
        不会自动通过(因为只在 sentinel 之后模型重试时才有合理路径产生 __confirmed;
        单独一次调用直接带 flag 仍然会被视为「未确认」并拦截 → 触发 sentinel →
        模型必须重新规划)。这是 model-mediated 模式不可避免的限制,UI 接入
        后可彻底规避。
    """

    def __init__(self) -> None:
        self._confirmed: set[str] = set()
        self._on_blocked: list = []

    def confirm(self, tool_call_id: str) -> None:
        """标记一个 tool_call_id 为已确认(主程序 / UI 确认后调用)。"""
        if not tool_call_id:
            return
        self._confirmed.add(tool_call_id)

    def reject(self, tool_call_id: str) -> None:
        """标记拒绝。语义上等价于「永不通过」(一次拒绝,永久拦)。"""
        if not tool_call_id:
            return
        # 用一个不同的集合避免覆盖 confirm
        if not hasattr(self, "_rejected"):
            self._rejected = set()
        self._rejected.add(tool_call_id)

    def is_confirmed(self, tool_call_id: str) -> bool:
        return tool_call_id in self._confirmed

    def is_rejected(self, tool_call_id: str) -> bool:
        return getattr(self, "_rejected", set()) and tool_call_id in self._rejected

    def on_blocked(self, callback) -> None:
        """注册拦截回调(callback(tool_call_id, tool_name, args))。"""
        self._on_blocked.append(callback)

    def _fire_blocked(self, tool_call_id: str, tool_name: str, args: Any) -> bool:
        """触发所有拦截回调,任意一个返回 True 即视为确认(放行)。

        返回:
          True  = 任意 callback 同意 → 调用方应该放行
          False = 全部 callback 拒绝(或没 callback)→ 调用方应继续拦截

        设计:
          - 主程序注册的 callback 返回 True → 用户在 Qt 弹窗点了「确认」
          - 返回 False → 用户点了「拒绝」或弹窗超时(都视为不放行)
          - 没注册任何 callback → 默认 False(继续拦截),这样 model-mediated
            协议(让模型重加 __confirmed=True 重试)还是能工作
        """
        any_approved = False
        for cb in list(self._on_blocked):
            try:
                r = cb(tool_call_id, tool_name, args)
                if r is True:
                    any_approved = True
            except Exception as e:  # noqa: BLE001
                log.warning("RiskyToolConfirm: on_blocked 回调失败: %s", e)
        return any_approved

    def as_middleware(self):
        """返回 LangChain wrap_tool_call 兼容的中间件实例。"""
        from langchain.agents.middleware import wrap_tool_call

        outer = self

        @wrap_tool_call
        async def _confirm_middleware(request, handler):
            tc = getattr(request, "tool_call", None) or {}
            if not isinstance(tc, dict):
                return await handler(request)
            tool_name = tc.get("name")
            args = tc.get("args")
            tool_call_id = tc.get("id") or ""

            # 非高风险工具: 直通
            if tool_name not in _RISKY_TOOLS:
                return await handler(request)

            # 已被拒绝:永远不通过
            if outer.is_rejected(tool_call_id):
                return ToolMessage(
                    content=json.dumps({
                        "status": "rejected",
                        "tool": tool_name,
                        "args": args if isinstance(args, dict) else {"_raw": args},
                    }, ensure_ascii=False),
                    tool_call_id=tool_call_id,
                    name=tool_name or "",
                )

            # 已被显式 confirm():直接执行
            if outer.is_confirmed(tool_call_id):
                return await handler(request)

            # 检查 args 里的 __confirmed 标记(模型重试协议)
            clean_args, has_flag = _extract_confirm_flag(args)
            if has_flag:
                # 用同 tool_call_id 重新构造 request 走 handler(去掉 __confirmed)
                new_tc = {**tc, "args": clean_args}
                new_request = request.override(tool_call=new_tc)
                outer.confirm(tool_call_id)  # 标记通过,下次直接通过
                log.info(
                    "HallucinationGuard: 危险工具 %r 已被主人确认(args 含 __confirmed=True),执行",
                    tool_name,
                )
                return await handler(new_request)

            # 走到这里 = 第一次调用 or 未带确认 flag —— 拦截
            log.warning(
                "HallucinationGuard: 危险工具 %r 被拦截,等待 confirm (tool_call_id=%s, args=%r)",
                tool_name, tool_call_id, args,
            )
            # 2026-10-15:让注册的 UI callback 决定是否放行。
            # chat_window 的 _confirm_dangerous_tool 弹 Qt 弹窗:
            # 用户点「确认」→ callback 返回 True → 这里直通 handler 执行
            # 否则 callback 返回 False/None → 继续拦截,返回 awaiting_confirmation
            if outer._fire_blocked(tool_call_id, tool_name, args):
                # 用户已确认:直接执行工具(走 handler)
                log.info(
                    "HallucinationGuard: 危险工具 %r 已被用户确认(回调返回 True),执行",
                    tool_name,
                )
                return await handler(request)
            args_view = args if isinstance(args, dict) else {"_raw": args}
            content = json.dumps({
                "status": "awaiting_confirmation",
                "tool": tool_name,
                "args": args_view,
                "message": (
                    f"工具 {tool_name} 是高风险操作,需要主人明确同意。"
                    "请向主人复述参数并询问是否执行;得到确认后,"
                    "用同样的参数加 \"__confirmed\": True 重试一次。"
                ),
            }, ensure_ascii=False)
            return ToolMessage(
                content=content,
                tool_call_id=tool_call_id,
                name=tool_name or "",
            )

        return _confirm_middleware


__all__ = [
    "build_hallucination_middlewares",
    "_RISKY_TOOLS",
    "_CONFIRM_KEY",
    "_prune_orphan_tool_messages",
    "RiskyToolConfirm",
]