"""角色情绪状态机。

8 种表情 + thinking/idle/talking 共 11 个状态。
对话时，模型回复末尾会带一个 [emotion] 标签，我们据此切换表情。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Emotion(str, Enum):
    IDLE = "idle"
    HAPPY = "happy"
    SAD = "sad"
    ANGRY = "angry"
    SURPRISED = "surprised"
    SCARED = "scared"
    CONFUSED = "confused"
    SHY = "shy"
    PROUD = "proud"
    THINKING = "thinking"
    TALKING = "talking"


# 模型回复末尾的 [xxx] 标签 → 情绪
TAG_TO_EMOTION: dict[str, Emotion] = {
    "happy": Emotion.HAPPY,
    "sad": Emotion.SAD,
    "angry": Emotion.ANGRY,
    "surprised": Emotion.SURPRISED,
    "scared": Emotion.SCARED,
    "confused": Emotion.CONFUSED,
    "shy": Emotion.SHY,
    "proud": Emotion.PROUD,
    "thinking": Emotion.THINKING,
    "talking": Emotion.TALKING,
}

# 匹配「整串末尾 1 个」[emotion] 标签，允许前后空格
_EMOTION_TAG_RE = re.compile(
    r"\s*\[(happy|sad|angry|surprised|scared|confused|shy|proud|thinking|talking)\]\s*$",
    re.IGNORECASE,
)


@dataclass
class ParsedReply:
    """模型回复解析结果：去掉标签后的纯文本 + 识别出的情绪。"""
    text: str
    emotion: Emotion
    tag_found: bool = False   # True = 真的在末尾识别到 [emotion] 标签；False = 用兜底默认


def parse_reply(raw: str, default: Emotion = Emotion.HAPPY) -> ParsedReply:
    """从模型回复文本**末尾**提取 [emotion] 标签，返回剩余文本与情绪。

    实现要点：
        - 只剥「整串末尾」一个标签（用 re.sub + 锚定）。
        - 不在 raw 中间做任何「贴标签识别 / 剥离」，避免误伤正文里的同类词（如「I feel happy today」）。
        - tag_found 字段让调用方区分「真的标了 HAPPY」和「没标，默认填 HAPPY」——
          这两种情况过去在 chat_window 里靠「parse 后文本 == 流式累积文本」来判定，
          容易因 sanitize_text 的多空清洗差异产生误判。
    """
    if not raw:
        return ParsedReply(text="", emotion=default, tag_found=False)
    m = _EMOTION_TAG_RE.search(raw)
    if not m:
        return ParsedReply(text=raw.rstrip(), emotion=default, tag_found=False)
    emotion = TAG_TO_EMOTION.get(m.group(1).lower(), default)
    # m.start() 即为标签起点；标签前的部分用切片取出（不会误剥文本中间的 [xxx]）
    text = raw[: m.start()].rstrip()
    return ParsedReply(text=text, emotion=emotion, tag_found=True)


# 中文情绪关键词表，用于在 LLM 没输出标签时做兜底匹配
_KEYWORD_EMOTION: list[tuple[Emotion, tuple[str, ...]]] = [
    (Emotion.HAPPY, ("开心", "高兴", "好耶", "哈哈", "喵~", "耶", "好棒", "喜欢")),
    (Emotion.SAD, ("难过", "伤心", "呜呜", "心疼", "抱歉", "对不起")),
    (Emotion.ANGRY, ("生气", "气死", "可恶", "哼", "烦")),
    (Emotion.SURPRISED, ("天呐", "哇", "天哪", "不会吧", "真的吗")),
    (Emotion.SCARED, ("害怕", "吓", "恐怖", "惊吓")),
    (Emotion.CONFUSED, ("不懂", "什么意思", "为什么", "困惑", "奇怪")),
    (Emotion.SHY, ("害羞", "脸红", "不好意思", "嘿嘿")),
    (Emotion.PROUD, ("得意", "厉害", "哈哈我", "小意思")),
    (Emotion.THINKING, ("让我想想", "稍等", "思考", "分析")),
]


def guess_emotion(text: str) -> Emotion:
    """当 LLM 没输出 [emotion] 标签时，根据关键词兜底猜一个。"""
    if not text:
        return Emotion.IDLE
    low = text.lower()
    for emo, kws in _KEYWORD_EMOTION:
        for kw in kws:
            if kw in low or kw in text:
                return emo
    return Emotion.HAPPY