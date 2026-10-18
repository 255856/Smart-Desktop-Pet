"""Agent 循环：让大模型通过工具调用真正「做事」。"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import AsyncIterator, Callable, Optional

from app.brain.llm_client import (LLMClient, count_request_actions,
                                  detect_action_intent)
from app.engine.tools import ToolRegistry

log = logging.getLogger(__name__)

MAX_TURNS = 6

# 需要用户确认的危险工具集合（不可逆 / 会打断用户 / 可能丢数据）
DANGEROUS_TOOLS = {"lock_screen", "kill_process", "run_script",
                   "set_wifi", "set_bluetooth", "shutdown_computer"}

# 连续多少轮纯调工具（无文字）后强制进入 final 阶段
MAX_CONSECUTIVE_TOOL_ONLY_TURNS = 3

# 同一个工具连续失败多少次后，本轮对话内不再提供它
_MAX_SAME_TOOL_FAIL = 2

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
    "  - 给最终答复前，先核对工具是否真的成功、结果是否足以回答主人；工具失败/被取消要如实说明，绝不假装成功。\n\n"
    "【多步任务拆解（重要）】主人的一句话往往包含**不止一个要求**"
    "（例如「查一下北京天气，然后帮我加到日程」「打开QQ顺便看看今天日程」）。"
    "收到这类请求时：\n"
    "  1) 先在心里拆成几步，列出还缺哪些信息；\n"
    "  2) 一次只推进一步，每步用工具拿到**真实结果**；\n"
    "  3) **不要在第一步拿到结果后就直接收尾**——只要还有任何一步没做、"
    "或任何一步的结果还不确定，就继续调用工具往下推进；\n"
    "  4) 前一步失败时，换个工具/换个参数再试，而不是降级成一句「我查不了」。\n"
    "  判断标准很简单：**主人的每个要求都落实了吗？** 没落实完就不算答完。\n"
)


def _sanitize_reply(text: str) -> str:
    """ReAct 回复清洗：复用 LLMClient 的清洗（think/emoji/空白/英文 CoT 剥离）。"""
    from app.brain.llm_client import sanitize_text
    return sanitize_text(text or "")


# 模型输出被清洗规则剥光、且无工具结果可兜底时说的话术（宁可给一句自然的「再来一次」也不要空气泡）
_EMPTY_REPLY_FALLBACK = "唔……刚刚没想好怎么说，主人再说一遍好不好？"

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


# 「用户主动取消」的判定标记（不能用裸「取消」子串，会和正常结果里的「取消静音」等词冲突）
_CANCEL_MARKERS = (
    "用户取消", "取消了此操作", "取消了该操作", "已取消操作",
    "被取消", "取消执行", "用户拒绝", "主人拒绝", "拒绝执行",
)


def _is_user_cancel(result: str) -> bool:
    return any(k in (result or "") for k in _CANCEL_MARKERS)


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

    # 用户主动取消
    if _is_user_cancel(result):
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


# 工具结果里代表「没做成」的关键词（任一命中 → 判 fail）
_TOOL_FAIL_MARKERS = (
    "失败", "错误", "无法", "未安装", "未找到",
    "没有找到", "找不到", "没找到", "未能", "为空",
    "没安装", "没装", "不支持", "未开启", "不可用", "未连接",
    "钱不够", "没有叫", "没成功", "超时", "未执行", "未响应",
    "未改变", "未生效", "没改变", "不支持",
)


def _tool_result_state(result: str) -> str:
    """根据工具结果文本判定 'cancel' / 'fail' / 'ok'。

    判定依据两条，任一命中即 fail：
      1. 带 `错误：` 前缀（docs/tools-reference.md 定的工具契约）；
      2. 命中失败关键词（兜底，照顾历史文案和第三方后端）。
    """
    r = result or ""
    if _is_user_cancel(r):
        return "cancel"
    if r.lstrip().startswith("错误"):
        return "fail"
    if any(k in r for k in _TOOL_FAIL_MARKERS):
        return "fail"
    return "ok"


# 最终答案里「声称动作成功」的信号（用于和工具实际失败做矛盾检测）
_SUCCESS_CLAIM_WORDS = (
    "打开啦", "打开了", "打开好", "已打开", "成功", "搞定", "弄好", "设置好",
    "设好", "已设置", "设了", "启动啦", "启动了", "已启动", "找到了", "已经找到",
    "完成", "帮你打开", "帮你开", "上号", "进去就能", "截好", "算好", "搜好",
    "帮主人打开", "帮主人开", "帮您打开", "帮您开",
    "已经打开", "已经启动", "已经设好", "已经记好",
    "记好啦", "记下啦", "截好啦", "算好啦", "搜好啦", "调好啦", "调好",
)
# 「动词+宾语+完成语气」句式（字面词表覆盖不到，用正则兜）
_ACTION_VERBS = (
    "打开|启动|开启|运行|调出|拉起|设置好|设好|记好|记下|记住|删掉|删除"
    "|截好图?|截图|搜到|查到|算好|调好|发送|复制好|静音|锁屏|添加好|保存好"
)
# 本身就带完成义的「已 XX」，不需要再跟语气词
_ACTION_DONE = (
    "已保存|已发送|已添加|已启动|已设置|已打开|已记录|已复制|已删除|已截"
)
# 成功断言：动作动词 + 宾语 + 完成语气（语气词必须出现，避免条件句误判）
_SUCCESS_CLAIM_RE = re.compile(
    rf"(?:{_ACTION_VERBS})[^，。！？!?、\n]{{0,20}}?"
    r"(?:啦|了|好了|成功|哦|喔|咯)"
    rf"|(?:{_ACTION_DONE})"
)
# 「我替主人做了某个动作」的断言（额外要求带主语，避免条件句误判）
_ACTION_CLAIM_RE = re.compile(
    r"(?:已经|已|刚|这就|这就给主人)?\s*"
    r"(?:帮你|帮主人|帮您|我|给主人)?\s*"
    rf"(?:{_ACTION_VERBS})[^，。！？!?、\n]{{0,20}}?"
    r"(?:啦|了|好了|成功|哦|喔|咯)"
    rf"|(?:{_ACTION_DONE})"
)
# 最终答案里「如实提及失败」的信号（出现则不算矛盾）
# 注意：不能放「唔」——它是小汐的口头禅，放进来会让校验器自我静音
_HONEST_FAIL_WORDS = (
    "失败", "没能", "没法", "无法", "找不到", "没找到", "未找到", "没有找到",
    "未安装", "没安装", "没装", "取消", "抱歉", "出错", "错误", "打不开",
    "开不了", "没成功", "没打开", "没反应", "不支持", "没办到",
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

    只看最后一个工具（通常是主人最关心的关键动作），保守判定。
    """
    if not executed_tools:
        return False
    last_name, last_args, last_result = executed_tools[-1]
    if _tool_result_state(last_result) == "ok":
        return False
    if any(w in final_text for w in _HONEST_FAIL_WORDS):
        return False
    return _claims_success(final_text)


