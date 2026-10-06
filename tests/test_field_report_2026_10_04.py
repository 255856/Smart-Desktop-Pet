"""2026-10-04 现场回归：反思回复被删空 + 重试刷屏 + tool_choice 空转。

现场（用户截图 + pet.log）：
  1) 主人问「你是不是在假装调用工具」，桌宠回
     「哎呀，被主人发现了呢~以后小汐会注意的，做了什么就老老实实调用工具，
     不再假装了啦~」——这句被清洗规则整句删光（"调用工具"命中工具独白），
     最终变成空气泡 + 「唔……刚刚没想好怎么说」。
  2) 一次「定一个1分钟闹钟」连出 5 张「自动重试 第N轮未调用工具」卡片，
     主人干等 12 秒。
  3) tool_choice='required' 服务端收下但模型不调，每轮先白等一次。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.brain.agent import _sanitize_reply  # noqa: E402
from app.brain.llm_client import (  # noqa: E402
    _TOOL_CHOICE_IGNORED, _is_pollution_sentence, _note_tool_choice_ignored,
    _tool_choice_is_ignored, sanitize_text,
)


# =============================================================================
# 1) 反思语境不得被当成工具独白删空
# =============================================================================

@pytest.mark.parametrize("text", [
    "哎呀，被主人发现了呢~以后小汐会注意的，做了什么就老老实实调用工具，不再假装了啦~",
    "主人对不起，我之前不该假装调用工具，已经改掉了~",
    "我下次不会再假装调工具了，真的会好好调~",
    "以后我老老实实调用工具，主人看着就好~",
])
def test_reflection_about_tools_survives(text):
    """这条整句必须原样送到主人眼前。

    修复前：`_META_TOOL_SENT_RE` 里的「调用…工具」命中 → 整句当工具独白删光
    → AgentLoop 判「过度清洗」→ 空气泡 + 兜底话术。
    """
    out = _sanitize_reply(text)
    assert out.strip(), f"反思回复被删空了：{text!r}"
    # 必须基本完整，不能只剩尾巴
    assert len(out) >= len(text) * 0.6, f"被削得只剩片段：{out!r}（原文 {text!r}）"


@pytest.mark.parametrize("text", [
    "让我调用 open_app 工具来打开它",
    "我应该使用 web_search 工具搜索一下",
    "这应该用 calculate 工具算",
])
def test_real_tool_monologue_still_stripped(text):
    """真正的工具独白仍然要删——别把修复做成漏洞。"""
    assert sanitize_text(text, is_final=True).strip() == "", text
    assert _is_pollution_sentence(text) is True, text


def test_normal_reply_unaffected():
    for t in ("好的主人，我已经帮主人打开QQ啦~", "主人今天辛苦啦~",
              "嗯嗯，我知道了，下次不会了~"):
        assert _sanitize_reply(t).strip() == t, t


# =============================================================================
# 2) 重试提示每轮只弹一次
# =============================================================================

def test_retry_notice_is_shown_once_per_turn():
    """连续 5 轮 force_retry 只能产生 1 张提示卡。

    现场是每轮都 _append_system_msg，聊天区被 5 张「自动重试」刷满。
    """
    from app.ui.chat_window import ChatWindow

    shown = []

    class _W:
        """只借用 ChatWindow._on_meta 的重试分支，不起真窗口。"""
        _retry_notice_shown = False
        trace = None
        _trace_run_id = None
        _current_bot_msg = None

        def _append_system_msg(self, text):
            shown.append(text)

    from app.ui.chat_window import ChatWindow as _C
    w = _W()
    _C._on_meta(w, "meta", {"event": "force_retry", "reason": "第 1 轮未调用工具"})
    for i in range(2, 6):
        _C._on_meta(w, "meta", {"event": "force_retry",
                                "reason": f"第 {i} 轮未调用工具"})
    assert len(shown) == 1, f"应只提示一次，实际 {len(shown)} 次：{shown}"
    assert "调用工具" in shown[0]

    # 新一轮用户消息要重新允许提示
    w._retry_notice_shown = False
    _C._on_meta(w, "meta", {"event": "force_retry", "reason": "新一轮"})
    assert len(shown) == 2, "新一轮对话应重新提示一次"


def test_retry_notice_notice_wording_has_no_false_promise():
    """提示语不能说「自动重试一次」——可能重试很多次，不能骗人。"""
    from app.ui.chat_window import ChatWindow
    shown = []

    class _W:
        _retry_notice_shown = False
        trace = None
        _trace_run_id = None
        _current_bot_msg = None

        def _append_system_msg(self, text):
            shown.append(text)

    ChatWindow._on_meta(_W(), "meta", {"event": "force_retry", "reason": "x"})
    assert "一次" not in shown[0], shown[0]


# =============================================================================
# 3) tool_choice 空转记忆（按模型、进程级）
# =============================================================================

def test_tool_choice_ignore_memory_is_per_model():
    _note_tool_choice_ignored("fake-model-xyz")
    try:
        assert _tool_choice_is_ignored("fake-model-xyz") is True
        assert _tool_choice_is_ignored("另一个模型") is False
    finally:
        _TOOL_CHOICE_IGNORED.discard("fake-model-xyz")


def test_tool_choice_memory_survives_new_client_instance():
    """记忆必须跨 LLMClient 实例——chat_window 每条消息都新建 client，
    挂在实例上的话根本记不到。"""
    from app.brain.llm_client import LLMClient, LLMConfig
    _note_tool_choice_ignored("fake-model-persist")
    try:
        cfg = LLMConfig(base_url="http://x/v1", api_key="k",
                        model="fake-model-persist")
        a = LLMClient(cfg, "p")
        b = LLMClient(cfg, "p")
        assert _tool_choice_is_ignored(a.cfg.model) is True
        assert _tool_choice_is_ignored(b.cfg.model) is True
    finally:
        _TOOL_CHOICE_IGNORED.discard("fake-model-persist")


# =============================================================================
# 4) 真实调用路径（回归：曾把模块级函数误写成 self._tool_choice_is_ignored，
#    现场直接 AttributeError，主人只看到「[错误] 调用出错」）
# =============================================================================

TOOLS = [{
    "type": "function",
    "function": {
        "name": "add_reminder",
        "description": "设置提醒",
        "parameters": {"type": "object",
                       "properties": {"text": {"type": "string"}},
                       "required": ["text"]},
    },
}]


def _client_with_fake_stream(model, payloads):
    """把 _do_stream_request 换掉，记录每次请求的 payload。"""
    import asyncio
    from app.brain.llm_client import LLMClient, LLMConfig

    cfg = LLMConfig(base_url="http://fake/v1", api_key="k", model=model,
                    stream=True, max_tokens=256)

    class _C(LLMClient):
        async def _do_stream_request(self, url, payload, headers,
                                     cancel_check=None):
            payloads.append(dict(payload))
            yield ("finish", {"reason": "stop",
                              "tool_calls": [{"id": "c1", "name": "add_reminder",
                                              "arguments": '{"text":"喝水"}'}],
                              "content": "", "reasoning": ""})

    c = _C(cfg, "人设")
    # client 建好就绪，假装 key 有效
    c.cfg.api_key = "sk-real"
    return c


def _drain(c):
    import asyncio

    async def go():
        out = []
        async for ev, data in c.chat_stream_events(
                [{"role": "user", "content": "30分钟后提醒我喝水"}],
                tools=TOOLS, force_tool_use=True):
            out.append((ev, data))
        return out
    return asyncio.run(go())


def test_force_path_runs_without_attributeerror():
    """没被标记过的模型：正常走 tool_choice=required，不许炸。"""
    payloads = []
    c = _client_with_fake_stream("fake-model-fresh", payloads)
    events = _drain(c)
    assert events, "应产出事件"
    assert payloads[0].get("tool_choice") == "required", \
        f"首次仍应尝试 tool_choice：{payloads[0]}"


def test_known_ignoring_model_skips_required_request():
    """被标记过的模型：直接 prompt 强制，不再白发一次 required 请求。"""
    model = "fake-model-ignored"
    _note_tool_choice_ignored(model)
    try:
        payloads = []
        c = _client_with_fake_stream(model, payloads)
        events = _drain(c)
        assert events, "应产出事件"
        assert len(payloads) == 1, f"应只发一次请求，实际 {len(payloads)} 次"
        assert "tool_choice" not in payloads[0], \
            f"已知被忽略就不该再带 tool_choice：{payloads[0]}"
        # tools 还是要带的
        assert payloads[0].get("tools") == TOOLS
    finally:
        _TOOL_CHOICE_IGNORED.discard(model)
