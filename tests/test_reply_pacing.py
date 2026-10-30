# -*- coding: utf-8 -*-
"""回复逐句投递 —— 「一句话一条消息」（跟 qq-bridge 一个路子）。

owner 原话：「聊天窗口回复还是一下子蹦出来…我需要按照 qq-bridge 一样
一句话一句话回复」。

这里锁住四件事：
  1. 一段 N 句的回复 → 聊天区里 N 条**独立气泡**，句与句之间有间隔；
     一句话的回复**立刻**到（不许磨蹭）；
  2. **一个字都不能丢** —— 气泡拼回去 == 原回复（切分点上少了空白而已），
     网址不能被劈开、emoji 不能被劈成半个；
  3. **模型看到的历史不变** —— 还是**一整条** assistant 消息；
  4. 主人叫停 → 剩下的句子不发了。
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".local-packages"))
sys.path.insert(0, str(ROOT))

from app.core.qt_compat import QApplication
app = QApplication.instance() or QApplication(sys.argv)

from app.core.config import CharacterConfig
from app.ui.chat_window import ChatWindow, Message
from app.ui.chat_window.utils import (
    compute_reply_gap_ms, reply_pacing_settings, split_reply_sentences,
)


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _cw(tmp_path) -> ChatWindow:
    """一个聊天窗（落库指到临时目录，别把测试消息写进主人的聊天记录）。"""
    from app.engine.chat_store import ChatStore
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, CharacterConfig(name="小汐", persona=""),
                    "assets/sprites", asr_enabled=False)
    cw.chat_store = ChatStore(data_file=tmp_path / "chat.db", legacy_json=None)
    cw.resize(900, 600)
    cw.show()
    cw.chat_view.clear()
    cw.history = []
    return cw


def _fast_gaps(cw, lo=60, hi=120) -> None:
    """把间隔调小（测试里不用真的等 1~3 秒）。"""
    cw._reply_pacing_cfg = {
        "enabled": True, "min_gap_ms": lo, "max_gap_ms": hi,
        "long_gap_probability": 0.0, "long_gap_min_ms": lo,
        "long_gap_max_ms": hi, "max_chars_per_bubble": 200,
    }


def _run_reply(cw, reply: str, chunks=None, timeout_s=8.0):
    """模拟一轮回复（流式吐字 → on_done），返回每条气泡出现的时刻。

    返回 [(出现的秒数, 句子), ...]。
    """
    if chunks is None:
        chunks = [reply]
    cw._reply_pacing_reset()
    cw._current_bot_msg = Message(role="assistant", content="", speaker="小汐")
    cw._streaming_raw = ""
    cw._san_len = 0
    cw._streaming_san = ""
    cw._streaming_anchor_pos = cw._insert_placeholder(cw._current_bot_msg)

    # 观察员：记下每条句子**发出去**的时刻（气泡真正冒出来的瞬间）
    expected, _tail = split_reply_sentences(reply, split_tail=True)
    marks = []
    pending = list(expected)
    t0 = time.monotonic()

    def _record(sentence):
        marks.append((time.monotonic() - t0, sentence))
        return _orig_render(sentence)

    _orig_render = cw._render_reply_bubble
    cw._render_reply_bubble = _record

    # 喂字（**不** processEvents —— 节拍器统一在下面的观察循环里走，
    # 这样「第一条立刻到」和「后面的有间隔」都能量出来）
    for piece in chunks:
        cw._on_chunk(piece)
    cw._on_done(reply)

    while len(marks) < len(expected) and time.monotonic() - t0 < timeout_s:
        app.processEvents()
        time.sleep(0.001)
    # 让收尾跑完（写历史 / 落库）
    t1 = time.monotonic()
    while time.monotonic() - t1 < 2.0:
        app.processEvents()
        time.sleep(0.005)
        if cw._reply_final and not cw._reply_queue and not cw._reply_pacer.isActive():
            break
    cw._render_reply_bubble = _orig_render
    return marks


def _norm(text: str) -> str:
    """比较用：去掉所有空白。"""
    return "".join(str(text).split())


# ---------------------------------------------------------------------------
# 1. 拆句的安全规矩（不丢字 / 不劈网址 / 不劈 emoji）
# ---------------------------------------------------------------------------

def test_split_reply_sentences_keeps_url_whole():
    """网址是**不可切分的原子** —— 不能把 https://… 劈成两半。"""
    text = "主人看这个 https://example.com/a?b=1&c=2 特别好用。"
    sentences, tail = split_reply_sentences(text)
    joined = "".join(sentences) + tail
    assert _norm(joined) == _norm(text), joined
    url = "https://example.com/a?b=1&c=2"
    for piece in sentences + ([tail] if tail else []):
        # 只要沾了网址，就必须是**整条**网址（不能被劈成两半）
        if "example.com" in piece:
            assert url in piece, piece


