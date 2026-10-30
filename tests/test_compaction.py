# -*- coding: utf-8 -*-
"""Stage 4.3：上下文压缩（摘要并遗忘）的验收测试。

对应任务书的五个验收点：

  A1 低于阈值（预算的 80%）→ **历史逐字节不变**（一个字都不动）
  A2 超阈值 → 最旧的一段变成**一条摘要**；最新 8 条**逐字节不变**；
     保留的 16% 是原文
  A3 摘要失败（网络错误 / 空结果 / 超时）→ 退回「丢最旧」，**这一轮照样能聊**
  A4 早期说过的关键事实，30 轮压缩之后仍然**能被召回**（记忆 sidecar 兜底）
  A5 压缩只发生在对话历史里 → system prompt **逐字节不变**（前缀缓存不能被打掉）

测试**不需要真 sidecar 进程，也不需要 API key**：
  - 记忆 sidecar 用 tests/fake_sidecar.py（进程内假实现）
  - 摘要器用注入的假实现（ChatWindow._compaction_summariser）
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/fake_sidecar.py
if (ROOT / ".local-packages").is_dir():
    sys.path.insert(0, str(ROOT / ".local-packages"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.brain.compaction import (  # noqa: E402
    SUMMARY_MARKER,
    CompactionSettings,
    load_compaction_settings,
    plan_compaction,
    summarise_history,
)
from app.ui.chat_window import ChatWindow, Message  # noqa: E402


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _make_window(**kwargs) -> ChatWindow:
    """一个最小聊天窗口（api_key 不是占位符 —— 否则压缩会直接退回丢最旧）。"""
    from app.core.config import CharacterConfig

    llm_cfg = type("L", (), {
        "model": "test", "api_key": "test-key-not-a-placeholder",
        "base_url": "http://127.0.0.1:1/v1", "timeout": 1, "stream": True,
        "temperature": 0.8, "max_tokens": 16,
    })()
    return ChatWindow(llm_cfg, CharacterConfig(name="测试桌宠"),
                      str(ROOT / "assets" / "sprites"), **kwargs)


def _messages(n: int, filler: str = "内", size: int = 400) -> list:
    """造 n 条交替 user / assistant 的消息（每条约 size 字符）。"""
    return [Message(role="user" if i % 2 == 0 else "assistant",
                    content=f"第 {i} 条消息：" + filler * size)
            for i in range(n)]


def _legacy_drop(history, max_history: int, budget: int) -> list:
    """今天的裁剪逻辑（丢最旧的）—— 测试里复刻一遍，钉住「退回旧行为」。"""
    hist = list(history)
    if len(hist) > max_history:
        hist = hist[-max_history:]
    kept: list = []
    used = 0
    for m in reversed(hist):
        cost = len(getattr(m, "content", "") or "")
        if kept and used + cost > budget:
            break
        kept.append(m)
        used += cost
    kept.reverse()
    while kept and kept[0].role == "assistant":
        kept.pop(0)
    return kept


# ---------------------------------------------------------------------------
# 参数：跟 qq-bridge dsh-compaction-basic 对齐
# ---------------------------------------------------------------------------

def test_settings_come_from_companion_config_yaml():
    """阈值 / 保留比例 / 永远保留的条数都走 yaml（0.8 / 0.16 / 8）。"""
    settings = load_compaction_settings(force=True)
    assert settings.threshold_ratio == pytest.approx(0.8)
    assert settings.retain_ratio == pytest.approx(0.16)
    assert settings.keep_recent == 8


def test_plan_compaction_is_none_below_threshold():
    """低于阈值 → 没有压缩计划（什么都不动）。"""
    settings = load_compaction_settings(force=True)
    hist = _messages(10, size=10)          # 10 × ~16 字符 = 160
    assert plan_compaction(hist, settings, budget=12000) is None


def test_plan_compaction_keeps_newest_8_and_summarises_the_rest():
    """超阈值 → 最旧的进摘要，最新 8 条永远原文保留。"""
    settings = load_compaction_settings(force=True)
    hist = _messages(40)
    plan = plan_compaction(hist, settings, budget=12000)
    assert plan is not None
    assert len(plan.to_summarise) == 32
    assert len(plan.keep) == 8
    assert plan.keep == hist[-8:]


def test_plan_compaction_retains_16_percent_of_recent_messages():
    """100 条 → 保留 16 条（16%），其余进摘要。"""
    settings = load_compaction_settings(force=True)
    hist = _messages(100)
    plan = plan_compaction(hist, settings, budget=12000)
    assert plan is not None
    assert len(plan.keep) == 16          # ceil(100 × 0.16)
    assert len(plan.to_summarise) == 84


# ---------------------------------------------------------------------------
# A1：低于阈值 → 历史逐字节不变
# ---------------------------------------------------------------------------

def test_a1_below_threshold_history_is_byte_identical(qapp):
    """A1：用量 < 预算的 80% → 历史一个字节都不动，也不起摘要。"""
    cw = _make_window()
    cw._compaction_summariser = lambda messages: pytest.fail("不该调摘要器")

    cw.history = _messages(20, size=100)
    before = [(m.role, m.content) for m in cw.history]

    cw._trim_history()

    after = [(m.role, m.content) for m in cw.history]
    assert after == before, "低于阈值时历史必须逐字节不变"
    assert cw._compaction_worker is None, "低于阈值不该起后台摘要"
    assert cw._compaction_inflight is False


# ---------------------------------------------------------------------------
# A2：超阈值 → 最旧的一段变成一条摘要
# ---------------------------------------------------------------------------

def test_a2_oldest_becomes_one_summary_and_newest_8_are_unchanged(qapp):
    """A2：最旧的 32 条 → 一条摘要；最新 8 条逐字节不变。"""
    cw = _make_window()
    seen: dict = {}

    def fake_summariser(messages):
        seen["messages"] = list(messages)
        return "主人交代了 A 项目的进度，桌宠答应周五交付。"

    cw._compaction_summariser = fake_summariser

    cw.history = _messages(40)
    original = list(cw.history)
    newest8 = original[-8:]

    cw._trim_history()
    assert cw.wait_compaction(5000)

    # 摘要器拿到的正是「最旧的那一段」
    assert len(seen["messages"]) == 32
    assert seen["messages"] == original[:32]

    # 压缩后：1 条摘要 + 8 条原文
    assert len(cw.history) == 9
    assert SUMMARY_MARKER in cw.history[0].content
    assert "周五交付" in cw.history[0].content

    # 最新 8 条：同一个对象 + 字节一致
    for kept, old in zip(cw.history[1:], newest8):
        assert kept is old
        assert kept.content.encode("utf-8") == old.content.encode("utf-8")

    # 最旧的那 32 条确实被摘要掉了
    assert all(m not in cw.history for m in original[:32])


def test_a2_retained_16_percent_is_verbatim(qapp):
    """A2：100 条历史 → 保留 16 条（16%）原文，其余 84 条进摘要。"""
    cw = _make_window()
    cw._compaction_summariser = lambda messages: "一段摘要。"

    cw.history = _messages(100)
    original = list(cw.history)

    cw._trim_history()
    assert cw.wait_compaction(5000)

    assert len(cw.history) == 17          # 1 条摘要 + 16 条原文
    assert cw.history[1:] == original[-16:]
    assert all(m is o for m, o in zip(cw.history[1:], original[-16:]))


def test_a2_real_summariser_path_goes_through_llm_client(qapp, monkeypatch):
    """生产路径（不打网络）：摘要器走真 LLMClient，只把 chat_once 换成假货。

    覆盖「注入点之外的」那条路 —— 真上线时跑的就是它。
    """
    from app.brain import llm_client as lc_mod

    calls: dict = {}

    async def _fake_chat_once(self, messages):
        calls["messages"] = list(messages)
        calls["system_prompt"] = self.system_prompt
        return "这是真摘要器写出来的摘要。"

    monkeypatch.setattr(lc_mod.LLMClient, "chat_once", _fake_chat_once)

    cw = _make_window()
    cw.history = _messages(40)
    cw._trim_history()
    assert cw.wait_compaction(5000)

    assert len(cw.history) == 9
    assert "这是真摘要器写出来的摘要。" in cw.history[0].content
    assert calls["messages"], "摘要请求没发出去"
    # 摘要请求是 user 消息（摘要内容不碰 system 串）
    assert calls["messages"][0].role == "user"


def test_drop_oldest_history_protect_tail_never_loses_newest_messages(qapp):
    """兜底裁剪里 protect_tail 比历史还长时，最新的消息一条都不能丢。"""
    cw = _make_window()
    cw.history = _messages(5, size=10)
    cw._drop_oldest_history(protect_tail=8)
    assert len(cw.history) == 5
    assert cw.history[-1].content.endswith("内" * 10)


# ---------------------------------------------------------------------------
# A3：摘要失败 → 退回「丢最旧」，这一轮照样能聊
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["network", "empty", "timeout"])
def test_a3_summariser_failure_falls_back_to_drop_oldest(qapp, mode):
    """A3：网络错误 / 空结果 / 超时 → 退回今天的行为（丢最旧）。"""
    cw = _make_window()

    def boom(messages):
        if mode == "network":
            raise RuntimeError("网络断了")
        if mode == "timeout":
            raise asyncio.TimeoutError("超时了")
        return ""

    cw._compaction_summariser = boom
    cw.history = _messages(40)
    original = list(cw.history)

    cw._trim_history()
    assert cw.wait_compaction(5000)

    expected = _legacy_drop(original, cw._MAX_HISTORY, cw._CTX_CHAR_BUDGET)
    assert cw.history == expected, "失败时必须退回「丢最旧」"
    assert SUMMARY_MARKER not in "".join(m.content for m in cw.history)


@pytest.mark.parametrize("mode", ["network", "empty", "timeout"])
def test_a3_turn_still_works_after_summariser_failure(qapp, monkeypatch, mode):
    """A3：摘要失败也不能把这一轮聊天搞坏（请求照发，窗口照能用）。"""
    import app.ui.chat_window as cw_mod

    cw = _make_window()

    def boom(messages):
        if mode == "network":
            raise RuntimeError("网络断了")
        if mode == "timeout":
            raise asyncio.TimeoutError("超时了")
        return ""

    cw._compaction_summariser = boom
    cw.history = _messages(40)
    cw._trim_history()
    assert cw.wait_compaction(5000)

    captured: dict = {}
    real_init = cw_mod._StreamWorker.__init__

    def _spy(self, client, messages):
        captured["messages"] = messages
        captured["system_prompt"] = client.system_prompt
        real_init(self, client, messages)

    monkeypatch.setattr(cw_mod._StreamWorker, "__init__", _spy)
    monkeypatch.setattr(cw_mod._StreamWorker, "start", lambda self: None)

    cw._kickoff_llm()          # 不抛异常 = 这一轮照样能聊

    assert "messages" in captured
    assert captured["messages"], "请求消息不能是空的"
    assert cw._generating is True


def test_a3_summarise_history_never_raises():
    """app/brain/compaction.py 的摘要函数：任何失败都只返回空串。"""
    messages = [Message(role="user", content="你好")]

    class _Boom:
        async def chat_once(self, msgs):
            raise RuntimeError("网络断了")

    class _Empty:
        async def chat_once(self, msgs):
            return "   "

    class _Slow:
        async def chat_once(self, msgs):
            await asyncio.sleep(5)
            return "太慢了"

    fast = CompactionSettings(summary_timeout_s=0.05)
    assert asyncio.run(summarise_history(_Boom(), messages)) == ""
    assert asyncio.run(summarise_history(_Empty(), messages)) == ""
    assert asyncio.run(summarise_history(_Slow(), messages, fast)) == ""


# ---------------------------------------------------------------------------
# A4：早期关键事实，30 轮压缩之后仍然能被召回
# ---------------------------------------------------------------------------

def test_a4_key_fact_still_recalled_after_30_turns_of_compaction(qapp):
    """A4：压缩把可见历史压没了，但**长期记忆 sidecar 兜住了关键事实**。

    不需要真 sidecar 进程，也不需要 API key：
      - sidecar 用 tests/fake_sidecar.py（进程内假实现）
      - 摘要器用注入的假实现
    """
    from app.brain.memory_client import MemoryClient
    from app.engine.tools import ToolRegistry
    from app.engine.tools import _memory as mem_tools
    from fake_sidecar import install

    cw = _make_window()
    client = MemoryClient()
    install(client)                 # 假 sidecar（进程内）

    calls = {"n": 0}

    def fake_summariser(messages):
        calls["n"] += 1
        return "（摘要）主人交代过一些事情。"

    cw._compaction_summariser = fake_summariser

    # 主人说了一件关键的事 → 桌宠用**真的** remember_fact 工具存进长期记忆
    fact = "主人的猫叫团子，生日是 3 月 3 号"
    reg = ToolRegistry()
    mem_tools.register(reg, memory=client)
    said = reg.get("remember_fact").fn(
        content=fact, category="fact", importance=0.9)
    assert "已记住" in said

    # 第 1 轮：主人把这件事说出口
    cw.history.append(Message(role="user", content="记住：" + fact))
    cw.history.append(Message(role="assistant", content="记住啦，主人。"))

    # 之后 29 轮：每轮都把上下文顶过阈值 → 反复压缩
    for i in range(29):
        cw.history.append(
            Message(role="user", content=f"第 {i} 轮的问题。" + "问" * 900))
        cw.history.append(
            Message(role="assistant", content=f"第 {i} 轮的回答。" + "答" * 900))
        cw._trim_history()
        assert cw.wait_compaction(5000)

    # 压缩确实反复发生了
    assert calls["n"] >= 5, "这 30 轮里应该反复触发压缩"

    # 关键事实已经不在可见历史里了（它被摘要掉了）
    assert not any("团子" in m.content for m in cw.history)

    # 但它**仍然能被召回** —— 长期记忆 sidecar 兜住了
    rows = client.recall("主人的猫叫什么")
    assert rows, "关键事实召不回来了"
    assert "团子" in rows[0].get("text", "")


# ---------------------------------------------------------------------------
# A5：压缩不碰 system prompt
# ---------------------------------------------------------------------------

def test_a5_system_prompt_is_byte_identical_after_compaction(qapp, monkeypatch):
    """A5：压缩只发生在历史里 —— system 串逐字节不变（前缀缓存不能被打掉）。"""
    import app.ui.chat_window as cw_mod

    cw = _make_window()
    cw._compaction_summariser = lambda messages: "一段摘要。"

    captured: list = []
    real_init = cw_mod._StreamWorker.__init__

    def _spy(self, client, messages):
        captured.append(client.system_prompt)
        real_init(self, client, messages)

    monkeypatch.setattr(cw_mod._StreamWorker, "__init__", _spy)
    monkeypatch.setattr(cw_mod._StreamWorker, "start", lambda self: None)

    cw.history = _messages(20, size=100)
    cw._kickoff_llm()
    before = captured[-1]

    cw.history = _messages(40)
    cw._trim_history()
    assert cw.wait_compaction(5000)
    cw._kickoff_llm()
    after = captured[-1]

    assert before.encode("utf-8") == after.encode("utf-8"), "system 串变了"
    assert SUMMARY_MARKER not in after, "摘要不许进 system prompt"
    assert "[当前时间:" not in after