def _claims_success(text: str) -> bool:
    """文本是否在断言「某件事已经做完」（纯格式判断）。"""
    if not text:
        return False
    if any(w in text for w in _HONEST_FAIL_WORDS):
        return False
    return any(w in text for w in _SUCCESS_CLAIM_WORDS) or bool(
        _SUCCESS_CLAIM_RE.search(text))


def _claims_unbacked_action(text: str) -> bool:
    """文本是否在断言「我替主人做了某个动作」，而这一轮**没有调过任何工具**。

    判据很硬：没调过工具，就不可能真的做过打开/设置/删除这类事。
    """
    if not text:
        return False
    if any(w in text for w in _HONEST_FAIL_WORDS):
        return False
    return bool(_ACTION_CLAIM_RE.search(text))


# 未执行任何工具却要报喜时的纠正提示
_NO_TOOL_CLAIM_HINT = (
    "[系统·纠正] 你刚才声称已经完成了某个操作，但**这一轮你没有调用过任何工具**，"
    "所以那件事根本没有发生——主人什么都没看到。请不要凭空声称成功。\n"
    "现在二选一：\n"
    "  1) 如果这件事需要工具，就立刻调用对应的工具（看上面 schema）；\n"
    "  2) 如果只是聊天/回答问题，就不要用「已经打开/设置好/记住了」这类说法，"
    "直接正常回答主人的问题。"
)

