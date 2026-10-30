"""Agent 循环：让大模型通过工具调用真正「做事」。"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import time
from typing import AsyncIterator, Callable, Optional

from app.brain.llm_client import (COT_ONLY_RETRY_HINT as _COT_ONLY_RETRY_HINT,
                                  COT_ONLY_RETRY_MARK as _COT_ONLY_RETRY_MARK,
                                  COT_ONLY_RETRY_MAX_TOKENS as _COT_ONLY_RETRY_MAX_TOKENS,
                                  LLMClient, MAX_COT_ONLY_RETRIES as _MAX_COT_ONLY_RETRIES,
                                  _build_full_system_prompt,
                                  count_request_actions, detect_action_intent,
                                  is_low_cjk_reply)
from app.brain.prompt_layer import (build_dynamic_block, build_system_prompt,
                                    build_tool_handbook, load_style_guide)
from app.engine.tools import ToolRegistry
from app.engine.tools._core import (TOOL_CALL_TIMEOUT_SECONDS,
                                  TOOL_PHASE_BUDGET_SECONDS, TOOL_RESULT_MAX_CHARS,
                                  ToolResult, classify_legacy_result,
                                  classify_tool_result, fit_tool_payload)

log = logging.getLogger(__name__)

MAX_TURNS = 6

# 需要用户确认的危险工具集合（不可逆 / 会打断用户 / 可能丢数据）
DANGEROUS_TOOLS = {"lock_screen", "kill_process", "run_script",
                   "set_wifi", "set_bluetooth", "shutdown_computer"}

# 连续多少轮纯调工具（无文字）后强制进入 final 阶段
MAX_CONSECUTIVE_TOOL_ONLY_TURNS = 3

# 「该调工具却只给文字」的重试上限（最多重试几次）
# 原来可以连重试 5 轮（MAX_TURNS=6）：第 2..6 次带的几乎是同一个请求
# （强制提示由 LLMClient 统一注入，tool_choice 降级还按模型名缓存了），
# 等于把同一个请求重复发 5 次——费钱且慢（实测带历史时可达 5 轮 / 12 秒）。
_MAX_FORCE_RETRIES = 1

# 「中文占比过低」的重试上限（Stage 1.3）：最多重问一次，绝不连发
_MAX_LOW_CJK_RETRIES = 1

# 重问时给模型的提示：上一条回复几乎不是中文
_LOW_CJK_RETRY_HINT = "上一条回复几乎全是英文，请只用简体中文重新回答一遍。"

# 「整轮只有思维链」的重问参数（Stage 2.10）—— **跟 standard 后端共用**
# （定义在 app/brain/llm_client.py）：判据、提示语、标记、预算只有一份，
# 两个后端的重问行为完全一致。上面的 import 已经把老名字接住了。

# 同一个工具连续失败多少次后，本轮对话内不再提供它
_MAX_SAME_TOOL_FAIL = 2

# 回复清洗：<think> 推理块 / 残留标记不进 UI 与 TTS
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_THINK_TAG_RE = re.compile(r"</?(?:think|reasoning)>", re.I)

# 防工具独白泄漏的 system 附加铁律（幂等标记）
#
# Stage 1.1：_STYLE_RULE 已搬进共享层 app/brain/prompt_layer.py，并按
# Stage 1.6 拆成两段：
#   【说话方式】    —— 新增，跟 characters/style-guide.md 配套
#   【任务执行协议】—— 原铁律内容收窄到「确实有工具意图时」
# ReAct 与 standard 两个后端都从共享层取，system prompt 才长得一样。


def _sanitize_reply(text: str) -> str:
    """ReAct 回复清洗：复用 LLMClient 的清洗（think/emoji/空白/英文 CoT 剥离）。"""
    from app.brain.llm_client import sanitize_text
    return sanitize_text(text or "")


def _supports_max_tokens(client) -> bool:
    """这个 client 的 chat_stream_events 支不支持 max_tokens= 参数。

    测试里的假 client 大多没有这个参数 —— 不支持就别传（免得 TypeError）。
    """
    fn = getattr(client, "chat_stream_events", None)
    if fn is None:
        return False
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):  # noqa: BLE001
        return False
    if "max_tokens" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD
               for p in params.values())


# 模型输出被清洗规则剥光、且无工具结果可兜底时说的话术（宁可给一句自然的「再来一次」也不要空气泡）
#
# Stage 1.3：固定一句会让桌宠反复说同一句话 —— 改成多条**轮换**。
# _EMPTY_REPLY_FALLBACK 保留为第一条（历史 import 不断），完整话术池见 _EMPTY_REPLY_FALLBACKS。
_EMPTY_REPLY_FALLBACKS = (
    "唔……刚刚没想好怎么说，主人再说一遍好不好？",
    "呜……小汐卡壳啦，主人再叫一次好不好嘛~",
    "诶？刚刚走神了喵……主人再说一遍好不好？",
    "唔喵……脑内一片空白，主人稍等一下下~",
)
_EMPTY_REPLY_FALLBACK = _EMPTY_REPLY_FALLBACKS[0]

# 轮换指针（进程内）：保证连续两次兜底不会说同一句
_EMPTY_REPLY_FALLBACK_INDEX = 0


def _next_empty_reply_fallback() -> str:
    """取下一条空回复兜底话术（轮换；连续两次不同）。"""
    global _EMPTY_REPLY_FALLBACK_INDEX
    line = _EMPTY_REPLY_FALLBACKS[
        _EMPTY_REPLY_FALLBACK_INDEX % len(_EMPTY_REPLY_FALLBACKS)]
    _EMPTY_REPLY_FALLBACK_INDEX += 1
    return line

# 工具结果工具名 → 中文（用于空总结时的角色化兜底）
#
# ⚠️ 键必须是**注册表里的真名**（见 app/engine/tools/_core.py 的注册表）。
# 以前这里混着一批早就不存在的名字（take_screenshot / calculator /
# get_system_info / memory_search / web_deep_search / set_timer / set_alarm /
# add_todo …），它们让「空总结兜底」永远走不到对应分支 —— 死路径。
_TOOL_PRETTY = {
    "open_app": "打开应用",
    "open_website": "打开网页",
    "web_search": "联网搜索",
    "add_reminder": "设置提醒",
    "take_screenshot_tool": "截图",
    "calculate": "计算",
    "system_info": "查看系统信息",
    "list_installed_apps": "查看已安装应用",
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


def _tool_ack_sentence(name: str, args: str, result) -> str:
    """工具执行成功但模型没给文字时，按工具类型生成一句角色化确认。

    绝不回显原始 result（可能含 URL / 路径 / JSON）；取消 / 失败如实表达，
    只有确实成功才报喜，避免把"没成功"说成"搞定啦"。

    result 可以是结构化信封（ToolResult）或裸字符串（旧调用方）。
    """
    result_text = _result_text(result)
    try:
        a = json.loads(args or "{}")
    except Exception:
        a = {}
    pretty = _TOOL_PRETTY.get(name, name)
    state = _tool_result_state(result)

    # 用户主动取消
    if state == "cancel":
        return "好的主人，那就不弄啦~"

    # 工具失败 / 无结果：诚实告知，绝不假装成功
    if state == "fail":
        if name == "open_app":
            target = _APP_NICE.get(str(a.get("app_name", "")).strip(),
                                   str(a.get("app_name", "")).strip())
            return f"好的，我尝试打开{target}了，不过好像没成功，主人帮我看看是不是名字不对？"
        if name == "open_website":
            return "好的，我尝试打开网页了，不过好像没成功，主人帮我看看网址对不对？"
        if name == "web_search":
            return f"唔，{a.get('query', '')}我这边没搜到结果，主人换个关键词试试？"
        if name == "recall_memory":
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
    if name == "web_search":
        q = a.get("query", "")
        return f"好的主人，{pretty}的结果来啦~" if not q else f"好的主人，{q}的结果来啦~"
    if name == "add_reminder":
        t = a.get("time", "")
        c = a.get("content", "")
        return f"好的，已经帮主人设好提醒啦~{t} {c}"
    if name == "take_screenshot_tool":
        return "好的，已经截好图啦，主人看看桌面~"
    if name == "calculate":
        return f"好的，算好啦：{result_text}"
    if name in ("get_pet_status", "get_pet_stats"):
        return "好的主人，我看看自己的状态~"
    if name == "recall_memory":
        return "好的主人，我翻了翻记忆~"
    if name == "system_info":
        return "好的主人，系统信息来啦~"
    if name == "list_installed_apps":
        return "好的，主人电脑上的应用都在这啦~"
    if name == "send_notification":
        return "好的，已经帮主人弹通知啦~"
    return "好的，已经帮主人搞定啦~"


# 工具成败的判定口径（Stage 3.2）
#
# 原来这里是一张 26 个中文关键词的表，agent 靠猜工具是成功还是失败。
# 现在工具层把成败翻译成结构化字段（status / error_code），
# agent 只读字段，不再猜关键词；裸字符串的旧路径由工具层的
# classify_legacy_result() 兜底（见 app/engine/tools/_core.py）。

#: error_code → 「用户主动取消」（区别于失败：主人就是不让做）
_CANCEL_ERROR_CODES = ("user_cancelled",)


def _fmt_seconds(x: float) -> str:
    """秒数 → 人话（60.0 → "60s"，0.5 → "0.5s"）。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    return f"{int(v)}s" if v.is_integer() else f"{v:.1f}s"


