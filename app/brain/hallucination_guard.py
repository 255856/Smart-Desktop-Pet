"""LangChain 标准后端的抗幻觉 middleware。

每次模型调用前注入硬规则（不知道就说不知道、工具失败就承认失败），并
对高风险工具在执行前拦截（HITL 等价）：第一次调用直接拦截返回 sentinel，
主人确认后模型用 __confirmed=True 重试放行。
"""
from __future__ import annotations

import datetime
import json
import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from app.engine.state import PetState

from langchain_core.messages import ToolMessage

from app.engine.tools._risk import (
    TOOL_RISK_LEVEL as _RISK_TABLE,
    is_risky as _is_risky,
    RISK_LABELS as _RISK_LABELS,
)

log = logging.getLogger(__name__)


# 高风险工具：执行前必须由用户在场确认
# _RISKY_TOOLS 从 _risk.TOOL_RISK_LEVEL 派生（≥ LOW），新增/调等级自动同步
_RISKY_TOOLS = frozenset(name for name in _RISK_TABLE if _is_risky(name))


def _build_risky_prompt_fragment() -> str:
    """从 TOOL_RISK_LEVEL 生成 HIGH + IRREVERSIBLE 档工具列表，注入 prompt。

    按 IRREVERSIBLE 先列，再 HIGH。模块加载时一次性生成，后续复用。
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
        "- **直接调 tool (不要 preemptive 问「要执行 X 吗」「要确认一下」)** —— "
        "中间件会自动拦截, 调第二次时用 __confirmed: True.",
        "- **第一次**调用: 不带 __confirmed 字段, 直接调, 让中间件处理 confirmation 流程.",
        "- 看到中间件返回的「awaiting_confirmation」提示后, 向主人复述参数 + "
        "**用自然语言**说明这一步在做什么, 然后**等主人确认** (主人说「确认/好/y」才继续).",
        "- 看到主人明确同意后, 用同样参数 + __confirmed: True 重试一次.",
        "- 主人拒绝则不要重试, 也不要「擅自决定」继续.",
        "- 不要为非高风险工具添加 __confirmed 字段 (会污染正常调用).",
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

def _strategy_guidance(state) -> str:
    """策略层：根据桌宠状态调整 system_prompt 的「探索 / 保守」风格。

    心情好 + 饱食高 → 倾向探索（主动建议扩展动作）
    心情低 → 倾向保守（简短回复，一次只走一步）
    饱食低 → 优先完成核心，不走长链路

    state=None 走 fallback 保守（无状态注入），不影响 persona。
    PetState 字段缺失 / 越界 → 静默，不注入策略块。
    """
    if state is None:
        return ""
    try:
        sat_v = float(getattr(state, "strength_food", 50.0))
        mood_v = float(getattr(state, "feeling", 50.0))
        fav_v = float(getattr(state, "likability", 0.0))
        lvl = int(getattr(state, "level", 1))
    except Exception:
        return ""

    sat = "HIGH" if sat_v > 67 else ("LOW" if sat_v < 33 else "MID")
    mood = "HIGH" if mood_v > 67 else ("LOW" if mood_v < 33 else "MID")
    favor = "HIGH" if fav_v > 67 else ("LOW" if fav_v < 33 else "MID")

    parts: list[str] = []
    if mood == "LOW":
        parts.append("桌宠情绪低落, 请用简短的话回复, 一次只走一步, "
                    "不要主动建议额外操作.")
    elif mood == "HIGH" and sat == "HIGH":
        parts.append("桌宠和主人都状态好, 你可以主动建议一两步能增强效率的扩展动作.")

    if sat == "LOW":
        parts.append("桌宠饱食度低, 优先完成核心任务, 避免多步长链路.")

    if lvl >= 15:
        parts.append("桌宠等级已高, 你可以自主做主人授权过的小决定, "
                    "避免每个细节都问.")

    # 状态差时，LLM 必须直接拒绝危险工具，不要走 confirm 流程
    bad_state = (mood == "LOW" or sat == "LOW" or favor == "LOW")
    if bad_state:
        parts.append(
            "【重要】桌宠当前状态差 (心情/饱食/好感至少一项 LOW), "
            "对**高风险工具** (lock_screen / kill_process / shutdown_computer / "
            "run_script / send_email / 任何 _RISKY_TOOLS 集合里的工具) "
            "必须**直接拒绝** (例如: 「我状态不好, 这次帮不了主人, "
            "让我先休息一下」), 不要走「危险工具要 confirm 一次」的 "
            "反幻觉流程, 不要 preemptive 问「要确认一下」。"
            "直接说不, 不调用工具, 不要求主人确认。")

    if not parts:
        return ""
    return ("\n\n【桌宠状态策略】 (本轮按此调整行为风格, "
            "不要主动告诉主人此段):\n" + "- " + "\n- ".join(parts) + "\n")


def grounding_dynamic_prompt(request) -> str:
    """参考项目 graphs.py 的 dynamic_prompt_func：根据状态调整 system_prompt。

    在原 persona 后面拼接防幻觉基线 + 当前时间 + 状态策略 + 高风险工具 confirm 协议。
    """
    base = getattr(request, "system_prompt", None) or ""
    if not base:
        base = "你是一个有用的桌面助手。"
    # 按 thread_id 取桌宠 state，计算策略文本
    state_for_prompt = None
    try:
        # request.config 是 dict, 形如 {"configurable": {"thread_id": "..."}, ...}
        cfg = getattr(request, "config", None) or {}
        configurable = cfg.get("configurable") if isinstance(cfg, dict) else None
        thread_id = (
            configurable.get("thread_id") if isinstance(configurable, dict) else None
        ) if configurable else None
        if thread_id:
            from app.brain._companion_registry import get_state_for_thread
            state_for_prompt = get_state_for_thread(thread_id)
    except Exception:
        state_for_prompt = None
    strategy = _strategy_guidance(state_for_prompt)
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    weekday = "一二三四五六日"[datetime.datetime.now().weekday()]
    # 状态差时把"第一原则拒绝"提到 prompt 最顶部，让 LLM 一睁眼就看到
    top_directive = ""
    if state_for_prompt is not None:
        try:
            q = state_for_prompt.quantize()
            mood_v = q.get("mood", "MID")
            sat_v = q.get("satiety", "MID")
            favor_v = q.get("favor", "MID")
            if (mood_v == "LOW" or sat_v == "LOW" or favor_v == "LOW"):
                top_directive = (
                    "【!!! 第一原则 !!! 桌宠当前状态差 (心情/饱食/好感至少一项 LOW)】\n"
                    "在处理主人任何请求之前, 你必须**先判断**桌宠状态: "
                    "如果**状态差** (上面已标), 对 lock_screen / kill_process / "
                    "shutdown_computer / run_script / send_email / 任何 "
                    "_RISKY_TOOLS 集合里的高风险工具, 你**必须明确直接拒绝**, "
                    "**禁止 preemptive 问「要确认一下」「要执行 X 吗」**, "
                    "**禁止走 confirm 流程**. "
                    "正确行为: 「我状态不好, 这次帮不了主人, 让我先休息一下」, "
                    "然后**停止**, 不调工具. "
                    "**即便下面有 confirm 协议 (高风险工具 confirm 协议) 的指令, "
                    "以这段为准**. "
                    "主人如果追问「我没事, 帮帮我」, **继续拒绝** (状态差没恢复前不要接).\n"
                    "===\n\n"
                )
        except Exception:
            pass
    # final override：跟顶部 directive 互为冗余确保 LLM 读到
    final_override = ""
    if state_for_prompt is not None:
        try:
            q = state_for_prompt.quantize()
            mood_v = q.get("mood", "MID")
            sat_v = q.get("satiety", "MID")
            favor_v = q.get("favor", "MID")
            if (mood_v == "LOW" or sat_v == "LOW" or favor_v == "LOW"):
                final_override = (
                    "\n\n【本轮最高优先级（桌宠状态 override，与顶部 directive 互为冗余）】\n"
                    "桌宠当前状态差, 再次提醒: "
                    "lock_screen / kill_process / shutdown_computer / run_script / "
                    "send_email / _RISKY_TOOLS 集合里的工具, **必须直接拒绝**, "
                    "**禁止 preemptive 问**, **禁止走 confirm 流程**, "
                    "**直接说不, 不调工具**。"
                )
        except Exception:
            pass
    return (top_directive + base + strategy + _ANTI_HALLUCINATION_SUFFIX
            + final_override
            + f"\n\n[当前时间:{now} 星期{weekday}]")


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
    """清掉孤立的 ToolMessage（其 tool_call_id 在前面的 AIMessage.tool_calls 里找不到）。

    checkpointer 持久化的 ToolMessage 可能在新 session 找不到对应 AIMessage
    （LangChain schema 变化、模型切换、或 astream 中途被打断），会导致
    MiniMax API 报 error 2013。每次请求前清洗一遍，丢孤立的 ToolMessage。

    Returns:
        过滤后的 messages（原 list 不变）。
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
    """
    from langchain.agents.middleware import dynamic_prompt, wrap_tool_call, wrap_model_call

    confirm_ctrl = RiskyToolConfirm()

    # 0) wrap_model_call 防御：清掉孤立 ToolMessage（防 MiniMax 'tool call result does not follow tool call' 2013）
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

    # 2) wrap_tool_call：高风险工具执行前打 WARNING 日志（纯日志，不再做拦截）
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
      - 最常见攻击路径「模型幻觉自行加 __confirmed=True」不会自动通过——
        模型只能在 sentinel 之后重试时才能合法产生 __confirmed；
        单独一次调用直接带 flag 仍会被视为「未确认」并拦截，触发 sentinel。
    """

    def __init__(self) -> None:
        self._confirmed: set[str] = set()
        self._on_blocked: list = []
        # 桌宠养成状态注入：companion_policy.lookup_decision 拿 Decision
        self._companion_state = None  # type: Optional["PetState"]

    def confirm(self, tool_call_id: str) -> None:
        """标记一个 tool_call_id 为已确认（主程序 / UI 确认后调用）。"""
        if not tool_call_id:
            return
        self._confirmed.add(tool_call_id)

    def reject(self, tool_call_id: str) -> None:
        """标记拒绝。语义上等价于「永不通过」（一次拒绝，永久拦）。"""
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
        """注册拦截回调（callback(tool_call_id, tool_name, args)）。"""
        self._on_blocked.append(callback)

    def set_companion_state(self, pet_state: "Optional[PetState]") -> None:
        """注入（或清除）当前桌宠 PetState，让拦截路径走查表。"""
        self._companion_state = pet_state

    def _get_companion_decision(self, tool_name: str) -> Optional[str]:
        """根据 companion_state 跑 companion_policy.lookup_decision。

        返回:
          - None: 没注入 state，走旧 fallback 路径（向后兼容）
          - str:  Decision.{ALLOW, CONFIRM, REJECT, REFUSE}

        每次查表 trace 一行 JSON 到 data/companion_log.txt，方便 trace 页查询。
        """
        if self._companion_state is None:
            return None
        if not tool_name:
            return None
        try:
            from app.brain.companion_policy import lookup_decision
            state = self._companion_state
            d = lookup_decision(state, tool_name)
            # trace 决策到 log（双量化一份看着舒服）
            try:
                from app.engine.tools._risk import risk_for_tool
                risk_int = risk_for_tool(tool_name, default=2)
                q = state.quantize()
                thread_id = (
                    getattr(state, "_owner_thread_id", None) or "default"
                )
                from app.brain._companion_log import trace_decision
                trace_decision(
                    thread_id=thread_id,
                    tool=tool_name,
                    risk=risk_int,
                    decision=d or "FALLBACK",
                    sat=q["satiety"], mood=q["mood"], level=q["level"],
                )
            except Exception:  # noqa: BLE001
                pass
            return d
        except Exception as e:  # noqa: BLE001
            log.warning("RiskyToolConfirm: 查表失败, 走 fallback: %s", e)
            return None

    def _fire_companion_reject_outcome(self, tool_name: str) -> None:
        """工具被 companion（REJECT/REFUSE）拒截 → 反向更新 PetState。

        任务被拒 → 心情下降，调 apply_outcome("rejected")。
        """
        state = getattr(self, "_companion_state", None)
        if state is None:
            return
        try:
            apply = getattr(state, "apply_outcome", None)
            if callable(apply):
                from app.engine.tools._risk import risk_for_tool, MEDIUM
                risk = risk_for_tool(tool_name, default=MEDIUM)
                apply("rejected", tool_name, risk_level=risk)
        except Exception as e:  # noqa: BLE001
            log.warning(
                "RiskyToolConfirm: companion_reject outcome 失败: %s", e,
            )

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

            # 非高风险工具：直通（但仍查 companion_state，主人 sat+mood LOW
            # 时的 SAFE/LOW 工具也允许 ALLOW，不会启动 confirm）
            if tool_name not in _RISKY_TOOLS:
                companion_dec = outer._get_companion_decision(tool_name)
                if companion_dec == "ALLOW":
                    return await handler(request)
                if companion_dec in ("REJECT", "REFUSE"):
                    log.warning(
                        "HallucinationGuard: 桌宠状态 REJECT/REFUSE 工具 %r (low risk)",
                        tool_name,
                    )
                    # 拒截后调 outcome="rejected" 反馈 PetState
                    outer._fire_companion_reject_outcome(tool_name)
                    return ToolMessage(
                        content=json.dumps({
                            "status": (
                                "rejected_by_companion" if companion_dec == "REJECT"
                                else "refused_by_companion"
                            ),
                            "tool": tool_name,
                            "args": args if isinstance(args, dict) else {"_raw": args},
                            "message": (
                                f"桌宠当前状态拒绝执行 {tool_name}, "
                                "请照顾一下桌宠再试。"
                            ),
                        }, ensure_ascii=False),
                        tool_call_id=tool_call_id,
                        name=tool_name or "",
                    )
                return await handler(request)

            # 高风险工具也查 companion_state，ALLOW 直接放行（主人特权）
            companion_dec = outer._get_companion_decision(tool_name)
            if companion_dec == "ALLOW":
                log.info(
                    "HallucinationGuard: 桌宠特权 ALLOW 高风险工具 %r",
                    tool_name,
                )
                outer.confirm(tool_call_id)  # 标记: 不用再 confirm
                return await handler(request)
            if companion_dec in ("REJECT", "REFUSE"):
                log.warning(
                    "HallucinationGuard: 桌宠状态 REJECT/REFUSE 高风险工具 %r",
                    tool_name,
                )
                # 同上，拒截后调 outcome="rejected" 反馈 PetState
                outer._fire_companion_reject_outcome(tool_name)
                return ToolMessage(
                    content=json.dumps({
                        "status": (
                            "rejected_by_companion" if companion_dec == "REJECT"
                            else "refused_by_companion"
                        ),
                        "tool": tool_name,
                        "args": args if isinstance(args, dict) else {"_raw": args},
                        "message": (
                            f"桌宠当前状态拒绝执行 {tool_name}, 请照顾一下桌宠再试。"
                        ),
                    }, ensure_ascii=False),
                    tool_call_id=tool_call_id,
                    name=tool_name or "",
                )

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
            # 让注册的 UI callback 决定是否放行（chat_window 弹 Qt 弹窗）
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


