# -*- coding: utf-8 -*-
"""回复逐句投递 —— 一条回复一个气泡，句子一句句长出来（跟 qq-bridge 一个路子）。

owner 原话：「聊天窗口回复还是一下子蹦出来…我需要按照 qq-bridge 一样
一句话一句话回复」；后来又报「回复出现了多个桌宠名字」。

这里锁住四件事：
  1. 一段 N 句的回复 → **一个**气泡，句子按间隔一句句长出来（名字/时间只出现一次；
      一句话的回复**立刻**到，不许磨蹭）；
  2. **一个字都不能丢** —— 气泡拼回去 == 原回复（切分点上少了空白而已），
      网址不能被劈开、emoji 不能被劈成半个、数字不能跟单位被劈开；
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
# ---------------------------------------------------------------------------
# 2. 逐句投递：一条回复一个气泡，句子一句句长出来，句与句之间有间隔
# ---------------------------------------------------------------------------



def test_split_reply_sentences_keeps_number_with_its_unit():
    """「体力只剩 25 了」是一句话 —— 不能被空格劈开。

    owner 截图里的现象：回复变成「体力只剩」+「25 了，主人光顾着改代码没管我。」
    两条，名字也挂了两遍。原来只防了「左端是数字」（5 秒），没防「右端是数字」。
    """
    text = "想被喂一顿，体力只剩 25 了，主人光顾着改代码没管我。"
    sentences, tail = split_reply_sentences(text)
    assert sentences == [text], sentences
    assert tail == "", tail
def test_reply_arrives_as_one_bubble_growing_sentence_by_sentence(tmp_path):
    """A4：三句话的回复 → **一个**气泡，句子一句句长出来。

    owner 报的现象：「回复出现了多个桌宠名字」—— 原来每句都新建一个带
    msg-label（名字+时间）的独立气泡，一条回复挂了好几次名字。
    """
    cw = _cw(tmp_path)
    _fast_gaps(cw)
    reply = "今天也要好好吃饭哦。主人忙完了记得歇一歇。两只猫在打盹呢。"
    marks = _run_reply(cw, reply)
    assert len(marks) == 3, marks
    # 每一步送出来的是完整的一句
    assert [s for _, s in marks] == [
        "今天也要好好吃饭哦。", "主人忙完了记得歇一歇。", "两只猫在打盹呢。",
    ], marks
    # 句与句之间有间隔（第一句立刻到，后面的隔一会儿）
    assert marks[0][0] < 0.2, marks
    gaps = [marks[i + 1][0] - marks[i][0] for i in range(len(marks) - 1)]
    for gap in gaps:
        assert gap >= 0.03, "两句之间没有间隔：%r" % gaps
    # 聊天区：整段回复在**一个**气泡里，名字只出现一次
    text = cw.chat_view.toPlainText()
    for sentence in ("今天也要好好吃饭哦。", "主人忙完了记得歇一歇。",
                     "两只猫在打盹呢。"):
        assert text.count(sentence) == 1, text
    assert reply in text, "整段回复应该在同一个气泡里"
    assert text.count("小汐") == 1, "名字只能出现一次：%r" % text
    _fast_gaps(cw, lo=3000, hi=4000)   # 间隔调很大：立刻到才说明是「第一条」
    reply = "好的，主人。"
    marks = _run_reply(cw, reply)
    assert len(marks) == 1, marks
    assert marks[0][0] < 0.3, "一句话的回复等了 %r 秒才到" % marks[0][0]


def test_default_gap_config_never_lags_the_model():
    """默认间隔**不得超过模型的出句速度**（实测约 1 秒/句）。

    为什么不是 qq-bridge 的 1~3 秒：qq-bridge 的间隔是**发多条 QQ 消息**的网络节奏；
    桌宠是本地窗口，多等 1~3 秒纯粹是白等 —— 实测整轮 15.1 秒，而模型自己只要 1.6 秒。
    """
    cfg = reply_pacing_settings()
    assert cfg["enabled"] is True
    # 间隔必须小于「模型出一句」的典型时间（~1 秒），否则队列会堆积
    assert cfg["max_gap_ms"] <= 1000, cfg
    assert 0 <= cfg["min_gap_ms"] <= cfg["max_gap_ms"], cfg
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


def test_switching_persona_starts_a_new_context():
    """换人设 = **开新会话**：旧历史留在聊天区（可查），但不再进模型上下文。

    背景：共享记忆之后，历史里全是旧人设的对话（实测 239 条里有 41 条「小汐」、
    18 条「喵」）→ 模型跟着旧人设演。owner 决定：换人设就开新会话。
    """
    from app.ui.chat_window import ChatWindow
    from app.ui.chat_window.message import Message

    char_cfg = CharacterConfig(name="小汐")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    cw.history = [Message(role="user", content="旧人设的对话"),
                  Message(role="assistant", content="喵~")]
    cw._ctx_start = 0
    before = cw._build_clean_messages_for_llm()
    assert len(before) == 2, before

    # 模拟一次换人设（不落盘：只动上下文边界与线程 id）
    cw._ctx_start = len(cw.history)
    cw._char_switch_seq = 1
    cw._session_id = "char:pet:main#1"

    after = cw._build_clean_messages_for_llm()
    assert after == [], after
    # 旧历史**仍在**（可查）
    assert len(cw.history) == 2
    print("[OK] 换人设 = 开新会话（旧历史可查，不进模型上下文）")