def _swallow_task_result(task) -> None:
    """取走后台任务的异常（避免 "Task exception was never retrieved"）。"""
    try:
        if not task.cancelled():
            task.exception()
    except Exception:  # noqa: BLE001
        pass

def _result_text(result) -> str:
    """取工具结果文本：信封 → data；裸字符串 → 原样。"""
    if isinstance(result, dict):
        return str(result.get("data") or "")
    return result or ""


def _as_envelope(result, name: str = "") -> dict:
    """把任意工具返回值规整成结构化信封（旧后端可能还返回裸字符串）。"""
    if isinstance(result, dict):
        return result
    text = "" if result is None else str(result)
    state = _tool_result_state(text)
    if state == "ok":
        return ToolResult("ok", text)
    if state == "cancel":
        return ToolResult("denied", text, error_code="user_cancelled")
    status, code = (classify_tool_result(name, text) if name
                    else classify_legacy_result(text))
    if status == "ok":
        status, code = "error", "tool_error"
    return ToolResult(status, text, error_code=code)


def _tool_result_state(result) -> str:
    """工具结果 → 'cancel' / 'fail' / 'ok'（Stage 3.2：读结构化字段）。

    - 新路径：ToolResult 信封 —— 直接看 status / error_code；
    - 兼容路径：裸字符串（旧调用方）—— 只认「错误：/失败：」前缀与取消标记。
    """
    if isinstance(result, dict):
        status = result.get("status") or "ok"
        if status == "ok":
            return "ok"
        if result.get("error_code") in _CANCEL_ERROR_CODES:
            return "cancel"
        return "fail"
    r = result or ""
    if _is_user_cancel(r):
        return "cancel"
    status, _code = classify_legacy_result(r)
    return "ok" if status == "ok" else "fail"