# 未执行任何工具、且轮数耗尽时的诚实兜底
_NO_TOOL_FALLBACK = "唔……主人，刚刚那个操作没能完成，主人再说一遍好不好？~"


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
            "  2) 把主人的原话拆成一个个具体要求，逐条对照工具结果——"
            "**只要还有任何一条没被工具结果覆盖，就说明没答完**；\n"
            "  3) 要写进答案的名称/数字/链接/时间是否都来自工具结果？没有结果支撑的一律不写。\n"
            "处理（默认倾向是「还不够，再调一次」）：\n"
            "  - 有工具失败/被取消 → 换个工具、换个参数再试一次；实在不行才如实说明；\n"
            "  - 只调用了一个工具，但主人的要求有多项 → **继续调用其余工具**，"
            "不要用这一条结果概括整件事；\n"
            "  - 结果缺关键信息（时间/地点/名称/数值） → 继续调用工具补齐，"
            "凭印象填空是严重错误；\n"
            "  - 确认每条要求都有工具结果支撑 → 才给主人完整、具体、有条理的中文总结。"
        )
    return f"[系统·结果核对] 本轮工具执行结果：\n{tool_list}\n{decision}"


# 意图 → 本轮下发的工具白名单
# 全量下发 52 个工具时模型容易选错；按意图裁到 8~15 个收窄选择空间
# 识别不出意图时照旧全量下发，不牺牲任何能力
_INTENT_TOOL_NAMES: dict[str, tuple[str, ...]] = {
    "open_app": (
        "open_app", "list_installed_apps", "open_website",
        "open_file_explorer", "open_terminal", "open_notepad",
        "list_desktop_files", "read_text_file", "get_current_time",
        "get_weather",
    ),
    "add_reminder": (
        "add_reminder", "list_reminders", "delete_reminder",
        "get_current_time", "date_info", "get_weather", "say_to_user",
    ),
    "remember_fact": (
        "remember_fact", "recall_memory", "forget_memory",
        "get_current_time", "get_pet_status", "date_info",
    ),
    "query": (
        "get_current_time", "get_pet_status", "system_info", "get_battery",
        "get_volume", "get_brightness", "calculate", "convert_units",
        "date_info", "take_screenshot", "get_clipboard",
        "list_installed_apps", "list_processes", "get_tts_mute_status",
        "get_weather",
    ),
    "get_weather": (
        "get_weather", "get_current_time", "date_info", "say_to_user",
        "web_search",
    ),
    "web_search": (
        "web_search", "fetch_url_text", "open_website", "ocr_image",
        "read_text_file", "get_current_time", "list_installed_apps",
        "get_weather",
    ),
    "aw_query": (
        "mcp_aw_aw_status",
        "mcp_aw_aw_get_recent_activities",
        "mcp_aw_aw_get_current_window",
        "mcp_aw_aw_get_browser_tabs",
    ),
}

# 意图下需要硬拒的工具（白名单是软约束，这里强制拒绝并说明原因）
_INTENT_BLOCKED_TOOLS: dict[str, dict[str, str]] = {
    "add_reminder": {
        "countdown": ("错误：本轮不提供 countdown。主人要的是「闹钟/提醒」，"
                      "请改用 add_reminder —— 它会存进 reminders.json，"
                      "桌宠重启后还在，也能取消。countdown 只活在内存里，一关就没。"),
        "list_countdowns": "错误：本轮不提供倒计时相关工具，请改用 list_reminders。",
        "cancel_countdown": "错误：本轮不提供倒计时相关工具，请改用 list_reminders / delete_reminder。",
    },
}