def test_split_reply_sentences_keeps_emoji_whole():
    """emoji（代理对）不能被劈成半个。"""
    text = "今天心情很好 😀 一起吃饭吧。"
    sentences, tail = split_reply_sentences(text)
    joined = "".join(sentences) + tail
    assert _norm(joined) == _norm(text), joined
    assert any("😀" in s for s in sentences + [tail]), sentences


def test_split_reply_sentences_never_loses_characters():
    """拆句只吃切分点上的空白，别的字一个都不能少。"""
    text = ("第一句话。第二句话！第三句话？"
            "还有一段没有句号的话 https://example.com/x 和一个 emoji 🎉")
    sentences, tail = split_reply_sentences(text)
    joined = "".join(sentences) + tail
    assert _norm(joined) == _norm(text), joined


def test_split_reply_sentences_splits_by_sentence():
    """按句末标点切：一句一条。"""
    sentences, tail = split_reply_sentences("第一句。第二句。第三句。")
    assert sentences == ["第一句。", "第二句。", "第三句。"], sentences
    assert tail == "", tail


# ---------------------------------------------------------------------------
# 2. 逐句投递：一句话一条气泡，句与句之间有间隔
# ---------------------------------------------------------------------------

def test_reply_arrives_as_one_bubble_per_sentence(tmp_path):
    """A4：三句话的回复 → 聊天区里三条独立气泡，句与句之间有间隔。"""
    cw = _cw(tmp_path)
    _fast_gaps(cw)
    reply = "今天也要好好吃饭哦。小汐在家等主人回来喵。主人忙完了记得歇一歇。"
    marks = _run_reply(cw, reply)
    assert len(marks) == 3, marks
    # 每条气泡都是完整的一句
    assert [s for _, s in marks] == [
        "今天也要好好吃饭哦。", "小汐在家等主人回来喵。", "主人忙完了记得歇一歇。",
    ], marks
    # 句与句之间有间隔（第一句立刻到，后面的隔一会儿）
    assert marks[0][0] < 0.2, marks
    gaps = [marks[i + 1][0] - marks[i][0] for i in range(len(marks) - 1)]
    for gap in gaps:
        assert gap >= 0.03, "两句之间没有间隔：%r" % gaps
    # 聊天区里三条气泡都在，且没有把整段回复塞进一个气泡
    text = cw.chat_view.toPlainText()
    for sentence in ("今天也要好好吃饭哦。", "小汐在家等主人回来喵。",
                     "主人忙完了记得歇一歇。"):
        assert text.count(sentence) == 1, text
    assert reply not in text, "整段回复被塞进了一个气泡"


def test_single_sentence_reply_arrives_immediately(tmp_path):
    """A4：一句话的回复**立刻**到（不许等间隔）。"""
    cw = _cw(tmp_path)
    _fast_gaps(cw, lo=3000, hi=4000)   # 间隔调很大：立刻到才说明是「第一条」
    reply = "好的，主人。"
    marks = _run_reply(cw, reply)
    assert len(marks) == 1, marks
    assert marks[0][0] < 0.3, "一句话的回复等了 %r 秒才到" % marks[0][0]


