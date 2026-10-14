# -*- coding: utf-8 -*-
"""P11 内部核对话术泄漏 / P13 同一工具反复重试。

两处都来自压测（真实 API，54 条语料四层）。

P11：模型把 AgentLoop 回传给它的核对清单原样复述给主人。
     「_STYLE_RULE」明令禁止，但逐句判污染的 _strip_meta_by_sentence
     盖不住「编号 + 内部词」「工具名（成功）」这两种形态。

P13：模型对一个打不开的网页连调 6 次 open_website，每次只换参数。
     MAX_CONSECUTIVE_TOOL_ONLY_TURNS 只管轮数，不管「同一个工具重复调」。
"""
from __future__ import annotations

import asyncio

import pytest

from app.brain.agent import AgentLoop, _MAX_SAME_TOOL_FAIL
from app.brain.llm_client import sanitize_text


# ------------------------------------------------------------------ P11

# 压测现场实况（真实 API 返回）
LEAKS = [
    # 编号 + 内部核对词
    "要查火星天气。1. 工具都成功执行了\n"
    "2. 主人要求：查火星天气 - 我已经搜到了相关数据\n"
    "3. 数据来源确认：\n- 火星表面平均温度约-60°C",
    # 爬取叙述
    "让我查广州天气。从最后一次抓取中央气象台的页面拿到了比较完整的预报数据：10/14 周三 晴 33℃",
    # schema 结果回显
    "1. get_weather（成功）：广州 晴 33度\n2. add_reminder（成功）：已设置提醒",
    # 复述指令本身
    "请给主人最终答复",
]

# 这些**必须活下来**，否则清洗器会把正文清空成空气泡
MUST_KEEP = [
    "我查到的天气是晴天哦",
    "我搜索了一下最近的新闻，主人要听吗？",
    "广州今天晴天33度，明天有小雨，记得带伞喵~",
    "已经帮主人设好明天早上8点的提醒啦~",
    "第一步带伞，第二步关窗，主人记得呀~",
    "我翻了翻记忆，暂时没找到相关内容~",
]


@pytest.mark.parametrize("leak", LEAKS)
def test_internal_checklist_is_stripped(leak):
    out = sanitize_text(leak, is_final=True)
    for word in ("工具都成功执行", "数据来源确认", "主人要求：",
                 "中央气象台", "get_weather（", "add_reminder（",
                 "请给主人最终"):
        assert word not in out, f"仍泄漏「{word}」：{out!r}"


@pytest.mark.parametrize("keep", MUST_KEEP)
def test_normal_reply_survives(keep):
    """清洗器不能把正常回复清空——那是比泄漏更糟的失败。"""
    out = sanitize_text(keep, is_final=True)
    assert out.strip(), f"被清空了：{keep!r}"
    assert len(out) >= len(keep) * 0.6, f"被削太多：{keep!r} -> {out!r}"


def test_step_by_step_owner_text_is_not_treated_as_checklist():
    """主人自己的分步说明不该被当内部清单删掉。"""
    out = sanitize_text("第一步把门窗关好，第二步检查煤气，主人出门前记得哦~",
                        is_final=True)
    assert "第一步" in out, out


# ------------------------------------------------------------------ P13


class _AlwaysFailRegistry:
    def __init__(self):
        self.calls = 0

    def names(self):
        return ["open_website", "get_current_time"]

    def to_openai(self):
        return [{"type": "function", "function": {"name": n}}
                for n in self.names()]

    def execute(self, name, args):
        self.calls += 1
        return "错误：无法打开网页，请检查网址是否正确"


class _KeepCallingWebsite:
    """一直调 open_website，永不换工具——复现压测里的连调 6 次。"""

    def __init__(self):
        self.calls = 0

    async def chat_stream_events(self, messages, tools=None, cancel_check=None,
                                 force_tool_use=False):
        self.calls += 1
        if self.calls > 12:
            yield "text", "放弃了喵~"
            yield "finish", {"content": "放弃了喵~", "tool_calls": []}
            return
        yield "finish", {"content": "", "tool_calls": [
            {"id": f"c{self.calls}", "name": "open_website",
             "arguments": '{"url":"https://example.com"}'}]}


def test_same_failing_tool_is_cut_off():
    """同一个工具连续失败 _MAX_SAME_TOOL_FAIL 次后就不再提供。"""
    reg = _AlwaysFailRegistry()
    client = _KeepCallingWebsite()
    agent = AgentLoop(client, reg, max_turns=8)

    results = []

    async def run():
        async for ev, *rest in agent.run([
            {"role": "system", "content": "你是桌宠"},
            {"role": "user", "content": "打开 https://example.com"},
        ]):
            if ev == "tool":
                results.append(rest[2])

    asyncio.run(run())

    # 真正打到 registry 的次数 = 允许的失败次数，超过就被闸拦下
    assert reg.calls <= _MAX_SAME_TOOL_FAIL, \
        f"实际执行 {reg.calls} 次，应在 {_MAX_SAME_TOOL_FAIL} 次后停手"
    # 闸门必须对模型把话说明白，否则模型还会继续换参数重试
    assert any("不再提供" in r for r in results), \
        f"没有给出停手说明：{results}"


def test_failing_tool_does_not_break_healthy_ones():
    """open_website 连续失败不该牵连 get_current_time。"""
    class _MixedRegistry(_AlwaysFailRegistry):
        def execute(self, name, args):
            if name == "get_current_time":
                return "2026-10-14 16:00:00"
            return super().execute(name, args)

    class _MixedClient:
        def __init__(self):
            self.calls = 0

        async def chat_stream_events(self, messages, tools=None, cancel_check=None,
                                     force_tool_use=False):
            self.calls += 1
            if self.calls <= 3:
                name = "open_website" if self.calls % 2 else "get_current_time"
                yield "finish", {"content": "", "tool_calls": [
                    {"id": f"c{self.calls}", "name": name, "arguments": "{}"}]}
            else:
                yield "text", "网页打不开，但现在是2026年10月14日喵~"
                yield "finish", {"content": "网页打不开，但现在是2026年10月14日喵~",
                                 "tool_calls": []}

    agent = AgentLoop(_MixedClient(), _MixedRegistry(), max_turns=8)
    texts, tools = [], []

    async def run():
        async for ev, *rest in agent.run([
            {"role": "system", "content": "你是桌宠"},
            {"role": "user", "content": "打开 https://example.com"},
        ]):
            if ev == "tool":
                tools.append(rest[0])
            elif ev == "text":
                texts.append(rest[0])

    asyncio.run(run())
    assert "get_current_time" in tools, f"健康的工具被牵连了：{tools}"
    assert "".join(texts).strip(), "空气泡"
