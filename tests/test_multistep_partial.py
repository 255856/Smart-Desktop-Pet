# -*- coding: utf-8 -*-
"""多步任务「只做一半」的纠正逻辑。"""
from __future__ import annotations

import asyncio

import pytest

from app.brain.agent import AgentLoop
from app.brain.llm_client import count_request_actions


# ------------------------------------------------------------ 分句计数


@pytest.mark.parametrize("text,expect", [
    ("查一下广州天气，提醒我明天带伞", 2),
    ("帮我搜一下十月新番，并打开B站", 2),
    ("搜一下最近的新闻，然后打开浏览器", 2),
    ("帮我查一下内存占用，然后列出进程", 2),
    ("帮我打开网易云音乐，顺便看看今天天气", 2),
    ("记一下我讨厌香菜，然后提醒我今晚吃饭", 2),
    # 单件事
    ("打开QQ", 1),
    ("1分钟后提醒我喝水", 1),
    ("提醒我吃药", 1),
    ("帮我记住我喜欢吃鸡蛋灌饼", 1),
    # 没有任何动作动词
    ("你好呀小汐", 0),
    ("我今天上班好累啊", 0),
    ("给我讲个笑话吧", 0),
    ("你觉得我这个人怎么样？", 0),
    ("晚安，明天见", 0),
    ("今天天气怎么样", 0),
    # 空输入不能炸
    ("", 0),
])
def test_count_request_actions(text, expect):
    assert count_request_actions(text) == expect, f"{text!r}"


def test_count_request_actions_handles_none():
    assert count_request_actions(None) == 0


# ------------------------------------------------------------ AgentLoop 侧


class _HalfClient:
    """只做第一件事就收尾——复现「查完天气忘了设提醒」。"""

    def __init__(self):
        self.calls = 0

    async def chat_stream_events(self, messages, tools=None, cancel_check=None,
                                 force_tool_use=False):
        self.calls += 1
        if self.calls == 1:
            yield "finish", {"content": "", "reason": "tool_calls", "tool_calls": [
                {"id": "w1", "name": "get_weather", "arguments": "{}"}]}
        elif self.calls == 2:
            # 主人要求两件事，只报了天气就要收尾
            yield "text", "广州今天晴天33度哦~"
            yield "finish", {"content": "广州今天晴天33度哦~",
                             "tool_calls": [], "reason": "stop"}
        elif self.calls == 3:
            yield "finish", {"content": "", "reason": "tool_calls", "tool_calls": [
                {"id": "r1", "name": "add_reminder", "arguments": "{}"}]}
        else:
            yield "text", "查好啦~天气晴33度，提醒也设好啦~"
            yield "finish", {"content": "查好啦~天气晴33度，提醒也设好啦~",
                             "tool_calls": [], "reason": "stop"}


class _Reg:
    def __init__(self, names):
        self._n = names

    def names(self):
        return self._n

    def to_openai(self):
        return [{"type": "function", "function": {"name": n}} for n in self._n]

    def execute(self, name, args):
        return "结果"


def _collect(agent, text):
    metas, tools, texts = [], [], []

    async def run():
        async for ev, *rest in agent.run([
            {"role": "system", "content": "你是桌宠"},
            {"role": "user", "content": text},
        ]):
            if ev == "meta":
                metas.append(rest[0])
            elif ev == "tool":
                tools.append(rest[0])
            elif ev == "text":
                texts.append(rest[0])

    asyncio.run(run())
    return metas, tools, texts


def test_partial_completion_triggers_one_correction():
    """只做完第一件事就收尾 → 必须抓到并给一次纠正机会。"""
    c = _HalfClient()
    agent = AgentLoop(c, _Reg(["get_weather", "add_reminder"]), max_turns=6)
    metas, tools, _ = _collect(agent, "查一下广州天气，提醒我明天带伞")
    assert any(m.get("event") == "partial_retry" for m in metas), metas
    assert "add_reminder" in tools, f"纠正后应该把第二件事做掉：{tools}"


