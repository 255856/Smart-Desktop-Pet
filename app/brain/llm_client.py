"""大模型客户端（OpenAI 兼容协议，支持流式 + 推理模型 + Ollama + 中文清洗）。"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable, Optional
import asyncio

import httpx

from app.core.config import LLMConfig
from app.core.api_keys import is_placeholder_key
from app.brain.token_ledger import next_request_index, record_usage
from app.brain.prompt_layer import (  # noqa: F401  (兼容导出: CHINESE_SYSTEM_SUFFIX / DYNAMIC_CONTEXT_HEADER)
    CHINESE_SYSTEM_SUFFIX,
    DYNAMIC_CONTEXT_HEADER,
    build_dynamic_block,
    build_system_prompt,
    build_turn_context,
    load_style_guide,
)

log = logging.getLogger(__name__)

# 瞬时故障重试：429 限流、5xx 网关/过载、529 集群繁忙
_TRANSIENT_STATUS = (408, 409, 425, 429, 500, 502, 503, 504, 529)
_TRANSIENT_MAX_ATTEMPTS = 3      # 含首次，共最多 3 次
_TRANSIENT_BACKOFF_S = 1.5       # 线性退避：1.5s / 3.0s

# 不发 tool_choice 的模型（实测发出去命中率反而崩掉）
# 出厂预置（小写，比较前先 norm）；运行期学到的条目分开存
_TOOL_CHOICE_SEED: frozenset[str] = frozenset({"minimax-m3"})
_TOOL_CHOICE_IGNORED: set[str] = set(_TOOL_CHOICE_SEED)

# 运行期学到的名单落盘位置（重启后不用再白探测一次）。
_TOOL_CHOICE_CACHE = Path("data/tool_choice_unsupported.json")


def _norm_model(model: str) -> str:
    """模型名归一化（比较前先 norm）。"""
    return (model or "").strip().lower()


def _load_tool_choice_ignored() -> None:
    try:
        if _TOOL_CHOICE_CACHE.is_file():
            for name in json.loads(_TOOL_CHOICE_CACHE.read_text(encoding="utf-8")) or []:
                if isinstance(name, str) and name.strip():
                    _TOOL_CHOICE_IGNORED.add(_norm_model(name))
    except Exception:  # noqa: BLE001
        log.debug("读取 tool_choice 黑名单失败，忽略", exc_info=True)


def _note_tool_choice_ignored(model: str) -> None:
    key = _norm_model(model)
    if not key or key in _TOOL_CHOICE_IGNORED:
        return
    _TOOL_CHOICE_IGNORED.add(key)
    log.info("LLMClient: 记录 %s 会忽略 tool_choice=required，"
             "后续请求直接用 prompt 强制", model)
    try:
        _TOOL_CHOICE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _TOOL_CHOICE_CACHE.write_text(
            json.dumps(sorted(_TOOL_CHOICE_IGNORED), ensure_ascii=False, indent=1),
            encoding="utf-8")
    except Exception:  # noqa: BLE001
        log.debug("写 tool_choice 黑名单失败，忽略", exc_info=True)


def _tool_choice_is_ignored(model: str) -> bool:
    return _norm_model(model) in _TOOL_CHOICE_IGNORED


_load_tool_choice_ignored()

# 推理模型的 think 标签（DeepSeek-r1 等原生标签格式）
THINK_TAG_START = "<think>"
THINK_TAG_END = "</think>"

# 中文约束片段：以对话示例形式给出，强化但避免被模型当成"待输出文本"复述
#
# Stage 1.1：常量搬进共享层 app/brain/prompt_layer.py（两个后端都得有它，
# 否则 ReAct 与 standard 发出去的 system prompt 长得不一样）。
# 上面那行 import 保留同名导出，老调用方（tests / scripts）不用改。
# Stage 1.4：末尾的硬上限「回复 1~3 句」已删，换成共享层里的【回复长度】策略。


# emoji 字符范围。Stage 1.3 起**不再全局剥离**（sanitize_text 保留 emoji），
# 这里保留定义，供需要「显式剥 emoji」的调用方使用。
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001F5FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "\U0001F700-\U0001F77F"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\u2600-\u26FF"
    "\u2700-\u27BF"
    "\u2300-\u23FF"
    "]+",
    flags=re.UNICODE,
)


# 去掉 LLM 在 content 里夹带的"思考痕迹"（中文模型常见括号式自言自语）
#
# ⚠️ 机制修正（Stage 1.3）：旧写法的前缀组里有**空分支**
#   （`因为|` 之后无内容），于是它匹配的是「任意」3~500 字的括号内容 ——
#   「（小声地笑）」「（打个哈欠）」这类拟声词也会被吞掉。
#   现在改成「**先匹配已知规划词，再匹配括号**」：
#   括号内容必须以规划词开头（让我 / 我要 / 我需要 / 我们刚才 …）才算「规划 / 思考」，
#   其余括号（拟声、情绪、括注）一律保留。
_META_NARRATION_PREFIX = (
    r"(?:我们刚才|刚才|现在|让我|我要|我需要|我打算|我准备|我先|我想|"
    r"嗯|好的|作为|毕竟|事实上|好了|由于|因为|思考|想想)"
)
_META_NARRATION_RE = re.compile(
    r"[（(](?=\s*" + _META_NARRATION_PREFIX + r")"   # 必须以已知规划词开头
    r"[^()（）\n]{3,500}"
    r"[)）]",
    flags=re.UNICODE,
)
# 兜底：任何长得像「（中文思考 200+ 字）」的整段括号
_LONG_PAREN_RE = re.compile(r"[（(][^)（）\n]{50,}[)）]")

# --------------------------------------------------------------------------
# 过渡语 vs 工具独白 —— Stage 1.3 的核心判别器
# --------------------------------------------------------------------------
# 人话里的自然过渡语：「我来…」「让我…」「我看看…」「我要…」
# —— 它们让回复像人说话，**不该**被清洗管线删掉。
# 但「让我调用工具查一下广州天气。」是**工具独白**（在描述要调什么工具），必须删。
#
# 判别规则（唯一标准，集中写在这里，避免各正则各写各的）：
#     **过渡语 + 工具调用标志 = 工具独白（删）；只有过渡语 = 自然过渡（留）**
#
# 工具调用标志 = 出现「工具 / 调用 / schema」这类词，
# 或 snake_case 的工具名（open_app / web_search / add_reminder …）。

#: 过渡语（人话里的自然开场 / 自我规划）
_TRANSITION_WORDS = (
    r"想让我|想我要|让我|需要我|应当我|要求我|"
    r"我需要|我应该|我要|我来|我会|我打算|我将|我准备|我先|我想|我看看|我再"
)

#: 工具调用标志（正则片段）：「工具 / 调用 / schema」这类词，
#: 或 snake_case 的工具名（open_app / web_search / add_reminder …）
_TOOL_CALL_MARKER = (
    r"(?:工具|调用|schema|calculator|"
    r"(?:open|web|add|set|get|list|take|memory|recall|calc|run|launch|start|stop|"
    r"create|delete|update|remove|browse|query|screen|system)_[a-z0-9_]+|"
    r"[a-z_]+_(?:website|app|tool|reminder|fact))"
)

#: 工具调用标志（编译好的）
_TOOL_CALL_MARKER_RE = re.compile(_TOOL_CALL_MARKER, flags=re.IGNORECASE)

# 兜底：模型把"我要做什么工具"类元描述裸句塞进 final answer
# Stage 1.3：与其它规则统一口径 —— 过渡语 + 工具调用标志才算工具独白
_META_TOOL_TALK_RE = re.compile(
    r"(?:" + _TRANSITION_WORDS + r"|接下来)"
    r"[^。.!?\n]*?"
    + _TOOL_CALL_MARKER +
    r"[^。.!?\n]*"
    r"[。.!?]?",
    flags=re.UNICODE | re.IGNORECASE,
)
# 兜底：「这应该用XX工具来YY」类建议句
_META_SHOULD_USE_TOOL_RE = re.compile(
    r"(?<=[。.!?\n])这\s*(应该|需要|可以)?\s*"
    r"(用|通过|借助|使用)\s*"
    r"[^。.!?\n]*?"
    r"工具"
    r"[^。.!?\n]*"
    r"[。.!?]?",
    flags=re.UNICODE,
)

# 兜底：「根据角色设定 / 根据人设 / 用户说 / 主人想要」类规划/复述元描述句
_META_PLAN_NARRATION_RE = re.compile(
    r"((?:^|(?<=[。.!?\n]))(?:但是\s*)?(?:然而\s*)?(?:所以\s*)?(?:因此\s*)?)"
    r"(根据|按照|依据)"                             # 引导词
    r"\s*(?:(?:我的|主人|LLM)\s*)?"               # 可选"我的/主人/LLM" + 0+ 空格
    r"(角色设定|角色|人设|设定|性格|要求|指令|用户|主人)"
    r"[^。.!?\n]*"
    r"[。.!?]?",
    flags=re.UNICODE,
)
_META_USER_REPEAT_RE = re.compile(
    r"(^|(?<=[。.!?\n]))"
    r"(用户|主人)\s*(说|想问|希望|想要|问的是|说的是)"
    r"[^。.!?\n]*"
    r"[。.!?]?",
    flags=re.UNICODE,
)
# 「过渡语 + 工具调用标志」= 工具独白，整句删（文本级）
_META_SELF_PLAN_RE = re.compile(
    r"(^|(?<=[。.!?\n]))"
    r"(?:" + _TRANSITION_WORDS + r")"
    r"[^。.!?\n]{0,80}?"
    + _TOOL_CALL_MARKER +
    r"[^。.!?\n]*"
    r"[。.!?]?",
    flags=re.UNICODE | re.IGNORECASE,
)
# 兜底：模型把 system prompt 原文复读出来
_META_PROMPT_ECHO_RE = re.compile(
    r"(^|(?<=[。.!?\n]))"
    r"(?:"                                                 # 整段匹配下列任一标志
    r"(?:唯一允许|唯一要求|输出规范|输出要求|输出风格|输出语言|输出格式|回复规范|回复风格|回复要求|对话规范|对话风格|对话要求|语言规范|格式规范)"
    r"|(?:禁止使用|禁止出现|禁止输出|禁止用|不要使用|不要出现|不要输出|不要用|不要列|不要解释|不要复述|不要描述|不要思考|不要规划|不要自我)"
    r"|(?:保持角色|保持人设|保持设定|保持风格|保持自然|保持简短|保持简洁|保持中文|保持输出|保持一致)"
    r"|(?:用简体中文|中文输出|输出中文|使用中文|使用简体中文|中文回复|中文对话)"
    r"|(?:简短自然|简短回答|简短输出|简短对话|简短回复|一句话|两三句|1\s*[-~]?\s*3\s*句|3\s*[-~]?\s*5\s*句)"
    r"|(?:不要长篇|不要长篇大论|不要使用任何|不要使用 emoji|禁止使用 emoji|禁止 emoji|不要 markdown|不要列表)"
    r")"
    r"[^。.!?\n]+?"                                        # 该句剩余内容（非贪婪：最短匹配）
    r"(?=[。.!?\n]|$)",                                    # 终止条件：句末标点 / 换行 / 字符串末尾
    flags=re.UNICODE,
)
# 注：早先还有一个 _META_NUMBERED_LIST_RE（死代码），放宽会误删正常编号列表
# （"1. 北京 2. 上海"）。编号规则项统一由 _NUMBERED_RULE_RE 保守处理。


#  句子级「规划 / 元描述 / 规则复读」识别
# 情绪标签词（系统铁律：模型不应输出，出现即剥，无论在句尾还是句中）
_EMOTION_WORDS = (
    "happy|sad|angry|surprised|scared|confused|shy|proud|thinking|talking|love|skip"
)
# 任意位置的完整情绪标签
_EMOTION_TAG_ANY_RE = re.compile(
    r"\s*\[(?:" + _EMOTION_WORDS + r")\]\s*", re.IGNORECASE)
# 未闭合的情绪标签（流式截断 / 模型漏写右括号）
_EMOTION_TAG_OPEN_RE = re.compile(
    r"\s*\[(?:" + _EMOTION_WORDS + r")\s*$", re.IGNORECASE)
# 孤立的 think 结束标签（起始标签已在更早 chunk 剥离）
_THINK_CLOSE_TAG_RE = re.compile(r"</think\s*>", re.IGNORECASE)

# 角色「真正开口」的发语锚点：从最后一个锚点处截断，只保留锚点之后的回答
_ANSWER_ANCHOR_RE = re.compile(
    r"(嘿嘿+|哈哈+|嘻嘻+|诶嘿|嗯哼|唔嗯|好嘞|好哒|好啦|好呀|"
    r"好的?[，,~～\s]?主人|主人[~～，,、呀呢啦嘛哦哟看]|"
    r"唔[~～，,。]?|呜哇|嘤嘤|啊嘞|欸+|嗯[~～])",
    flags=re.UNICODE,
)

# prompt 规则 / 风格指令被模型复读（高置信，几乎不可能出现在正常回答里）
_META_RULE_KEYWORDS_RE = re.compile(
    r"(唯一允许|唯一要求|输出规范|输出要求|输出风格|输出语言|输出格式|回复规范|回复风格|"
    r"回复要求|对话规范|对话风格|对话要求|语言规范|格式规范|禁止使用|禁止出现|禁止输出|"
    r"禁止用|不要使用|不要出现|不要输出|不要用|不要列|不要解释|不要复述|不要描述|"
    r"不要思考|不要规划|不要自我|不要长篇|保持角色|保持人设|保持设定|保持风格|保持自然|"
    r"保持简短|保持简洁|保持中文|保持输出|保持一致|用简体中文|中文输出|输出中文|使用中文|"
    r"使用简体中文|中文回复|中文对话|简短自然|简短回答|简短输出|简短对话|简短回复|"
    r"一句话|两三句|不要\s*markdown|不要\s*列表|emoji)",
    re.IGNORECASE,
)
# 角色卡罗列 / 设定复述（"根据角色设定…"、"外貌：…"、"性格：…"、"擅长：…"）
_META_CHAR_SHEET_RE = re.compile(
    r"(?:根据|按照|依据)[^。！？!?\n]{0,8}"
    r"(?:角色|人设|设定|性格|要求|指令|我的设定)|"
    r"(?:^|[\s\-、，,：:；;])(?:外貌|性格|擅长|角色设定|人物设定|说话风格|常用语气词)\s*[:：]",
    flags=re.UNICODE,
)
# 句首对用户的复述（"用户说 / 主人想要 / 主人在问…"）
# 注意"主人想玩 / 想看 / 想吃"是角色对主人的正常回应语气，不算复述
_META_USER_REPEAT_SENT_RE = re.compile(
    r"^\s*(?:用户\s*(?:在?说|想问|想要|想|希望|问的是|说的是|是说)|"
    r"主人\s*(?:在?说|想问|想要|希望|问的是|说的是|是说))"
)
# 句首第一人称过渡 / 自我规划。
# Stage 1.3：它**只负责认出「这是一句过渡 / 规划」**；
# 算不算污染由工具调用标志（_TOOL_CALL_MARKER_RE）决定 —— 见 _is_pollution_sentence。
_META_SELF_PLAN_SENT_RE = re.compile(
    r"^\s*(?:" + _TRANSITION_WORDS + r"|"
    r"我(?:可以|不应该|不能|这就|马上))"
)
# 规划 / 元语境关键词：与句首自我规划搭配出现。
# Stage 1.3 收窄：只保留「明显在复述系统设定」的词 ——
# 「看看 / 试试 / 查询 / 应该 / 需要 / 回答」这类词同样会出现在自然过渡语里
# （「让我看看。」），不能再当污染依据，否则过渡语会被误删。
_META_PLAN_CONTEXT_RE = re.compile(
    r"(角色|人设|角色卡|性格|设定|根据|依据|按照|指令|要求|规划|"
    r"schema|提示词|prompt|系统提示|输出规范|输出要求)"
)
# 工具元描述（"这应该用 open_website 工具"、"让我调用工具"、"通过 XX 工具来…"）
_META_TOOL_SENT_RE = re.compile(
    r"(?:调用|使用|借助|运行|执行|通过)\s*[A-Za-z_0-9\u4e00-\u9fff]{0,20}?工具|"
    r"工具\s*(?:来|去|打开|执行|查|完成|处理|帮)|"
    r"这应该用|应该用\s*[a-z_]+|open_[a-z_]+\s*工具|"
    r"[a-z_]+_(?:website|app|tool|reminder|fact)|"
    r"说\s*[\"“「『\'][^。！？!?\n]{0,24}?[\"”」』]?\s*[,，]?\s*这是(?:要|一|在|个|应用|网页|链接|动作)",
    flags=re.IGNORECASE,
)
# 工具过程废话子句：模型解释「动作有很多种方式」，常与真回答同句
_META_PROCESS_CLAUSE_RE = re.compile(
    r"[，,~～\s]*(?:打开|启动|搜索|查询|使用|调用|运行|执行)"
    r"[^。！？!?\n，,~～]{0,10}的方式有很多种[呢呀啊~～。！？!?]*",
    flags=re.UNICODE,
)
# 编号规则项（"1. 禁止使用 emoji"、"4.不要复述"）
# 只认「编号 + 规则关键词」组合，避免误删正常编号列表
_NUMBERED_RULE_RE = re.compile(
    r"^\s*[0-9]+\s*[.、)）]\s*"
    r"(?:禁止|保持|不要|输出|使用|用|回复|简短|角色|人设|复述|emoji|表情|自然|中文|句)"
)
# 句子切分（保留分隔符）
_SENT_SPLIT_RE = re.compile(r"([。！？!?\n]+)")


def sanitize_text(text: str, *, is_final: bool = True) -> str:
    """清洗 LLM 输出：去思考痕迹 + 多余空白 + 兜底剥离低中文占比段。

    Stage 1.3 起**不再剥离 emoji / 装饰符号**（回复里带表情才像人发的）。
    ⚠️ 红线：TTS 走独立管线（app/voice/voice.py），这里改不动音色 / 韵律。

    Args:
        text: 待清洗文本
        is_final: True 表示这是一段完整的最终回复（流式累积完或非流式结果）；
                  此时启用「全文本 CJK 占比 < 15% 则整段丢弃」的兜底。
                  False 表示这是一段流式 chunk（部分内容），不做占比兜底，
                  否则单段 chunk 几乎必然被丢弃，导致流式 / langchain_agent 文本丢失。

    低中文占比被整段丢弃时，调用方（AgentLoop）应据此**重试一次**再走兜底，
    判据见 is_low_cjk_reply()。
    """
    if not text:
        return text
    # 1) 推理段 <think>...</think>（含未闭合 / 孤立结束标签）
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL)
    text = _THINK_CLOSE_TAG_RE.sub("", text)
    # 2) 情绪标签（仅最终阶段；流式 chunk 不剥，单 chunk 可能是被拆开的）
    if is_final:
        text = _EMOTION_TAG_ANY_RE.sub(" ", text)
        text = _EMOTION_TAG_OPEN_RE.sub("", text)
    # 3) emoji 与装饰符号：**不再剥离**（Stage 1.3）
    #    旧行为全局删 emoji，回复因此「不像人发的」。
    #    ⚠️ 红线：TTS 是独立管线（app/voice/voice.py），这里不碰音色 / 韵律。
    # 4) 括号式自言自语与超长括号思考段
    text = _META_NARRATION_RE.sub("", text)
    text = _LONG_PAREN_RE.sub("", text)
    # 5) 收紧空白
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    # 6) 最终阶段：逐句剥离 CoT 规划 / 元描述 / 规则复读（循环处理嵌套）
    if is_final:
        text = _strip_internal_checklist(text)
        prev_text = None
        while prev_text != text:
            prev_text = text
            text = _strip_meta_by_sentence(text)
    # 7) 多余空行收紧
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 8) 低中文占比段落剥离
    text = _drop_leading_low_cjk_paragraphs(text)
    text = _drop_low_cjk_paragraphs(text)
    # 9) 终极兜底：清洗后整段中文占比 < 15% → 整体丢弃
    #    ⚠️ Stage 1.3：这里不再「静默丢掉」整条回复 ——
    #    调用方（AgentLoop）用 is_low_cjk_reply() 判定后**重试一次**，仍失败才走兜底。
    cleaned = text.strip()
    if is_final and cleaned:
        body_for_ratio = _EMOTION_TAG_ANY_RE.sub("", cleaned).strip()
        if _cjk_ratio(body_for_ratio) < _LOW_CJK_RATIO:
            return ""
    return cleaned


def _strip_internal_checklist(text: str) -> str:
    """剥掉「编号 + 内部核对话术」的清单行。

    模型常把 AgentLoop 的核对清单原样复述给主人（如「1. 工具都成功执行了
    2. 主人要求：…」），或回显 `工具名（成功/失败）` 这种 schema 结果。

    按行删：行首是序号，且行内出现内部过程词或 schema 回显格式就整行去掉。
    要求「序号 + 内部词」同时成立，避免误删主人的分步说明。
    """
    if not text:
        return text
    keep = []
    for line in text.split("\n"):
        if _INTERNAL_CHECKLIST_LINE_RE.match(line) and _INTERNAL_VOCAB_RE.search(line):
            continue
        if _SCHEMA_ECHO_LINE_RE.match(line):
            continue
        keep.append(line)
    text = "\n".join(keep)
    # 行内序号（句号后面开始的清单）
    text = re.sub(rf"[^。！？\n]*?{_LINE_INDEX_RE_BODY}[^\n]*?_INTERNAL_VOCAB_END",
                  "", text)
    # 无序号的单句叙述
    text = _NARRATION_SENT_RE.sub("", text)
    return text


# 句内序号（不带行首锚点，供行内清理拼正则用）
_LINE_INDEX_RE_BODY = r"\d{1,2}\s*[.、)）]\s*"
# 以内部词结尾的清单行剩余部分（吃到换行）
_INTERNAL_VOCAB_END = r"[^\n]*?(?:工具都成功执行了|数据来源确认|主人要求：)[^\n]*"

# 只在**句级**使用的收窄词表。
# 不能复用 _INTERNAL_VOCAB_RE：那里有「我查」「我搜」，
# 按宽表删会把正文清空。真正只属于内部叙述的是「抓取/爬取页面」「调用了 schema」类描述。
_NARRATION_SENT_RE = re.compile(
    r"[^。！？\n]*?(?:"
    # 过渡语 + 查/搜索/看 —— 只有**带工具标志**时才算工具独白（Stage 1.3）
    r"(?:让我|我来|我先)\s*(?:查|搜索|搜|看|打开|抓)[^。！？\n]{0,20}?"
    + _TOOL_CALL_MARKER +
    r"|从第?[一二三四五六七八九十\d]*次?(?:抓取|爬取|搜索|访问|打开)[^。！？\n]{0,25}"
    r"|(?:抓取|爬取|爬虫)(?:了)?[^。！？\n]{0,20}"
    r"|调用了?\s*schema[^。！？\n]{0,20}"
    r"|(?:请|需要)?给(?:主人|用户)最终(?:答复|回答|总结)[^。！？\n]{0,10}"
    r"|(?:给|对)主人(?:一个|一份)?(?:完整|最终)?(?:答案|答复|总结)[^。！？\n]{0,10}"
    r"|工具(?:都|已)?(?:成功)?执行(?:了|完)?[^。！？\n]{0,15}"
    r")[^。！？\n]*[。！？]?",
    re.UNICODE)


# 行首序号（1. / 1) / 1、/ 第1点）
_LINE_INDEX_RE = r"^\s*(?:\d{1,2}\s*[.、)）]|[（(]\d{1,2}[)）]|第\s*\d{1,2}\s*[点项步])"
# 内部过程词：这些词只可能出现在给模型看的核对清单里，不该给主人看
_INTERNAL_VOCAB_RE = re.compile(
    r"(工具(?:都|已)?(?:成功)?执行|本轮工具|执行结果|结果核对|数据来源|"
    r"主人要求|逐条对照|核对(?:结果|过程|清单)|是否真的成功|"
    r"主人原话|已搜到|搜到了相关数据|我(?:来|先)?查|我(?:来|先)?搜索|"
    r"抓取(?:了)?(?:页面|内容)|爬(?:取|虫)|调用(?:了)?(?:工具|schema))",
    re.UNICODE)
# 「工具名（成功）」「工具名（失败）：…」这种 schema 结果回显
_SCHEMA_ECHO_LINE_RE = re.compile(
    _LINE_INDEX_RE + r"\s*[A-Za-z_][A-Za-z0-9_]{1,30}\s*[（(]\s*(?:成功|失败|已取消)\s*[)）]",
    re.UNICODE)
_INTERNAL_CHECKLIST_LINE_RE = re.compile(_LINE_INDEX_RE, re.UNICODE)


#: 中文占比低于该值 → 判定「这条回复几乎不是中文」（清洗后会被整段丢弃）
_LOW_CJK_RATIO = 0.15


def _cjk_ratio(text: str) -> float:
    """文本段里中日韩字符的占比（区分英文 CoT 与中文正文）。"""
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff"
              or "\u3040" <= ch <= "\u30ff")
    return cjk / len(text)


def is_low_cjk_reply(text: str) -> bool:
    """这条回复是不是「几乎全是英文 / 乱码」。

    Stage 1.3：清洗管线对这类文本会**整段丢弃**（中文占比 < 15%），
    丢完等于把主人的话弄没了。调用方（AgentLoop）据此判定：
    **重试一次**，重试仍失败才走空回复兜底。

    Args:
        text: 模型原始输出（未经清洗）

    Returns:
        True = 中文占比过低，应当重试；False = 正常（或本来就是空的）
    """
    if not text:
        return False
    body = _EMOTION_TAG_ANY_RE.sub("", text).strip()
    if not body:
        return False
    return _cjk_ratio(body) < _LOW_CJK_RATIO


def _drop_leading_low_cjk_paragraphs(text: str) -> str:
    """剥离开头的低中文占比段落。

    推理模型的英文 CoT 会以纯文本漏进正文，且常不带 <think> 标签，
    正则无法匹配，只能按段落中文占比判定：
    首段 CJK 占比 < 15% 且后续存在占比 ≥ 30% 的段落时，
    连续剥离开头的低占比段。中文正文（占比通常 > 60%）与纯英文回复均不受影响。
    """
    paras = text.split("\n\n")
    if len(paras) < 2:
        return text
    ratios = [_cjk_ratio(p) for p in paras]
    if ratios[0] >= 0.15:
        return text
    if not any(r >= 0.3 for r in ratios[1:]):
        return text
    i = 0
    while i < len(paras) and ratios[i] < 0.15:
        i += 1
    return "\n\n".join(paras[i:]).strip()


def _drop_low_cjk_paragraphs(text: str, threshold: float = 0.3) -> str:
    """剥离任何占比低于 threshold 的段落（尾部工具独白兜底）。

    开头段已在 _drop_leading_low_cjk_paragraphs 中剥离；本函数处理
    夹在中文段落之间或结尾的低中文占比段（Ollama/MiniMax 等推理模型常把工具
    名称、URL 等英文夹杂在尾部）。
    """
    paras = text.split("\n\n")
    if len(paras) < 2:
        return text
    kept = [p for p in paras if _cjk_ratio(p) >= threshold]
    return "\n\n".join(kept).strip()


# 「反思 / 否认工具行为」语境（如「以后小汐会注意的…老老实实调用工具」）
# _META_TOOL_SENT_RE 里的「调用…工具」会命中，这种句子讲的是对工具的态度，必须放行
_REFLECTION_RE = re.compile(
    r"(?:不再|别再|没再|不会再|不该|不应该|没有(?:再)?(?:调用|使用)?工具|"
    r"假装|骗|糊弄|老老实实|以后.{0,8}注意|下次(?:会|不)|改掉|改正|"
    r"是不是在骗|有没有骗|有没有调用|是不是(?:在)?(?:假装|骗))"
)


def _is_pollution_sentence(core: str) -> bool:
    """判断一个句子单元是否为 CoT 规划 / 元描述 / 规则复读（高置信、保守）。"""
    if _META_RULE_KEYWORDS_RE.search(core):
        return True
    if _META_CHAR_SHEET_RE.search(core):
        return True
    if _META_USER_REPEAT_SENT_RE.search(core):
        return True
    if _META_TOOL_SENT_RE.search(core):
        # 工具独白判定前先让「反思语境」过关
        if not _REFLECTION_RE.search(core):
            return True
    if _NUMBERED_RULE_RE.search(core):
        return True
    # 句首过渡/规划 +（工具调用标志 或 复述系统设定的语境）→ 污染
    # ⚠️ Stage 1.3 的判别标准：**过渡语 + 工具标志 = 工具独白（删）；
    #    只有过渡语 = 自然过渡（留）** —— 「我来帮你查一下。」必须活着。
    if (_META_SELF_PLAN_SENT_RE.search(core)
            and (_TOOL_CALL_MARKER_RE.search(core)
                 or _META_PLAN_CONTEXT_RE.search(core))):
        return True
    # 英文为主的句子（推理模型英文 CoT）
    cjk = sum(1 for ch in core if "\u4e00" <= ch <= "\u9fff")
    ascii_letters = sum(1 for ch in core if ch.isascii() and ch.isalpha())
    if ascii_letters > 10 and cjk / max(1, ascii_letters + cjk) < 0.15:
        return True
    return False


def _split_answer_anchor(core: str) -> Optional[str]:
    """污染句里若在「角色发语锚点」之后还有正常回答，返回该回答（含锚点），否则 None。"""
    for m in reversed(list(_ANSWER_ANCHOR_RE.finditer(core))):
        tail = core[m.start():].strip()
        if len(tail) < 4:
            continue
        if _is_pollution_sentence(tail):
            continue
        cjk = sum(1 for ch in tail if "\u4e00" <= ch <= "\u9fff")
        if cjk >= 2:
            return tail
    return None


def _strip_leading_english_prefix(core: str) -> str:
    """句首若为一段较长英文（CoT 碎片），其后紧跟中文回答，剥掉英文前缀。

    处理英文 CoT 与中文回答黏在同一句（无句号分隔）的情况，如
    "- The story involves ... old friend搜到啦~……"。
    阈值要求英文前缀 ≥ 15 个字母且 ≥ 3 个英文单词，避免误伤
    "VS Code 打开了吗"、"打开 VS Code 和 Chrome" 等正常中英混排。
    """
    n = len(core)
    k = 0
    while k < n and not ("\u4e00" <= core[k] <= "\u9fff"):
        k += 1
    if k == 0 or k >= n:
        return core   # 本来就中文开头，或整句无中文（交给英文占比判定）
    prefix = core[:k]
    letters = sum(1 for ch in prefix if ch.isascii() and ch.isalpha())
    words = len(re.findall(r"[A-Za-z']+", prefix))
    if letters >= 15 and words >= 3:
        rest = core[k:].strip()
        if rest:
            return rest
    return core


def _strip_meta_by_sentence(text: str) -> str:
    """逐句剥离规划 / 元描述 / 规则复读，保留角色回答句。

    - 非污染句原样保留；
    - 污染句若含角色发语锚点，只保留锚点之后；
    - 否则整句丢弃。
    """
    parts = _SENT_SPLIT_RE.split(text)
    out: list[str] = []
    for i in range(0, len(parts), 2):
        body = parts[i]
        sep = parts[i + 1] if i + 1 < len(parts) else ""
        core = body.strip()
        if not core:
            continue
        # 工具过程废话子句按短语剥掉；仅在确实删除时才 strip 残留标点
        core_before = core
        core = _META_PROCESS_CLAUSE_RE.sub("", core)
        if core != core_before:
            core = core.strip("，, ~～")
        # 句首英文 CoT 前缀剥离
        core = _strip_leading_english_prefix(core)
        if not core:
            continue
        if _is_pollution_sentence(core):
            kept = _split_answer_anchor(core)
            if kept:
                out.append(kept + sep)
            continue
        out.append(core + sep)
    return "".join(out)


# LLMError 已迁移到 app.core.errors —— 这里 re-export 以保持向后兼容
from app.core.errors import LLMError  # noqa: E402, F401


@dataclass
class ChatMessage:
    role: str
    # content 可以是普通字符串,也可以是 multimodal list[dict]
    # (OpenAI 风格: [{type: text|text, ...}, {type: image_url, ...}])
    content: object


def _extract_text_chunk(delta: dict) -> str:
    """从 SSE delta 里拿正文（只拿最终回复 content，不拿 reasoning）。

    注意：**永远不**回退到 reasoning_content / reasoning。
    推理模型的思考内容不应该作为正文 yield 给用户。
    如果 content 为空但 reasoning 有内容，返回空字符串。
    """
    return delta.get("content") or ""


def _extract_reasoning_chunk(delta: dict) -> str:
    """从 SSE delta 里拿推理模型的思考内容（reasoning_content / reasoning）。"""
    return (
        delta.get("reasoning_content")
        or delta.get("reasoning")
        or ""
    )


def _answer_from_reasoning(reasoning: str) -> str:
    """从推理模型的思考痕迹里捞最终答案。

    推理模型（DeepSeek-R1 / MiniMax-M3 等）有时整轮只吐 reasoning_content
    而不吐 content。这时**不需要重发一次请求**（那是第二次计费），
    直接从已经收到的思考痕迹里取答案即可。

    取法：优先取最后一段（模型习惯把答案写在思考末尾）；
    太长（> 300 字，说明不是答案段）就整段返回，交给上层 sanitize。
    """
    reasoning = reasoning or ""
    if not reasoning:
        return ""
    idx = reasoning.rfind("\n\n")
    if idx > 0:
        tail = reasoning[idx + 2:].strip()
        if tail and len(tail) < 300:
            return sanitize_text(tail)
    return sanitize_text(reasoning)


# 从思考痕迹里捞出来的答案，最长多少字才算「答案」而不是「整段自述」
_MAX_REASONING_ANSWER_CHARS = 300


def _build_full_system_prompt(persona: str) -> str:
    """拼装最终 system 字符串（Stage 1.1：唯一拼装点 = 共享层）。

    这里只拿到「人设 + 上游注入的会话上下文」；中文输出约束 / 说话方式 /
    任务执行协议 / 防幻觉基线 / 长度策略 / style guide / 项目文档+skills
    全部由 app.brain.prompt_layer 统一拼 —— 两个后端因此发出**逐字节相同**
    的 system 字符串。

    ⚠️ 工具手册（【工具解锁】等级提示）需要工具注册表 + 当前等级，
    LLMClient 拿不到 —— ReAct 后端在起跑前用同一套共享层函数重拼一次
    （见 app/brain/agent.py 的 AgentLoop.assemble_system_prompt）。
    """
    return build_system_prompt(
        persona=persona or "",
        style_guide=load_style_guide(),
        tool_handbook="",
        dynamic=build_dynamic_block(),
    )


# --------------------------------------------------------------------------
# 本轮易变上下文（当前时间 / 桌宠实时状态）
# --------------------------------------------------------------------------
# 为什么它不能进 system prompt
# ----------------------
#   MiniMax 的 prompt cache 按 8192-token 块做前缀缓存。system message 落在
#   **第一块**里，块里任何一处每轮变化的内容（时间戳！）都会让整块失效 ——
#   实测缓存命中率从 98.9% 掉到 1.5%（每轮白付 ~8173 个 prompt token）。
#   所以每轮变的东西一律走 **user 消息**（排在主人这句话前面）。
#
# 为什么用「独立的 user 消息」而不是拼进主人那句话
# --------------------------------------------
#   主人原话保持字节不变：意图判定（detect_action_intent）和动作计数
#   （count_request_actions）读的是最后一条 user 消息的原文，前面拼东西
#   会让这两个判定看到不该看到的内容。

#: 动态上下文消息的标记行（同时用作幂等判断）—— 定义在共享层，这里兼容导出。


def build_dynamic_context(state=None, now=None, query: str = "") -> str:
    """构造本轮易变上下文：当前时间 + 桌宠实时状态 + 状态策略 + 召回的记忆。

    Stage 1.1：真正的拼装在共享层（app.brain.prompt_layer.build_turn_context），
    这里保留旧名 —— brain_controller / chat_window / langchain_agent 都 import 它。

    state: 可选的 PetState（有 stats_summary 就附上实时状态）
    now:   可选的 datetime（测试用；默认取当前时间）
    query: 本轮主人说的话（决定召回哪些长期记忆 —— Stage 2.1）
    """
    return build_turn_context(state=state, now=now, query=query)


def _msg_role(msg) -> str:
    """取消息的 role —— dict / ChatMessage 两种形状都认。"""
    if isinstance(msg, dict):
        return str(msg.get("role") or "")
    return str(getattr(msg, "role", "") or "")


def _msg_content(msg) -> str:
    """取消息的 content（字符串化）—— dict / ChatMessage 两种形状都认。"""
    if isinstance(msg, dict):
        content = msg.get("content")
    else:
        content = getattr(msg, "content", "")
    return content if isinstance(content, str) else ""


def is_dynamic_context(msg) -> bool:
    """这条消息是不是「本轮易变上下文」消息。"""
    return DYNAMIC_CONTEXT_HEADER in _msg_content(msg)


def inject_dynamic_context(messages: list, context: str | None = None,
                           state=None, query: str = "") -> list:
    """在最后一条 user 消息**前面**插一条「本轮易变上下文」user 消息。

    - messages 支持 [{role, content}, ...] 和 ChatMessage 两种形状。
    - 幂等：最后一条 user 消息前面已经是动态上下文 → 什么都不做。
    - 返回**新** list（不原地改调用方的 list）。
    """
    msgs = list(messages or [])
    if not msgs:
        return msgs
    idx = None
    for i in range(len(msgs) - 1, -1, -1):
        if _msg_role(msgs[i]) == "user":
            idx = i
            break
    if idx is None:
        return msgs
    if idx > 0 and is_dynamic_context(msgs[idx - 1]):
        return msgs
    ctx = (context if context is not None
            else build_dynamic_context(state=state, query=query))
    if not ctx:
        return msgs
    like = msgs[idx]
    if isinstance(like, dict):
        new_msg = {"role": "user", "content": ctx}
    else:
        try:
            new_msg = type(like)(role="user", content=ctx)
        except Exception:  # noqa: BLE001
            new_msg = {"role": "user", "content": ctx}
    msgs.insert(idx, new_msg)
    return msgs


# ---------------------------------------------------------------------------
# usage 归一化：各家 provider 的字段名千奇百怪，统一成一份「桶」字典
# ---------------------------------------------------------------------------
#
# 支持的拼写（provider -> 字段名）：
#
#   prompt tokens    prompt_tokens(OpenAI) / input_tokens(Anthropic, LangChain)
#                    / prompt_token_count(Gemini)
#   completion       completion_tokens(OpenAI) / output_tokens(Anthropic, LangChain)
#                    / candidates_token_count(Gemini)
#   total            total_tokens / total_token_count
#   cache hit        prompt_tokens_details.cached_tokens(OpenAI)
#                    prompt_cache_hit_tokens(DeepSeek) / cache_hit_tokens
#                    cache_read_input_tokens(Anthropic) / cache_read_tokens
#                    input_token_details.cache_read(LangChain)
#                    cached_content_token_count(Gemini)
#   cache write      cache_creation_input_tokens(Anthropic) / cache_write_tokens
#                    input_token_details.cache_creation(LangChain)
#   cache miss       prompt_cache_miss_tokens(DeepSeek) / cache_miss_tokens
#
# 语义差别（决定「未命中」怎么算，算错就会得出假的命中率）：
#   subset    命中数是 prompt_tokens 的**子集**（OpenAI / DeepSeek）
#             -> 未命中 = prompt_tokens - 命中数
#   exclusive prompt_tokens **不含**命中数（Anthropic 原始载荷）
#             -> 未命中 = prompt_tokens（写缓存的 token 也要算「没命中」，
#                因为写缓存同样要现算）
#   注: LangChain 的 usage_metadata.input_token_details.cache_read 走 subset ——
#   LangChain 的 input_tokens 就是 OpenAI 的 prompt_tokens（含缓存命中那部分），
#   这样同一次请求走哪条路径算出来的桶都一样。
#
# 认不出来的形状 -> 全 0 + got_real=False（上层回退到字符估算）。


def _usage_int(value) -> int:
    """usage 字段转 int（认不出来的当 0，绝不抛）。"""
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            return int(float(value.strip()))
        return 0
    except Exception:  # noqa: BLE001
        return 0


def _usage_first(raw: dict, keys: tuple[str, ...]) -> int:
    """按顺序取第一个能转成 int 的字段值（取不到就 0）。"""
    for key in keys:
        if key in raw:
            value = _usage_int(raw.get(key))
            if value:
                return value
    return 0


def _usage_cache_hit(raw: dict) -> tuple[int, str]:
    """取缓存命中（cache read）数 + 它的语义。

    返回 (tokens, semantics)，semantics ∈ {"subset", "exclusive", "none"}。
    """
    details = raw.get("prompt_tokens_details")
    if isinstance(details, dict):
        value = _usage_int(details.get("cached_tokens"))
        if value:
            return value, "subset"
    value = _usage_int(raw.get("prompt_cache_hit_tokens"))
    if value:
        return value, "subset"
    for key in ("cache_hit_tokens", "cached_tokens"):
        value = _usage_int(raw.get(key))
        if value:
            return value, "subset"
    value = _usage_int(raw.get("cache_read_input_tokens"))
    if value:
        return value, "exclusive"
    for key in ("cache_read_tokens", "cache_read_input_token_count"):
        value = _usage_int(raw.get(key))
        if value:
            return value, "exclusive"
    details = raw.get("input_token_details")
    if isinstance(details, dict):
        # LangChain 的 usage_metadata: input_tokens 就是 OpenAI 的 prompt_tokens
        # （**含**缓存命中那部分）-> subset 语义。
        # 这样同一次请求无论走轻量路径还是标准路径，算出来的桶完全一样。
        for key in ("cache_read", "cache_read_tokens"):
            value = _usage_int(details.get(key))
            if value:
                return value, "subset"
    value = _usage_int(raw.get("cached_content_token_count"))
    if value:
        return value, "subset"
    return 0, "none"


def _usage_cache_write(raw: dict) -> int:
    """取缓存写入（cache creation）数。"""
    value = _usage_int(raw.get("cache_creation_input_tokens"))
    if value:
        return value
    for key in ("cache_write_tokens", "cache_creation_tokens",
                "prompt_cache_write_tokens"):
        value = _usage_int(raw.get(key))
        if value:
            return value
    details = raw.get("input_token_details")
    if isinstance(details, dict):
        value = _usage_int(details.get("cache_creation"))
        if value:
            return value
    details = raw.get("prompt_tokens_details")
    if isinstance(details, dict):
        value = _usage_int(details.get("cache_creation_tokens"))
        if value:
            return value
    return 0


def _usage_cache_miss(raw: dict) -> int:
    """取缓存未命中数（provider 明确报了才算，0 = 没报）。"""
    value = _usage_int(raw.get("prompt_cache_miss_tokens"))
    if value:
        return value
    value = _usage_int(raw.get("cache_miss_tokens"))
    if value:
        return value
    details = raw.get("input_token_details")
    if isinstance(details, dict):
        value = _usage_int(details.get("cache_miss"))
        if value:
            return value
    return 0


#: 认得出来的 usage 字段（用来判断 provider 到底返没返 usage）
_KNOWN_USAGE_KEYS = (
    "prompt_tokens", "input_tokens", "prompt_token_count",
    "completion_tokens", "output_tokens", "candidates_token_count",
    "total_tokens", "total_token_count",
    "prompt_tokens_details", "input_token_details",
    "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
    "cache_hit_tokens", "cached_tokens", "cache_read_input_tokens",
    "cache_read_tokens", "cache_write_tokens", "cache_creation_input_tokens",
    "cache_creation_tokens", "cache_miss_tokens", "cached_content_token_count",
)


def normalize_usage(raw) -> dict:
    """把 provider 的 usage 载荷归一化成统一的「桶」字典。

    返回：
        {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int,
         "cache_hit_tokens": int, "cache_miss_tokens": int,
         "cache_write_tokens": int, "got_real": bool, "cache_hit_rate": float}

    认不出来的形状 -> 全 0、got_real=False、cache_hit_rate=0.0（绝不抛）。
    """
    empty = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cache_hit_tokens": 0,
        "cache_miss_tokens": 0,
        "cache_write_tokens": 0,
        "got_real": False,
        "cache_hit_rate": 0.0,
    }
    try:
        if not isinstance(raw, dict) or not raw:
            return empty
        prompt = _usage_first(raw, ("prompt_tokens", "input_tokens",
                                    "prompt_token_count"))
        completion = _usage_first(raw, ("completion_tokens", "output_tokens",
                                        "candidates_token_count"))
        total = _usage_first(raw, ("total_tokens", "total_token_count"))
        hit, semantics = _usage_cache_hit(raw)
        write = _usage_cache_write(raw)
        miss = _usage_cache_miss(raw)
        if not miss:
            if semantics == "subset":
                # 命中是 prompt 的子集 -> 没命中的就是剩下的
                miss = max(0, prompt - hit) if prompt else 0
            elif semantics == "exclusive":
                # prompt 不含命中部分；写缓存的也算「没命中」（写缓存要现算）
                miss = prompt + write if prompt else 0
        if not total and (prompt or completion):
            total = prompt + completion
        known = any(key in raw for key in _KNOWN_USAGE_KEYS)
        out = dict(empty)
        out.update(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            cache_hit_tokens=hit,
            cache_miss_tokens=miss,
            cache_write_tokens=write,
            got_real=known,
        )
        denom = hit + miss
        out["cache_hit_rate"] = round(hit / denom, 4) if denom > 0 else 0.0
        return out
    except Exception:  # noqa: BLE001
        return empty


class UsageEvent(str):
    """usage 事件（str 子类，文本为空）。

    为什么是 str 子类而不是 tuple：
    chat_stream 的老消费方（chat_window/worker._StreamWorker）只认 str ——
    它把每个 yield 直接丢进 Signal(str)，PyQt5 收到 tuple 会直接抛
    TypeError（实测：emit(): argument 1 has unexpected type 'tuple'），
    整轮聊天会变成一个失败气泡。

    做成「空文本的 str 子类」之后：
      - 老消费方：当成空字符串 -> UI 一个字都不变；
      - 新消费方：ev.payload / ev[1] 拿到完整 usage 字典，
        ev[0] 是 "meta"，tuple(ev) == ("meta", payload)，
        跟 LangChain 那条路径的事件形状完全一致。
    """

    __slots__ = ("payload",)

    def __new__(cls, payload: dict):
        obj = super().__new__(cls, "")
        obj.payload = dict(payload or {})
        return obj

    @property
    def event(self) -> str:
        return str(self.payload.get("event") or "usage")

    def __getitem__(self, key):
        # str 的下标访问被换成「元组式」访问：ev[0] -> "meta"，ev[1] -> payload
        if isinstance(key, str):
            return self.payload[key]
        return ("meta", self.payload)[key]

    def __iter__(self):
        return iter(("meta", self.payload))


def build_usage_event(usage: dict) -> UsageEvent:
    """把归一化后的 usage 打包成事件（两条后端路径形状一致）。"""
    payload = {"event": "usage"}
    try:
        payload.update(usage or {})
    except Exception:  # noqa: BLE001
        pass
    return UsageEvent(payload)


class LLMClient:
    # 「必须调工具」的 prompt 强制已经用了几次（同一轮对话内）。
    # 类属性兜底：测试里常用 LLMClient.__new__(LLMClient) 绕过 __init__。
    _force_attempts = 0

    def __init__(
        self,
        cfg: LLMConfig,
        system_prompt: str,
        request_timeout_s: int | None = None,
    ):
        self.cfg = cfg
        # 原始人设（+ 上游注入的会话上下文）留一份：
        # ReAct 后端（agent.AgentLoop）起跑前要用它 **加工具手册** 重拼一次
        # —— 工具注册表 / 当前等级只有那边拿得到，两个后端才拼得一模一样。
        self.base_system_prompt = system_prompt or ""
        self.system_prompt = _build_full_system_prompt(self.base_system_prompt)
        # 单次 chat 超时：调用方显式传(来自 chat.request_timeout_s)优先,否则用 cfg.timeout。
        self._request_timeout_s = (
            cfg.timeout if request_timeout_s is None else int(request_timeout_s)
        )
        # 共享的异步 HTTP 客户端（连接池复用）
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._request_timeout_s, connect=10.0),
        )
        # 「必须调工具」的 prompt 强制已经用了几次（同一轮对话内）。
        # 用它让重试的请求**真正不同于上一次**——否则第 2..N 次请求是
        # 字节级相同的重复调用（费钱且慢）。模型真调了工具就清零。
        self._force_attempts = 0

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()

    def _build_payload(self, messages: list[ChatMessage]) -> dict:
        full = [ChatMessage(role="system", content=self.system_prompt).__dict__]
        full.extend({"role": m.role, "content": m.content} for m in messages)
        payload = {
            "model": self.cfg.model,
            "messages": full,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": self.cfg.stream,
        }
        # 推理强度：仅推理模型支持，非空才带
        effort = getattr(self.cfg, "reasoning_effort", None)
        if effort:
            payload["reasoning_effort"] = effort
        # v31: 让 API 在最后 chunk 返回真实 token usage (miniMax 标准协议
        # 支持, OpenAI 兼容). stream=True 时默认不返, 加这个选项 server 才发。
        # 状态栏精确显示 token 数, 不再走 "字符数/4" 粗估。
        payload["stream_options"] = {"include_usage": True}
        return payload

    # -------------------- 视觉输入 --------------------
    @staticmethod
    def build_vision_messages(
        text: str,
        image_data_urls: list[str],
        history: Optional[list[ChatMessage]] = None,
    ) -> list[ChatMessage]:
        """构造 multimodal message list:history + 一条 user (text + images)。

        后端会自动 base64 解码 image_url,不需要再 base64 一次。
        """
        from app.brain.vision import build_multimodal_content
        out: list[ChatMessage] = list(history or [])
        out.append(ChatMessage(
            role="user",
            content=build_multimodal_content(text, image_data_urls),
        ))
        return out

    async def _fetch_non_streaming(self, messages: list[ChatMessage]) -> str:
        """非流式 fallback：拿 Ollama 自动拆分后的 message.content。"""
        payload = self._build_payload(messages)
        return await self._fetch_non_streaming_payload(payload)

    async def _fetch_non_streaming_payload(self, payload: dict) -> str:
        """用现成的 payload 发一次非流式请求。

        payload["messages"] 原样使用（**含 system 消息**，不做 [1:] 切片），
        避免重试时把「人设 / 中文约束 / 工具规则」丢掉。
        复用同一个 self._client，不新建连接。
        """
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        payload = {**payload, "stream": False}
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = await self._client.post(url, json=payload, headers=headers)
        except Exception as e:  # noqa: BLE001
            log.warning("非流式 fallback 请求失败: %s", e)
            return ""
        if resp.status_code != 200:
            log.warning("非流式 fallback HTTP %d: %s", resp.status_code,
                        resp.text[:200])
            return ""
        obj = resp.json()
        choices = obj.get("choices") or []
        if not choices:
            return ""
        msg = choices[0].get("message") or {}
        content = msg.get("content") or ""
        if content:
            return sanitize_text(content)
        # 退化：从 reasoning 末尾找 answer
        reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
        return _answer_from_reasoning(reasoning)

    async def chat_stream(
        self,
        messages: list[ChatMessage],
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[str]:
        """异步流式调用，逐 token 产出 content（自动清洗 emoji）。

        关键：区分「思考内容」和「最终回复」——
        - delta.content → 最终回复（yield 给 UI 显示）
        - delta.reasoning_content / delta.reasoning → 推理模型的思考痕迹
          （直接丢弃，绝不 yield 给 UI）

        还会处理 `` / `` 这种内嵌标签（DeepSeek-r1 早期格式）——
        如果 content 字段里塞了 `` 标签，剥掉标签内的内容，只 yield 正文。
        """
        if is_placeholder_key(self.cfg.api_key):
            raise LLMError("未配置 API Key，请先在 config.yaml 填好 llm.api_key（当前是占位符）")

        # 关了流式就别再发 SSE —— 之前照发流式、等收不到 content 再 fallback
        # 重发一次非流式，等于同一个请求发两遍（费钱且慢）。
        if not getattr(self.cfg, "stream", True):
            full = await self._fetch_non_streaming(messages)
            if full:
                yield full
            return

        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        payload = self._build_payload(messages)
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        saw_content = False
        reasoning_parts: list[str] = []   # 推理模型的思考痕迹（不 yield 给 UI）
        # v31: 攒 LLM API 返回的 usage 字段 (prompt/completion/total + 缓存三桶)。
        # 多个 chunk 里同一字段会重复, 后到的覆盖先到的 (流式协议最后一条 SSE
        # 通常带累计总量, 前面的可能只有部分增量)。
        # 这里存**原始载荷**, 收完统一交给 normalize_usage() 归一化 ——
        # 各家 provider 的字段名不一样 (cached_tokens / prompt_cache_hit_tokens /
        # cache_read_input_tokens ...), 现场按名字读字段必然漏掉缓存桶。
        usage_acc: dict = {}
        # 本次请求的序号:台账主键 = (session_id, request_index, backend, retry)
        request_index = next_request_index()
        async with self._client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise LLMError(f"HTTP {resp.status_code}: {body[:300].decode('utf-8', errors='ignore')}")
            try:
                # 处理 `` 标签（DeepSeek-r1 原生标签格式）
                in_think = False
                think_buf = ""
                async for line in resp.aiter_lines():
                    if cancel_check is not None and cancel_check():
                        break
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    # v31: 攒 usage 字段 — 必须在 choices 检查之前!
                    # 流式协议最后一条 chunk (MiniMax / OpenAI) 通常是
                    # `{"choices":[], "usage":{...}}` —— 跳 choices 检查会吞掉 usage
                    # 捕获, got_real 永远 False, 状态栏 token 数一直 0。
                    if obj.get("usage"):
                        usage = obj["usage"]
                        if isinstance(usage, dict):
                            # 原样收下, 后面统一 normalize_usage() —— 缓存桶
                            # 的字段名各家不一样, 在这里硬读必然漏。
                            usage_acc.update(usage)
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}

                    # 跳过思考内容（reasoning_content / reasoning 字段）
                    reasoning_chunk = _extract_reasoning_chunk(delta)
                    if reasoning_chunk:
                        # 只丢弃「不 yield 给 UI」，但要留着——
                        # 整轮只吐 reasoning 不吐 content 时可以直接从里面捞答案，
                        # 不必再发一次（重复计费）的请求。
                        reasoning_parts.append(reasoning_chunk)
                        continue

                    # 取 content（只取最终回复）
                    content_chunk = _extract_text_chunk(delta)
                    if not content_chunk:
                        continue
                    saw_content = True

                    # 处理 `` 标签（如果模型把思考塞在 content 里）
                    if in_think:
                        think_buf += content_chunk
                        marker = think_buf.find(THINK_TAG_END)
                        if marker >= 0:
                            rest = think_buf[marker + len(THINK_TAG_END):].lstrip("\n\r ")
                            if rest:
                                yield sanitize_text(rest, is_final=False)
                            in_think = False
                            think_buf = ""
                        continue

                    # 第一次检测到 `` 起始
                    if content_chunk.startswith(THINK_TAG_START):
                        in_think = True
                        think_buf = content_chunk[len(THINK_TAG_START):]
                        # 检查是否在同一 chunk 内闭合
                        marker = think_buf.find(THINK_TAG_END)
                        if marker >= 0:
                            rest = think_buf[marker + len(THINK_TAG_END):].lstrip("\n\r ")
                            if rest:
                                yield sanitize_text(rest, is_final=False)
                            in_think = False
                            think_buf = ""
                        continue

                    yield sanitize_text(content_chunk, is_final=False)
            finally:
                try:
                    await resp.aclose()
                except Exception:  # noqa: BLE001
                    pass

        # v31: usage 归一化 + 落台账 + 抛 usage 事件。
        # 以前 usage_acc 只写不 yield —— 无工具的纯聊天路径
        # (chat_window/worker._StreamWorker 直接调 chat_stream) 收不到任何
        # usage 事件, 状态栏 token 数永远是 0。
        # 事件形状跟 LangChain 那条路径一致: ("meta", {"event": "usage", ...})
        usage = normalize_usage(usage_acc)
        record_usage(usage, backend="lightweight",
                     model=getattr(self.cfg, "model", ""),
                     request_index=request_index)
        usage_event = build_usage_event(usage)

        # 推理模型 fallback
        if not saw_content:
            # 优先复用已经流式收到的 reasoning —— 重发一次是**第二次计费**，
            # 而且慢一倍。整轮只吐 reasoning_content 不吐 content 时，
            # 从思考痕迹里捞出的就是同一个答案。
            answer = _answer_from_reasoning("".join(reasoning_parts))
            if answer and len(answer) <= _MAX_REASONING_ANSWER_CHARS:
                log.info("LLMClient: 流式 content 为空，从 reasoning 提取答案")
                yield answer
                yield usage_event
                return
            # 一个字节都没收到 / 捞出来的只是一整段自述 → 重发一次非流式兜底
            log.info("LLMClient: 流式无有效内容，fallback 到非流式")
            full = await self._fetch_non_streaming(messages)
            if full:
                yield full
        yield usage_event

    async def chat_stream_events(
        self,
        messages: list[dict],
        tools: Optional[list[dict]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
        force_tool_use: bool = False,
    ) -> AsyncIterator[tuple[str, object]]:
        """带工具调用支持的流式接口（agent 循环用）。

        Args:
            messages: 对话历史
            tools: 工具 schema 列表
            cancel_check: 取消检查回调
            force_tool_use: True 时设 tool_choice="required"（OpenAI 协议扩展，
                让模型至少调一个工具）。很多模型（包括国产 LLM）默认行为是
                直接回文本，强制 tool_choice 解决「该调不调」的问题。

                【降级策略】
                服务端不识别 tool_choice="required" 时会返回 4xx。
                此时本方法自动 fallback：
                    1) 用不带 tool_choice 的 payload 重试一次；
                    2) 把"必须调工具"作为 user 提示注入到 messages 末尾
                       （模型至少看到这条提示，命中率大幅提升）。
                整个过程对外只产出一份事件流，调用方无感。
        """
        if is_placeholder_key(self.cfg.api_key):
            raise LLMError("未配置 API Key，请先在 config.yaml 填好 llm.api_key（当前是占位符）")

        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        # 是否要尝试 force_tool_use（只有第一轮有意义，且只有提供 tools 时才有意义）
        want_force = bool(force_tool_use and tools)

        # 如果开了强制：先尝试 tool_choice="required"，失败则降级（带 user 提示）
        if want_force:
            # 这个模型已经「确认过会忽略 tool_choice」时，直接走 prompt 强制
        # 记忆放在模块级并按模型名区分：这是模型的行为，不是单次请求的
            if _tool_choice_is_ignored(self.cfg.model):
                log.debug("LLMClient: %s 已确认忽略 tool_choice，本轮直接 prompt 强制",
                          self.cfg.model)
                async for ev, data in self._stream_with_force_prompt(
                        url, list(messages), tools, cancel_check):
                    yield ev, data
                return
            forced_msgs = list(messages)
            async for ev, data in self._stream_with_optional_force(
                    url, forced_msgs, tools,
                    tool_choice="required",
                    cancel_check=cancel_check):
                yield ev, data
            return

        # 否则走普通路径
        async for ev, data in self._stream_with_optional_force(
                url, list(messages), tools,
                tool_choice=None,
                cancel_check=cancel_check):
            yield ev, data

    async def _stream_with_optional_force(
        self,
        url: str,
        messages: list[dict],
        tools: Optional[list[dict]],
        tool_choice: Optional[str],
        cancel_check: Optional[Callable[[], bool]] = None,
        allow_prompt_fallback: bool = True,
    ) -> AsyncIterator[tuple[str, object]]:
        """底层流式调用：tool_choice 不为空时直接用，为空时普通模式。

        当 tool_choice="required" 服务端报错（400/422）：
            自动降级为「不带 tool_choice + 末尾追加 user 强制提示」重试一次。

        当 tool_choice="required" 服务端**不报错**但模型依然回了文字没调工具：
            也降级到 user-prompt 强制（这种 case 是国产模型「忽略 tool_choice 字段」，
            最常见，命中率比 tool_choice 低一些但仍有 80%+）。
        """
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        full = [ChatMessage(role="system", content=self.system_prompt).__dict__]
        full.extend(messages)
        payload = {
            "model": self.cfg.model,
            "messages": full,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": True,
        }
        # 推理强度:仅推理模型支持;非空才带
        effort = getattr(self.cfg, "reasoning_effort", None)
        if effort:
            payload["reasoning_effort"] = effort
        # v31: 让 API 在最后 chunk 返回真实 token usage (miniMax 标准协议
        # 支持, OpenAI 兼容). _build_payload 也加了, 但本方法自己构造
        # payload 不走 _build_payload, 这里要再加一次。
        payload["stream_options"] = {"include_usage": True}
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice

        saw_content = False
        saw_tool_calls = False
        try:
            async for ev, data in self._do_stream_request(
                    url, payload, headers, cancel_check):
                if ev == "finish":
                    tc = (data or {}).get("tool_calls") or []
                    saw_tool_calls = bool(tc)
                yield ev, data
        except LLMError as e:
            # 服务端不支持 tool_choice="required" → 降级
            if tool_choice and _is_tool_choice_unsupported(e):
                log.warning("LLMClient: tool_choice=%r 不被服务端支持（%s），"
                            "降级为 prompt 强制", tool_choice, str(e)[:120])
                async for ev, data in self._stream_with_force_prompt(
                        url, messages, tools, cancel_check):
                    yield ev, data
                return
            # 其它错误：原样抛
            raise

        # 模型真调了工具 → 强制提示的计数清零（下次再失败重新从 1 开始数）
        if saw_tool_calls:
            self._force_attempts = 0

        if (tool_choice and tools and not saw_tool_calls
                and not (cancel_check and cancel_check())
                and allow_prompt_fallback):
            # 服务端不报错但模型就是没调工具。记下来，下次直接走 prompt 强制
            _note_tool_choice_ignored(self.cfg.model)
            log.warning(
                "LLMClient: tool_choice=%r 服务端接受，但模型仍然没调工具，"
                "二次降级为 user-prompt 强制", tool_choice)
            async for ev, data in self._stream_with_force_prompt(
                    url, messages, tools, cancel_check):
                yield ev, data

    async def _do_stream_request(
        self,
        url: str,
        payload: dict,
        headers: dict,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[tuple[str, object]]:
        """执行一次流式请求，产 (text/finish) 事件。

        关键：区分「思考内容」和「最终回复」——
        - delta.content → 最终回复（yield 给 UI 显示）
        - delta.reasoning_content / delta.reasoning → 推理模型的思考痕迹
          （不 yield 给 UI，但累计到 reasoning_parts 给 Trace 调试用）

        这是国产推理模型（MiniMax-M3 / DeepSeek-R1 / Qwen3.5 等）的常见坑：
        SSE 流里 content 之前会先输出大量 reasoning_content，如果原样输出
        聊天窗口就会显示「嗯，用户说晚上好，我应该友好回复...晚上好主人~」，
        把思考痕迹当正文给主人看 = 体验崩溃。
        """
        saw_content = False
        reasoning_parts: list[str] = []   # 给 Trace 看，不给 UI 看

        # 瞬时故障重试：429 / 5xx / 529 等临时错误重试几次，避免主人看到空气泡
        # 重试只包住「还没吐出任何字节」的阶段，避免内容重复
        last_err: Exception | None = None
        for attempt in range(_TRANSIENT_MAX_ATTEMPTS):
            if attempt:
                await asyncio.sleep(_TRANSIENT_BACKOFF_S * attempt)
            try:
                async for ev, data in self._stream_once(
                        url, payload, headers, cancel_check):
                    yield ev, data
                return
            except LLMError as e:
                if (not _is_transient_llm_error(e)
                        or attempt == _TRANSIENT_MAX_ATTEMPTS - 1):
                    raise
                last_err = e
                log.warning("LLMClient: 瞬时故障（第 %d/%d 次）%s，%.1fs 后重试",
                            attempt + 1, _TRANSIENT_MAX_ATTEMPTS,
                            str(e)[:80], _TRANSIENT_BACKOFF_S * attempt)
        if last_err is not None:
            raise last_err

    async def _stream_once(self, url, payload, headers, cancel_check=None):
        """真正发一次流式请求（不含重试逻辑）。"""
        saw_content = False
        # v31: usage 累加器 — 多个 chunk 里 usage 会重复, 后到的覆盖先到的。
        # 存**原始载荷**, 收完统一 normalize_usage() —— 缓存桶的字段名各家
        # provider 不一样 (cached_tokens / prompt_cache_hit_tokens /
        # cache_read_input_tokens ...), 在这里硬读字段名必然漏。
        usage_acc: dict = {}
        # 台账主键的一部分: 本次请求的序号 (session_id + request_index + backend + retry)
        request_index = next_request_index()
        reasoning_parts: list[str] = []   # 给 Trace 看，不给 UI 看
        async with self._client.stream("POST", url, json=payload, headers=headers) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                raise LLMError(f"HTTP {resp.status_code}: {body[:300].decode('utf-8', errors='ignore')}")
            content_parts: list[str] = []
            tc_acc: dict[int, dict] = {}
            finish_reason = ""
            try:
                async for line in resp.aiter_lines():
                    if cancel_check is not None and cancel_check():
                        break
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    # v31: 攒 usage 字段 — 必须在 choices 检查之前!
                    # 流式协议最后一条 chunk (MiniMax / OpenAI) 通常是
                    # `{"choices":[], "usage":{...}}` —— 跳 choices 检查会吞掉 usage
                    # 捕获, got_real 永远 False, 状态栏 token 数一直 0。
                    if obj.get("usage"):
                        usage = obj["usage"]
                        if isinstance(usage, dict):
                            # 原样收下, 后面统一 normalize_usage() —— 缓存桶
                            # 的字段名各家不一样, 在这里硬读必然漏。
                            usage_acc.update(usage)
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}

                    # 1) 思考内容（reasoning_content / reasoning）—— 不给 UI，给 Trace
                    reasoning_chunk = (
                        delta.get("reasoning_content")
                        or delta.get("reasoning")
                        or ""
                    )
                    if reasoning_chunk:
                        reasoning_parts.append(reasoning_chunk)
                        yield ("meta", {"event": "reasoning_delta",
                                        "content": reasoning_chunk})

                    # 2) 最终回复（content）—— 给 UI
                    content_chunk = delta.get("content") or ""
                    if content_chunk:
                        saw_content = True
                        # chunk 只做流式安全清洗；完整文本的最终规范在 finish.content
                        clean = sanitize_text(content_chunk, is_final=False)
                        content_parts.append(clean)
                        yield ("text", clean)

                    # 3) tool_calls 增量
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        acc = tc_acc.setdefault(
                            idx, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            acc["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            acc["name"] = fn["name"]
                        if fn.get("arguments"):
                            acc["arguments"] += fn["arguments"]
                    if choices[0].get("finish_reason"):
                        finish_reason = choices[0]["finish_reason"]
            finally:
                try:
                    await resp.aclose()
                except Exception:  # noqa: BLE001
                    pass

            tool_calls = [tc_acc[i] for i in sorted(tc_acc)]
            if not saw_content and not tool_calls:
                # 模型整轮只吐 reasoning 不吐 content → 直接从已收到的思考痕迹
                # 里捞答案，**不再重发一次请求**（那是第二次计费）。
                full_text = _answer_from_reasoning("".join(reasoning_parts))
                if full_text and len(full_text) <= _MAX_REASONING_ANSWER_CHARS:
                    yield ("text", full_text)
                    content_parts.append(full_text)
                else:
                    # 流式一个字节都没吐（丢包）→ 才重发一次非流式兜底。
                    # 用完整的 payload（含 system 消息），不切 [1:]——
                    # 否则重试时人设 / 中文约束 / 工具规则全丢了。
                    fallback_payload = {
                        k: v for k, v in payload.items()
                        if k not in ("tools", "tool_choice")
                    }
                    full_text = await self._fetch_non_streaming_payload(
                        fallback_payload)
                    if full_text:
                        yield ("text", full_text)
                        content_parts.append(full_text)
            # finish 事件带上 reasoning（如果非空）→ 上层可选择写到 Trace。
            # content 做一次最终规范（is_final=True）：保证任何不经过上层
            # _on_done 的消费方（子 agent / 路由之外的文本出口）拿到的也是干净文本。
            final_content = sanitize_text("".join(content_parts), is_final=True)
            # v31: usage 归一化（多了缓存命中 / 未命中 / 写入三个桶）+ 落台账。
            # 形状跟 chat_stream 和 LangChain 那条路径完全一致。
            usage = normalize_usage(usage_acc)
            record_usage(usage, backend="lightweight",
                         model=getattr(self.cfg, "model", ""),
                         request_index=request_index)
            yield ("finish", {
                "reason": finish_reason,
                "tool_calls": tool_calls,
                "content": final_content,
                "reasoning": "".join(reasoning_parts),   # 给 Trace 用，不给 UI
                # v31: API 返回的真实 token usage (got_real=True 时) — chat_window
                # 用这个在状态栏精确显示, 不再走 "字符数 / 4" 粗估。某些 API
                # (老 ollama / 本地) 不返 usage, got_real=False, 上层 fallback
                "usage": usage,
            })

    async def _stream_with_force_prompt(
        self,
        url: str,
        messages: list[dict],
        tools: Optional[list[dict]],
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> AsyncIterator[tuple[str, object]]:
        """降级路径：去掉 tool_choice，在 messages 末尾追加 user 强制提示。

        这条 user 提示会显著提升国产模型「看到 tools 但仍然直接回文本」
        时的工具调用命中率。

        提示里带「第 N 次」标记：让重试的请求与上一次**真正不同**
        （否则就是同一个请求重复发，既费钱又慢）。
        """
        self._force_attempts += 1
        attempt = self._force_attempts
        forced = list(messages)
        forced.append({
            "role": "user",
            "content": (
                "[系统指令] 你**必须**调用上面 schema 里列出的工具来完成用户的需求，"
                "**禁止**只用文本回复。请在这次响应里调用至少一个工具。"
                "如果用户的要求不止一个，就在**同一次响应里把所有需要的工具都发出来**。"
                + (
                    f"（这是第 {attempt} 次要求：上一次你仍然只用文字回答了，"
                    f"这次请直接发出工具调用，不要再输出任何说明文字。）"
                    if attempt > 1 else ""
                )
            ),
        })
        async for ev, data in self._stream_with_optional_force(
                url, forced, tools,
                tool_choice=None,
                cancel_check=cancel_check):
            yield ev, data

    async def chat_once(self, messages: list[ChatMessage]) -> str:
        parts: list[str] = []
        async for tok in self.chat_stream(messages):
            parts.append(tok)
        return "".join(parts)


#  工具调用意图识别（轻量级正则判定，用于自动开启 force_tool_use）
# 命中后第一轮强制调工具
_INTENT_TOOL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 提醒类（多种常见说法）—— 放在最前，避免被 open_app 的「开」误匹配
    ("add_reminder", re.compile(
        r"(?:(?:帮?我)?(?:设置|设个|设一下|加个|定个|定)?(?:提醒|闹钟)|"
        r"提醒我|叫我|到点叫我|"
        r"\d+\s*(?:分钟|秒钟|秒|小时|天|个钟头?)\s*(?:之后?|后)\s*提醒|"
        r"(?:之后?|后)\s*(?:提醒|叫我|告诉我))"
        r"[^.。!！?？\n]*",
        re.UNICODE)),
    # 备用：纯「XX 分钟后」类
    ("add_reminder", re.compile(
        r"\d+\s*(?:分钟|秒钟|秒|小时|天|个钟头?)\s*(?:之后?|后)\s*"
        r"(?:提醒|叫我|告诉我)",
        re.UNICODE)),
    # 「10 秒后叫我」「5 分钟后告诉我要」
    ("add_reminder", re.compile(
        r"\d+\s*(?:分钟|秒钟|秒|小时|天|个钟头?)"
        r"\s*(?:之后?|后)\s*(?:叫我|告诉我|喊我)",
        re.UNICODE)),
    # 记住类（也在 open_app 前，避免「记住 QQ」之类被误判）
    ("remember_fact", re.compile(
        r"(?:请|帮我)?(?:记住|记一下|别忘了|记着|记下)"
        r"(?:[：:，,。\s]*)"
        r"([\u4e00-\u9fffA-Za-z0-9].{1,80})",
        re.UNICODE)),
    # 中文动作：打开 / 启动 / 拉起 / 调出 / 运行 / 打开 XX 吧
    # 「开」单独使用时必须后接空白 / 引号 / 数字 / 字母，避免「开会」误匹配
    ("open_app", re.compile(
        r"(?:帮我|请)?(?:打开|启动|拉起|调出|运行|调用|跑)"
        r"\s*[\"「『]?([^\s\"」』。,，.!！?？]{1,30})",
        re.UNICODE)),
    # 单独的「开」+ 空格 / 引号 / 数字 等明确动作语境
    ("open_app", re.compile(
        r"(?:帮我|请)?开\s+[\"「『]?([^\s\"」』。,，.!！?？]{1,30})",
        re.UNICODE)),
    # 查询类（计算/时间/状态/单位换算/农历/截图/系统信息）
    # 「今天」必须后接 (几号|周几|星期几|几)，避免「今天心情不错」误匹配
    ("query", re.compile(
        r"(?:(?:现在)?几[点号]了?|今天(?:几号|周几|星期几)|现在时间|"
        r"你(?:怎么?样|状态如何|心情如何|饿不饿))",
        re.UNICODE)),
    # 备用：屏幕/系统类查询
    ("query", re.compile(
        r"(?:(?:电脑|机器|系统|内存|CPU|处理器)\s*(?:配置|信息|占用|多少|咋样|如何)?|"
        r"看下?(?:下)?(?:桌面|屏幕)|截(?:一张|个)?图|"
        r"查(?:一下)?\s*(?:cpu|内存|CPU|占用|天气|温度)|"
        r"(?:帮我)?(?:查|看看|了解)(?:一下)?\s*(?:系统|电脑|机器|配置|状态))",
        re.UNICODE)),
    # 天气查询意图，容忍「查…天气」中间夹城市名
    ("get_weather", re.compile(
        r"(?:天气|气温|温度|降雨|降水概率|冷不冷|热不热|要不要带伞|该带伞)"
        r"|查(?:一下|一查|查)?\s*[^，。！？\n]{0,8}?(?:天气|气温|温度)"
        r"|[^，。！？\n]{0,8}?天气(?:怎么样|如何|如何呀|咋样|好吗)",
        re.UNICODE)),
    # ActivityWatch 活动查询意图：触发 force_tool_use 让国产模型乖乖发出 tool_calls
    # 触发关键词：刚才/最近/过去/在干嘛/在做什么/看了什么/用过什么/
    #             当前窗口/焦点窗口/打开什么/哪些网站
    ("aw_query", re.compile(
        r"(?:"
        # 「我刚才在干嘛/做什么/看了什么/用了什么」
        #    / 「过去一小时看了什么」(中文时间词 + 动词)
        r"(?:我|主人)?(?:刚才|刚刚|之前|最近|过去)"
        r"(?:(?:一?[半个]?|(?:[两三四五六七八九零一1-9]|\d+)\s*)?\s*(?:分钟|秒钟|秒|min|小时|h|个?钟头))?"
        r"(?:在|都|)?"
        r"(?:干(?:嘛|什么|了些啥|了啥)|做(?:什么了|些啥|了啥)|看了?什么|用了?什么|看过?什么|浏览过?什么)"
        r"|"
        # 「我现在用哪个窗口/当前打开什么应用」
        r"(?:我|主人)?(?:当前|此刻|现在|正?在)"
        r"(?:用|开|看|浏览|焦点)?(?:哪个|什么|哪儿|啥)"
        r"(?:窗口|应用|程序|网站|标签|页面|网页)"
        r"|"
        # 「最近看了哪些网站」
        r"(?:我|主人)?(?:最近|刚才|刚刚|过去)"
        r"(?:逛了?|看了?|打开了?|上过?|用过?)?\s*(?:哪些|什么|哪)(?:网站|网页|标签|应用)"
        r"|"
        # 「aw 状态 / 工具在不在」
        r"aw\s*(?:状态|工具|在不在|有没有)"
        r"|"
        # 「ActivityWatch / 活动感知 在吗」
        r"(?:activity\s*watch|活动感知)\s*(?:在吗|开了吗|状态|怎么用)"
        r")",
        re.UNICODE | re.IGNORECASE)),
    # 搜索类（实时信息/时效性内容/未知答案）—— web_search 工具
    # 触发 force_tool_use 的关键词：
    # - 时效性词（最新一集/最近一期等）
    # - 求知欲（搜一下/搜索/查查 + XX 是什么）
    # - 内容查询（讲了什么/说了什么/演了什么）
    # - 事实核查（XX 是不是 / XX 真的吗）
    ("web_search", re.compile(
        r"(?:最新[一]?(?:集|期|话|话集|章节|章|话剧情|消息|动态|进展|进展如何)|"
        r"最近[一]?(?:集|期|话|章|更新|发布)|"
        r"(?:(?:帮?我)?(?:搜|搜索|查查|查一下|查一查|找下?|找一下|搜一下|搜一搜|搜搜))"
        r"[^.。!！?？\n]{0,40}|"
        r"(?:讲(?:了|的)?什么|演(?:了|的)?什么|说(?:了|的)?什么|内容|剧情|进展|消息|"
        r"新(?:闻|动态)|官方(?:说|发[布布]?))"
        r"[^.。!！?？\n]{0,40}|"
        r"(?:现在|当前|今年|最近)\s*(?:.{1,20}?)\s*(?:怎么样|如何|怎样|什么|几号|多少))",
        re.UNICODE)),
)


def detect_action_intent(text: str) -> Optional[str]:
    """轻量判定用户消息是否需要工具调用。

    返回：
        - None：自由文本（闲聊/情绪/否定），不强制调工具
        - str：命中的工具类别（"open_app"/"add_reminder"/"remember_fact"/
              "query"/"get_weather"/"web_search"），
              代表「这一轮必须调工具，否则就是幻觉」
    """
    if not text:
        return None
    text = text.strip()
    for intent, pat in _INTENT_TOOL_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        # 否定要认：触发词前面带否定词的一律不算（主人说「别做」桌宠不能做完还报喜）
        if _NEGATION_RE.search(text[max(0, m.start() - 8):m.start()]):
            log.debug("detect_action_intent: %s 命中但前面有否定，跳过", intent)
            continue
        return intent
    return None


# 否定词：出现在动作词之前的一个小窗口里就算数
# 紧邻匹配会漏掉「不用设提醒」（触发词在「提醒」上，前面是「不用设」）
# 放在动作词后面的不算否定——「提醒我别忘了带伞」是提醒的内容，仍要设提醒
_NEGATION_RE = re.compile(r"(?:不要|别|不用|不需要|无需|没必要|禁止|甭|勿|取消)")


def count_request_actions(text: str) -> int:
    """数出主人这句话里并列了几件要做的事（0 / 1 / 2 / 3+）。

    用纯语言分句计数，不碰工具注册表（意图名不等于实际工具名，按名字比会误判）。
    调用方拿它跟「实际执行了几个不同工具」比大小，判断是不是只做了一半。

        「查一下广州天气，提醒我明天带伞」   → 2
        「帮我搜一下十月新番，并打开B站」     → 2
        「打开QQ」                            → 1
        「今天天气怎么样」                    → 0（没有动作动词）
    """
    if not text:
        return 0
    # 并列连接词 / 标点。顿号和句号不算并列（「打开QQ、微信」是一个动作的宾语）
    parts = re.split(r"[，,；;]|\s*(?:然后|接着|再帮|并且|并|同时|顺便|另外|以及|还有)\s*",
                     text.strip())
    n = 0
    for p in parts:
        if not p.strip():
            continue
        if _ACTION_VERB_RE.search(p):
            n += 1
    return n


# 「要做一件事」的动词信号（保守，宁可少算也不要多数）
_ACTION_VERB_RE = re.compile(
    r"(?:查|搜|搜一|寻找|找一|打开|启动|运行|拉起|设置|设个|设一下|定个|定一个|加个|添加|"
    r"记住|记一下|记下|提醒|叫我|到点|翻译|算一下|计算|下载|发送|发一下|安排|"
    r"看看|看一下|看一下|列出|帮我查|帮我想|生成|写一个|新建|删除|关闭|锁屏)"
)


def _is_transient_llm_error(err: Exception) -> bool:
    """这个 LLMError 是「等一下就好」还是「再试也没用」。

    MiniMax 集群繁忙时返回 HTTP 529（{"type":"overloaded_error"}），必须重试。
    400/401/404 等重试多少次都一样，不重试。
    """
    msg = str(err) if err else ""
    for code in _TRANSIENT_STATUS:
        if f"HTTP {code}" in msg:
            return True
    return False


def _is_tool_choice_unsupported(err: Exception) -> bool:
    """LLMError 是否由 tool_choice 不被支持引起。

    经验性判定：
        - HTTP 400 / 422 / 500
        - 错误正文里出现 "tool_choice"、"unsupported"、"unknown"、
          "invalid_request" 等关键词
    """
    msg = str(err) if err else ""
    if "HTTP 400" in msg or "HTTP 422" in msg or "HTTP 500" in msg:
        lowered = msg.lower()
        if any(k in lowered for k in (
                "tool_choice", "tool choice", "unsupported", "unknown",
                "invalid_request", "unsupported value", "not support",
                "不支持", "未知参数")):
            return True
    return False
