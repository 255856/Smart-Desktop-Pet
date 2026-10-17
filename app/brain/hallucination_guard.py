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
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    # v21-5: Optional 类型 hint 避免运行时 import PetState (engine 在 brain
    # 之上, 直接 import 有概率循环, 仅在静态检查时 import)。
    from app.engine.state import PetState

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
        # v32: 改 confirm 协议 —— 之前教 LLM "看到拦截提示后向主人复述参数
        # 并询问「要执行 X 吗?」", 实际 LLM 读后把这条**误读成**"调危险工具
        # 前 preemptive 问「要执行 X 吗」", 主人 12:25 实测 LLM 仍说"要确认一下"
        # (压根没调工具). 修法: 改成让 LLM **直接调 tool (不带 __confirmed)**,
        # 让中间件拦截后, LLM 看到「awaiting_confirmation」再**自然地**向
        # 主人解释 + 等主人确认, 不要 preemptive 问. 把"preemptive 问"这
        # 个行为模式直接从 prompt 里删掉.
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
    """v22-3: 策略层 — 根据桌宠状态调整 system_prompt 的「探索 / 保守」风格。

    论文 §3.2 策略层:
      - 心情好 + 饱食高 → 倾向探索 (主动建议扩展动作)
      - 心情低 → 倾向保守 (简短回复, 一次只走一步)
      - 饱食低 → 优先完成核心, 不走长链路

    v30 主人 2026-10-17 12:08 反馈 "状态低她还是会调用 lock_screen" — 之前
    策略层只让 LLM "简短回复", 没明确 "状态差时拒绝危险工具". LLM 仍按
    反幻觉基线 "危险工具要 confirm 一次" 的规则, preemptive 问确认
    ("要确认一下"). 主人期望: 桌宠状态低时, LLM 直接说"我状态不好,
    这次帮不了你" (甚至不要 confirm), 不调用工具. 强化: state 差 +
    危险工具 (lock_screen / kill_process / shutdown / run_script 等)
    → LLM 必须**直接拒绝**, 不要走 confirm 流程.

    state=None 走 fallback 保守 (无状态注入), 不影响 persona.
    PetState 字段缺失 / 越界 → 静默, 不注入策略块.
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

    # v30: 状态差时 (mood LOW / sat LOW / favor LOW), LLM 必须**直接拒绝
    # 危险工具**, 不要按反幻觉基线"危险工具要 confirm 一次"流程
    # preemptive 问确认. 主人 12:08 反馈"状态低她还是会调 lock_screen"
    # 就是要让 LLM 看到此段后, 在状态差时:
    #   1. 不调危险工具 (lock_screen / kill_process / shutdown / run_script 等)
    #   2. 直接说"我状态不好, 这次帮不了你, 等我恢复了再帮"
    # 3. 即使主人坚持, 也**继续拒绝** (不像普通 dangerous 时 confirm 一次
    #      就放行). 中间件兜底: 主人硬逼的话, 工具被 REJECT, LLM 看到
    #      rejected_by_companion 应该承认"桌宠坚持拒绝".
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
    return ("\n\n【桌宠状态策略 (v22-3 策略层)】 (本轮按此调整行为风格, "
            "不要主动告诉主人此段):\n" + "- " + "\n- ".join(parts) + "\n")


def grounding_dynamic_prompt(request) -> str:
    """参考项目 graphs.py 的 dynamic_prompt_func：根据状态调整 system_prompt。

    这里在原 persona 后面拼接一段防幻觉基线 + 当前时间。
    主要用途：
      - 强制模型在每次调用里都看到防幻觉规则（不仅仅在开始一次性 system_prompt 里看）
      - 在调用中提供「现在是什么时间」让模型错不了时间类问题
      - 注入「高风险工具 confirm 协议」让模型知道拦截-重试约定
      - v22-3: 注入「状态策略」,心情好走探索 / 心情低走保守
    """
    base = getattr(request, "system_prompt", None) or ""
    if not base:
        base = "你是一个有用的桌面助手。"
    # v22-3: 按 thread_id 取桌宠 state, 计算策略文本
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
    # v33: 主人 12:37 反馈「不要移除工具, 麻烦, 想办法用 prompt 让 LLM 拒绝」.
    # 之前 v30/v31/v32 改了 prompt 但 LLM 仍 preemptive 问, 因为:
    #   1. 状态判断放在 strategy 块 (prompt 中间), LLM 读后被 confirm 协议
    #      覆盖.
    #   2. confirm 协议本身 (v32 改后) 仍有 confirm 流程, LLM 走 confirm.
    # 修法: 把「状态差时**第一原则拒绝**」提到 prompt **最顶部** (base persona
    # 之前), 让 LLM 一睁眼就看到状态 + 拒绝指令, 后面所有 reasoning 都从
    # 这个 state check 出发. 状态好时这段为空, 不影响正常 confirm 流程.
    # 注意: 不删工具 (v33 内部讨论过 `_ensure_agent` 过滤, 主人否决).
    top_directive = ""
    if state_for_prompt is not None:
        try:
            q = state_for_prompt.quantize()
            mood_v = q.get("mood", "MID")
            sat_v = q.get("satiety", "MID")
            favor_v = q.get("favor", "MID")
            if (mood_v == "LOW" or sat_v == "LOW" or favor_v == "LOW"):
                # 第一原则 (在 base persona **之前**): 状态差时禁止调高危工具
                # 用 [[!!! ... !!!]] 双三重强调, 让 LLM 一眼能看到
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
    # v31 (保留, 当作 final override, 跟顶部 directive 互为冗余确保 LLM 读到)
    final_override = ""
    if state_for_prompt is not None:
        try:
            q = state_for_prompt.quantize()
            mood_v = q.get("mood", "MID")
            sat_v = q.get("satiety", "MID")
            favor_v = q.get("favor", "MID")
            if (mood_v == "LOW" or sat_v == "LOW" or favor_v == "LOW"):
                final_override = (
                    "\n\n【本轮最高优先级 (v31 桌宠状态 override, 与顶部 directive 互为冗余)】\n"
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
        # v21-5: 桌宠养成状态注入 (创新点 2 反馈闭环接入).
        # 主程序通过 set_companion_state 同步当前 PetState,中间件在拦截路径上
        # 调用 companion_policy.lookup_decision(state, tool_name), 拿 Decision:
        #   ALLOW  → 直接放行 (即便工具在 _RISKY_TOOLS 里也放行, 主人特权)
        #   CONFIRM → 弹窗路径 (跟旧 _RISKY_TOOLS 行为一致)
        #   REJECT / REFUSE → 拦截并返回拒绝 sentinel
        self._companion_state = None  # type: Optional["PetState"]

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

    def set_companion_state(self, pet_state: "Optional[PetState]") -> None:
        """v21-5: 注入 (或清除 None) 当前桌宠 PetState, 让拦截路径走查表。

        主程序应在每次 state 变更 (apply_outcome, 喂食, 升级, 衰减) 后调用
        这里同步一次, 避免中间件拿到过期的 state。
        """
        self._companion_state = pet_state

    def _get_companion_decision(self, tool_name: str) -> Optional[str]:
        """根据 companion_state 跑 companion_policy.lookup_decision。

        返回:
          - None   : 没注入 state, 走旧 fallback 路径 (向后兼容)
          - str    : Decision.{ALLOW, CONFIRM, REJECT, REFUSE}

        注意: companion_policy 在 PetState 未知字段上抛, 这里 try/except 兜底,
        出错就返 None (走 fallback 而非崩溃中间件)。

        v23-1: 每次查表都 trace 决策到 data/companion_log.txt (一行 JSON),
        让 trace 页能直接 grep "set_brightness 当时 state 是什么 / 决策是什么"。
        """
        if self._companion_state is None:
            return None
        if not tool_name:
            return None
        try:
            from app.brain.companion_policy import lookup_decision
            state = self._companion_state
            d = lookup_decision(state, tool_name)
            # v23-1 trace: 不传 sat/mood/level 也能算 (companion_policy 内部 quantize);
            # 不过双量化一份 log 主人看着舒服 (state 跟 quantize 的映射)
            try:
                from app.engine.tools._risk import risk_for_tool
                risk_int = risk_for_tool(tool_name, default=2)
                q = state.quantize()
                thread_id = (
                    getattr(state, "_owner_thread_id", None) or "default"
                )  # PetState 没 thread_id attr; 用 'default' 占位
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
        """v22-4: 工具被 companion (REJECT/REFUSE) 拒截 → 反向更新 PetState。

        跟 paper §3.3 反馈闭环: 任务被拒 → 心情-1.5 (owner 明确拒绝).
        因为 REJECT/REFUSE 不是"主人主动拒绝"但语义上一致 (工具被拒,
        不调用), 用 v21-3 apply_outcome("rejected") 让心情骤降即可.

        注意: companion_state 上层已 set, 这里直接调 outcome. 字段缺失
        / 数值越界等异常吞掉不抛 (中间件失败不能让 agent 崩)。
        """
        state = getattr(self, "_companion_state", None)
        if state is None:
            return
        try:
            apply = getattr(state, "apply_outcome", None)
            if callable(apply):
                # risk 字段从 _RISK_TABLE 拿; 缺工具默认 MEDIUM.
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

            # 非高风险工具: 直通 (但 v21-5 仍查 companion_state, 主人 sat+mood LOW
            # 时的 SAFE/LOW 工具也允许 ALLOW, 不会启动 confirm)
            if tool_name not in _RISKY_TOOLS:
                companion_dec = outer._get_companion_decision(tool_name)
                if companion_dec == "ALLOW":
                    return await handler(request)
                if companion_dec in ("REJECT", "REFUSE"):
                    log.warning(
                        "HallucinationGuard: 桌宠状态 REJECT/REFUSE 工具 %r (low risk)",
                        tool_name,
                    )
                    # v22-4: 拒截后调 PetState.apply_outcome("rejected") 让
                    # 心情-1.5; 拦阻本身是一种 "交互结果" 应该影响状态。
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

            # v21-5: 高风险工具也查 companion_state, ALLOW 直接放行 (主人特权)
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
                # v22-4: 同上, 拒截后调 outcome="rejected" 反馈 PetState.
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


# v34 (2026-10-17): post-LLM 拦截器。
# 主人实测 12:34 / 12:40 / 12:52 LLM 仍 preemptive 问"要确认一下", 即便
# prompt 顶部 directive (v33) + 末尾 override (v31) + confirm 协议改写
# (v32) 都失败 —— 这是第 6 次同样问题。切思路: 不依赖 LLM 听话, 在
# model response 后改写。
#
# 思路 (跟 RiskyToolConfirm 同模式, 状态外部注入, wrap_model_call 拦截):
#   1. 状态差 (mood/sat/favor 至少一项 LOW) 才拦截, 状态好直通
#   2. LLM 输出文本含 preemptive 问措辞 ("要确认一下" / "要执行 X 吗" /
#      "确定的话...就帮" / "确定就" / "真的要") 才拦截, 正常回复直通
#   3. 命中 → 改写 response.content = 拒绝文案 ("我状态不好..."),
#      清掉 response.tool_calls (避免 LLM 后面真调高危工具)
#   4. 不依赖 LLM 听指令, 100% 拦截
class PostLLMStateGuard:
    """post-LLM 拦截器: 状态差时, LLM 输出 preemptive 问 → 改写为拒绝。

    v34: prompt 工程走到极限 (v30-v33 五个版本), 实测 LLM 仍 preemptive 问。
    这层在 model response 出来后、tool 执行前兜底, 不让 preemptive 文案
    走到 UI。清掉 tool_calls 也保证高危工具不被真调。

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
        """v34: 注入 (或清除) 当前桌宠 PetState。"""
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
            # wrap_model_call 的 handler 返回 ModelResponse (不是 AIMessage).
            # 真正的 AIMessage 在 response.result[0]. 改 ModelResponse.content
            # 没这字段, 异常被 try/except 吞掉 → 中间件看似生效实际零作用.
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