def test_complete_multi_step_is_not_corrected():
    """两件事都做完了 → 不得再纠正（否则就是白刷一轮，主人干等）。"""
    class _BothClient:
        def __init__(self):
            self.calls = 0

        async def chat_stream_events(self, messages, tools=None, cancel_check=None,
                                     force_tool_use=False):
            self.calls += 1
            if self.calls == 1:
                yield "finish", {"content": "", "tool_calls": [
                    {"id": "a", "name": "web_search", "arguments": "{}"}]}
            elif self.calls == 2:
                yield "finish", {"content": "", "tool_calls": [
                    {"id": "b", "name": "open_website", "arguments": "{}"}]}
            else:
                yield "text", "都办好啦~"
                yield "finish", {"content": "都办好啦~", "tool_calls": []}

    agent = AgentLoop(_BothClient(), _Reg(["web_search", "open_website"]), max_turns=6)
    metas, tools, _ = _collect(agent, "帮我搜一下十月新番，并打开B站")
    assert not any(m.get("event") == "partial_retry" for m in metas), metas
    assert len(set(tools)) == 2, tools


def test_single_step_request_is_never_flagged_partial():
    """只要求一件事 → 分句数 < 2，永远不该触发纠正。"""
    class _OneShot:
        def __init__(self):
            self.calls = 0

        async def chat_stream_events(self, messages, tools=None, cancel_check=None,
                                     force_tool_use=False):
            self.calls += 1
            if self.calls == 1:
                yield "finish", {"content": "", "tool_calls": [
                    {"id": "a", "name": "get_weather", "arguments": "{}"}]}
            else:
                yield "text", "今天晴33度~"
                yield "finish", {"content": "今天晴33度~", "tool_calls": []}

    agent = AgentLoop(_OneShot(), _Reg(["get_weather"]), max_turns=6)
    metas, _, _ = _collect(agent, "查一下广州天气")
    assert not any(m.get("event") == "partial_retry" for m in metas), metas


def test_partial_correction_is_capped_at_one():
    """纠正最多一次。模型就是不做也不能无限刷（每轮 1.5s，主人干等）。"""

    class _OneThenStubborn:
        """第 1 轮调一个工具，之后死活不肯调第二个。"""

        def __init__(self):
            self.calls = 0

        async def chat_stream_events(self, messages, tools=None, cancel_check=None,
                                     force_tool_use=False):
            self.calls += 1
            if self.calls == 1:
                yield "finish", {"content": "", "tool_calls": [
                    {"id": "a", "name": "get_weather", "arguments": "{}"}]}
            else:
                yield "text", "主人我只能查到这里啦~"
                yield "finish", {"content": "主人我只能查到这里啦~", "tool_calls": []}

    agent = AgentLoop(_OneThenStubborn(), _Reg(["get_weather", "add_reminder"]),
                      max_turns=6)
    metas, _, _ = _collect(agent, "查一下广州天气，提醒我明天带伞")
    n = sum(1 for m in metas if m.get("event") == "partial_retry")
    assert n == 1, f"应该正好纠正一次，实际 {n}：{metas}"


# ------------------------------------------------------ 空回复兜底（压测发现）


def test_agent_never_emits_an_empty_bubble():
    """任何路径都不许「一条文字都不给」。

    压测抓到：主人的话跑了 6 轮、9 次工具调用，最后回复是空的。原有两条兜底
    各有前提（要 executed_tools 非空 / 要 over_sanitized 为真），两条都不成立
    时就什么都��不发——主人面对空气泡，这是最伤的 UX 失败模式。

    这里构造最窄的缝：模型既没调工具、也没吐出任何内容。
    """
    from app.brain.agent import _EMPTY_REPLY_FALLBACK

    class _SilentClient:
        def __init__(self):
            self.calls = 0

        async def chat_stream_events(self, messages, tools=None, cancel_check=None,
                                     force_tool_use=False):
            self.calls += 1
            # 既无 content 也无 tool_calls：finish 一律空
            yield "finish", {"content": "", "tool_calls": [], "reason": "stop"}

    agent = AgentLoop(_SilentClient(), _Reg(["get_weather"]), max_turns=3)
    _, _, texts = _collect(agent, "你好呀")
    assert texts, "一条文字都没产出——主人会看到空气泡"
    assert "".join(texts).strip(), "产出的是空白"
    assert "".join(texts) == _EMPTY_REPLY_FALLBACK

