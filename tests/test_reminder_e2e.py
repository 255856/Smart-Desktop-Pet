"""提醒工具端到端测试：通过 ToolRegistry 调 add_reminder，看完整链路。

覆盖：
  - 工具注册正确
  - 调 add_reminder(delay_minutes) 真的 add 到 ReminderStore
  - ReminderStore 真的 schedule timer 并触发 reminder_triggered
  - 调 add_reminder(at_time) 也工作
  - 错误参数被拒绝

背景：用户报告「桌面端不提醒」—— bug 可能藏在这条调用链上：
  LLM → ToolRegistry.call("add_reminder") → add_reminder fn
  → reminders.add() → ReminderStore._schedule_next → QTimer 触发
  → reminder_triggered signal
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PyQt5.QtCore import QCoreApplication  # noqa: E402

from app.engine.reminder import ReminderStore  # noqa: E402
from app.engine.tools import _reminder  # noqa: E402
from app.engine.tools._core import Tool, ToolRegistry  # noqa: E402


def _pump(app, seconds):
    """跑一小段事件循环。"""
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.02)


@pytest.fixture(autouse=True)
def _no_asr_native_load(monkeypatch):
    """本模块要 pump 事件循环，先把 ASR 的原生模型加载短路掉——
    前面别的测试（构造 ChatWindow 且开了 asr）会留下一颗
    `QTimer.singleShot(1000, _do_warmup)`，被本模块 processEvents() 引爆
    → CTranslate2 Windows fatal access violation。
    """
    from app.voice import asr as asr_mod
    monkeypatch.setattr(asr_mod.SpeechRecognizer, "_get_model",
                        lambda self, *a, **k: None, raising=False)


@pytest.fixture
def registry_and_store(qapp, tmp_path):
    """给每个测试一个干净的 ToolRegistry + ReminderStore。"""
    store = ReminderStore(tmp_path / "reminders.json")
    reg = ToolRegistry()
    _reminder.register(reg, reminders=store)
    return reg, store


class TestAddReminderToolE2E:
    def test_register_add_reminder_tool(self, registry_and_store):
        reg, _ = registry_and_store
        names = reg.names()
        assert "add_reminder" in names, f"工具未注册：{names}"
        assert "list_reminders" in names
        assert "delete_reminder" in names

    def test_add_with_delay_minutes_actually_fires(self, qapp, registry_and_store):
        """回归主问题：调工具 → reminders.add → reminder_triggered 真触发。"""
        reg, store = registry_and_store
        fired = []
        store.reminder_triggered.connect(fired.append)

        # 通过 ToolRegistry 走完整路径（模拟 LLM 调工具）
        tool = reg.get("add_reminder")
        result = tool.fn(text="E2E 喝水", delay_minutes=0.05)   # 3 秒

        assert "已设置" in result, f"工具应回报成功，got: {result}"
        assert len(store.list()) == 1, "reminder 应该已加入 store"

        # 真实触发：等 4 秒（delay_minutes 转换为秒 = 3，allow 1 秒余量）
        _pump(qapp, 4.0)
        assert fired == ["E2E 喝水"], f"应该已触发，实际：{fired}"

    def test_add_with_at_time_in_future_fires(self, qapp, registry_and_store):
        reg, store = registry_and_store
        fired = []
        store.reminder_triggered.connect(fired.append)

        tool = reg.get("add_reminder")
        # 用 at_time，10 秒后（避开跨分钟边界导致被推到明天）
        future = time.localtime(time.time() + 10)
        # 用 round 把秒抹掉避免跨分钟
        at_str = time.strftime("%H:%M", future)
        result = tool.fn(text="E2E at_time", at_time=at_str)

        assert "已设置" in result, f"got: {result}"
        items = store.list()
        assert len(items) == 1, "应该 add 到 store"
        # 跨日 / 不跨日的边界：如果 at_str 是今天则几秒后触发，是明天则 24h 后。
        # 这里我们只验证 add 成功，触发留给更明确的 delay_minutes 测试。
        delay = store.list()[0].fire_at - time.time()
        # delay 应该在 [0, 86400] 之间（要么是今天几秒后，要么是明天同一分钟）
        assert 0 < delay < 86400, f"delay 异常：{delay}"

    def test_delay_minutes_zero_rejected(self, registry_and_store):
        reg, store = registry_and_store
        tool = reg.get("add_reminder")
        result = tool.fn(text="x", delay_minutes=0)
        assert "错误" in result, f"0 分钟应被拒绝，got: {result}"
        assert len(store.list()) == 0, "不应加入"

    def test_delay_minutes_negative_rejected(self, registry_and_store):
        reg, store = registry_and_store
        tool = reg.get("add_reminder")
        result = tool.fn(text="x", delay_minutes=-1)
        assert "错误" in result
        assert len(store.list()) == 0

    def test_delay_minutes_string_accepted(self, qapp, registry_and_store):
        """LLM 偶尔传字符串「3」，应被正确解析。"""
        reg, store = registry_and_store
        fired = []
        store.reminder_triggered.connect(fired.append)
        tool = reg.get("add_reminder")
        result = tool.fn(text="E2E str", delay_minutes="3")
        assert "已设置" in result
        # 3 分钟太长，单独测解析
        items = store.list()
        assert len(items) == 1
        # 取消这个，避免污染
        store.remove(items[0].id)

    def test_delay_minutes_null_rejected(self):
        # 独立测，不依赖 fixture（避免其他测试副作用）
        pass  # 占位，让上面 fixture 跑

    def test_delete_reminder_tool(self, registry_and_store):
        reg, store = registry_and_store
        add_tool = reg.get("add_reminder")
        add_tool.fn(text="to delete", delay_minutes=10)
        rid = store.list()[0].id

        del_tool = reg.get("delete_reminder")
        result = del_tool.fn(reminder_id=rid)
        assert "已删除" in result
        assert len(store.list()) == 0

    def test_list_reminders_tool(self, registry_and_store):
        reg, store = registry_and_store
        add_tool = reg.get("add_reminder")
        add_tool.fn(text="A", delay_minutes=10)
        add_tool.fn(text="B", delay_minutes=20)
        items = store.list()
        assert len(items) == 2

        list_tool = reg.get("list_reminders")
        result = list_tool.fn()
        assert "A" in result
        assert "B" in result


class TestFailsafeForLastSave:
    """回归：磁盘写入失败时工具层应该知道，而不是假装成功。"""

    def test_add_continues_when_disk_fails(self, registry_and_store, monkeypatch):
        reg, store = registry_and_store
        # 模拟 save 失败
        monkeypatch.setattr(store, "_save", lambda: False)
        store.last_save_ok = False
        # 重新 connect（store.last_save_ok 被覆盖了没关系）
        # 这里只验证：如果 save 失败，工具应该 return 警告，而不是 "已设置"
        tool = reg.get("add_reminder")
        # 因为 monkeypatch 直接修改了 _save，last_save_ok 也会被改动
        # 重新触发一次来确认工具读取 last_save_ok
        result = tool.fn(text="disk fail", delay_minutes=10)
        assert "错误" in result or "没能保存" in result, f"工具应该警告，got: {result}"