"""闹钟意图识别的覆盖度回归（2026-10-04）。

现场：主人说「定一个1分钟闹钟」，`detect_action_intent` 返回 **None** ——
意图正则里**根本没有「闹钟」这个词**。于是下发全量 52 个工具，模型在
`add_reminder` / `countdown` 之间随机挑（实测带历史时调了
`add_reminder` × 2 还外加一个 `countdown`）。

漏词 = 路由不确定。所以这条锁的是**词表覆盖**，不是某一句话。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.brain.llm_client import detect_action_intent  # noqa: E402

# 必须判为闹钟意图的说法（含「闹钟」这个词的各种说法）
ALARM_PHRASES = [
    "定一个1分钟闹钟", "定个闹钟", "帮我定个两点的闹钟", "设个闹钟",
    "设定一个闹钟", "添加闹钟", "提醒我喝水", "帮我设置提醒",
    "30分钟后提醒我", "1小时后叫我", "到点叫我起床",
    "定一个一分钟的闹钟", "设个 5 分钟的提醒", "明天9点提醒我开会",
]

# 绝不能判为闹钟意图的说法
NOT_ALARM_PHRASES = [
    "煮面计时 3 分钟", "番茄钟 25 分钟", "倒数 60 秒",   # 纯计时 → countdown
    "打开星穹铁道", "打开QQ", "现在几点了", "今天天气怎么样",
    "主人今天辛苦啦", "我有点累", "记住我喜欢冰美式",
]


@pytest.mark.parametrize("q", ALARM_PHRASES)
def test_alarm_phrasings_detected(q):
    assert detect_action_intent(q) == "add_reminder", \
        f"{q!r} 应判为 add_reminder（漏词会让路由退化成全量下发）"


@pytest.mark.parametrize("q", NOT_ALARM_PHRASES)
def test_non_alarm_phrasings_not_misjudged(q):
    got = detect_action_intent(q)
    assert got != "add_reminder", \
        f"{q!r} 不该判为 add_reminder（会平白把工具集裁到提醒组）"


def test_alarm_word_itself_is_covered():
    """回归主问题：原表没有「闹钟」二字。

    「闹钟」是设闹钟最自然的说法，漏了它整个闹钟意图就退化成「识别不出 →
    下发全量工具 → 模型随机挑」——现场正是如此。
    """
    for q in ("定一个1分钟闹钟", "定个闹钟", "设个闹钟", "帮我定个闹钟"):
        assert detect_action_intent(q) == "add_reminder", q
    # 覆盖到词表里了：多几句口语也得认
    for q in ("给我弄个闹钟", "来定个闹钟吧", "闹钟定一下"):
        assert detect_action_intent(q) == "add_reminder", q
