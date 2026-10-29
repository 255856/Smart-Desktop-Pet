# -*- coding: utf-8 -*-
"""Stage 1.3「拆回复清洗管线」的验收测试（离线，不需要 key / 网络 / GUI）。

对应 PLAN §1.3 的五条改动：
  A1 括号式自言自语 —— **Stage 3 起整族删除**：括号一律不再改写（见本文件末尾说明）
  A2 过渡语 / 工具独白判别器 —— **Stage 3 起删除**：只留「过渡语必须活」这半边钉子
  A4 中文占比过低 → **重试一次**（最多一次），再失败才走兜底
  A5 空回复兜底话术轮换（不再每次同一句）
  A6 正常中文回复永远不会被整条丢弃

判别标准（写在实现里，这里再钉一遍）：
    **过渡语 + 工具调用标志 = 工具独白（删）；只有过渡语 = 自然过渡（留）**

Stage 3（业主裁决「没必要硬限制」）之后前半句被判失败：那套判别器连
「让我看看。」「主人说今天会下雨」都会当工具独白删掉，所以整族正则已删除。
本文件现在钉的是**新的契约**：过渡语照旧必须活，工具独白不再由清洗管线改写，
而真正的模型工件（<think> 块 / 情绪标签 / schema 回显行）照旧必删。
"""
from __future__ import annotations

import asyncio
import os

import pytest

# Qt 单例：app.brain.agent 会拉起 Qt 相关模块，离线跑也不能弹窗
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.brain.agent import (  # noqa: E402
    AgentLoop,
    _EMPTY_REPLY_FALLBACK,
    _EMPTY_REPLY_FALLBACKS,
    _next_empty_reply_fallback,
)
from app.brain.llm_client import (  # noqa: E402
    is_low_cjk_reply,
    sanitize_text,
)


# --------------------------------------------------------------------------
# A1：括号式自言自语的机制
# --------------------------------------------------------------------------

class TestNarrationBracket:
    """Stage 3：括号内容**一律不再改写**（连「先认规划词再吃括号」也不要了）。"""

    def test_planning_bracket_is_no_longer_stripped(self):
        """规划词开头的括号也保留。

        Stage 1.3 曾把它收窄成「规划词开头的括号才删」；Stage 3 整族删除 ——
        「（我打算明天早点睡）」这种角色括注跟模型的思考痕迹长得一样，
        正则分不清，误删的是人话。
        """
        for raw in ("（让我查一下天气）今天真好",
                    "（我们刚才说了半天）主人这么晚说有点累。",
                    "（我打算明天早点睡）主人晚安啦~"):
            assert sanitize_text(raw) == raw, f"括号被改了：{raw!r}"

    def test_ordinary_bracket_is_kept(self):
        """拟声 / 情绪 / 括注不是规划独白，必须保留。"""
        assert sanitize_text("（小声）今天真好") == "（小声）今天真好"
        assert sanitize_text("（小声地笑）今天真好") == "（小声地笑）今天真好"
        assert sanitize_text("（笑）今天真好") == "（笑）今天真好"

    def test_arbitrary_bracket_content_is_kept(self):
        """旧实现的空分支会吞掉**任意**3~500 字括号；Stage 3 之后连正则都不在了。"""
        for raw in ("（今天真不错的一天）主人早呀", "（主人今天辛苦了）好的喵~"):
            assert sanitize_text(raw) == raw, f"括号被误吞：{raw!r}"

    def test_meta_regex_family_is_gone(self):
        """结构性钉子（Stage 3）：改写模型散文的那族正则已从模块里消失。"""
        import app.brain.llm_client as llm
        for name in (
            "_META_NARRATION_RE", "_LONG_PAREN_RE",
            "_META_TOOL_TALK_RE", "_META_SHOULD_USE_TOOL_RE",
            "_META_PLAN_NARRATION_RE", "_META_USER_REPEAT_RE",
            "_META_SELF_PLAN_RE", "_META_PROMPT_ECHO_RE",
            "_META_RULE_KEYWORDS_RE", "_META_CHAR_SHEET_RE",
            "_META_USER_REPEAT_SENT_RE", "_META_SELF_PLAN_SENT_RE",
            "_META_PLAN_CONTEXT_RE", "_META_TOOL_SENT_RE",
            "_META_PROCESS_CLAUSE_RE", "_NUMBERED_RULE_RE",
            "_NARRATION_SENT_RE", "_INTERNAL_VOCAB_RE",
            "_INTERNAL_CHECKLIST_LINE_RE", "_ANSWER_ANCHOR_RE",
            "_TRANSITION_WORDS", "_TOOL_CALL_MARKER", "_TOOL_CALL_MARKER_RE",
            "_EMOJI_PATTERN",
        ):
            assert not hasattr(llm, name), f"{name} 不该还留在清洗管线里"


