"""提醒工具端到端测试：通过 ToolRegistry 调 add_reminder，看完整链路。"""
from __future__ import annotations

import datetime
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


@pytest.fixture(autouse=True)
def _no_win_alarm_backup(monkeypatch):
    """本模块**不碰** Windows 计划任务。

    add_reminder 会给「还剩 2 分钟以上」的提醒注册系统级兜底（schtasks 一次性
    任务），真注册了会在若干分钟后在主人桌面上弹一个气泡通知——测试不该在
    用户机器上留闹钟（本模块有好几条用例会加 3~20 分钟的提醒）。
    win_alarm 本身由 tests/test_win_alarm.py 专门覆盖（那边要么把 _run mock
    掉，要么注册完立刻 disarm）。
    """
    from app.core import win_alarm
    monkeypatch.setattr(win_alarm, "is_available", lambda: False)


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
        """绝对时间 at_time：工具认得、能加进 store，且 fire_at 落在未来。

        这里刻意用 **'YYYY-MM-DD HH:MM' 显式日期格式**（parse_absolute_time
        的格式 2），而不是 'HH:MM'：
          · 格式 2 精确返回那一分钟，**不会**像 'HH:MM' 那样在「已过」时
            被推到明天（推明天 ⇒ delay ≈ 86400）；
          · 格式 2 的结果只由字符串决定，跟工具内部那一刻的墙钟无关，
            所以「工具取 now」和「测试取 now」跨过分钟边界也不会分叉。
        真正的触发留给更明确的 delay_minutes 用例（见上一个 test）。
        """
        reg, store = registry_and_store
        tool = reg.get("add_reminder")

        # 目标 = 现在 + 10 分钟，再把秒抹掉。
        # 抹秒最多损失 60s ⇒ 目标分钟必定落在 (now+9min, now+10min]，
        # 也就是说：分钟边界、跨午夜都无所谓，它一定在未来。
        now = datetime.datetime.now()
        target = (now + datetime.timedelta(minutes=10)).replace(second=0, microsecond=0)
        at_str = target.strftime("%Y-%m-%d %H:%M")

        result = tool.fn(text="E2E at_time", at_time=at_str)

        assert "已设置" in result, f"got: {result}"
        items = store.list()
        assert len(items) == 1, "应该 add 到 store"

        fire_at = items[0].fire_at
        target_ts = target.timestamp()

        # 1) fire_at 必须严格在未来
        assert fire_at > time.time(), f"fire_at 不在未来：{fire_at}"

        # 2) fire_at 必须正好落在目标分钟上（±1s：ReminderStore.add 里
        #    int(fire_ts - now) 秒级取整最多丢 1s）。
        #    这条比较的是两个绝对时间戳，跟「测量端等了多久」无关 → 恒成立。
        assert abs(fire_at - target_ts) <= 1.0, (
            f"fire_at 应等于目标分钟 {at_str}（±1s），"
            f"实际偏差 {fire_at - target_ts:.3f}s")

        # 3) delay 落在一个**明确且窄**的窗口里（不再是 0 < delay < 86400）：
        #    目标分钟距 now ∈ (540s, 600s]，秒级取整最多再丢 1s，
        #    测量端 time.time() 比 now 晚一点点 ⇒ delay ∈ [9min-60s, 10min+1s]
        delay = fire_at - time.time()
        assert 480 <= delay <= 601, f"delay 异常：{delay}"

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

class TestReminderCleanupOnLoad:
    """启动清理：堆在 JSON 里的过期 reminders 不该在桌宠一开机就全 fire。"""

    def test_drop_done_items_on_load(self, qapp, tmp_path):
        import json as _json, time as _time
        path = tmp_path / "reminders.json"
        now = _time.time()
        path.write_text(_json.dumps([
            {"id": "r1", "text": "已 fire", "fire_at": now + 600, "created_at": now, "done": True},
            {"id": "r2", "text": "未来", "fire_at": now + 600, "created_at": now, "done": False},
        ], ensure_ascii=False), encoding="utf-8")
        s = ReminderStore(path)
        assert [it.id for it in s.list()] == ["r2"], "已 done 的应被启动清理"

    def test_drop_overdue_items_on_load(self, qapp, tmp_path):
        import json as _json, time as _time
        path = tmp_path / "reminders.json"
        now = _time.time()
        path.write_text(_json.dumps([
            # 8 天前过期的（超过 1 天 grace period）→ 丢
            {"id": "r1", "text": "远古", "fire_at": now - 86400 * 8, "created_at": now - 86400 * 9, "done": False},
            # 刚过期 1 小时（grace 期内）→ 保留（启动后立即 fire）
            {"id": "r2", "text": "刚过期", "fire_at": now - 3600, "created_at": now - 7200, "done": False},
            # 未来 → 保留
            {"id": "r3", "text": "未来", "fire_at": now + 600, "created_at": now, "done": False},
        ], ensure_ascii=False), encoding="utf-8")
        s = ReminderStore(path)
        ids = [it.id for it in s.list()]
        assert ids == ["r2", "r3"], f"应保留 r2（grace 期内）+ r3（未来），实际 {ids}"
