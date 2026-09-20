"""Agent 循环：让大模型通过工具调用真正「做事」。

这是一个真正的 ReAct 循环（与 LangChain `create_agent` 语义对齐）：

    1. 流式请求 LLM（带 tools schema）；
    2. 模型可以决定：
       a) 直接给文字回复（闲聊 / 总结）→ 循环结束，文字即为 final answer
       b) 调用工具 → 工具结果作为 ToolMessage 回传，循环回到步骤 1
    3. 重复 1-2 直到：
       a) 模型给出 final answer（不调工具），或
       b) 达到 max_turns 上限，或
       c) 连续多轮纯调工具没给文字 → 强制进入 final 阶段（去掉 tools，让模型必须总结）

这是「真正的智能体」—— 模型自主决定调什么工具、调几次、什么时候给 final answer。
不像 Planner+Executor 模式那样：先规划后执行、模型不再回头参与决策。

产出事件（yield）：
    ("text",  chunk)                       正文增量
    ("tool",  name, args_str, result_str)  一次工具执行完成
    ("meta",  {...})                       内部事件（force_retry / force_final 等）
    ("done",  final_text)                  整个循环结束

危险工具确认：
    对 DANGEROUS_TOOLS 中的工具（如 open_app / open_website），
    执行前会调用 confirm_tool 回调（由调用方注入），返回 False 则跳过执行。

【抗幻觉】三层机制：
    1. force_tool_use（首轮）→ 工具可解决的意图，第一轮带 tool_choice="required"
       服务端不支持时降级为 user-prompt 强制（见 LLMClient）
    2. force_retry（首轮）→ 首轮 force 后模型仍只回文字 → 注入强提示重试一次
    3. force_final（连续多轮纯调工具）→ 去掉 tools，强制模型给出 final answer
       避免「无限调工具不给最终回复」的退化行为
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import AsyncIterator, Callable, Optional

from app.brain.llm_client import LLMClient, detect_action_intent
from app.engine.tools import ToolRegistry

log = logging.getLogger(__name__)

MAX_TURNS = 6

# 需要用户确认的危险工具集合
DANGEROUS_TOOLS = {"open_app", "open_website"}

# 连续多少轮纯调工具（无文字）后强制进入 final 阶段
MAX_CONSECUTIVE_TOOL_ONLY_TURNS = 3

# 回复清洗：<think> 推理块 / 残留标记不进 UI 与 TTS
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_THINK_TAG_RE = re.compile(r"</?(?:think|reasoning)>", re.I)

# 防工具独白泄漏的 system 附加铁律（幂等标记）
_STYLE_RULE = (
    "\n【回复风格铁律】给主人的回复里严禁出现：工具名称（如 open_website/open_app）、"
    "对内部操作的描述（如「我应该使用XX工具」「这是要打开一个应用」「主人想打开XX，让我来」）、"
    "复述主人的指令、推理过程、<think> 标记。调用工具的那一轮**不要输出任何说明文字**"
    "（只给工具调用本身）；只有在不再调用工具、给主人最终答复时，才直接输出一句"
    "符合角色人设的自然中文。\n\n"
    "【自主使用工具（重要）】像一个能干的助手一样自主工作：先判断需要什么信息或操作，"
    "需要时就连续调用工具（搜索、查询、计算、打开应用/网页等），可以多轮调用、边用边分析，"
    "直到信息足够。当信息足够、且不再需要工具时，**直接基于工具结果给主人完整答案**：\n"
    "  - 必须把工具结果里的具体内容（具体名称、数字、结论、链接含义）写进答案，"
    "严禁只说「让我给主人推荐」「我查到了一些」这类空泛过渡话；\n"
    "  - 对已经执行成功的动作（打开应用/网页、设置提醒等），自然确认一句结果即可，"
    "不要再描述你将要做什么、该用什么工具；\n"
    "  - 信息不足或不确定时，应再调用一次工具确认，而不是直接编造或空泛回应。\n"
    "  - 给最终答复前，先核对工具是否真的成功、结果是否足以回答主人；工具失败/被取消要如实说明，绝不假装成功。\n"
)


def _sanitize_reply(text: str) -> str:
    """ReAct 回复清洗：复用 LLMClient 的清洗（think/emoji/空白/英文 CoT 剥离）。"""
    from app.brain.llm_client import sanitize_text
    return sanitize_text(text or "")


# 工具结果工具名 → 中文（用于空总结时的角色化兜底）
_TOOL_PRETTY = {
    "open_app": "打开应用",
    "open_website": "打开网页",
    "web_search": "联网搜索",
    "web_deep_search": "深度搜索",
    "add_reminder": "设置提醒",
    "add_todo": "添加待办",
    "set_timer": "设置计时器",
    "set_alarm": "设置闹钟",
    "take_screenshot": "截图",
    "screen_capture": "截图",
    "calculator": "计算",
    "get_system_info": "查看系统信息",
    "list_installed_apps": "查看已安装应用",
    "memory_search": "记忆检索",
    "recall_memory": "记忆检索",
}

# 常见应用别名（工具结果里是英文/通用名，回给主人时换成更自然的说法）
_APP_NICE = {
    "QQ": "QQ",
    "WeChat": "微信",
    "wechat": "微信",
    "微信": "微信",
    "Bilibili": "B站",
    "bilibili": "B站",
    "b站": "B站",
    "B站": "B站",
    "chrome": "浏览器",
    "Chrome": "浏览器",
    "浏览器": "浏览器",
}


def _tool_ack_sentence(name: str, args: str, result: str) -> str:
    """工具执行成功但模型没给文字时，按工具类型生成一句角色化确认。

    绝不回显原始 result（可能含 URL / 路径 / JSON）；取消 / 失败如实表达，
    只有确实成功才报喜，避免把"没成功"说成"搞定啦"。
    """
    result = result or ""
    try:
        a = json.loads(args or "{}")
    except Exception:
        a = {}
    pretty = _TOOL_PRETTY.get(name, name)

    # 用户取消
    if "取消" in result:
        return "好的主人，那就不弄啦~"

    # 工具失败 / 无结果：诚实告知，绝不假装成功
    if _tool_result_state(result) == "fail":
        if name == "open_app":
            target = _APP_NICE.get(str(a.get("app_name", "")).strip(),
                                   str(a.get("app_name", "")).strip())
            return f"好的，我尝试打开{target}了，不过好像没成功，主人帮我看看是不是名字不对？"
        if name == "open_website":
            return "好的，我尝试打开网页了，不过好像没成功，主人帮我看看网址对不对？"
        if name in ("web_search", "web_deep_search"):
            return f"唔，{a.get('query', '')}我这边没搜到结果，主人换个关键词试试？"
        if name in ("memory_search", "recall_memory"):
            return "好的主人，我翻了翻记忆，暂时没找到相关的内容~"
        if name == "add_reminder":
            return "好的，我试着帮主人记提醒了，不过好像没成功，主人再说一遍？"
        return f"好的，我尝试{pretty}了，不过好像没成功……"

    # —— 以下均为成功 ——
    if name == "open_app":
        target = _APP_NICE.get(str(a.get("app_name", "")).strip(),
                               str(a.get("app_name", "")).strip())
        return f"好的，已经帮主人打开{target}啦~"
    if name == "open_website":
        return "好的主人，已经帮你打开网页啦~"
    if name in ("web_search", "web_deep_search"):
        q = a.get("query", "")
        return f"好的主人，{pretty}的结果来啦~" if not q else f"好的主人，{q}的结果来啦~"
    if name == "add_reminder":
        t = a.get("time", "")
        c = a.get("content", "")
        return f"好的，已经帮主人设好提醒啦~{t} {c}"
    if name == "add_todo":
        c = a.get("content", "")
        return f"好的，已经记到待办里啦：{c}"
    if name in ("set_timer", "set_alarm"):
        return f"好的主人，{pretty}设好啦~"
    if name in ("take_screenshot", "screen_capture"):
        return "好的，已经截好图啦，主人看看桌面~"
    if name == "calculator":
        return f"好的，算好啦：{result}"
    if name in ("get_pet_status", "get_pet_stats"):
        return "好的主人，我看看自己的状态~"
    if name in ("memory_search", "recall_memory"):
        return "好的主人，我翻了翻记忆~"
    if name == "get_system_info":
        return "好的主人，系统信息来啦~"
    if name == "list_installed_apps":
        return "好的，主人电脑上的应用都在这啦~"
    if name == "send_notification":
        return "好的，已经帮主人弹通知啦~"
    return "好的，已经帮主人搞定啦~"


def _tool_result_state(result: str) -> str:
    """根据工具结果文本判定 'cancel' / 'fail' / 'ok'。"""
    r = result or ""
    if "取消" in r:
        return "cancel"
    if any(k in r for k in ("失败", "错误", "无法", "未安装", "未找到",
                            "没有找到", "找不到", "没找到", "未能", "为空")):
        return "fail"
    return "ok"


# 最终答案里「声称动作成功」的信号（用于和工具实际失败做矛盾检测）
_SUCCESS_CLAIM_WORDS = (
    "打开啦", "打开了", "打开好", "已打开", "成功", "搞定", "弄好", "设置好",
    "设好", "已设置", "设了", "启动啦", "启动了", "已启动", "找到了", "已经找到",
    "完成", "帮你打开", "帮你开", "上号", "进去就能", "截好", "算好", "搜好",
)
# 最终答案里「如实提及失败」的信号（出现则不算矛盾）
_HONEST_FAIL_WORDS = (
    "失败", "没能", "没法", "无法", "找不到", "没找到", "未找到", "没有找到",
    "未安装", "没安装", "没装", "取消", "抱歉", "出错", "错误", "打不开",
    "开不了", "没成功", "没打开", "没反应", "唔",
)

# 工具失败却报喜时的纠正提示
_CORRECTION_HINT = (
    "[系统·纠正] 你刚才的答复与实际工具结果矛盾：上面的工具其实没有成功"
    "（可能失败、未安装或被取消）。请不要假装成功。你可以换办法再调用合适的工具尝试"
    "（换关键词、换应用、换网址等），或者如实、自然地告诉主人操作没能完成、可能的原因是什么，"
    "不要再声称它成功了。"
)


def _final_contradicts_tools(final_text: str, executed_tools: list) -> bool:
    """检测「最后一个关键动作失败/取消，最终答案却报喜且不提失败」的矛盾。

    只看最后一个工具（通常是主人最关心的关键动作），且要求答案既无失败措辞、
    又含明确报喜词，保守判定，避免误伤「部分成功」或「先说明失败」的答复。
    """
    if not executed_tools:
        return False
    last_name, last_args, last_result = executed_tools[-1]
    if _tool_result_state(last_result) == "ok":
        return False
    if any(w in final_text for w in _HONEST_FAIL_WORDS):
        return False
    return any(w in final_text for w in _SUCCESS_CLAIM_WORDS)


def _format_verify_hint(executed_tools: list, force_final: bool = False) -> str:
    """工具执行后，引导模型核实结果并决定「继续调工具」还是「给最终总结」。"""
    lines = []
    for i, (name, _args, result) in enumerate(executed_tools, 1):
        state = _tool_result_state(result)
        mark = {"ok": "成功", "fail": "失败", "cancel": "已取消"}[state]
        if state == "cancel":
            lines.append(f"  {i}. {name}（{mark}）")
            continue
        brief = (result or "").replace("\n", " ").strip()
        # 去掉开头重复的状态词（"失败：/错误：/未找到应用…"），避免与括号状态重复
        brief = re.sub(
            r"^(?:错误|失败|无法|未能|未找到应用|未找到|没有找到|找不到)[：:，,。\s]*",
            "", brief).strip()
        if len(brief) > 60:
            brief = brief[:60] + "…"
        lines.append(f"  {i}. {name}（{mark}）：{brief}" if brief
                     else f"  {i}. {name}（{mark}）")
    tool_list = "\n".join(lines)
    if force_final:
        decision = (
            "你已经连续多轮调用工具，**不要再调用任何工具**，直接基于以上结果用中文给主人最终答复；"
            "若结果不足以回答，就如实说明你查到了什么、又有哪些没能查到，不要编造。"
        )
    else:
        decision = (
            "给最终答复前请先在内部核对（核对过程不要写进答复）：\n"
            "  1) 工具是否真的成功？有无失败/取消？\n"
            "  2) 结果是否足以回答主人的问题，有没有遗漏主人问的关键点？\n"
            "  3) 要写进答案的名称/数字/链接/时间是否都来自工具结果？没有结果支撑的一律不写。\n"
            "处理：\n"
            "  - 有工具失败/被取消 → 如实告诉主人，不要假装成功；可以换办法再试；\n"
            "  - 结果还不足以回答 → 继续调用合适的工具，不要急着下结论；\n"
            "  - 结果充分且成功 → 直接给主人完整、具体、有条理的中文总结。"
        )
    return f"[系统·结果核对] 本轮工具执行结果：\n{tool_list}\n{decision}"


class AgentLoop:
    def __init__(self, client: LLMClient, registry: ToolRegistry,
                 max_turns: int = MAX_TURNS,
                 confirm_tool: Optional[Callable[[str, str], bool]] = None):
        """
        Args:
            client: LLM 客户端
            registry: 工具注册表
            max_turns: 最大循环轮数
            confirm_tool: 危险工具执行前的确认回调，签名为 (tool_name, args_json) -> bool。
                          返回 False 表示用户拒绝执行。为 None 时不确认直接执行。
        """
        self.client = client
        self.registry = registry
        self.max_turns = max_turns
        self.confirm_tool = confirm_tool

    @staticmethod
    def _first_user_text(messages: list[dict]) -> str:
        """取最后一条 user 消息的文本（用于意图判定）。"""
        for m in reversed(messages):
            if m.get("role") == "user" and m.get("content"):
                return str(m["content"])
        return ""

    @staticmethod
    def _format_tool_results(messages: list[dict]) -> str:
        """把最近 N 条 ToolMessage 整理成文本片段，用于「强制 final」时的 user 提示。
        让模型知道前面调过哪些工具、结果是什么，方便总结。
        """
        bits: list[str] = []
        for m in messages[-10:]:
            if m.get("role") == "tool":
                content = (m.get("content") or "")[:200]
                bits.append(f"  - {content}")
        return "\n".join(bits) if bits else "（无）"

    async def _confirm_or_skip(self, name: str, args: str) -> Optional[str]:
        """对危险工具执行确认。返回 None 表示继续执行，返回字符串表示跳过（附带结果文本）。"""
        if name not in DANGEROUS_TOOLS or self.confirm_tool is None:
            return None
        try:
            confirmed = await asyncio.to_thread(self.confirm_tool, name, args)
            if not confirmed:
                return "用户取消了此操作。"
        except Exception as e:  # noqa: BLE001
            log.warning("确认回调异常：%s", e)
            return None  # 回调失败时不阻塞执行
        return None

    async def _execute_tools_parallel(
        self,
        tool_calls: list[dict],
        messages: list[dict],
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[tuple]:
        """并行执行所有工具调用，按原顺序 yield 结果。"""

        async def _exec_one(i: int, tc: dict) -> tuple[int, str, str, str]:
            """在后台线程中执行单个工具，返回 (index, name, args, result)。"""
            name = tc["name"] or f"unknown_{i}"
            args = tc["arguments"] or "{}"

            # 危险工具确认
            skip_result = await self._confirm_or_skip(name, args)
            if skip_result is not None:
                log.info("tool %s(%s) -> SKIPPED", name, args[:120])
                return i, name, args, skip_result

            # 在线程池中执行同步工具函数
            result = await asyncio.to_thread(self.registry.execute, name, args)
            log.info("tool %s(%s) -> %s", name, args[:120], result[:120])
            return i, name, args, result

        # 检查取消
        if cancel_check is not None and cancel_check():
            return

        # 并行执行所有工具
        tasks = [_exec_one(i, tc) for i, tc in enumerate(tool_calls)]
        results = await asyncio.gather(*tasks)

        # 按原始顺序 yield 结果
        for i, name, args, result in sorted(results, key=lambda x: x[0]):
            yield "tool", name, args, result
            messages.append({
                "role": "tool",
                "tool_call_id": tool_calls[i]["id"] or f"call_{i}",
                "content": result[:4000],
            })

    async def run(
        self,
        messages: list[dict],
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[tuple]:
        """真正的 ReAct 循环。

        关键设计：
        - 第 1 轮 force_tool_use（抗幻觉第 1 层）
        - 第 1 轮没调工具 + 有工具意图 → 注入 user-prompt 强制重试（抗幻觉第 2 层）
        - 连续多轮纯调工具无文字 → 去掉 tools 强制 final（抗幻觉第 3 层）
        """
        tools = self.registry.to_openai() if self.registry.names() else None
        final_text = ""
        # 累计「最近连续纯调工具、无文字」的轮数（用于抗幻觉第 3 层）
        consecutive_tool_only = 0
        # 整个循环中【已经成功执行】的工具调用 (name, args, result)。
        # 作用：① 区分「该调工具却没调」与「已调完工具、正在给最终答案」；
        #      ② 模型没给最终文字时按工具类型兜底一句角色化确认。
        tools_used = 0
        executed_tools: list[tuple[str, str, str]] = []
        verify_corrections = 0   # 「工具失败却报喜」的纠正次数（最多 1 次）

        # 【抗幻觉】第 1 层：意图识别 → 第一轮 force_tool_use
        intent = detect_action_intent(self._first_user_text(messages))
        force_first_turn = intent is not None
        if force_first_turn:
            log.info("AgentLoop: 检测到动作意图 %s，第一轮开启 force_tool_use",
                     intent)

        # 防工具独白泄漏：给 system 追加回复风格铁律（幂等）
        if messages and messages[0].get("role") == "system":
            base = str(messages[0].get("content") or "")
            if "回复风格铁律" not in base:
                messages[0]["content"] = base + _STYLE_RULE

        for turn in range(self.max_turns):
            if cancel_check is not None and cancel_check():
                break
            content_parts: list[str] = []
            tool_calls: list[dict] = []

            # 【抗幻觉】第 3 层：连续多轮纯调工具 → 去掉 tools 强制 final
            # 避免模型「调工具上瘾」一直不总结
            force_final = consecutive_tool_only >= MAX_CONSECUTIVE_TOOL_ONLY_TURNS
            if force_final:
                log.warning(
                    "AgentLoop: 已连续 %d 轮纯调工具无文字，强制进入 final 阶段（去掉 tools）",
                    consecutive_tool_only)
                yield ("meta", {"event": "force_final",
                                "reason": f"连续 {consecutive_tool_only} 轮纯调工具无文字",
                                "turn": turn})

            async for ev, data in self.client.chat_stream_events(
                    messages,
                    tools=None if force_final else tools,  # final 阶段不带 tools
                    cancel_check=cancel_check,
                    force_tool_use=(force_first_turn and turn == 0)):
                if ev == "text":
                    # 只缓存、不上屏：带工具意图的轮次里模型常先输出工具独白
                    # （「我应该使用XX工具…」），这类文本绝不能进聊天窗口/TTS，
                    # 否则多轮独白会拼成一条精神污染回复。确认本轮是最终回复后
                    # 在轮末一次性发出。
                    content_parts.append(data)
                elif ev == "finish":
                    if data.get("content") and not content_parts:
                        # 模型没走流式正文（罕见），兜底拿 finish 里的完整 content
                        content_parts.append(data["content"])
                    tool_calls = data.get("tool_calls") or []

            final_text = _sanitize_reply("".join(content_parts))

            # 【抗幻觉】第 2 层：识别到工具意图 + 【从头到尾还没成功调过任何工具】
            # + 本轮仍只回文字 → 注入强提示重试。
            # 关键：tools_used == 0 才算「该调没调」。一旦已经调过工具，模型本轮不再
            # 调工具而给出文字，就是在做最终总结——此时必须放行（走下面的 not tool_calls
            # 分支结束循环），绝不能再强令它「必须继续调工具」，否则模型永远无法基于
            # 工具结果给出最终答案（旧版会在工具后反复 force_retry 直到耗尽轮数）。
            if (force_first_turn and not tool_calls and tools_used == 0
                    and not (cancel_check and cancel_check())):
                # 剩余轮数不足以再走一次 force_retry 时（最后一轮），放弃
                if turn + 1 >= self.max_turns:
                    log.warning(
                        "AgentLoop: 已 %d 轮仍调不到工具，放弃（意图=%s）", turn + 1, intent)
                    if final_text:
                        yield "text", final_text
                    break
                log.warning(
                    "AgentLoop: 第 %d 轮仍未调工具（意图=%s），注入强提示重试", turn + 1, intent)
                yield ("meta", {"event": "force_retry",
                                "reason": f"第 {turn + 1} 轮未调用工具",
                                "intent": intent})
                messages.append({
                    "role": "user",
                    "content": (
                        "[系统提醒] 你刚才只回了文字但**没有调用任何工具**。"
                        f"主人要的是「{intent}」类操作（{self._first_user_text(messages)[:60]}），"
                        "必须调用对应工具（看上面 schema）。请立刻调用，不要再回文字。"
                    ),
                })
                consecutive_tool_only = 0
                continue   # 进入下一轮

            # 没调工具 → 本轮准备给最终答复。先核实结果：
            # 若关键动作失败/取消，模型却报喜且不提失败，给一次纠正重答机会。
            if not tool_calls:
                # 有剩余轮次 + 尚未纠正过 + 工具失败却报喜 → 让模型重答一次
                if (final_text and executed_tools and verify_corrections < 1
                        and _final_contradicts_tools(final_text, executed_tools)
                        and turn + 1 < self.max_turns):
                    verify_corrections += 1
                    log.warning("AgentLoop: 工具结果与最终答案矛盾（工具失败却报喜），要求重答")
                    yield ("meta", {"event": "verify_retry",
                                    "reason": "工具失败但答案报喜"})
                    # 回传模型自己的报喜答复，让它看到「错在哪」，再纠正
                    messages.append({"role": "assistant", "content": final_text})
                    messages.append({"role": "user", "content": _CORRECTION_HINT})
                    continue
                # 已纠正过模型仍报喜、或没有剩余轮次：用代码层诚实失败文案兜底
                if (final_text and executed_tools
                        and _final_contradicts_tools(final_text, executed_tools)):
                    ln, la, lr = executed_tools[-1]
                    honest = _tool_ack_sentence(ln, la, lr)
                    if honest:
                        log.warning("AgentLoop: 最终答复仍与工具结果矛盾，改用诚实失败文案")
                        final_text = honest
                if final_text:
                    yield "text", final_text
                break

            # 追加 assistant 的工具调用消息（OpenAI 格式）。
            # content 一律置空：工具轮模型常在 content 里写「我应该用XX工具」这类过程
            # 独白（尤其推理模型），把它回传会让模型下一轮延续这种自言自语、并最终漏进
            # 给主人的回复。带 tool_calls 的 assistant 消息 content 允许为空（OpenAI 协议）。
            messages.append({
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": tc["id"] or f"call_{i}",
                        "type": "function",
                        "function": {"name": tc["name"],
                                     "arguments": tc["arguments"] or "{}"},
                    }
                    for i, tc in enumerate(tool_calls)
                ],
            })

            # 并行执行所有工具调用
            async for event in self._execute_tools_parallel(
                    tool_calls, messages, cancel_check=cancel_check):
                yield event
                if event[0] == "tool":
                    # event = ("tool", name, args, result)
                    tools_used += 1
                    executed_tools.append((event[1], event[2], event[3]))

            # 统计「纯调工具无文字」连续次数
            if not final_text.strip():
                consecutive_tool_only += 1
            else:
                consecutive_tool_only = 0

            # 结果核实与决策：每批工具执行后，把「各工具成败 + 核对清单 + 决策」回传，
            # 让模型核实结果后自主决定：失败/不足 → 继续调工具或如实告知；充分 → 给总结。
            # 连续纯工具接近阈值（下一轮 force_final）时，要求直接总结、不再调工具。
            force_final_next = consecutive_tool_only >= MAX_CONSECUTIVE_TOOL_ONLY_TURNS - 1
            messages.append({
                "role": "user",
                "content": _format_verify_hint(executed_tools, force_final=force_final_next),
            })

        # 兜底：final_text 为空（模型调完工具却没给最终文字）。
        # 旧版直接取最后一条工具结果（常含 URL / JSON / 路径），会被输出清洗清空，
        # 主人看到空气泡。改为按「最后执行的工具」类型生成一句角色化确认。
        if not final_text.strip() and executed_tools:
            last_name, last_args, last_result = executed_tools[-1]
            final_text = _tool_ack_sentence(last_name, last_args, last_result)
            if final_text:
                yield "text", final_text

        yield "done", final_text