def _tool_event_status(env) -> str:
    """工具信封 → 工具事件携带的**权威状态**（'ok' / 'error' / 'denied' / 'cancel'）。

    Stage 3.5：界面不再从**结果文本**猜成败 —— 文本可能没有「错误：」前缀
    （失败被显示成「完成」），正文也可能碰巧出现「错误：」（成功被显示成「失败」）。
    唯一权威答案是工具信封里的 status。

      · 信封 status 原样带出：ok / error / denied；
      · denied + error_code=user_cancelled（主人主动取消）单独报 cancel ——
        「主人不让做」和「策略拒绝（白名单 / 等级闸）」不是一回事，
        界面要分开显示：前者是「取消」，后者是「失败」。
    """
    if not isinstance(env, dict):
        return "ok"
    status = str(env.get("status") or "ok")
    if status == "denied" and env.get("error_code") in _CANCEL_ERROR_CODES:
        return "cancel"
    return status


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
#
# Stage 3.2：原来的 24 词失败词表换成**一条语义正则**：
# 工具侧已经结构化成 status / error_code，agent 不再需要词表猜工具成败；
# 这里剩下的唯一一件事是「模型自己说的话是不是在如实交代失败」，
# 用「否定 / 失败 + 动作」的语义组合表达，比维护 24 个词更准。
#
# 注意：不能放「唔」——它是小汐的口头禅，放进来会让校验器自我静音
_HONEST_FAIL_RE = re.compile(
    r"(?:没能|没法|无法|未能|未|没|无|不)(?:有)?(?:能|成功|办法|法)?\s*"
    r"(?:打开|启动|开启|运行|调出|拉起|设置|设好|记好|记下|记住|删掉|删除"
    r"|截好|截图|搜到|查到|算好|调好|发送|复制好|保存好|完成|搞定|弄好|找到)"
    r"|(?:打不开|开不了|没成功|没反应|没办到|没装|不支持|不可用|失败|出错"
    r"|错误|抱歉|对不起|取消)"
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
    if _HONEST_FAIL_RE.search(final_text or ""):
        return False
    return _claims_success(final_text)


def _claims_success(text: str) -> bool:
    """文本是否在断言「某件事已经做完」（纯格式判断）。"""
    if not text:
        return False
    if _HONEST_FAIL_RE.search(text):
        return False
    return any(w in text for w in _SUCCESS_CLAIM_WORDS) or bool(
        _SUCCESS_CLAIM_RE.search(text))


def _claims_unbacked_action(text: str) -> bool:
    """文本是否在断言「我替主人做了某个动作」，而这一轮**没有调过任何工具**。

    判据很硬：没调过工具，就不可能真的做过打开/设置/删除这类事。
    """
    if not text:
        return False
    if _HONEST_FAIL_RE.search(text):
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
        brief = _result_text(result).replace("\n", " ").strip()
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
            "  - 确认每条要求都有工具结果支撑 → 按人设口吻自然作答，**内容不遗漏**，"
            "行文格式由行为层决定。"
        )
    return f"[系统·结果核对] 本轮工具执行结果：\n{tool_list}\n{decision}"


