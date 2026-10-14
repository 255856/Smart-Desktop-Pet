# -*- coding: utf-8 -*-
"""MiniMax-M3 工具调用失败的回归测试（2026-10-14 现场）。

现场日志：主人说「查一下广州天气，提醒我明天带伞」，AgentLoop 连 6 轮都没调成工具，
最后回了一句「刚刚那个操作没能完成」。

真实原因不是「模型不听话」，是本项目自己把命中率打崩了。用真实失败输入精确回放
（真实 113 条聊天历史 + 7 个提醒类工具 + 主人原话，各跑 10~12 次）测出来：

    tool_choice="required"                 → 调工具 1/12   ← 项目第 1 轮用的就是它
    tool_choice={"type":"function", ...}   → 调工具 1/12   ← 官方文档支持的写法
    完全不带 tool_choice                   → 调工具 4/12 / 0/10
    不带 + LLMClient 那条干净强制提示       → 调工具 9/12
    不带 + AgentLoop 自拼的「[系统提醒]」   → 调工具 5/12
    两条叠加                                → 调工具 4/12
    不带 + 5 条干净强制提示                 → 调工具 8/10

官方 API 文档里 tool_choice 的取值只有 "auto" / "none" / 指定具体工具，
**"required" 不在其中**——服务端不报错、直接吞掉，但这一轮几乎必挂。

所以这里锁死四条：
  1. MiniMax-M3 的请求里**不能出现** tool_choice（预置黑名单，大小写无关）；
  2. 不发 tool_choice 就必须有 prompt 强制顶上；
  3. force 贯穿整个重试阶段，不能只有第 0 轮；
  4. AgentLoop 不再往对话里塞自拼的伪 user 消息（叠加会把命中率打回 4/12）。
"""
from __future__ import annotations

import asyncio

import pytest

from app.brain.agent import AgentLoop
from app.brain import llm_client as lc
from app.core.config import LLMConfig


