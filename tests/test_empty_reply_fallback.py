"""空回复兜底回归测试。

背景：清洗规则（<think> 块 / 低中文占比 / schema 回显）会把模型输出整段剥光。
若本轮又没执行任何工具，就拿不到 _tool_ack_sentence 兜底，
原实现会 `if final_text: yield` 后直接 break —— 主人面对空气泡，以为桌宠坏了。
现已在 agent.py 加 _EMPTY_REPLY_FALLBACK 兜底，这里锁死该行为。
"""
import asyncio
import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 项目根目录（按文件位置推导）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.brain.agent import (AgentLoop, _EMPTY_REPLY_FALLBACK,  # noqa: E402
                             _EMPTY_REPLY_FALLBACKS)


class GarbageClient:
    """模型只吐出 prompt 复读 / 工具独白，且不调任何工具 → 清洗后为空。"""

    def __init__(self, text):
        self.text = text
        self.calls = 0

    async def chat_stream_events(self, messages, tools=None, cancel_check=None,
                                 force_tool_use=False):
        self.calls += 1
        yield "text", self.text
        yield "finish", {"content": "", "tool_calls": []}


class EmptyRegistry:
    def names(self):
        return []

    def to_openai(self):
        return []

    def execute(self, name, args):
        return ""


def _run(client):
    # 显式新建 event loop：全量跑时别的测试可能已改动/关闭了全局 loop，
    # 用 get_event_loop() 会随执行顺序时好时坏。
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_collect(AgentLoop(
            client, EmptyRegistry(), max_turns=1).run(
            [{"role": "system", "content": "你是桌宠"},
             {"role": "user", "content": "你好呀"}])))
    finally:
        loop.close()


async def _collect(agen):
    return [ev async for ev in agen]


@pytest.mark.parametrize("raw", (
    "Hello World",                                  # 低中文占比 → 整段丢弃
    "<think>只有思维链，没有可见答案</think>",        # CoT 全剥 → 空
))
def test_reply_empty_after_cleaning_still_gets_fallback(raw):
    """清洗后真为空时 → 必须给出兜底话术，不能空。

    Stage 1.3：兜底话术改成多条轮换 → 断言「轮换池里任意一条出现」。
    Stage 3：入口换成「真会空掉」的两类（纯英文 / 只有思维链）——
    原来用的 prompt 复读文本不再被剥光（见下一条测试）。
    """
    evs = _run(GarbageClient(raw))
    texts = [d for ev, d in evs if ev == "text"]
    assert texts, "没有任何回复，主人会看到空气泡"
    assert any(t.strip() for t in texts), "兜底文案是空的"
    joined = " ".join(texts)
    assert any(line in joined for line in _EMPTY_REPLY_FALLBACKS), (
        f"兜底话术不在轮换池里：{joined!r}")


def test_prompt_echo_is_no_longer_emptied():
    """Stage 3：prompt 复读不再被正则剥光 —— 它原样送达（业主裁决去掉硬限制）。"""
    echo = "唯一允许的输出语言是简体中文。1. 不要用 emoji"
    evs = _run(GarbageClient(echo))
    texts = [d for ev, d in evs if ev == "text"]
    assert " ".join(texts) == echo


def test_tool_monologue_is_no_longer_emptied():
    """Stage 3：纯工具独白也不再被剥光，原样送达（工具独白交给 prompt 层管）。"""
    raw = "让我调用工具查一下天气。"
    evs = _run(GarbageClient(raw))
    texts = [d for ev, d in evs if ev == "text"]
    assert " ".join(texts) == raw


def test_normal_reply_passes_through_untouched():
    """正常回复不能被兜底话术污染。"""
    evs = _run(GarbageClient("今天天气不错呢，主人要不要出去玩~"))
    texts = [d for ev, d in evs if ev == "text"]
    joined = " ".join(texts)
    assert "出去玩" in joined
    # 轮换池里**任何**一条兜底话术都不该混进正常回复
    assert not any(line in joined for line in _EMPTY_REPLY_FALLBACKS)


def test_done_event_always_carries_text():
    """done 事件的 final_text 也必须有内容（UI 依赖它显示气泡）。"""
    evs = _run(GarbageClient("根据角色设定，我是软萌 girl。"))
    done = [d for ev, d in evs if ev == "done"]
    assert done and done[0].strip(), "done 事件带空文本，UI 会显示空气泡"