#: **无论本轮意图是什么都在**的工具。
#:
#: 记忆三件套：Stage 2.3 的【记忆规则】是**每轮都在**的行为规则 ——
#: 主人随口说出的耐久事实（"我今天给你换了新模型"）可能出现在**任何**一轮里，
#: 不该因为本轮被判定成 query / web_search / open_app 就把 remember_fact
#: 从工具列表里删掉：列表里没有 = 模型**调不到**（standard 后端 LangChain 的
#: ToolNode 只认下发过的工具，ReAct 后端 registry.execute 也会直接拒）。
#: 那正是「模型好像不会主动进行记忆」的一个真实成因。
_ALWAYS_TOOL_NAMES: tuple[str, ...] = (
    "remember_fact", "recall_memory", "forget_memory",
)

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
        "date_info", "take_screenshot_tool", "get_clipboard",
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


class _ToolPhaseBudget:
    """整轮工具阶段的**墙钟预算**（Stage 3.3）。

    - 单工具超时 60s（与 MCP 桥接现状一致）；
    - 整轮工具阶段总预算 120s：超时后**不再执行新工具**，
      并把结果标成 truncated（可观测：meta 事件 + 日志 + 信封 truncated）。
    """

    def __init__(self, total_seconds: Optional[float] = None) -> None:
        # 默认值在**调用时**读模块全局（单测可以改预算）
        self.total_seconds = (TOOL_PHASE_BUDGET_SECONDS
                              if total_seconds is None else total_seconds)
        self._started: Optional[float] = None
        self.exhausted = False
        self.truncated = False

    def start(self) -> None:
        if self._started is None:
            self._started = time.monotonic()

    def elapsed(self) -> float:
        if self._started is None:
            return 0.0
        return time.monotonic() - self._started

    def remaining(self) -> float:
        return max(0.0, self.total_seconds - self.elapsed())

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def mark_exhausted(self) -> None:
        self.exhausted = True
        self.truncated = True


class _ToolPayloadBudget:
    """整轮注入上下文的**工具结果总预算**（Stage 3.3）。

    超过 TOOL_RESULT_MAX_CHARS 时：保头（45000）、保尾（2000），
    中间整段省略并留明确标记；被裁的结果标 truncated。
    """

    def __init__(self, budget_chars: Optional[int] = None) -> None:
        self.budget_chars = (TOOL_RESULT_MAX_CHARS
                             if budget_chars is None else budget_chars)
        self._entries: list[tuple[dict, dict]] = []
        self.total = 0
        self.truncated = False

    def add(self, message: dict, env: dict) -> None:
        """登记一条工具结果；超预算时回头重裁已注入的内容。"""
        self._entries.append((message, env))
        self.total += len(_result_text(env))
        if self.total > self.budget_chars:
            self._refit()

    def _refit(self) -> None:
        contents = [_result_text(env) for _msg, env in self._entries]
        fitted, _changed = fit_tool_payload(contents)
        for (msg, env), new in zip(self._entries, fitted):
            if new != _result_text(env):
                msg["content"] = new
                env["truncated"] = True
        self.truncated = True


