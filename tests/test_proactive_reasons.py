# -*- coding: utf-8 -*-
"""主动搭话：多源触发（记忆 / 最近对话 / AW / 自主想法 / 状态 / 规律 / 时间）。

owner 报的现象：主动打招呼只会顺着 AW 说话。根因：素材一股脑丢给模型，
模型永远挑最容易的那一样；而且**最近对话压根没进上下文**。

这里钉的是：
  A1 有素材的理由才可用（没 AW 数据就别硬聊 AW）
  A2 理由会轮换（不会连着三轮同一个理由）
  A3 选中的理由要进提示词（模型知道这一轮从哪看）
  A4 最近对话 + 自主念头要进上下文
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.qt_compat import QApplication  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _app():
    return QApplication.instance() or QApplication(sys.argv[:1])


def _brain(**kw):
    """造一个真 ProactiveBrain（不调模型、不连 sidecar）。"""
    from app.brain.proactive import ProactiveBrain
    llm = type("L", (), {"api_key": "sk-test", "model": "test"})()
    return ProactiveBrain(llm_cfg=llm, persona="我是小汐。", state=None,
                          memory=None, char_name="小汐", **kw)


def _ctx(**over):
    base = {
        "now": "2026-11-05 10:00 星期三",
        "is_night": False,
        "time_hint": "上午",
        "pet_status": "心情不错",
        "memories": [],
        "shared_memories": [],
        "recent_remarks": [],
        "routine_hint": "",
        "aw_context": "",
        "recent_dialogue": [],
        "own_ideas": [],
    }
    base.update(over)
    return base


# --- A1：理由的可用性 ----------------------------------------------------


def test_a_reason_needs_its_own_material():
    """没素材的理由这一轮不可用（不能硬聊）。"""
    b = _brain()
    avail = b._available_reasons(_ctx())
    assert "memory" not in avail, avail
    assert "context" not in avail, avail
    assert "aw" not in avail, avail
    assert "routine" not in avail, avail
    # 不需要外部数据的这几个永远可用
    assert "idea" in avail, avail
    assert "state" in avail, avail
    assert "time" in avail, avail


def test_aw_reason_is_off_when_the_server_is_not_running():
    """AW 没跑时它返回的是一句提示，不是素材 → 理由不可用。"""
    b = _brain()
    ctx = _ctx(aw_context="ActivityWatch 未运行")
    assert "aw" not in b._available_reasons(ctx)
    ctx2 = _ctx(aw_context="最近 30 分钟：VSCode / 浏览器")
    assert "aw" in b._available_reasons(ctx2)


def test_memory_and_context_turn_on_when_they_have_material():
    b = _brain()
    ctx = _ctx(memories=["主人说过他怕冷"], shared_memories=["一起下过棋"],
               recent_dialogue=["主人：帮我看下这个 bug"])
    avail = b._available_reasons(ctx)
    assert "memory" in avail, avail
    assert "context" in avail, avail


# --- A2：理由会轮换 ------------------------------------------------------


def test_reasons_do_not_repeat_the_same_one_over_and_over():
    """连着挑 12 次，不能老是同一个理由（原来只有 AW）。"""
    b = _brain()
    ctx = _ctx(memories=["a"], shared_memories=["b"],
               recent_dialogue=["c"], aw_context="VSCode", routine_hint="写代码")
    picked = []
    for _ in range(12):
        picked.append(b._pick_reasons(ctx)[0])
    assert len(set(picked)) >= 3, picked
    for i in range(2, len(picked)):
        assert not (picked[i] == picked[i - 1] == picked[i - 2]), picked


def test_a_reason_used_twice_in_a_row_is_discounted():
    """刚用过的理由权重要被压下去 → 下一轮更可能换一个。"""
    b = _brain()
    # 只留 AW 与「自主想法」两个理由（其余的素材都拿掉）
    ctx = _ctx(aw_context="VSCode")
    ctx["pet_status"] = ""
    ctx["time_hint"] = ""
    # A: 没有历史 → aw 的份额
    b._recent_reasons = []
    picks_a = [b._pick_reasons(ctx)[0] for _ in range(200)]
    # B: 每次都当成「aw 刚用过」 → 份额必须明显更低
    b._recent_reasons = []
    picks_b = []
    for _ in range(200):
        b._recent_reasons = ["aw", "aw"]
        picks_b.append(b._pick_reasons(ctx)[0])
    share_a = picks_a.count("aw") / len(picks_a)
    share_b = picks_b.count("aw") / len(picks_b)
    assert share_b < share_a / 2, (share_a, share_b)


def test_own_thoughts_stay_available_even_with_nothing_else():
    """「自主想法」永远可用 —— 桌宠本来就会自己嘀咕。"""
    b = _brain()
    ctx = _ctx()
    ctx["pet_status"] = ""
    ctx["time_hint"] = ""
    assert b._available_reasons(ctx) == ["idea"]
    assert b._pick_reasons(ctx) == ["idea"]


# --- A3：选中的理由要进提示词 --------------------------------------------


def test_the_chosen_reason_shows_up_in_the_prompt():
    b = _brain()
    ctx = _ctx(focus=["idea"])
    system, messages = b._compose(ctx)
    assert "自己的念头" in system, system[-800:]
    assert "6. 这一轮从" in system, system[-800:]
    payload = json.loads(messages[0].content)
    assert payload["focus"] == ["idea"]


def test_two_reasons_get_two_rules():
    b = _brain()
    system, _ = b._compose(_ctx(focus=["memory", "aw"]))
    assert "这一轮从**记忆**" in system, system[-800:]
    assert "这一轮从**主人当下在做的事**" in system, system[-800:]


# --- A4：最近对话 + 自主念头进上下文 --------------------------------------


def test_recent_dialogue_is_formatted_with_who_said_it():
    """桌宠要能接着刚才的话冒泡 → 最近对话必须进上下文。"""
    b = _brain(recent_turns_provider=lambda n: [
        ("user", "帮我看下这个 bug"), ("assistant", "在看啦"),
    ])
    turns = b._recent_dialogue(4)
    assert turns == ["主人：帮我看下这个 bug", "小汐：在看啦"], turns


def test_recent_dialogue_failure_does_not_break_the_bubble():
    """取不到最近对话 → 当没有，不能因此不冒泡。"""
    def boom(n):
        raise RuntimeError("db gone")
    b = _brain(recent_turns_provider=boom)
    assert b._recent_dialogue(4) == []


def test_own_ideas_are_always_available():
    b = _brain()
    ideas = b._pick_ideas(2)
    assert len(ideas) == 2
    assert all(str(x).strip() for x in ideas)