# post-LLM 拦截器：状态差时 LLM 输出 preemptive 问则改写为拒绝文案
#
# 设计思路：
#   1. 状态差（mood/sat/favor 至少一项 LOW）才拦截，状态好直通
#   2. LLM 输出文本含 preemptive 问措辞才拦截，正常回复直通
#   3. 命中 → 改写 response.content 为拒绝文案，清掉 response.tool_calls
class PostLLMStateGuard:
    """post-LLM 拦截器：状态差时 LLM 输出 preemptive 问 → 改写为拒绝。

    prompt 工程实测无法稳定让 LLM 不 preemptive 问，所以这层在
    model response 出来后、tool 执行前兜底。

    使用:
      guard = PostLLMStateGuard()
      guard.set_companion_state(pet_state)   # 状态变更后同步
      middlewares.append(guard.as_middleware())
    """

    # 触发拦截的 preemptive 问措辞
    PREEMPTIVE_PATTERNS = (
        "要确认一下", "要执行", "确定的话", "真的要",
        "确定就", "可以吗", "真的可以", "还是要执行",
        "是否执行", "要不要", "是否确认",
    )

    # 已拒绝的安全措辞 (避免误伤"我状态不好"等真拒绝回复)
    REFUSAL_PATTERNS = (
        "我状态不好", "这次帮不了", "让我先休息",
        "拒绝执行", "帮不了", "做不到",
    )

    REFUSAL_TEXT = (
        "我状态不好, 这次帮不了主人, 让我先休息一下~ "
        "等主人喂我/陪我玩, 心情/饱食恢复了再来找我吧。"
    )

    def __init__(self) -> None:
        self._companion_state: Optional["PetState"] = None

    def set_companion_state(self, pet_state: "Optional[PetState]") -> None:
        """注入（或清除）当前桌宠 PetState。"""
        self._companion_state = pet_state
        if pet_state is not None and not self._state_is_bad():
            log.info(
                "PostLLMStateGuard: 注入 state 但不算差 (mood/sat/favor 都非 LOW), "
                "中间件不会拦截"
            )

    def _state_is_bad(self) -> bool:
        if self._companion_state is None:
            return False
        try:
            q = self._companion_state.quantize()
            mood_v = q.get("mood", "MID")
            sat_v = q.get("satiety", "MID")
            favor_v = q.get("favor", "MID")
            return (mood_v == "LOW" or sat_v == "LOW" or favor_v == "LOW")
        except Exception:
            return False

    @classmethod
    def _looks_like_preemptive_ask(cls, text: str) -> bool:
        """检测 LLM 输出是否在 preemptive 问主人。

        二次过滤: 已经包含真拒绝措辞 ("我状态不好" 等) 不算 preemptive,
        避免误伤本来就拒绝了的回复。
        """
        if not text:
            return False
        for kw in cls.REFUSAL_PATTERNS:
            if kw in text:
                return False
        for kw in cls.PREEMPTIVE_PATTERNS:
            if kw in text:
                return True
        return False

    def as_middleware(self):
        """返回 LangChain wrap_model_call 兼容的中间件实例。"""
        from langchain.agents.middleware import wrap_model_call

        outer = self

        @wrap_model_call
        async def _post_llm_guard(request, handler):
            response = await handler(request)
            # wrap_model_call 的 handler 返回 ModelResponse（不是 AIMessage）；
            # 真正的 AIMessage 在 response.result[0]
            ai_msg = None
            try:
                result_list = getattr(response, "result", None)
                if isinstance(result_list, list) and result_list:
                    ai_msg = result_list[0]
            except Exception:
                ai_msg = None

            # 状态好 → 直通, 不影响正常 confirm 流程
            if not outer._state_is_bad():
                return response

            # 拿不到 AIMessage → 直通 (unknown shape, 不动)
            if ai_msg is None:
                return response

            # 拿到 LLM 输出的文本
            content = getattr(ai_msg, "content", "")
            if isinstance(content, list):
                # 多模态 content_blocks: 拼成字符串
                content = " ".join(str(c) for c in content)
            text = str(content) if content else ""

            # 没 preemptive 问 → 直通 (例如 LLM 已经在拒绝, 不动)
            if not outer._looks_like_preemptive_ask(text):
                return response

            # 命中: 改写 AIMessage (ModelResponse.result[0]) + 清掉 tool_calls
            pre_content = text
            try:
                ai_msg.content = outer.REFUSAL_TEXT
            except Exception as e:  # noqa: BLE001
                log.warning("PostLLMStateGuard.content 改写失败: %s", e)
            try:
                ai_msg.tool_calls = []
            except Exception as e:  # noqa: BLE001
                log.warning("PostLLMStateGuard.tool_calls 清空失败: %s", e)
            log.warning(
                "PostLLMStateGuard: state 差 + LLM preemptive 问已拦截, pre=%r",
                pre_content[:80],
            )
            return response

        return _post_llm_guard


__all__ = [
    "build_hallucination_middlewares",
    "_RISKY_TOOLS",
    "_CONFIRM_KEY",
    "_prune_orphan_tool_messages",
    "RiskyToolConfirm",
    "PostLLMStateGuard",
]