def apply_persona_to_client(client, persona: str) -> str:
    """把新人设装到 client 上并重拼 system prompt（运行时换角色用）。

    跟 LLMClient.__init__ 里做的事**完全一致**（同一个拼装函数
    _build_full_system_prompt）—— 所以换完人设后发出去的 system 字符串
    跟「用新人设新建一个 client」逐字节相同，模型立刻换人设，不用重启。

    client 为 None（没有活着的 client）→ 只返回按新人设拼好的字符串。
    """
    base = persona or ""
    if client is None:
        return _build_full_system_prompt(base)
    try:
        client.base_system_prompt = base
    except Exception as e:  # noqa: BLE001
        log.warning("写 client.base_system_prompt 失败: %s", e)
    try:
        client.system_prompt = _build_full_system_prompt(base)
    except Exception as e:  # noqa: BLE001
        log.warning("重拼 system prompt 失败: %s", e)
        client.system_prompt = base
    return getattr(client, "system_prompt", base)


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

    def assemble_system_prompt(self, persona: str | None = None) -> str:
        """ReAct 后端的 system prompt 组装路径（唯一入口，Stage 1.1）。

        与 standard 后端（langchain_agent.LangChainAgent.assemble_system_prompt）
        用**同一套**共享层函数、**同一批**输入 ⇒ 两边发出的 system 字符串
        逐字节相同（见 tests/test_backend_parity.py）。

        - persona: 人设（+ 上游注入的会话上下文）。None → 取 client 里的原始人设。
        - 工具手册（【工具解锁】等级提示）只有这边拼得出来：需要工具注册表 + 当前等级。
        """
        if persona is None:
            persona = getattr(self.client, "base_system_prompt", None)
        if persona is None:
            persona = getattr(self.client, "system_prompt", "") or ""
        return build_system_prompt(
            persona=persona,
            style_guide=load_style_guide(),
            tool_handbook=build_tool_handbook(
                registry=self.registry, level=self._current_level()),
            dynamic=build_dynamic_block(),
        )

    def _apply_system_prompt(self) -> str:
        """把 ReAct 后端最终 system 字符串装到 client 上（共享层拼的）。

        返回装上去的那条 system 字符串（换人设时拿它做对比 / 展示）。
        """
        client = self.client
        if client is None:
            return ""
        base = getattr(client, "base_system_prompt", None)
        if base is None:
            # 不是 LLMClient（老路径 / 测试用的假 client）：保持原样，不碰
            return ""
        prompt = self.assemble_system_prompt(base)
        client.system_prompt = prompt
        return prompt

    def set_persona(self, persona: str) -> str:
        """运行时换人设：改 base_system_prompt 并重拼 system prompt。

        换完下一轮（甚至正在跑的下一轮）就按新人设说话 —— 不必重启。
        返回重拼后的 system 字符串。
        """
        apply_persona_to_client(self.client, persona)
        return self._apply_system_prompt()

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
        # 记忆三件套永远留着（见 _ALWAYS_TOOL_NAMES 的注释）
        allow = set(wanted) | set(_ALWAYS_TOOL_NAMES)
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

    async def _confirm_or_skip(self, name: str, args: str) -> Optional[ToolResult]:
        """对危险工具执行确认。

        返回 None 表示继续执行；返回**结构化信封**表示跳过（未执行）。
        """
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
            return ToolResult("denied", "确认弹窗超时未响应，未执行。",
                              error_code="confirm_timeout")
        if not confirmed:
            return ToolResult("denied", "用户取消了此操作。",
                              error_code="user_cancelled")
        return None

    async def _execute_tools_parallel(
        self,
        tool_calls: list[dict],
        messages: list[dict],
        cancel_check: Optional[Callable[[], bool]] = None,
        intent: Optional[str] = None,
        failed_tools: Optional[dict[str, int]] = None,
        budget: Optional["_ToolPhaseBudget"] = None,
        payload: Optional["_ToolPayloadBudget"] = None,
        out_envelopes: Optional[list] = None,
    ) -> AsyncIterator[tuple]:
        """并行执行所有工具调用，按原顺序 yield 结果。

        Stage 3.2：结果一律是**结构化信封**（ToolResult）。
        Stage 3.3：单工具 60s 超时 + 整轮工具阶段 120s 墙钟预算。
        """
        blocked = _INTENT_BLOCKED_TOOLS.get(intent or "", {})
        # 同一工具反复失败的累计次数（由 run() 持有，跨轮次）
        failed_tools = failed_tools if failed_tools is not None else {}
        budget = budget if budget is not None else _ToolPhaseBudget()
        payload = payload if payload is not None else _ToolPayloadBudget()
        out_envelopes = out_envelopes if out_envelopes is not None else []
        budget.start()

        async def _exec_one(i: int, tc: dict) -> tuple[int, str, str, dict]:
            """在后台线程中执行单个工具，返回 (index, name, args, 信封)。"""
            name = tc["name"] or f"unknown_{i}"
            args = tc["arguments"] or "{}"

            # 整轮工具阶段预算：超了就不再执行新工具（Stage 3.3）
            if budget.expired():
                log.warning("AgentLoop: 工具阶段预算已用尽，跳过 %s", name)
                return i, name, args, ToolResult(
                    "error",
                    (f"错误：工具阶段预算已用尽（{_fmt_seconds(budget.total_seconds)}），"
                     f"本轮不再执行新工具（{name} 未执行）。"),
                    truncated=True, error_code="budget_exhausted")

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
                return i, name, args, ToolResult("denied", msg,
                                              error_code="level_locked")

            # 意图级硬闸：白名单是软约束，这里硬拒并说明原因
            if name in blocked:
                msg = blocked[name]
                log.warning("AgentLoop: 意图 %s 下拒绝调用 %s（已改用正确工具）",
                            intent, name)
                return i, name, args, ToolResult("denied", msg,
                                              error_code="intent_blocked")

            # 失败次数硬闸：同一个工具已经失败 _MAX_SAME_TOOL_FAIL 次就别再试了
            if failed_tools.get(name, 0) >= _MAX_SAME_TOOL_FAIL:
                msg = (f"错误：{name} 已经连续失败 {failed_tools[name]} 次，"
                       "本轮不再提供它。请不要再调用同一个工具换参数重试，"
                       "改用别的工具，或直接如实告诉主人这一步做不到、为什么。")
                log.warning("AgentLoop: %s 已失败 %d 次，拒绝再次调用",
                            name, failed_tools[name])
                return i, name, args, ToolResult("denied", msg,
                                              error_code="repeat_failure")

            # 危险工具确认（第三道：单次高危要主人点头）
            skip_result = await self._confirm_or_skip(name, args)
            if skip_result is not None:
                log.info("tool %s(%s) -> SKIPPED", name, args[:120])
                return i, name, args, skip_result

            # 在线程池中执行同步工具函数（单工具 60s 超时）
            #
            # ⚠️ 用 shield：超时时**不等**那个跑不动的线程（线程没法杀），
            # agent 立刻拿到「超时」的信封继续往下走；线程在后台跑完即弃。
            task = asyncio.ensure_future(asyncio.to_thread(
                self.registry.execute, name, args))
            try:
                raw = await asyncio.wait_for(
                    asyncio.shield(task), timeout=TOOL_CALL_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                log.warning("AgentLoop: 工具 %s 超时（%s）",
                            name, _fmt_seconds(TOOL_CALL_TIMEOUT_SECONDS))
                task.add_done_callback(_swallow_task_result)
                return i, name, args, ToolResult(
                    "error",
                    (f"错误：工具 {name} 超时（{_fmt_seconds(TOOL_CALL_TIMEOUT_SECONDS)}），"
                     "没有返回结果。"),
                    error_code="timeout")
            env = _as_envelope(raw, name=name)
            log.info("tool %s(%s) -> %s", name, args[:120],
                     _result_text(env)[:120])
            return i, name, args, env

        # 检查取消
        if cancel_check is not None and cancel_check():
            return
        if not tool_calls:
            return

        # 并行执行所有工具（整轮墙钟预算兜底：超时的按预算耗尽记账）
        tasks = []
        index_of = {}
        for i, tc in enumerate(tool_calls):
            task = asyncio.ensure_future(_exec_one(i, tc))
            tasks.append(task)
            index_of[task] = i
        done, pending = await asyncio.wait(tasks, timeout=budget.remaining())
        results = [t.result() for t in done]
        for task in pending:
            task.cancel()
            i = index_of[task]
            tc = tool_calls[i]
            name = tc["name"] or f"unknown_{i}"
            results.append((i, name, tc["arguments"] or "{}", ToolResult(
                "error",
                (f"错误：工具阶段预算已用尽（{_fmt_seconds(budget.total_seconds)}），"
                 f"{name} 未执行。"),
                truncated=True, error_code="budget_exhausted")))
        if pending:
            budget.mark_exhausted()
            log.warning("AgentLoop: 工具阶段预算 %s 用尽，%d 个工具未执行",
                        _fmt_seconds(budget.total_seconds), len(pending))
            yield ("meta", {"event": "tool_budget_exhausted",
                            "reason": (f"工具阶段预算 {_fmt_seconds(budget.total_seconds)} "
                                       f"已用尽，{len(pending)} 个工具未执行"),
                            "pending": len(pending)})

        # 按原始顺序 yield 结果
        for i, name, args, env in sorted(results, key=lambda x: x[0]):
            # 工具事件把信封里的**权威状态**一起带出去（ok/error/denied/cancel）：
            # 界面直接拿它渲染成败标记，不再从结果文本猜成败（Stage 3.5）。
            yield "tool", name, args, _result_text(env), _tool_event_status(env)
            # 累计失败次数（供下一轮 _exec_one 的失败闸使用）
            if _tool_result_state(env) == "fail":
                failed_tools[name] = failed_tools.get(name, 0) + 1
            elif name in failed_tools:
                failed_tools[name] = 0   # 成功了就把计数清零
            msg = {
                "role": "tool",
                "tool_call_id": tool_calls[i]["id"] or f"call_{i}",
                "content": _result_text(env),
            }
            # 整轮注入预算（Stage 3.3）：超了保头保尾、中间省略
            payload.add(msg, env)
            messages.append(msg)
            out_envelopes.append((name, args, env))

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
        # 已经成功执行的工具调用 (name, args, 信封)，用于矛盾检测和兜底
        tools_used = 0
        executed_tools: list[tuple[str, str, dict]] = []
        # Stage 3.3：整轮工具阶段的墙钟预算 + 注入上下文的总预算
        budget = _ToolPhaseBudget()
        payload = _ToolPayloadBudget()
        verify_corrections = 0   # 「工具失败却报喜」的纠正次数（最多 1 次）
        partial_retries = 0     # 「多步任务只做一半」的纠正次数（最多 1 次）
        force_retries = 0       # 「该调工具却只给文字」的重试次数（最多 1 次）
        # 同一工具连续失败次数（跨轮次）
        failed_tools: dict[str, int] = {}
        # 模型的原始输出被清洗规则全部剥掉（过度清洗）时置 True
        over_sanitized = False
        # 「中文占比过低」的重试次数（Stage 1.3，最多 1 次）
        low_cjk_retries = 0
        # 「整轮只有思维链」的重试次数（Stage 2.10，最多 1 次）
        cot_retries = 0
        # 重问那一轮用的输出预算（None = 用配置里的预算）
        retry_max_tokens: Optional[int] = None

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

        # Stage 1.1：system 消息**只由共享层**拼（ReAct 与 standard 两个后端
        # 发出去的 system 字符串必须逐字节相同）。
        #   - client.system_prompt = 最终 system 字符串（ReAct 真正发出去的那条）
        #   - messages[0] 若是 system 消息（老调用方 / 测试），也用同一套共享层重拼
        self._apply_system_prompt()
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = self.assemble_system_prompt(
                persona=str(messages[0].get("content") or ""))

        for turn in range(self.max_turns):
            if cancel_check is not None and cancel_check():
                break
            content_parts: list[str] = []
            tool_calls: list[dict] = []
            # 整轮只有思维链（客户端在 finish 事件里给的标记）
            cot_only = False

            # 抗幻觉第 3 层：连续多轮纯调工具 → 去掉 tools 强制 final
            force_final = consecutive_tool_only >= MAX_CONSECUTIVE_TOOL_ONLY_TURNS
            if force_final:
                log.warning(
                    "AgentLoop: 已连续 %d 轮纯调工具无文字，强制进入 final 阶段（去掉 tools）",
                    consecutive_tool_only)
                yield ("meta", {"event": "force_final",
                                "reason": f"连续 {consecutive_tool_only} 轮纯调工具无文字",
                                "turn": turn})

            # 重问那一轮用更大的输出预算（CoT 已经把原预算吃光了）
            stream_kwargs: dict = {}
            if retry_max_tokens and _supports_max_tokens(self.client):
                stream_kwargs["max_tokens"] = retry_max_tokens
            async for ev, data in self.client.chat_stream_events(
                    messages,
                    tools=None if force_final else tools,
                    cancel_check=cancel_check,
                    force_tool_use=force_first_turn,
                    **stream_kwargs):
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
                    if isinstance(data, dict):
                        # Stage 2.10：整轮只有思维链（CoT 吃光输出预算）
                        cot_only = bool(data.get("cot_only"))
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

            # Stage 2.10：整轮只有思维链（CoT 把输出预算吃光）→ **重问一次**。
            # 这时候 raw_text 是**空的**（清洗管线逐块把 CoT 剥光了），
            # 低中文占比那条重试判不出来（它要求 raw_text 非空），
            # 所以只能靠客户端给的 cot_only 标记来认。
            if (cot_only and not tool_calls
                    and cot_retries < _MAX_COT_ONLY_RETRIES
                    and turn + 1 < self.max_turns
                    and not (cancel_check and cancel_check())):
                cot_retries += 1
                log.warning("AgentLoop: 第 %d 轮回复只有思维链、没有可见答案，"
                            "重问一次（预算 %d）", turn + 1,
                            _COT_ONLY_RETRY_MAX_TOKENS)
                yield ("meta", {"event": "cot_only_retry",
                                "reason": "回复只有思维链，没有可见答案",
                                "turn": turn})
                # 把「上一条只有思维链」告诉模型，重问才有意义
                messages.append({"role": "assistant",
                                 "content": _COT_ONLY_RETRY_MARK})
                messages.append({"role": "user",
                                 "content": _COT_ONLY_RETRY_HINT})
                # 重问这一轮给更大的输出预算：原预算已经被 CoT 吃光了
                retry_max_tokens = _COT_ONLY_RETRY_MAX_TOKENS
                consecutive_tool_only = 0
                continue

            # Stage 1.3：中文占比过低（几乎全是英文 / 乱码）→ **重问一次**。
            # 旧行为是整条丢掉，主人只能看到兜底话术；
            # 现在先让模型重答一次（最多一次，绝不连发），仍失败才走兜底。
            # ⚠️ cot_only 的那轮已经在上面处理掉了 —— 两条重试互斥，
            #    绝不会对同一条回复连发两次请求。
            if (raw_text.strip() and not tool_calls and not final_text.strip()
                    and not cot_only
                    and is_low_cjk_reply(raw_text)
                    and low_cjk_retries < _MAX_LOW_CJK_RETRIES
                    and turn + 1 < self.max_turns
                    and not (cancel_check and cancel_check())):
                low_cjk_retries += 1
                log.warning("AgentLoop: 第 %d 轮回复中文占比过低（%r），重问一次",
                            turn + 1, raw_text[:120])
                yield ("meta", {"event": "low_cjk_retry",
                                "reason": "回复中文占比过低",
                                "turn": turn})
                # 把「上一条不是中文」告诉模型，重问才有意义
                messages.append({"role": "assistant", "content": raw_text})
                messages.append({"role": "user", "content": _LOW_CJK_RETRY_HINT})
                consecutive_tool_only = 0
                continue

            # 抗幻觉第 2 层：第 1 轮没调工具 → 重试
            # 关键：tools_used == 0 才算「该调没调」，调过工具后给文字就是最终总结
            if (force_first_turn and not tool_calls and tools_used == 0
                    and not (cancel_check and cancel_check())):
                # 重试上限：最多 _MAX_FORCE_RETRIES 次。
                # 原来能连重试 5 轮（MAX_TURNS=6），而第 2..6 次带的几乎是
                # 同一个请求（强制提示由 LLMClient 统一注入，tool_choice 降级
                # 还按模型名缓存着）——等于把同一请求重复发 5 次（费钱且慢）。
                if (force_retries >= _MAX_FORCE_RETRIES
                        or turn + 1 >= self.max_turns):
                    # 剩余轮数不足（最后一轮）或重试已用完 → 放弃
                    log.warning(
                        "AgentLoop: 已 %d 轮仍调不到工具，放弃（意图=%s）", turn + 1, intent)
                    if final_text:
                        if _claims_unbacked_action(final_text):
                            log.warning("AgentLoop: 重试耗尽，丢弃未验证的报喜话术")
                            final_text = _NO_TOOL_FALLBACK
                        yield "text", final_text
                    break
                force_retries += 1
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
            batch: list[tuple[str, str, dict]] = []
            async for event in self._execute_tools_parallel(
                    tool_calls, messages, cancel_check=cancel_check,
                    intent=intent, failed_tools=failed_tools,
                    budget=budget, payload=payload, out_envelopes=batch):
                yield event
                if event[0] == "tool":
                    # event = ("tool", name, args, 结果文本, 信封状态)；信封在 batch 里
                    tools_used += 1
            executed_tools.extend(batch)

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
            final_text = _next_empty_reply_fallback()
            log.warning("AgentLoop: 输出被过度清洗且无工具兜底，使用空回复话术")
            yield "text", final_text

        # 兜底三：无条件兜底（避免主人面对空气泡）
        if not final_text.strip():
            final_text = _next_empty_reply_fallback()
            log.warning("AgentLoop: 所有路径都没产出文字，使用空回复兜底话术")
            yield "text", final_text

        yield "done", final_text