def test_default_gap_config_is_qq_bridge_like():
    """默认间隔在 qq-bridge 的那个量级（1~3 秒），偶尔来一个长停顿。"""
    cfg = reply_pacing_settings()
    assert cfg["enabled"] is True
    assert cfg["min_gap_ms"] == 1000, cfg
    assert cfg["max_gap_ms"] == 3000, cfg
    assert 0.0 <= cfg["long_gap_probability"] <= 1.0, cfg
    assert cfg["long_gap_min_ms"] >= cfg["max_gap_ms"], cfg
    # 算出来的间隔都落在配置的范围里
    import random
    rnd = random.Random(7)
    for _ in range(50):
        gap = compute_reply_gap_ms(cfg, rnd)
        assert cfg["min_gap_ms"] <= gap <= max(
            cfg["max_gap_ms"], cfg["long_gap_max_ms"]), gap


def test_gap_is_configurable(tmp_path, monkeypatch):
    """间隔是**配置项**：把 yaml 段改了，间隔就跟着变。"""
    from app.ui.chat_window import utils as _u
    monkeypatch.setattr(_u, "_read_yaml_section", lambda section: {
        "enabled": True, "min_gap_ms": 40, "max_gap_ms": 80,
        "long_gap_probability": 0.0, "max_chars_per_bubble": 200,
    })
    cfg = reply_pacing_settings()
    assert cfg["min_gap_ms"] == 40 and cfg["max_gap_ms"] == 80, cfg


# ---------------------------------------------------------------------------
# 3. 模型看到的历史不变（拆句只是显示层的事）
# ---------------------------------------------------------------------------

def test_history_keeps_one_assistant_message(tmp_path):
    """A6：模型看到的仍然是一整条 assistant 消息（拆句只是显示层）。"""
    cw = _cw(tmp_path)
    _fast_gaps(cw)
    reply = "第一句。第二句。第三句。"
    _run_reply(cw, reply)
    assistant = [m for m in cw.history if m.role == "assistant"]
    assert len(assistant) == 1, cw.history
    assert assistant[0].content == reply, assistant[0].content


def test_bubbles_text_equals_original_reply(tmp_path):
    """A5：气泡拼回去 == 原回复（切分点上少了空白而已）。"""
    cw = _cw(tmp_path)
    _fast_gaps(cw)
    reply = ("今天也要好好吃饭哦。小汐在家等主人回来喵~ "
             "主人看这个 https://example.com/a?b=1 特别好用。"
             "开心到飞起 😀")
    _run_reply(cw, reply)
    assert cw._reply_sent, "一句都没发出去"
    assert _norm("".join(cw._reply_sent)) == _norm(reply), cw._reply_sent


# ---------------------------------------------------------------------------
# 4. 主人叫停 → 剩下的句子不发了
# ---------------------------------------------------------------------------

def test_stop_stops_remaining_sentences(tmp_path):
    """A7：叫停之后，排队里的句子不再冒出来。"""
    cw = _cw(tmp_path)
    _fast_gaps(cw, lo=3000, hi=4000)
    reply = "第一句。第二句。第三句。第四句。"
    cw._reply_pacing_reset()
    cw._current_bot_msg = Message(role="assistant", content="", speaker="小汐")
    cw._streaming_raw = ""
    cw._san_len = 0
    cw._streaming_san = ""
    cw._streaming_anchor_pos = cw._insert_placeholder(cw._current_bot_msg)
    cw._on_chunk("第一句。第二句。第三句。第四句。")
    app.processEvents()
    cw._on_done(reply)
    app.processEvents()
    # 队列里应该有剩下的句子
    assert len(cw._reply_queue) >= 2, cw._reply_queue
    queued = list(cw._reply_queue)
    cw._on_stop()
    app.processEvents()
    # 等一会儿：那些句子一条都不许再冒出来
    t0 = time.monotonic()
    while time.monotonic() - t0 < 0.6:
        app.processEvents()
        time.sleep(0.01)
    text = cw.chat_view.toPlainText()
    for sentence in queued:
        assert sentence not in text, "叫停之后还在发：%r" % sentence
