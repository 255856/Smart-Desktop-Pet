"""大模型客户端（OpenAI 兼容协议，支持流式 + 推理模型 + Ollama + 中文清洗）。

用法：
    client = LLMClient(cfg.llm, cfg.character.persona)
    async for token in client.chat_stream([{"role": "user", "content": "你好"}]):
        ...

支持的正文字段（按优先级）：
    - content            — OpenAI 标准
    - reasoning_content  — DeepSeek-r1 推理模型
    - reasoning          — Ollama qwen3.5 风格（OpenAI 兼容端口）

Ollama reasoning 模型特殊处理：
    Ollama 在 SSE 流式时把整段（thinking + answer）都塞进 reasoning 字段，
    content 字段一直是空。本客户端检测这种情况，fallback 到非流式拿真正的 answer。

输出清洗：
    LLMClient 会自动给 system prompt 追加「中文 + 禁 emoji」规范，
    并对每条 yield 的 chunk 调用 sanitize_text 去 emoji / 装饰符号。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import AsyncIterator, Callable, Optional

import httpx

from app.core.config import LLMConfig

log = logging.getLogger(__name__)

# 推理模型的 think 标签（DeepSeek-r1 等原生标签格式）
THINK_TAG_START = "<think>"
THINK_TAG_END = "</think>"

# 中文约束片段：以对话示例形式给出（模型复读示例不会污染 UI 反而让风格更一致）
# 关键：不列编号、不写规则清单——这些格式会被模型当成"待输出文本"复述。
# 用「像这样说话」+「不要这样说话」对比，强化但避免复读。
CHINESE_SYSTEM_SUFFIX = (
    "\n\n你是用简体中文聊天的桌宠，像跟主人面对面说话一样。回答只用最终内容，"
    "不要做任何铺垫、不要解释你在想什么、不要复述主人刚才问的问题、"
    "不要把上面的要求重复念出来。下面是两个对比——\n\n"
    "✅ 像这样：「嘿嘿~主人来啦，今天想干嘛呀？~」\n"
    "❌ 不要这样：「我需要以可爱软萌角色回答。主人问我想做什么，我会说游泳。」"
    "「唯一允许的输出语言是简体中文。1. xxx 2. xxx」"
    "「根据角色设定我会……」\n\n"
    "工具调用只走 tool schema；不要在文字里描述要调什么工具。"
    "信息不够 / 不确定 / 时效性问题（天气/新闻/榜单/日期/……），"
    "必须调 web_search 拿真实信息，不允许编造或说「我不知道」。"
    "不要用 emoji 和装饰符号。回复 1~3 句。"
)


# 全部占位符 key —— 没填真 key 时 LLMClient 直接抛「未配置」而不是发请求被 401
PLACEHOLDER_KEYS: frozenset[str] = frozenset({
    "",
    "PUT-YOUR-API-KEY-HERE",
    "PUT-YOUR-MINIMAX-API-KEY-HERE",
    "PUT-YOUR-MINIMAX-KEY-HERE",
})


def is_placeholder_key(k: str) -> bool:
    return not k or k in PLACEHOLDER_KEYS


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
# 触发条件：括号里以这些开头（最长 500 字）
_META_NARRATION_RE = re.compile(
    r"[（(]"
    r"(我们刚才|刚才|现在|让我|我要|嗯|好的|作为|毕竟|事实上|好了|由于|因为|)"
    r"[^()（）\n]{3,500}"
    r"[)）]",
    flags=re.UNICODE,
)
# 兜底：任何长得像「（中文思考 200+ 字）」的整段括号
_LONG_PAREN_RE = re.compile(r"[（(][^)（）\n]{50,}[)）]")

# 兜底：模型把"我要做什么工具"类元描述裸句塞进 final answer（即使本轮真的调了工具）
# 整句（句号内）包含「工具」一词，且必须以主语（让/我/主人/LLM）+ 动作开头才剥。
# 避免误伤正文中恰好出现"工具"一词但不是元描述的情况。
# 例：「让我调用工具。」 「我应该用 open_website 工具。」 「让我调用工具查天气。」
_META_TOOL_TALK_RE = re.compile(
    r"(让我|我要|我来|我应该|我会|我将|先来|接下来|现在|"
    r"我\s*(需要|想要|打算|准备)?)"
    r"[^。.!?\n]*?"
    r"(调用|使用|借助|执行|运行)?"
    r"[^。.!?\n]*?"
    r"工具"
    r"[^。.!?\n]*"
    r"[。.!?]?",
    flags=re.UNICODE,
)
# 兜底：「这应该用XX工具来YY」类建议句（前一句结尾是句号/感叹号/问号或行首）
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
# （prompt 已明令禁止，sanitize 兜底防止 prompt 失效时污染 UI）
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
# 「我需要/我应该/我要/我来/我会 + 任意动作 + 句号」类内心独白（不带工具字也算）
_META_SELF_PLAN_RE = re.compile(
    r"(^|(?<=[。.!?\n]))"
    r"(想让我|想我要|让我|需要我|应当我|要求我|"
    r"我需要|我应该|我要|我来|我会|我打算|我将|我准备)"
    r"[^。.!?\n]{2,80}"
    r"[。.!?]?",
    flags=re.UNICODE,
)
# 兜底：模型把 system prompt 原文复读出来（推理模型 reasoning 段漏到 content 的常见漏网）
# 识别标志：句子里出现「唯一允许」「禁止使用」「禁止出现」「不要复述」「简短自然」
# 「保持角色」「回复风格」「输出规范」「输出语言」等 system 提示词关键词，
# 或者「1. xxx\n2. yyy」/「1） xxx」这种编号列表样式（明显是规则清单）
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
# 兜底：「1. xxx」「2）xxx」这种编号列表整段（看起来像 prompt 规则）
_META_NUMBERED_LIST_RE = re.compile(
    r"(?:^|(?<=[。.!?\n]))(?:[\s\u3000]*)"
    r"[0-9]+[\.．、\)）]\s*"                               # "1. " / "2）" / "3、" 编号开头
    r"[^。.!?\n]*"                                         # 本项内容（不含句末标点）
    r"[。.!?\n]",
    flags=re.UNICODE,
)

# ---------------------------------------------------------------------------
#  句子级「规划 / 元描述 / 规则复读」识别（更精细，避免旧贪婪正则吞掉同段真回答）
# ---------------------------------------------------------------------------
# 情绪标签词（系统铁律：模型不应输出，出现即剥，无论在句尾还是句中）
_EMOTION_WORDS = (
    "happy|sad|angry|surprised|scared|confused|shy|proud|thinking|talking|love|skip"
)
# 任意位置的完整情绪标签（"好的主人 [happy] 马上" 也剥）
_EMOTION_TAG_ANY_RE = re.compile(
    r"\s*\[(?:" + _EMOTION_WORDS + r")\]\s*", re.IGNORECASE)
# 未闭合的情绪标签（流式截断 / 模型漏写右括号），如句尾「……[happy」
_EMOTION_TAG_OPEN_RE = re.compile(
    r"\s*\[(?:" + _EMOTION_WORDS + r")\s*$", re.IGNORECASE)
# 孤立的 think 结束标签（起始标签在更早的 chunk 已被剥离，残留 </think>）
_THINK_CLOSE_TAG_RE = re.compile(r"</think\s*>", re.IGNORECASE)

# 角色「真正开口」的发语锚点：推理模型常把「规划前缀 + 锚点 + 真正回答」塞进同一句，
# 一旦该句被判为污染，从最后一个锚点处截断，只保留锚点之后的回答。
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
# 句首对用户的复述（"用户说 / 主人想要 / 主人在问…"）。
# 注意"主人想玩 / 主人想看 / 主人想吃"是角色对主人的正常回应语气（"主人想玩X呢，我去启动"），
# 不算复述——只有"主人想要 / 想问 / 说"这类复述需求才剥离；"用户"开头几乎必是复述。
_META_USER_REPEAT_SENT_RE = re.compile(
    r"^\s*(?:用户\s*(?:在?说|想问|想要|想|希望|问的是|说的是|是说)|"
    r"主人\s*(?:在?说|想问|想要|希望|问的是|说的是|是说))"
)
# 句首第一人称规划 / 自我过程（需配合 _META_PLAN_CONTEXT_RE 才算污染，避免误伤回答）
_META_SELF_PLAN_SENT_RE = re.compile(
    r"^\s*(?:想让我|需要我|要求我|"
    r"让我(?:想想|思考|先|来看看|看看|确认|调用|使用|用|执行|再试|尝试|检查|处理)?|"
    r"我(?:需要|应该|要|打算|准备|来|将|会|可以|不应该|不能|先|再|这就|马上))"
)
# 规划 / 元语境关键词：与句首自我规划搭配出现
_META_PLAN_CONTEXT_RE = re.compile(
    r"(角色|人设|回答|回复|简短|简洁|保持|自然|工具|调用|使用|执行|确认|需求|根据|设定|"
    r"规划|思考|试试|尝试|查询|检查|看看|应该|需要|上面|schema|反馈|再试|没成功|失败|"
    r"编造|如实|建议用户|相关网站)"
)
# 工具元描述（"这应该用 open_website 工具"、"让我调用工具"、"通过 XX 工具来…"）
_META_TOOL_SENT_RE = re.compile(
    r"(?:调用|使用|借助|运行|执行|通过)\s*[A-Za-z_0-9\u4e00-\u9fff]{0,20}?工具|"
    r"工具\s*(?:来|去|打开|执行|查|完成|处理|帮)|"
    r"这应该用|应该用\s*[a-z_]+|open_[a-z_]+\s*工具|"
    r"[a-z_]+_(?:website|app|tool|reminder|fact)",
    flags=re.IGNORECASE,
)
# 编号规则项（"1. 禁止使用 emoji"、"4.不要复述"）
_NUMBERED_RULE_RE = re.compile(
    r"^\s*[0-9]+\s*[.、)）]\s*"
    r"(?:禁止|保持|不要|输出|使用|用|回复|简短|角色|人设|复述|emoji|表情|自然|中文|句)"
)
# 句子切分（保留分隔符）
_SENT_SPLIT_RE = re.compile(r"([。！？!?\n]+)")


def sanitize_text(text: str, *, is_final: bool = True) -> str:
    """清洗 LLM 输出：去 emoji + 装饰符号 + 思考痕迹 + 多余空白 + 兜底剥离低中文占比段。

    Args:
        text: 待清洗文本
        is_final: True 表示这是一段完整的最终回复（流式累积完或非流式结果）；
                  此时启用「全文本 CJK 占比 < 15% 则整段丢弃」的兜底。
                  False 表示这是一段流式 chunk（部分内容），不做占比兜底，
                  否则单段 chunk 几乎必然被丢弃，导致流式 / langchain_agent 文本丢失。
    """
    if not text:
        return text
    # 1) 推理段 <think>...</think>（DeepSeek-r1 / MiniMax-M3 等原生标签），
    #    含未闭合的起始标签（流式截断）和孤立的结束标签（起始标签已在更早 chunk 剥掉）
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<think>.*$", "", text, flags=re.DOTALL)
    text = _THINK_CLOSE_TAG_RE.sub("", text)
    # 2) 情绪标签（仅最终阶段；任意位置都剥，系统铁律不允许出现）。
    #    流式 chunk 不剥：单个 chunk 可能是被拆开的 " [hap" / "py]"，逐 chunk 剥会
    #    把合法 chunk 变空、跳过 yield，导致文字丢失（最终阶段还会再剥一次）。
    if is_final:
        text = _EMOTION_TAG_ANY_RE.sub(" ", text)
        text = _EMOTION_TAG_OPEN_RE.sub("", text)
    # 3) emoji 与装饰符号
    text = _EMOJI_PATTERN.sub("", text)
    for sym in ["✨", "★", "☆", "♥", "♡", "♪", "♫"]:
        text = text.replace(sym, "")
    # 4) 括号式自言自语（"（让我想想…）"）与超长括号思考段
    text = _META_NARRATION_RE.sub("", text)
    text = _LONG_PAREN_RE.sub("", text)
    # 5) 先收紧空白，让后续按句切分 / 占比判定更准确
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    # 6) 最终阶段：逐句剥离 CoT 规划 / 元描述 / 规则复读（保留角色回答句）。
    #    循环到不再变化，处理"污染句被删后又暴露出新污染句"的嵌套情况。
    if is_final:
        prev_text = None
        while prev_text != text:
            prev_text = text
            text = _strip_meta_by_sentence(text)
    # 7) 多余空行收紧
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 8) 低中文占比段落剥离（推理模型英文 CoT 以纯文本漏进正文的段落级兜底）
    text = _drop_leading_low_cjk_paragraphs(text)
    text = _drop_low_cjk_paragraphs(text)
    # 9) 终极兜底（仅最终回复）：清洗后整段中文占比 < 15% → 模型完全没走中文铁律，
    #    整体丢弃，交给上层走"空回复兜底"。情绪标签第 2 步已剥，这里再剔一次再判占比。
    cleaned = text.strip()
    if is_final and cleaned:
        body_for_ratio = _EMOTION_TAG_ANY_RE.sub("", cleaned).strip()
        if _cjk_ratio(body_for_ratio) < 0.15:
            return ""
    return cleaned


def _cjk_ratio(text: str) -> float:
    """文本段里中日韩字符的占比（区分英文 CoT 与中文正文）。"""
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff"
              or "\u3040" <= ch <= "\u30ff")
    return cjk / len(text)


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


def _is_pollution_sentence(core: str) -> bool:
    """判断一个句子单元是否为 CoT 规划 / 元描述 / 规则复读（高置信、保守）。"""
    if _META_RULE_KEYWORDS_RE.search(core):
        return True
    if _META_CHAR_SHEET_RE.search(core):
        return True
    if _META_USER_REPEAT_SENT_RE.search(core):
        return True
    if _META_TOOL_SENT_RE.search(core):
        return True
    if _NUMBERED_RULE_RE.search(core):
        return True
    # 句首自我规划 + 规划语境（两者同时满足才判污染，避免误伤「让我帮你」类回答）
    if _META_SELF_PLAN_SENT_RE.search(core) and _META_PLAN_CONTEXT_RE.search(core):
        return True
    # 英文为主的句子（推理模型英文 CoT；即使夹带少量中文引用 / 语气词也判污染）。
    # 用字母占比（不含标点 / 空格）：正常中文回答 CJK 占比通常远高于 0.15，
    # 这里取低阈值并要求足够多英文字母，避免误删「打开 VS Code 和 Chrome」这类中英混排。
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


def _strip_meta_by_sentence(text: str) -> str:
    """逐句剥离规划 / 元描述 / 规则复读，保留角色回答句。

    旧版用「命中引导词就替换到段尾」的贪婪正则，当模型把 CoT 和真正回答塞进
    同一段（中间缺少句号/换行）时会连真回答一起删掉。这里改为按句末标点切句：
    - 非污染句原样保留；
    - 污染句若含角色发语锚点（嘿嘿~ / 主人… / 好的主人…），只保留锚点之后；
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
        if _is_pollution_sentence(core):
            kept = _split_answer_anchor(core)
            if kept:
                out.append(kept + sep)
            continue
        out.append(body + sep)
    return "".join(out)