# --------------------------------------------------------------------------
# A2：过渡语 vs 工具独白
# --------------------------------------------------------------------------

class TestTransitionVsToolMonologue:
    """过渡语必须活；工具独白**不再由清洗管线改写**（Stage 3 业主裁决）。

    判「这句在说工具」是猜模型意图：实测它连「主人说今天会下雨」「让我看看。」
    都会整句删掉。真正管住工具独白的是 prompt 层（【任务执行协议】+
    最终轮禁用工具），管线只管模型工件。
    """

    @pytest.mark.parametrize("raw", (
        "我来帮你查一下。",
        "让我看看。",
        "我看看。",
        "我先看看今天天气。",
        "我来查一下今天的天气吧~",
        "我要去睡觉了，主人晚安喵~",
    ))
    def test_natural_transition_survives(self, raw):
        assert sanitize_text(raw) == raw, f"过渡语被误删：{raw!r}"

    @pytest.mark.parametrize("raw", (
        "让我调用工具查一下广州天气。",
        "我需要调用 open_app 打开记事本。",
        "这应该用open_website工具来打开B站的网址。",
        "我来使用 web_search 查一下天气。",
    ))
    def test_tool_monologue_is_no_longer_rewritten(self, raw):
        """Stage 3：管线原样放行（改写模型散文的代价比漏一句独白更大）。"""
        assert sanitize_text(raw) == raw, f"流水线仍在改写模型散文：{raw!r}"

    def test_answer_next_to_monologue_is_never_taken_with_it(self):
        """这条是**永久契约**：不管独白删不删，同段的正常回答绝不能被带走。"""
        raw = "我需要调用 open_app 打开记事本。今天天气不错呢。"
        out = sanitize_text(raw)
        assert out == raw
        assert "今天天气不错呢" in out


# --------------------------------------------------------------------------
# A3：emoji 保留，但 think / CoT / 规则复读照旧剥离
# --------------------------------------------------------------------------

class TestEmojiKept:
    def test_emoji_is_kept(self):
        assert sanitize_text("你好\U0001F600世界") == "你好\U0001F600世界"
        assert sanitize_text("\U0001F389恭喜主人") == "\U0001F389恭喜主人"
        assert "\u2728" in sanitize_text("闪亮\u2728一下")

    def test_think_block_still_stripped(self):
        assert sanitize_text("<think>模型在思考</think>今天天气不错呢") == "今天天气不错呢"
        assert sanitize_text("<think>还在想\n今天天气不错呢~") == ""

    def test_rule_repetition_is_no_longer_rewritten(self):
        """Stage 3：prompt 规则复读不再按关键词删（_NUMBERED_RULE_RE 已删除）。

        「1. 不要使用 emoji / 2. 保持简短」跟主人自己的分步说明长得一模一样，
        正则只能靠「编号 + 规则词」猜，猜错的代价是把真回复吃掉。
        """
        text = "1. 不要使用 emoji\n2. 保持简短"
        out = sanitize_text(text)
        assert out == text


# --------------------------------------------------------------------------
# A4：中文占比过低 → 重试一次（最多一次）
# --------------------------------------------------------------------------

class _ScriptedClient:
    """按脚本返回文本的假客户端（离线，不发网络请求）。"""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls = 0

    async def chat_stream_events(self, messages, tools=None, cancel_check=None,
                                 force_tool_use=False):
        self.calls += 1
        text = self.replies[min(self.calls - 1, len(self.replies) - 1)]
        yield "text", text
        yield "finish", {"content": text, "tool_calls": []}


class _EmptyRegistry:
    """空工具表（走不到任何工具分支）。"""

    def names(self):
        return []

    def to_openai(self):
        return []

    def execute(self, name, args):
        return ""


