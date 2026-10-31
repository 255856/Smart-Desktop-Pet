# -*- coding: utf-8 -*-
"""输出侧预算：按场景分档 + 「思维链吃光预算」的早期止损。

背景
----
config.yaml 的 llm.max_tokens 是**所有请求共用**的天花板，于是一句「几点了」
也拿到 4096。max_tokens 是天花板不是预付款，真正烧钱的是
**思维链把预算吃光、正文一个字没出** —— 那时候整条回复被丢掉重问一遍
（Stage 2.10 的 cot_only 重问）。

这里锁住四件事
--------------
1. **档位存在、可配置、有下限**（A1）：短闲聊拿小档，长回答/工具密集拿满档，
   任何档位都不低于 floor（普通短回复绝不被截断）；
2. **早期止损**（A2）：思维链占掉预算的一大半、还没出正文 → 这次请求
   **当场停掉**（剩下的预算不再烧），并标记成 CoT-only 让上层用满档重问；
3. **重问有界**（A4）：止损连发也不许变成死循环 —— 最多重问一次；
4. **质量不倒退**（A3）：出了正文就不止损；止损后的重问必须给出完整答案。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.brain.agent import AgentLoop  # noqa: E402
from app.brain.llm_client import (LLMClient, load_output_budget,  # noqa: E402
                                  resolve_output_budget)
from app.core.config import LLMConfig  # noqa: E402

#: 一段典型的英文思维链（MiniMax-M3 的真实风格）
COT = ("We need answer Chinese in persona, short but this is technical. "
       "Need to explain asyncio.gather vs create_task, mention blocking IO, "
       "event loop, etc. Let's draft carefully and then output the answer.")


# --------------------------------------------------------------------------
# 离线假客户端
# --------------------------------------------------------------------------

class _FakeResp:
    """假的 httpx 流式响应（记录消费了几行 → 用来判断「有没有提前停」）。"""

    status_code = 200

    def __init__(self, lines: list[str]):
        self._lines = lines
        self.consumed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            self.consumed += 1
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
        self.resp = _FakeResp(lines)

    def stream(self, method, url, json=None, headers=None):
        self.sent.append(json)
        return self.resp

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
    """跑一次 chat_stream_events，返回 finish 事件 + 消费了多少行 SSE。"""
    client, fake = _client(lines, max_tokens)
    try:
        out = {}
        async for ev, data in client.chat_stream_events(
                [{"role": "user", "content": "主人说的话"}]):
            if ev == "finish":
                out = data
        out["_consumed"] = fake.resp.consumed
        out["_total_lines"] = len(fake.lines)
        out["_payload"] = fake.sent[0] if fake.sent else {}
        return out
    finally:
        await client.close()


def _long_cot_chunks(n_chunks: int = 30, size: int = 200) -> list[str]:
    """n 段超长思维链（每段 size 个字符），模拟「CoT 吃光预算」。"""
    body = "We need to think about this in great detail before answering. "
    return ["<think>" + body * 8 for _ in range(n_chunks)]


class _ScriptedClient:
    """按脚本逐轮返回的假 client（离线，不发网络请求）。"""

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
                         "cot_only": bool(turn.get("cot_only")),
                         "wasted": bool(turn.get("wasted"))}


class _EmptyRegistry:
    """空工具表（走不到任何工具分支）。"""

    def names(self):
        return []

    def to_openai(self):
        return []

    def execute(self, name, args):
        return ""


def _run_agent(client, max_turns: int = 3, user_text: str = "你好呀") -> list:
    """跑一轮 AgentLoop，返回事件列表（离线）。"""
    loop = asyncio.new_event_loop()
    try:
        async def _collect():
            agen = AgentLoop(client, _EmptyRegistry(),
                             max_turns=max_turns).run(
                [{"role": "system", "content": "你是桌宠"},
                 {"role": "user", "content": user_text}])
            return [ev async for ev in agen]
        return loop.run_until_complete(_collect())
    finally:
        loop.close()


# --------------------------------------------------------------------------
# 1) 档位：存在、可配置、有下限（A1）
# --------------------------------------------------------------------------

class TestOutputBudgetTiers:
    def test_repo_yaml_has_the_tiers(self):
        """档位写在 companion_config.yaml（不是 config.yaml —— 那里放 API key）。"""
        import yaml
        raw = yaml.safe_load(
            Path(__file__).resolve().parent.parent.joinpath(
                "app/core/companion_config.yaml").read_text(encoding="utf-8"))
        block = raw["output_budget"]
        for key in ("floor", "short", "medium", "long",
                    "short_input_chars", "long_input_chars"):
            assert key in block, f"output_budget 段缺 {key}"
        assert block["floor"] >= 256, "下限太小，短回复会被截断"
        assert block["short"] >= block["floor"]
        assert block["medium"] >= block["short"]
        assert block["long"] >= block["medium"]

    def test_loader_reads_the_yaml_section(self):
        cfg = load_output_budget(force=True)
        assert cfg.floor >= 256
        assert cfg.short >= cfg.floor
        assert cfg.medium >= cfg.short
        assert cfg.long >= cfg.medium

    def test_tiers_are_configurable(self, tmp_path, monkeypatch):
        """用户覆盖表（data/companion_config.yaml）能改档位。"""
        from app.core import companion_config as cc
        override = tmp_path / "companion_config.yaml"
        override.write_text(
            "output_budget:\n  floor: 300\n  short: 400\n"
            "  medium: 900\n  long: 2000\n",
            encoding="utf-8")
        monkeypatch.setattr(cc, "_user_yaml_path", lambda: override)
        try:
            cfg = load_output_budget(force=True)
            assert (cfg.floor, cfg.short, cfg.medium, cfg.long) == (
                300, 400, 900, 2000)
            assert cfg.budget_for("short") == 400
        finally:
            monkeypatch.undo()
            load_output_budget(force=True)

    def test_every_scene_respects_the_floor(self):
        """A1：任何场景的预算都不低于下限（普通短回复绝不被截断）。"""
        cfg = load_output_budget(force=True)
        for scene in ("short", "medium", "long", "不存在的档位"):
            assert cfg.budget_for(scene) >= cfg.floor

    def test_classify_picks_the_scene(self):
        cfg = load_output_budget(force=True)
        assert cfg.classify("几点了") == "short"
        assert cfg.classify("x" * (cfg.long_input_chars + 1)) == "long"
        assert cfg.classify("中间长度的一句话用来占位看看档位对不对", True) == "long"
        assert cfg.classify("x" * 40) == "medium"

    def test_resolve_output_budget_entry_point(self):
        assert resolve_output_budget("几点了") >= load_output_budget().floor
        # 工具密集 → 满档
        assert (resolve_output_budget("帮我把记事本打开", True)
                == load_output_budget().long)


# --------------------------------------------------------------------------
# 2) 早期止损：思维链吃光预算 → 当场停掉（A2）
# --------------------------------------------------------------------------

class TestWasteAbort:
    def test_long_cot_without_answer_is_stopped_early(self):
        """CoT 占掉预算的一大半、还没出正文 → 这次请求当场停掉。"""
        out = asyncio.run(_collect_finish(_sse(_long_cot_chunks()),
                                           max_tokens=1024))
        assert out.get("wasted") is True, "应该被止损"
        assert out.get("cot_only") is True, "止损后要让上层知道要重问"
        assert out.get("content") == "", "止损时还没有正文"
        assert out["_consumed"] < out["_total_lines"], "请求不该跑完"

    def test_abort_saves_the_rest_of_the_budget(self):
        """止损点在预算的 ratio 处（默认 60%），不是跑到预算尽头。"""
        out = asyncio.run(_collect_finish(_sse(_long_cot_chunks()),
                                           max_tokens=1024))
        assert out["_consumed"] < out["_total_lines"] * 0.9

    def test_answer_before_the_threshold_is_not_stopped(self):
        """出了正文就不止损 —— 正常回复不受影响。"""
        chunks = _long_cot_chunks(n_chunks=4, size=50)
        chunks = ["<think>" + "thinking " * 20, "</think>", "喵~主人好呀"]
        out = asyncio.run(_collect_finish(_sse(chunks), max_tokens=1024))
        assert out.get("wasted") is False
        assert out.get("cot_only") is False
        assert "喵~主人好呀" in out.get("content", "")
        assert out["_consumed"] == out["_total_lines"]

    def test_abort_can_be_switched_off(self, monkeypatch):
        """开关关掉 → 行为跟以前一样（跑完整个请求）。"""
        import dataclasses
        import app.brain.llm_client as lc
        base = lc.load_output_budget(force=True)
        monkeypatch.setattr(
            lc, "load_output_budget",
            lambda *a, **k: dataclasses.replace(base, waste_abort_enabled=False))
        out = asyncio.run(_collect_finish(_sse(_long_cot_chunks()),
                                           max_tokens=1024))
        assert out.get("wasted") is False
        assert out.get("cot_only") is True, "跑完了还是 CoT-only（老行为）"
        assert out["_consumed"] == out["_total_lines"]

    def test_abort_threshold_has_a_floor(self):
        """小额请求不许被误杀（min_tokens 下限）。"""
        cfg = load_output_budget(force=True)
        assert cfg.abort_threshold_tokens(64) >= cfg.waste_abort_min_tokens
        assert cfg.abort_threshold_tokens(0) == 0

    def test_no_budget_no_abort(self):
        """没有预算上限（max_tokens=0）→ 不止损。"""
        cfg = load_output_budget(force=True)
        assert cfg.abort_threshold_tokens(0) == 0


# --------------------------------------------------------------------------
# 3) 重问有界：止损也不能变成死循环（A4）
# --------------------------------------------------------------------------

class TestRetryIsBounded:
    def test_retry_happens_at_most_once(self):
        """CoT-only（含止损）→ 最多重问一次，绝不连发。"""
        client = _ScriptedClient([{"text": "", "cot_only": True, "wasted": True}])
        _run_agent(client, max_turns=6)
        assert client.calls == 2, f"最多重问一次，实际 {client.calls} 次"

    def test_retry_uses_the_full_budget(self):
        """止损之后用满档重问（CoT 已经把小档吃光了）。"""
        client = _ScriptedClient([
            {"text": "", "cot_only": True, "wasted": True},
            {"text": "唔……主人上次问的 asyncio 问题，重点是别在协程里做同步阻塞。"},
        ])
        evs = _run_agent(client)
        assert client.max_tokens_seen[1] == 4096
        assert client.max_tokens_seen[1] > client.max_tokens_seen[0]
        texts = [d for e, d in evs if e == "text"]
        assert texts and "asyncio" in texts[-1]

    def test_retry_event_is_announced(self):
        client = _ScriptedClient([{"text": "", "cot_only": True, "wasted": True}])
        evs = _run_agent(client)
        metas = [d for e, d in evs if e == "meta"]
        assert any(m.get("event") == "cot_only_retry" for m in metas)


# --------------------------------------------------------------------------
# 4) 档位真的传到了请求里（场景 → 预算）
# --------------------------------------------------------------------------

class TestAgentUsesSceneBudget:
    def test_casual_request_gets_the_small_tier(self):
        client = _ScriptedClient([{"text": "喵~主人好呀"}])
        _run_agent(client, user_text="你好呀")
        assert client.max_tokens_seen[0] == resolve_output_budget("你好呀", False)
        assert client.max_tokens_seen[0] < resolve_output_budget("你好呀", True)

    def test_tool_request_gets_the_full_tier(self):
        client = _ScriptedClient([{"text": "喵~主人好呀"}])
        _run_agent(client, user_text="帮我把记事本打开")
        assert client.max_tokens_seen[0] == resolve_output_budget(
            "帮我把记事本打开", True)

    def test_long_question_gets_the_full_tier(self):
        client = _ScriptedClient([{"text": "喵~主人好呀"}])
        _run_agent(client, user_text="讲讲 asyncio 的并发模型，" * 8)
        assert client.max_tokens_seen[0] == resolve_output_budget(
            "讲讲 asyncio 的并发模型，" * 8, False)

    def test_budget_never_exceeds_the_configured_ceiling(self):
        """分档只往下调，绝不越过 config.yaml 里的 llm.max_tokens。"""
        client = _ScriptedClient([{"text": "喵~主人好呀"}])
        _run_agent(client, user_text="帮我把记事本打开")
        assert client.max_tokens_seen[0] <= 4096
