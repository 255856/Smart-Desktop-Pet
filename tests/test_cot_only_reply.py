# -*- coding: utf-8 -*-
"""Stage 2.10：CoT 吃光输出预算 → 主人看不到答案 → 重问一次。

背景
----
MiniMax-M3 把英文思维链以 <think>...</think> 塞在 message.content 里，
而 **CoT 的 token 计入 max_tokens**：CoT 比输出预算还长 → 回复在 CoT 中间
被截断 → 清洗管线把 CoT 剥光 → 主人只看到兜底话术（看不到任何答案）。

这里锁住四件事
--------------
1. 客户端认得出「整轮只有思维链」（finish 事件里的 cot_only 标记）；
2. AgentLoop 收到标记后**重问一次**（最多一次），重问用更大的输出预算；
3. 正常回复**不触发**重问；低中文那条重试跟这条**互斥**（不会连发两次）；
4. 重问不动 system 消息（前缀缓存不失效）+ 可见答案里永远没有 CoT。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.brain.agent import (AgentLoop, _COT_ONLY_RETRY_HINT,  # noqa: E402
                             _COT_ONLY_RETRY_MAX_TOKENS,
                             _EMPTY_REPLY_FALLBACKS)
from app.brain.llm_client import (LLMClient, is_cot_only_reply,  # noqa: E402
                                  is_low_cjk_reply, load_output_budget,
                                  sanitize_text)
from app.core.config import LLMConfig  # noqa: E402

# 一段典型的英文思维链（MiniMax-M3 的真实风格）
COT = ("We need answer Chinese in persona, short but this is technical. "
       "Need to explain asyncio.gather vs create_task, mention blocking IO, "
       "event loop, etc. Let's draft carefully and then output the answer.")

#: CoT 被预算截断后的典型形态：只有开标签，没有闭标签，也没有可见正文
COT_TRUNCATED = "<think>" + COT


# --------------------------------------------------------------------------
# 离线假客户端
# --------------------------------------------------------------------------

class _FakeResp:
    """假的 httpx 流式响应。"""

    status_code = 200

    def __init__(self, lines: list[str]):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self) -> bytes:
        return b""

    async def aclose(self) -> None:
        return None


class _FakeHttp:
    """假的 httpx.AsyncClient（只实现 stream）。"""

    def __init__(self, lines: list[str]):
        self.lines = lines
        self.sent: list[dict] = []

    def stream(self, method, url, json=None, headers=None):
        self.sent.append(json)
        return _FakeResp(self.lines)

    async def aclose(self) -> None:
        return None


def _sse(chunks: list[str], finish_reason: str = "stop") -> list[str]:
    """把若干 content chunk 拼成 SSE 行。"""
    lines = []
    for c in chunks:
        lines.append("data: " + json.dumps(
            {"choices": [{"delta": {"content": c}}]}))
    lines.append("data: " + json.dumps(
        {"choices": [{"delta": {}, "finish_reason": finish_reason}]}))
    lines.append("data: [DONE]")
    return lines


def _client(lines: list[str], max_tokens: int = 1024) -> tuple[LLMClient, _FakeHttp]:
    cfg = LLMConfig(api_key="sk-test-not-a-placeholder",
                    base_url="https://api.minimaxi.com/v1",
                    model="MiniMax-M3", temperature=0.8, max_tokens=max_tokens)
    client = LLMClient(cfg, "你是桌宠")
    fake = _FakeHttp(lines)
    client._client = fake
    return client, fake


async def _collect_finish(lines: list[str], max_tokens: int = 1024) -> dict:
    """跑一次 chat_stream_events，返回 finish 事件。"""
    client, fake = _client(lines, max_tokens)
    try:
        out = {}
        async for ev, data in client.chat_stream_events(
                [{"role": "user", "content": "主人说的话"}]):
            if ev == "finish":
                out = data
        out["_payload"] = fake.sent[0] if fake.sent else {}
        return out
    finally:
        await client.close()


class _ScriptedClient:
    """按脚本逐轮返回的假 client（离线，不发网络请求）。

    turns: [{"text": str, "cot_only": bool}]
    text  = 客户端 yield 出去的**已清洗**正文（跟真实客户端一致）
    """

    def __init__(self, turns: list[dict]):
        self.turns = list(turns)
        self.calls = 0
        self.max_tokens_seen: list = []
        self.messages_seen: list = []

    async def chat_stream_events(self, messages, tools=None, cancel_check=None,
                                 force_tool_use=False, max_tokens=None):
        self.calls += 1
        self.max_tokens_seen.append(max_tokens)
        self.messages_seen.append([dict(m) for m in messages])
        turn = self.turns[min(self.calls - 1, len(self.turns) - 1)]
        text = turn.get("text") or ""
        if text:
            yield "text", text
        yield "finish", {"content": text, "tool_calls": [],
                         "cot_only": bool(turn.get("cot_only"))}


class _EmptyRegistry:
    """空工具表（走不到任何工具分支）。"""

    def names(self):
        return []

    def to_openai(self):
        return []

    def execute(self, name, args):
        return ""


def _run_agent(client, max_turns: int = 3) -> list:
    """跑一轮 AgentLoop，返回事件列表（离线）。"""
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


# --------------------------------------------------------------------------
# 1) 判据函数
# --------------------------------------------------------------------------

class TestIsCotOnlyReply:
    def test_only_think_block_is_cot_only(self):
        assert is_cot_only_reply("<think>" + COT + "</think>")

    def test_truncated_think_block_is_cot_only(self):
        """预算在 CoT 中间用完 —— 闭标签都没有，照样是 CoT-only。"""
        assert is_cot_only_reply(COT_TRUNCATED)

    def test_think_plus_visible_answer_is_not_cot_only(self):
        assert not is_cot_only_reply(
            "<think>" + COT + "</think>主人，今天的 asyncio 关键在于……")

    def test_plain_chinese_reply_is_not_cot_only(self):
        assert not is_cot_only_reply("唔……主人今天辛苦啦~")

    def test_english_without_think_tag_is_not_cot_only(self):
        """没有 think 标签的英文回复归「低中文占比」那条重试管，不归这里。"""
        assert not is_cot_only_reply("Hello there, how are you doing today?")

    def test_empty_is_not_cot_only(self):
        assert not is_cot_only_reply("")
        assert not is_cot_only_reply("   ")


# --------------------------------------------------------------------------
# 2) 客户端：finish 事件里的 cot_only 标记
# --------------------------------------------------------------------------

class TestClientDetectsCotOnly:
    def test_truncated_cot_sets_cot_only(self):
        out = asyncio.run(_collect_finish(_sse(["<think>", COT[:80]])))
        assert out.get("cot_only") is True
        assert out.get("content") == "", "可见正文应该是空的"

    def test_closed_cot_with_answer_is_not_cot_only(self):
        out = asyncio.run(_collect_finish(
            _sse(["<think>", COT, "</think>", "主人，今天的 asyncio 关键在于……"])))
        assert out.get("cot_only") is False
        assert "asyncio" in out.get("content", "")

    def test_normal_reply_is_not_cot_only(self):
        out = asyncio.run(_collect_finish(_sse(["主人好呀~"])))
        assert out.get("cot_only") is False

    def test_visible_answer_never_contains_cot(self):
        """A5：清洗管线照旧把 think 块剥干净。"""
        out = asyncio.run(_collect_finish(
            _sse(["<think>", COT, "</think>", "主人好呀~"])))
        content = out.get("content", "")
        assert "<think>" not in content
        assert "</think>" not in content
        assert "We need answer Chinese" not in content

    def test_max_tokens_override_is_used(self):
        """重问时能自带更大的输出预算（参数能一路传到 payload）。"""
        client, fake = _client(_sse(["ok"]), max_tokens=1024)
        try:
            async def _go():
                async for _ in client.chat_stream_events(
                        [{"role": "user", "content": "hi"}],
                        max_tokens=4096):
                    pass
            asyncio.run(_go())
            assert fake.sent[0]["max_tokens"] == 4096
        finally:
            asyncio.run(client.close())

    def test_default_max_tokens_unchanged(self):
        client, fake = _client(_sse(["ok"]), max_tokens=1024)
        try:
            async def _go():
                async for _ in client.chat_stream_events(
                        [{"role": "user", "content": "hi"}]):
                    pass
            asyncio.run(_go())
            assert fake.sent[0]["max_tokens"] == 1024
        finally:
            asyncio.run(client.close())


# --------------------------------------------------------------------------
# 3) AgentLoop：重问一次
# --------------------------------------------------------------------------

class TestAgentRetriesOnCotOnly:
    def test_cot_only_reply_triggers_one_retry(self):
        """A2：CoT-only → 重问一次 → 第二次给出答案 → 主人看到答案。"""
        client = _ScriptedClient([
            {"text": "", "cot_only": True},
            {"text": "唔……主人上次问的 asyncio 问题，重点是别在协程里做同步阻塞。"},
        ])
        evs = _run_agent(client)
        texts = [d for e, d in evs if e == "text"]
        assert client.calls == 2, f"应该只重问一次，实际调用 {client.calls} 次"
        assert texts, "主人不能面对空气泡"
        assert "asyncio" in texts[-1]
        assert texts[-1] not in _EMPTY_REPLY_FALLBACKS

    def test_retry_is_announced_to_ui(self):
        """重问要发 meta 事件（可观测）。"""
        client = _ScriptedClient([
            {"text": "", "cot_only": True},
            {"text": "喵~主人，今天也要加油哦"},
        ])
        evs = _run_agent(client)
        metas = [d for e, d in evs if e == "meta"]
        assert any(m.get("event") == "cot_only_retry" for m in metas)

    def test_retry_is_bounded_to_one_extra_request(self):
        """A2：重问也失败 → 只多发一次请求 → 走空回复兜底（不空泡）。"""
        client = _ScriptedClient([{"text": "", "cot_only": True}])
        evs = _run_agent(client, max_turns=6)
        texts = [d for e, d in evs if e == "text"]
        assert client.calls == 2, f"最多重问一次，实际调用 {client.calls} 次"
        assert texts, "主人不能面对空气泡"
        assert texts[0] in _EMPTY_REPLY_FALLBACKS

    def test_retry_uses_bigger_output_budget(self):
        """重问那一轮要用更大的输出预算（原预算已经被 CoT 吃光了）。

        Stage：普通轮走**场景档位**预算（短闲聊拿小档），
        重问那一轮拿**满档**（CoT 已经把小档吃光了）。
        """
        client = _ScriptedClient([
            {"text": "", "cot_only": True},
            {"text": "喵~主人，今天也要加油哦"},
        ])
        _run_agent(client)
        first = client.max_tokens_seen[0]
        assert first is not None, "普通轮应该带场景档位预算"
        assert first >= load_output_budget().floor, "档位预算不许低于下限"
        assert client.max_tokens_seen[1] == _COT_ONLY_RETRY_MAX_TOKENS
        assert client.max_tokens_seen[1] > first, "重问必须比普通轮宽裕"

    def test_retry_tells_the_model_what_happened(self):
        """重问要把「上一条只有思维链」告诉模型。"""
        client = _ScriptedClient([
            {"text": "", "cot_only": True},
            {"text": "喵~主人，今天也要加油哦"},
        ])
        _run_agent(client)
        last = client.messages_seen[1]
        assert last[-1]["content"] == _COT_ONLY_RETRY_HINT
        assert last[-1]["role"] == "user"

    def test_retry_keeps_system_message_byte_identical(self):
        """A6：重问不动 system 消息（前缀缓存不失效）。"""
        client = _ScriptedClient([
            {"text": "", "cot_only": True},
            {"text": "喵~主人，今天也要加油哦"},
        ])
        _run_agent(client)
        first, second = client.messages_seen[0], client.messages_seen[1]
        sys_first = [m for m in first if m.get("role") == "system"]
        sys_second = [m for m in second if m.get("role") == "system"]
        assert sys_first and sys_second
        assert (sys_first[0]["content"].encode("utf-8")
                == sys_second[0]["content"].encode("utf-8"))


# --------------------------------------------------------------------------
# 4) 正常回复不受影响 + 两条重试互斥（A3 / A4）
# --------------------------------------------------------------------------

class TestNormalRepliesAreNotAffected:
    def test_normal_reply_does_not_retry(self):
        """A3：正常回复一次就够，不许多发请求。"""
        client = _ScriptedClient([{"text": "喵~主人，今天也要加油哦"}])
        evs = _run_agent(client)
        assert client.calls == 1, f"不该重试，实际调用 {client.calls} 次"
        metas = [d for e, d in evs if e == "meta"]
        assert not any(m.get("event") == "cot_only_retry" for m in metas)

    def test_think_block_with_visible_answer_does_not_retry(self):
        """有 think 块但也有可见答案 → 正常，不重试。"""
        client = _ScriptedClient([{"text": "喵~主人今天也要加油哦",
                                   "cot_only": False}])
        _run_agent(client)
        assert client.calls == 1

    def test_low_cjk_retry_still_works(self):
        """A4：低中文占比那条重试照旧能救回来。"""
        client = _ScriptedClient([
            {"text": "Hello there, how are you doing today?"},
            {"text": "今天天气不错呢，主人要不要出去玩~"},
        ])
        evs = _run_agent(client)
        texts = [d for e, d in evs if e == "text"]
        assert client.calls == 2
        assert texts[-1] == "今天天气不错呢，主人要不要出去玩~"

    def test_low_cjk_and_cot_only_do_not_stack(self):
        """A4：同一条回复不会触发两次重试（英文 CoT 只算 CoT-only 一次）。"""
        client = _ScriptedClient([{"text": "", "cot_only": True}])
        _run_agent(client, max_turns=6)
        assert client.calls == 2, "只能多发一次请求"

    def test_cot_only_requires_a_think_tag(self):
        """CoT-only 的判据必须有 think 标签；没有标签的英文归低中文那条。"""
        assert is_cot_only_reply("<think>abc</think>")
        assert not is_cot_only_reply("abc")
        assert is_low_cjk_reply("abc")

    def test_the_two_predicates_can_overlap_but_the_agent_does_not_stack(self):
        """两个判据在**原始文本**上可能同时成立（英文 CoT 中文占比也低）。

        但 AgentLoop 里两条重试是互斥的：低中文那条判的是**清洗后**的文本
        （CoT-only 的回复清洗后是空的，压根进不去那条分支），
        并且那条分支还显式带了 `not cot_only` 兜底。
        """
        assert is_low_cjk_reply("<think>abc</think>")     # 原始文本：两条都成立
        assert is_cot_only_reply("<think>abc</think>")
        # 清洗之后 CoT 被剥光 → 低中文那条拿到的是空串 → 判不出来
        assert sanitize_text("<think>abc</think>") == ""
        assert not is_low_cjk_reply(sanitize_text("<think>abc</think>"))


# --------------------------------------------------------------------------
# 5) CoT 不许漏进正文（A5：可见答案里永远没有思维链）
# --------------------------------------------------------------------------

class TestCotNeverLeaksIntoTheAnswer:
    """思维链是**跨好几块**的英文正文。

    单块清洗里的 `<think>.*$` 规则只剥得掉「带开标签的那一块」，
    后面的 CoT 块里没有标签 → 原样漏进正文。
    平时靠句级清洗看不出来，但**输出预算被 CoT 吃光、回复被截断**时，
    中英碎片混在同一句里 → 主人直接看到垃圾
    （实测：'Theyask "是不是哪里写错了". Need perhapsaskforcode?本身不会卡死…'）。
    """

    def test_cot_split_across_chunks_never_reaches_the_answer(self):
        out = asyncio.run(_collect_finish(_sse([
            "<think>",
            "We need answer Chinese in persona. ",
            "Should explain asyncio carefully. ",
            "Now draft the final answer.",
            "</think>",
            "本身不会卡死呀~",
        ])))
        content = out.get("content", "")
        assert "We need answer" not in content
        assert "Should explain" not in content
        assert "Now draft" not in content
        assert "本身不会卡死呀~" in content
        assert out.get("cot_only") is False

    def test_truncated_cot_never_reaches_the_answer(self):
        """CoT 把预算吃光、回复在 CoT 中间断掉 → 正文一个字都不能漏。"""
        out = asyncio.run(_collect_finish(_sse([
            "<think>",
            "We need answer Chinese. ",
            "Should explain asyncio.",
        ])))
        assert out.get("content") == ""
        assert out.get("cot_only") is True

    def test_split_open_tag_is_not_mistaken_for_content(self):
        """标签被 SSE delta 切开（「<thi」+「nk>」）→ 整段 CoT 都要剥掉。"""
        out = asyncio.run(_collect_finish(_sse([
            "<thi",
            "nk>We need answer Chinese.",
            "Still thinking.",
            "</thi",
            "nk>喵~主人早呀",
        ])))
        content = out.get("content", "")
        assert "We need answer" not in content
        assert "Still thinking" not in content
        assert "喵~主人早呀" in content

    def test_text_events_are_cot_free(self):
        """事件流本身也不能带 CoT（事件流会进 Trace / 子 agent）。"""
        client, _ = _client(_sse([
            "<think>",
            "We need answer Chinese.",
            "</think>",
            "喵~主人早呀",
        ]))

        async def _go():
            texts = []
            async for ev, data in client.chat_stream_events(
                    [{"role": "user", "content": "hi"}]):
                if ev == "text":
                    texts.append(data)
            return texts

        try:
            texts = asyncio.run(_go())
        finally:
            asyncio.run(client.close())
        joined = "".join(texts)
        assert "We need answer" not in joined
        assert "喵~主人早呀" in joined

    def test_plain_reply_is_not_delayed_or_lost(self):
        """没有 think 标签的普通回复必须一字不少地送到主人眼前。"""
        out = asyncio.run(_collect_finish(_sse(["主人好呀", "，今天想干嘛呀~"])))
        assert out.get("content") == "主人好呀，今天想干嘛呀~"


# --------------------------------------------------------------------------
# 6) 清洗管线（A5）
# --------------------------------------------------------------------------

class TestCleaningStillStripsThink:
    def test_sanitize_strips_think_block(self):
        cleaned = sanitize_text("<think>" + COT + "</think>喵~主人今天也要加油哦")
        assert "<think>" not in cleaned
        assert "We need answer" not in cleaned
        assert "主人今天也要加油哦" in cleaned

    def test_truncated_think_block_strips_to_nothing(self):
        cleaned = sanitize_text(COT_TRUNCATED)
        assert cleaned == ""
        assert "<think>" not in cleaned