TOOLS = [{"type": "function", "function": {
    "name": "add_reminder", "description": "设置提醒",
    "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                   "required": ["text"]}}}]


@pytest.fixture(autouse=True)
def _seeded_denylist():
    """确认出厂预置项没被别的用例带走。

    真正的隔离（落盘路径 + 逐用例还原）在 tests/conftest.py 里全局做，
    这里只断言出厂 seed 本身是完整的——预置项被清空的话，下面几条都会假绿。
    """
    assert "minimax-m3" in lc._TOOL_CHOICE_SEED, "出厂预置丢了 MiniMax-M3"


def _capture(sent: list[dict], tool_calls=None):
    """替换 _do_stream_request —— 拦在最底层，断言的是**真正发出去的 payload**，
    不是某个辅助函数的返回值。"""

    async def fake(self, url, payload, headers, cancel_check=None):
        sent.append(payload)
        if tool_calls:
            yield "finish", {"reason": "tool_calls", "tool_calls": tool_calls,
                             "content": ""}
        else:
            yield "finish", {"reason": "stop", "tool_calls": [], "content": "hi"}

    return fake


def _cfg(model: str) -> LLMConfig:
    return LLMConfig(api_key="sk-test-not-a-placeholder",
                     base_url="https://api.minimaxi.com/v1",
                     model=model, temperature=0.8, max_tokens=1024)


def _drain(client, messages, tools, force):
    async def go():
        async for _ in client.chat_stream_events(messages, tools,
                                                 force_tool_use=force):
            pass

    return asyncio.run(go())


# ------------------------------------------------------------------ 发包层


def test_minimax_m3_payload_never_carries_tool_choice(monkeypatch):
    """MiniMax-M3 预置在黑名单里：一次请求都不该带 tool_choice 出去。

    这是本次事故的核心。旧实现第 1 轮发 tool_choice="required"，实测命中率 1/12
    ——白等一次，还把命中率最高的第 1 轮直接作废。
    """
    sent: list[dict] = []
    monkeypatch.setattr(lc.LLMClient, "_do_stream_request",
                        _capture(sent, [{"id": "c1", "name": "add_reminder",
                                          "arguments": "{}"}]))
    client = lc.LLMClient(_cfg("MiniMax-M3"), "你是桌宠")
    _drain(client, [{"role": "user", "content": "1分钟后提醒我喝水"}], TOOLS, force=True)

    assert sent, "一次请求都没发出，测试没打中真实路径"
    for p in sent:
        assert "tool_choice" not in p, f"payload 仍带 tool_choice: {p.get('tool_choice')!r}"
        # 工具本身必须照发——不能为了躲 tool_choice 把 tools 也丢了
        assert p.get("tools"), "tools 被误删，模型就没得调了"


def test_minimax_m3_still_gets_a_force_prompt(monkeypatch):
    """不发 tool_choice 的代价是失去协议层强制，必须补上 prompt 强制顶上。"""
    sent: list[dict] = []
    orig = lc.LLMClient._stream_with_optional_force

    async def spy(self, url, messages, tools, tool_choice, cancel_check=None,
                  allow_prompt_fallback=True):
        sent.append({"tool_choice": tool_choice,
                     "msgs": [dict(m) for m in messages]})
        async for ev, data in orig(self, url, messages, tools, tool_choice,
                                   cancel_check, allow_prompt_fallback):
            yield ev, data

    monkeypatch.setattr(lc.LLMClient, "_do_stream_request",
                        _capture([], []))
    monkeypatch.setattr(lc.LLMClient, "_stream_with_optional_force", spy)

    client = lc.LLMClient(_cfg("MiniMax-M3"), "你是桌宠")
    _drain(client, [{"role": "user", "content": "1分钟后提醒我喝水"}], TOOLS, force=True)

    assert sent, "没走真实请求路径"
    assert sent[0]["tool_choice"] is None, "仍在发 tool_choice"
    assert any(str(m.get("content") or "").startswith("[系统指令]")
               for m in sent[0]["msgs"]), "缺少 prompt 强制提示"


def test_model_name_case_does_not_bypass_the_denylist():
    """config 里写的是 'MiniMax-M3'，预置表里是小写——不归一化就永远命中不了。"""
    for name in ("MiniMax-M3", "minimax-m3", "MINIMAX-M3", " minimax-m3 "):
        assert lc._tool_choice_is_ignored(name), f"{name} 应命中黑名单"
    assert not lc._tool_choice_is_ignored("gpt-4o")


def test_other_models_still_get_required(monkeypatch):
    """没进黑名单的模型仍应走 tool_choice=required，不能被一刀切掉。"""
    sent: list[dict] = []
    monkeypatch.setattr(lc.LLMClient, "_do_stream_request", _capture(sent, []))

    client = lc.LLMClient(_cfg("gpt-4o"), "你是桌宠")
    _drain(client, [{"role": "user", "content": "1分钟后提醒我喝水"}], TOOLS, force=True)

    assert sent and sent[0].get("tool_choice") == "required", \
        f"gpt-4o 应继续用 tool_choice=required，实际 {sent[0].get('tool_choice')!r}"


def test_denylist_persists_across_restart(monkeypatch, tmp_path):
    """运行期学到的黑名单要落盘，否则每次重启都要再白探测一次。"""
    monkeypatch.setattr(lc, "_TOOL_CHOICE_CACHE", tmp_path / "tc.json")
    lc._note_tool_choice_ignored("Some-New-Model")
    assert lc._TOOL_CHOICE_CACHE.is_file(), "黑名单没落盘"

    # 模拟重启：清空内存后重新加载
    lc._TOOL_CHOICE_IGNORED.clear()
    lc._load_tool_choice_ignored()
    assert lc._tool_choice_is_ignored("Some-New-Model"), "重启后黑名单丢了"


# ------------------------------------------------------------------ 重试阶段


class _SpyClient:
    """永远只回文字、从不调工具——逼 AgentLoop 把重试走满，并记录每轮实参。"""

    def __init__(self):
        self.force_flags: list[bool] = []
        self.seen_messages: list[list[dict]] = []
        self.seen_tools: list = []

    async def chat_stream_events(self, messages, tools=None, cancel_check=None,
                                 force_tool_use=False):
        self.force_flags.append(force_tool_use)
        self.seen_messages.append([dict(m) for m in messages])
        self.seen_tools.append(tools)
        yield "text", "小汐帮你查天气~"
        yield "finish", {"content": "小汐帮你查天气~", "tool_calls": [], "reason": "stop"}


class _TinyRegistry:
    def names(self):
        return ["get_weather", "add_reminder"]

    def to_openai(self):
        return TOOLS

    def execute(self, name, args):
        return "ok"


def _run_exhausting_loop(agent, msgs):
    async def run():
        async for _ in agent.run(msgs):
            pass

    asyncio.run(run())


def test_force_applies_on_every_retry_turn_not_just_turn_zero():
    """force 必须贯穿整个重试阶段。

    旧实现是 `force_tool_use=(force_first_turn and turn == 0)`：第 1 轮强制失败后，
    后面 5 轮全部裸奔，只能靠 AgentLoop 自己拼的那条弱提示（实测 5/12）。
    改成全程 True 后由 LLMClient 统一发干净版本（实测 9/12）。
    """
    client = _SpyClient()
    agent = AgentLoop(client, _TinyRegistry(), max_turns=6)
    _run_exhausting_loop(agent, [
        {"role": "system", "content": "你是桌宠"},
        {"role": "user", "content": "查一下广州天气，提醒我明天带伞"},
    ])

    assert len(client.force_flags) >= 3, "重试没发生，测试没打中真实路径"
    assert all(client.force_flags), \
        f"有轮次没开 force：{client.force_flags}"


def test_agent_loop_injects_no_fake_user_turns():
    """AgentLoop 不能再往 messages 里塞自拼的伪 user 消息。

    两条强制提示叠加实测 4/12，比单发一条（9/12）差一倍多。
    """
    client = _SpyClient()
    agent = AgentLoop(client, _TinyRegistry(), max_turns=6)
    _run_exhausting_loop(agent, [
        {"role": "system", "content": "你是桌宠"},
        {"role": "user", "content": "查一下广州天气，提醒我明天带伞"},
    ])

    assert len(client.seen_messages) >= 3, "重试没发生，测试没打中真实路径"
    for msgs in client.seen_messages:
        injected = [str(m.get("content") or "") for m in msgs
                    if str(m.get("content") or "").startswith(("[系统提醒]", "[系统指令]"))]
        assert not injected, f"对话里出现了自拼的伪 user 消息：{injected}"


def test_force_final_turn_drops_tools_so_force_cannot_stall():
    """连续纯调工具耗尽后要去掉 tools；此时 force 也不能把模型按死在工具上。

    tools=None 时 LLMClient 的 want_force 自然是 False，模型才能正常收尾。
    """
    calls: list[tuple[bool, object]] = []

    class _ToolSpamClient:
        async def chat_stream_events(self, messages, tools=None, cancel_check=None,
                                     force_tool_use=False):
            calls.append((force_tool_use, tools))
            yield "finish", {"content": "", "reason": "tool_calls", "tool_calls": [
                {"id": "t1", "name": "get_weather", "arguments": "{}"}]}

    class _R(_TinyRegistry):
        def execute(self, name, args):
            return "晴 24 度"

    agent = AgentLoop(_ToolSpamClient(), _R(), max_turns=6)
    _run_exhausting_loop(agent, [
        {"role": "system", "content": "你是桌宠"},
        {"role": "user", "content": "查一下广州天气"},
    ])

    assert calls, "一次请求都没发生"
    assert any(force and tools is None for force, tools in calls) or \
        any(tools is None for _, tools in calls), \
        f"force_final 阶段没有去掉 tools：{calls}"