def _run_agent(client, max_turns: int = 2) -> list:
    """跑一轮 AgentLoop，返回事件列表（离线，不发网络请求）。"""
    loop = asyncio.new_event_loop()
    try:
        async def _collect():
            agen = AgentLoop(client, _EmptyRegistry(),
                             max_turns=max_turns).run(
                [{"role": "system", "content": "你是桌宠"},
                 {"role": "user", "content": "你好呀"}])
            return [ev async for ev in agen]
        return loop.run_until_complete(_collect())
    finally:
        loop.close()


class TestLowCjkRetry:
    def test_is_low_cjk_reply(self):
        assert is_low_cjk_reply("Hello World")
        assert is_low_cjk_reply("It is Sunday morning at 08:14")
        assert not is_low_cjk_reply("今天天气不错呢")
        assert not is_low_cjk_reply("主人今天辛苦了~")
        assert not is_low_cjk_reply("")

    def test_low_cjk_reply_is_retried_once(self):
        """第一次几乎全是英文 → 重问一次，第二次正常 → 用第二次的回答。"""
        client = _ScriptedClient([
            "Hello there, how are you doing today?",
            "今天天气不错呢，主人要不要出去玩~",
        ])
        evs = _run_agent(client, max_turns=2)
        texts = [d for e, d in evs if e == "text"]
        assert client.calls == 2, f"应该只重试一次，实际调用 {client.calls} 次"
        assert texts == ["今天天气不错呢，主人要不要出去玩~"]

    def test_low_cjk_reply_twice_falls_back(self):
        """一直返回低中文占比 → 重试一次后走空回复兜底（不再无限重试）。"""
        client = _ScriptedClient(["Hello there, how are you doing today?"])
        evs = _run_agent(client, max_turns=6)
        texts = [d for e, d in evs if e == "text"]
        assert client.calls == 2, f"最多重试一次，实际调用 {client.calls} 次"
        assert texts, "主人不能面对空气泡"
        assert texts[0] in _EMPTY_REPLY_FALLBACKS

    def test_retry_is_announced_to_ui(self):
        """重试要发 meta 事件（UI 能提示「正在重答」）。"""
        client = _ScriptedClient(["Hello there!", "喵~今天也爱你哦"])
        evs = _run_agent(client, max_turns=2)
        metas = [d for e, d in evs if e == "meta"]
        assert any(m.get("event") == "low_cjk_retry" for m in metas)


# --------------------------------------------------------------------------
# A5：空回复兜底话术轮换
# --------------------------------------------------------------------------

class TestEmptyReplyFallbackRotation:
    def test_pool_has_at_least_three_distinct_lines(self):
        assert len(_EMPTY_REPLY_FALLBACKS) >= 3
        assert len(set(_EMPTY_REPLY_FALLBACKS)) == len(_EMPTY_REPLY_FALLBACKS)

    def test_legacy_name_is_still_importable(self):
        """历史 import 不能断：_EMPTY_REPLY_FALLBACK 仍然是轮换池的第一条。"""
        assert _EMPTY_REPLY_FALLBACK in _EMPTY_REPLY_FALLBACKS

    def test_consecutive_fallbacks_differ(self):
        lines = [_next_empty_reply_fallback()
                 for _ in range(len(_EMPTY_REPLY_FALLBACKS) * 2)]
        assert all(lines[i] != lines[i + 1] for i in range(len(lines) - 1))


# --------------------------------------------------------------------------
# A6：正常中文回复永远不被整条丢弃
# --------------------------------------------------------------------------

NORMAL_REPLIES = (
    # 自然度门禁里的四条基线
    "今天天气不错呢，主人要不要出去玩~",
    "广州今天晴天33度，明天有小雨，记得带伞喵~",
    "我搜索了一下最近的新闻，主人要听吗？",
    "唔……那个……水杯是不是空了呀，先喝口水吧。",
    # 日常口语
    "主人今天辛苦了~",
    "唔……那我先去睡会儿",
    "好呀，我记下啦",
)


def test_normal_reply_is_never_emptied():
    """正常回复既不能被清空，也不能被改（字节一致）。"""
    for raw in NORMAL_REPLIES:
        out = sanitize_text(raw)
        assert out, f"正常回复被整条丢弃：{raw!r}"
        assert out == raw, f"正常回复被改了：{raw!r} -> {out!r}"


def test_transition_reply_is_not_emptied():
    """带过渡语的正常回复也不能被清空。"""
    for raw in ("我来帮你查一下。", "让我看看。", "我看看。",
                "今天真不错呢，我来看看主人的日程~"):
        assert sanitize_text(raw) == raw, f"过渡语回复被改了：{raw!r}"