class AgentLoop:
    def __init__(self, client: LLMClient, registry: ToolRegistry,
                 max_turns: int = MAX_TURNS,
                 confirm_tool: Optional[Callable[[str, str], bool]] = None,
                 level_getter: Optional[Callable[[], int]] = None):
        """
        Args:
            client: LLM 客户端
            registry: 工具注册表
            max_turns: 最大循环轮数
            confirm_tool: 危险工具执行前的确认回调，签名为
                          (tool_name, args_json) -> Optional[bool]。
                          True=放行 / False=主人拒绝 / None=弹窗超时未响应。
                          三者都表示「不执行」时返回跳过结果文本。
                          为 None 时不确认直接执行。
            level_getter: 返回当前桌宠等级 int 的回调。LLM 工具列表按等级过滤,
                          未解锁工具直接拦截 (返回「等级不够」文本)。
                          为 None 时所有工具可见可调,兼容旧版。
        """
        self.client = client
        self.registry = registry
        self.max_turns = max_turns
        self.confirm_tool = confirm_tool
        self.level_getter = level_getter

    def _current_level(self) -> int:
        """读当前桌宠等级,无 level_getter 时按 Lv10 处理 (全部可见)。"""
        if self.level_getter is None:
            return 10
        try:
            lvl = int(self.level_getter())
        except Exception:  # noqa: BLE001
            return 10
        if lvl < 1:
            return 1
        if lvl > 10:
            return 10
        return lvl

    def _select_tools(self, intent: Optional[str],
                      all_tools: Optional[list[dict]]) -> Optional[list[dict]]:
        """按意图裁剪本轮下发的工具列表。

        - intent 为 None（识别不出要做什么）→ 原样全量返回，不丢能力；
        - 意图命中白名单 → 只保留白名单里**注册表确实存在**的工具；
        - 裁完为空 / 注册表里一个都没有 → 退回全量（宁可多给也不能把路堵死）。
        """
        if not all_tools:
            return all_tools
        wanted = _INTENT_TOOL_NAMES.get(intent or "")
        if not wanted:
            return all_tools
        allow = set(wanted)
        picked = [t for t in all_tools
                  if (t.get("function") or {}).get("name") in allow]
        return picked or all_tools

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
        except Exception as e:  # noqa: BLE001
            log.warning("确认回调异常：%s", e)
            return None  # 回调失败时不阻塞执行
        # 确认回调三态：True=放行 / False=主人拒绝 / None=弹窗超时没人管
        # 超时必须和「主人点了否」分开，否则会对主人谎称「你取消了」
        if confirmed is None:
            log.warning("tool %s 确认超时，未执行", name)
            return "确认弹窗超时未响应，未执行。"
        if not confirmed:
            return "用户取消了此操作。"
        return None

    async def _execute_tools_parallel(
        self,
        tool_calls: list[dict],
        messages: list[dict],
        cancel_check: Optional[Callable[[], bool]] = None,
        intent: Optional[str] = None,
        failed_tools: Optional[dict[str, int]] = None,
    ) -> AsyncIterator[tuple]:
        """并行执行所有工具调用，按原顺序 yield 结果。"""
        blocked = _INTENT_BLOCKED_TOOLS.get(intent or "", {})
        # 同一工具反复失败的累计次数（由 run() 持有，跨轮次）
        failed_tools = failed_tools if failed_tools is not None else {}

        async def _exec_one(i: int, tc: dict) -> tuple[int, str, str, str]:
            """在后台线程中执行单个工具，返回 (index, name, args, result)。"""
            name = tc["name"] or f"unknown_{i}"
            args = tc["arguments"] or "{}"

            # 工具解锁闸：未达等级的工具有可见不可调,提示主人升级
            from app.engine.tools._unlock import is_unlocked, level_for_tool
            current_lvl = self._current_level()
            if level_for_tool(name) is not None and not is_unlocked(name, current_lvl):
                req_lvl = level_for_tool(name)
                msg = (f"错误：{name} 需要升级到 Lv.{req_lvl} 才能使用。"
                       f"当前等级 Lv.{current_lvl}。请告诉主人升级到对应等级后再试，"
                       "或在聊天窗口顶部点「工具解锁」查看进度。")
                log.warning("AgentLoop: %s 未解锁 (level=%d < required %d)",
                            name, current_lvl, req_lvl)
                return i, name, args, msg

            # 意图级硬闸：白名单是软约束，这里硬拒并说明原因
            if name in blocked:
                msg = blocked[name]
                log.warning("AgentLoop: 意图 %s 下拒绝调用 %s（已改用正确工具）",
                            intent, name)
                return i, name, args, msg

            # 失败次数硬闸：同一个工具已经失败 _MAX_SAME_TOOL_FAIL 次就别再试了
            if failed_tools.get(name, 0) >= _MAX_SAME_TOOL_FAIL:
                msg = (f"错误：{name} 已经连续失败 {failed_tools[name]} 次，"
                       "本轮不再提供它。请不要再调用同一个工具换参数重试，"
                       "改用别的工具，或直接如实告诉主人这一步做不到、为什么。")
                log.warning("AgentLoop: %s 已失败 %d 次，拒绝再次调用",
                            name, failed_tools[name])
                return i, name, args, msg

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
            # 累计失败次数（供下一轮 _exec_one 的失败闸使用）
            if (_tool_result_state(result) == "fail"
                    and not _is_user_cancel(result)):
                failed_tools[name] = failed_tools.get(name, 0) + 1
            elif name in failed_tools:
                failed_tools[name] = 0   # 成功了就把计数清零
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
        """ReAct 循环：意图识别 → force_tool_use → 工具执行 → 结果核实 → 最终答复。

        抗幻觉三层：
          1) 第 1 轮 force_tool_use
          2) 没调工具 + 有工具意图 → 重试
          3) 连续多轮纯调工具无文字 → 强制 final
        """
        if not self.registry.names():
            all_tools = None
        elif hasattr(self.registry, "to_openai_for_level"):
            all_tools = self.registry.to_openai_for_level(self._current_level())
        else:
            all_tools = self.registry.to_openai()
        final_text = ""
        # 累计「最近连续纯调工具、无文字」的轮数
        consecutive_tool_only = 0
        # 已经成功执行的工具调用 (name, args, result)，用于矛盾检测和兜底
        tools_used = 0
        executed_tools: list[tuple[str, str, str]] = []
        verify_corrections = 0   # 「工具失败却报喜」的纠正次数（最多 1 次）
        partial_retries = 0     # 「多步任务只做一半」的纠正次数（最多 1 次）
        # 同一工具连续失败次数（跨轮次）
        failed_tools: dict[str, int] = {}
        # 模型的原始输出被清洗规则全部剥掉（过度清洗）时置 True
        over_sanitized = False

        # 抗幻觉第 1 层：意图识别 → 第一轮 force_tool_use
        first_user_text = self._first_user_text(messages)
        intent = detect_action_intent(first_user_text)
        force_first_turn = intent is not None
        if force_first_turn:
            log.info("AgentLoop: 检测到动作意图 %s，第一轮开启 force_tool_use",
                     intent)

        # 多步任务：主人这句话里并列了几件事，用于识别「只做了一半」
        required_actions = count_request_actions(first_user_text)
        if required_actions >= 2:
            log.info("AgentLoop: 主人这句话含 %d 个并列要求，"
                     "本轮若只完成一部分会纠正一次", required_actions)

        # 工具裁剪：按意图只下发相关工具；识别不出意图就全量下发
        tools = self._select_tools(intent, all_tools)
        if all_tools and tools is not None and len(tools) < len(all_tools):
            log.info("AgentLoop: 意图 %s → 本轮只下发 %d/%d 个工具 %s",
                     intent, len(tools), len(all_tools),
                     [t["function"]["name"] for t in tools])

        # 防工具独白泄漏：给 system 追加回复风格铁律（幂等）
        if messages and messages[0].get("role") == "system":
            base = str(messages[0].get("content") or "")
            if "回复风格铁律" not in base:
                messages[0]["content"] = base + _STYLE_RULE
            # 工具解锁状态行:让 LLM 知道"有更高级工具但现在用不了"
            # 没注入 level_getter 的旧路径(测试 / 无解锁环境)不追加,保持原行为
            if self.level_getter is not None:
                from app.engine.tools._unlock import MCP_DEFAULT_LEVEL
                lvl = self._current_level()
                try:
                    visible_n = len(self.registry.names(lvl))
                    total_n = len(self.registry.names())
                except TypeError:
                    visible_n = total_n = len(self.registry.names())
                unlock_line = (
                    f"\n\n【工具解锁】当前等级 Lv.{lvl}。"
                    f"可见工具 {visible_n} / 全部 {total_n} 个。"
                    "更高级别才能用更高级工具（如联网、写文件、生成图片、系统控制等），"
                    "调用未解锁工具会被自动拦截并返回'等级不够'提示。"
                    "不要假装调用成功,直接告诉主人当前等级不能做这件事并提示升级。"
                )
                if MCP_DEFAULT_LEVEL and lvl < MCP_DEFAULT_LEVEL:
                    unlock_line += f"外部扩展 (MCP) 需要 Lv.{MCP_DEFAULT_LEVEL} 才能用。"
                if "工具解锁" not in base:
                    messages[0]["content"] += unlock_line

        for turn in range(self.max_turns):
            if cancel_check is not None and cancel_check():
                break
            content_parts: list[str] = []
            tool_calls: list[dict] = []

            # 抗幻觉第 3 层：连续多轮纯调工具 → 去掉 tools 强制 final
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
                    tools=None if force_final else tools,
                    cancel_check=cancel_check,
                    force_tool_use=force_first_turn):
                if ev == "text":
                    # 只缓存、不上屏：避免模型先输出工具独白（「我应该使用XX工具…」）被多轮拼成精神污染
                    content_parts.append(data)
                elif ev == "reasoning":
                    # 推理模型思考内容 → UI 折叠的「思考过程」块
                    if data:
                        yield ("meta", {"kind": "thought", "text": str(data)})
                elif ev == "finish":
                    # v31: 抓 LLM API 真实 token usage (精确值, 替代 "字符数/4" 估算),
                    # meta 事件透传给 chat_window 状态栏显示。某些 API (老 ollama
                    # / 某些本地) 不返 usage → 上层 got_real=False 走 fallback。
                    if isinstance(data, dict) and data.get("usage"):
                        yield ("meta", {"event": "usage",
                                        **data["usage"]})
                    if data.get("content") and not content_parts:
                        content_parts.append(data["content"])
                    tool_calls = data.get("tool_calls") or []

            raw_text = "".join(content_parts)
            final_text = _sanitize_reply(raw_text)
            # 清洗规则把模型输出全剥掉了（过度清洗）。记录下来，循环结束后不能让主人面对空气泡
            if raw_text.strip() and not final_text.strip():
                over_sanitized = True
                log.info("AgentLoop: 模型输出被清洗规则全部剥离（过度清洗），"
                         "raw=%r", raw_text[:120])

            # 抗幻觉第 2 层：第 1 轮没调工具 → 重试
            # 关键：tools_used == 0 才算「该调没调」，调过工具后给文字就是最终总结
            if (force_first_turn and not tool_calls and tools_used == 0
                    and not (cancel_check and cancel_check())):
                # 剩余轮数不足（最后一轮），放弃
                if turn + 1 >= self.max_turns:
                    log.warning(
                        "AgentLoop: 已 %d 轮仍调不到工具，放弃（意图=%s）", turn + 1, intent)
                    if final_text:
                        if _claims_unbacked_action(final_text):
                            log.warning("AgentLoop: 重试耗尽，丢弃未验证的报喜话术")
                            final_text = _NO_TOOL_FALLBACK
                        yield "text", final_text
                    break
                log.warning(
                    "AgentLoop: 第 %d 轮仍未调工具（意图=%s），重试", turn + 1, intent)
                yield ("meta", {"event": "force_retry",
                                "reason": f"第 {turn + 1} 轮未调用工具",
                                "intent": intent})
                # force_tool_use 贯穿全程，由 LLMClient 统一发提示，这里只发 UI 事件
                consecutive_tool_only = 0
                continue

            # 多步任务只做了一半：要求数 > 已执行工具数 → 给一次纠正机会
            if (force_first_turn and not tool_calls
                    and required_actions >= 2
                    and tools_used >= 1
                    and len({n for n, _, _ in executed_tools}) < required_actions
                    and partial_retries < 1
                    and turn + 1 < self.max_turns
                    and not (cancel_check and cancel_check())):
                partial_retries += 1
                done = sorted({n for n, _, _ in executed_tools})
                log.warning(
                    "AgentLoop: 主人要求 %d 件事，本轮只执行了 %d 个（%s），"
                    "疑似只做一半，要求继续", required_actions, len(done), done)
                yield ("meta", {"event": "partial_retry",
                                "reason": f"{len(done)}/{required_actions} 项已完成",
                                "required": required_actions,
                                "done": done})
                consecutive_tool_only = 0
                continue

            # 没调工具 → 本轮准备给最终答复。先核实结果
            if not tool_calls:
                # 零工具执行却声称做了动作 = 100% 幻觉
                if (final_text and tools_used == 0
                        and _claims_unbacked_action(final_text)):
                    if verify_corrections < 1 and turn + 1 < self.max_turns:
                        verify_corrections += 1
                        log.warning("AgentLoop: 未调用任何工具却声称动作成功，要求重答")
                        yield ("meta", {"event": "verify_retry",
                                        "reason": "未调用工具却声称成功"})
                        messages.append({"role": "assistant", "content": final_text})
                        messages.append({"role": "user", "content": _NO_TOOL_CLAIM_HINT})
                        continue
                    log.warning("AgentLoop: 零工具仍声称成功，改用诚实兜底话术")
                    final_text = _NO_TOOL_FALLBACK
                # 工具失败却报喜 → 让模型重答一次
                elif (final_text and executed_tools and verify_corrections < 1
                        and _final_contradicts_tools(final_text, executed_tools)
                        and turn + 1 < self.max_turns):
                    verify_corrections += 1
                    log.warning("AgentLoop: 工具结果与最终答案矛盾（工具失败却报喜），要求重答")
                    yield ("meta", {"event": "verify_retry",
                                    "reason": "工具失败但答案报喜"})
                    messages.append({"role": "assistant", "content": final_text})
                    messages.append({"role": "user", "content": _CORRECTION_HINT})
                    continue
                # 已纠正过仍报喜 / 无剩余轮次：用代码层诚实失败文案兜底
                elif (final_text and executed_tools
                        and _final_contradicts_tools(final_text, executed_tools)):
                    ln, la, lr = executed_tools[-1]
                    honest = _tool_ack_sentence(ln, la, lr)
                    if honest:
                        log.warning("AgentLoop: 最终答复仍与工具结果矛盾，改用诚实失败文案")
                        final_text = honest
                if final_text:
                    yield "text", final_text
                break

            # 追加 assistant 的工具调用消息（OpenAI 格式）
            # content 一律置空，避免把工具轮的独白回传后漏进给主人的回复
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
                    tool_calls, messages, cancel_check=cancel_check,
                    intent=intent, failed_tools=failed_tools):
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

            # 结果核实与决策：每批工具执行后，把核对清单回传
            force_final_next = consecutive_tool_only >= MAX_CONSECUTIVE_TOOL_ONLY_TURNS - 1
            messages.append({
                "role": "user",
                "content": _format_verify_hint(executed_tools, force_final=force_final_next),
            })

        # 兜底一：final_text 为空，按最后工具类型生成角色化确认
        if not final_text.strip() and executed_tools:
            last_name, last_args, last_result = executed_tools[-1]
            final_text = _tool_ack_sentence(last_name, last_args, last_result)
            if final_text:
                yield "text", final_text

        # 兜底二：模型输出被过度清洗且无工具兜底
        if not final_text.strip() and over_sanitized:
            final_text = _EMPTY_REPLY_FALLBACK
            log.warning("AgentLoop: 输出被过度清洗且无工具兜底，使用空回复话术")
            yield "text", final_text

        # 兜底三：无条件兜底（避免主人面对空气泡）
        if not final_text.strip():
            final_text = _EMPTY_REPLY_FALLBACK
            log.warning("AgentLoop: 所有路径都没产出文字，使用空回复兜底话术")
            yield "text", final_text

        yield "done", final_text