class LLMError(RuntimeError):
    """大模型调用异常。"""


@dataclass
class ChatMessage:
    role: str
    content: str


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


def _build_full_system_prompt(persona: str) -> str:
    """拼接 persona + 中文输出约束。"""
    base = persona or ""
    return base + CHINESE_SYSTEM_SUFFIX


class LLMClient:
    def __init__(self, cfg: LLMConfig, system_prompt: str):
        self.cfg = cfg
        self.system_prompt = _build_full_system_prompt(system_prompt or "")
        # 共享的异步 HTTP 客户端（连接池复用）
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.cfg.timeout, connect=10.0),
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "LLMClient":
        return self

    async def __aexit__(self, *args) -> None:
        await self.close()

    def _build_payload(self, messages: list[ChatMessage]) -> dict:
        full = [ChatMessage(role="system", content=self.system_prompt).__dict__]
        full.extend({"role": m.role, "content": m.content} for m in messages)
        return {
            "model": self.cfg.model,
            "messages": full,
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": self.cfg.stream,
        }

    async def _fetch_non_streaming(self, messages: list[ChatMessage]) -> str:
        """非流式 fallback：拿 Ollama 自动拆分后的 message.content。"""
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        payload = self._build_payload(messages)
        payload["stream"] = False
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
        if not reasoning:
            return ""
        idx = reasoning.rfind("\n\n")
        if idx > 0:
            tail = reasoning[idx + 2:].strip()
            if tail and len(tail) < 300:
                return sanitize_text(tail)
        return sanitize_text(reasoning)

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

        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        payload = self._build_payload(messages)
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        saw_content = False
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
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}

                    # 跳过思考内容（reasoning_content / reasoning 字段）
                    if _extract_reasoning_chunk(delta):
                        continue   # 直接丢弃，不 yield

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

        # Ollama reasoning 模型 fallback
        if not saw_content:
            log.info("LLMClient: 流式 content 为空，fallback 到非流式")
            full = await self._fetch_non_streaming(messages)
            if full:
                yield full

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
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice

        # ---- 第一次请求 ----
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

        # ---- tool_choice="required" 服务端没报错但模型依然没调工具 → 二次降级 ----
        if (tool_choice and tools and not saw_tool_calls
                and not (cancel_check and cancel_check())
                and allow_prompt_fallback):
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
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}

                    # 1) 思考内容（reasoning_content / reasoning）——
                    #    不 yield 给 UI，但 yield 一个 meta 事件供 Trace 收集
                    reasoning_chunk = (
                        delta.get("reasoning_content")
                        or delta.get("reasoning")
                        or ""
                    )
                    if reasoning_chunk:
                        reasoning_parts.append(reasoning_chunk)
                        # 透传给 Trace（AgentLoopV2 / 上层收集）；UI 不会显示
                        yield ("meta", {"event": "reasoning_delta",
                                        "content": reasoning_chunk})

                    # 2) 最终回复（content）—— 只 yield 给 UI
                    content_chunk = delta.get("content") or ""
                    if content_chunk:
                        saw_content = True
                        # chunk 只做流式安全清洗（不做句级剥离 / CJK 兜底，避免误删）；
                        # 完整文本的最终规范在 finish.content 与上层 _on_done 完成。
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
                # 模型没回任何 content（可能是流式丢包）→ fallback 到非流式
                full_text = await self._fetch_non_streaming(
                    [ChatMessage(role=m["role"], content=m.get("content") or "")
                     for m in payload["messages"][1:]])  # 去掉 system
                if full_text:
                    yield ("text", full_text)
                    content_parts.append(full_text)
            # finish 事件带上 reasoning（如果非空）→ 上层可选择写到 Trace。
            # content 做一次最终规范（is_final=True）：保证任何不经过上层
            # _on_done 的消费方（子 agent / 路由之外的文本出口）拿到的也是干净文本。
            final_content = sanitize_text("".join(content_parts), is_final=True)
            yield ("finish", {
                "reason": finish_reason,
                "tool_calls": tool_calls,
                "content": final_content,
                "reasoning": "".join(reasoning_parts),   # 给 Trace 用，不给 UI
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
        """
        forced = list(messages)
        forced.append({
            "role": "user",
            "content": (
                "[系统指令] 你**必须**调用上面 schema 里列出的工具来完成用户的需求，"
                "**禁止**只用文本回复。请在这次响应里调用至少一个工具。"
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


# ---------------------------------------------------------------------------
#  工具调用意图识别（轻量级正则判定，用于自动开启 force_tool_use）
# ---------------------------------------------------------------------------

# 这些模式一旦命中，说明用户就是要工具做事 —— 第一轮强制调工具
_INTENT_TOOL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # 提醒类（多种常见说法）—— 放在最前，避免被 open_app 的「开」误匹配
    # （「开会」单独出现时不应该误判为 open_app）
    ("add_reminder", re.compile(
        r"(?:(?:帮?我)?(?:设置|设个|设一下|加个)?提醒|提醒我|叫我|"
        r"\d+\s*(?:分钟|秒钟|秒|小时|天|个钟头?)\s*(?:之后?|后)\s*提醒)"
        r"[^.。!！?？\n]*",
        re.UNICODE)),
    # 备用：纯「XX 分钟后」类（不需要显式「提醒」二字）
    ("add_reminder", re.compile(
        r"\d+\s*(?:分钟|秒钟|秒|小时|天|个钟头?)\s*(?:之后?|后)\s*"
        r"(?:提醒|叫我|告诉我)",
        re.UNICODE)),
    # 「10 秒后叫我」「5 分钟后告诉我要」—— 「叫我」「告诉我」也算
    ("add_reminder", re.compile(
        r"\d+\s*(?:分钟|秒钟|秒|小时|天|个钟头?)"
        r"\s*(?:之后?|后)\s*(?:叫我|告诉我|喊我)",
        re.UNICODE)),
    # 记住类（也在 open_app 前，避免「记住 QQ」之类被误判）
    # 「记住」+ 可选分隔符 + 内容（允许「记住X」「记住：X」「记住，X」）
    ("remember_fact", re.compile(
        r"(?:请|帮我)?(?:记住|记一下|别忘了|记着|记下)"
        r"(?:[：:，,。\s]*)"
        r"([\u4e00-\u9fffA-Za-z0-9].{1,80})",
        re.UNICODE)),
    # 中文动作：打开 / 启动 / 拉起 / 调出 / 运行 / 打开 XX 吧
    # 注意：「开」单独使用时必须后接空白 / 引号 / 数字 / 字母，避免「开会」误匹配
    ("open_app", re.compile(
        r"(?:帮我|请)?(?:打开|启动|拉起|调出|运行|调用|跑)"
        r"\s*[\"「『]?([^\s\"」』。,，.!！?？]{1,30})",
        re.UNICODE)),
    # 单独的「开」+ 空格 / 引号 / 数字 等明确动作语境
    ("open_app", re.compile(
        r"(?:帮我|请)?开\s+[\"「『]?([^\s\"」』。,，.!！?？]{1,30})",
        re.UNICODE)),
    # 查询类（计算/时间/状态/单位换算/农历/截图/系统信息）
    # 注意：「今天」必须后接 (几号|周几|星期几|几)，避免「今天心情不错」误匹配
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
)


def detect_action_intent(text: str) -> Optional[str]:
    """轻量判定用户消息是否需要工具调用。

    返回：
        - None：自由文本（闲聊/情绪），不强制调工具
        - str：命中的工具类别（"open_app"/"add_reminder"/"remember_fact"/"query"），
              代表「这一轮必须调工具，否则就是幻觉」
    """
    if not text:
        return None
    text = text.strip()
    for intent, pat in _INTENT_TOOL_PATTERNS:
        if pat.search(text):
            return intent
    return None


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
