"""提醒定时器跨线程回归（2026-10-04 现场）。

背景：AgentLoop 通过 `asyncio.to_thread` 在工作线程里执行工具，
`add_reminder` → `ReminderStore.add()` → `_schedule_next()` → `QTimer.start()`。
但 QTimer 只允许在**属主线程**（主线程）start()，跨线程调用是 Qt 明令禁止的
no-op：

    QObject::startTimer: Timers cannot be started from another thread

它不抛异常、`isActive()` 甚至还返回 True，于是：提醒真的写进了 reminders.json、
设置面板里真的看得见、工具也真的回报「已设置提醒」——但**永远不会响**。
这就是最恶劣的「假装成功」。

修法：用一个 `reschedule` 信号把调度请求排队回主线程（与本仓库
ui_controller 里 TTS 口型同步桥同一套路）。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.engine.reminder import ReminderStore  # noqa: E402


@pytest.fixture(autouse=True)
def _no_asr_native_load(monkeypatch):
    """本模块要 pump 事件循环，必须先把 ASR 的原生模型加载短路掉。

    前面别的测试（构造 ChatWindow 且开了 asr）会留下一颗
    `QTimer.singleShot(1000, _do_warmup)`。本模块一旦调 processEvents()，
    它就会被引爆 → faster_whisper 的 CTranslate2 在构造模型时抛
    **Windows fatal access violation**（try/except 接不住，原生崩溃）。
    本模块的用例跟语音识别毫无关系，直接短路即可。
    """
    from app.voice import asr as asr_mod
    monkeypatch.setattr(asr_mod.SpeechRecognizer, "_get_model",
                        lambda self, *a, **k: None, raising=False)


def _pump(app, seconds):
    """跑一小段事件循环，让排队的信号 / 定时器有机会执行。"""
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.02)


@pytest.fixture
def store(qapp, tmp_path):
    s = ReminderStore(tmp_path / "reminders.json")
    yield s


def test_add_from_worker_thread_actually_fires(qapp, store):
    """回归主问题：在工作线程里 add，提醒必须真的到点触发。"""
    fired: list[str] = []
    store.reminder_triggered.connect(fired.append)

    def worker():
        store.add(1, "该喝水了")

    t = threading.Thread(target=worker, name="tool-worker")
    t.start()
    t.join()

    # 调度是排队到主线程的，泵一下事件循环让定时器真正武装起来
    _pump(qapp, 0.3)
    _pump(qapp, 2.0)

    assert fired == ["该喝水了"], (
        f"跨线程 add 的提醒没有触发：{fired}。"
        "若这里空，多半是 QTimer.start() 又跑回工作线程去了。")


def test_add_from_main_thread_still_fires(qapp, store):
    """主线程路径不能被修复搞坏。"""
    fired: list[str] = []
    store.reminder_triggered.connect(fired.append)
    store.add(1, "主线程的提醒")
    _pump(qapp, 2.0)
    assert fired == ["主线程的提醒"], fired


def test_schedule_never_starts_timer_off_thread(qapp, store):
    """结构性防线：_schedule_next 本身不能在非属主线程碰 QTimer。"""
    assert store._timer.thread() == qapp.thread(), "定时器属主应为主线程"
    done = []

    def worker():
        store._schedule_next()      # 不应抛，也不应在这里 start
        done.append(True)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=5)
    assert done == [True], "跨线程调度请求应被安全接收"
    _pump(qapp, 0.1)               # 主线程侧执行真正的 start


def test_remove_from_worker_thread_is_safe(qapp, store):
    """跨线程删除提醒（delete_reminder 工具）同样要走信号桥。"""
    item = store.add(60, "很久以后")
    done = []

    def worker():
        assert store.remove(item.id) is True
        done.append(True)

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout=5)
    assert done == [True]
    _pump(qapp, 0.2)
    assert not [i for i in store.list() if not i.done], "提醒应已删除"


def test_scheduled_item_persists_and_can_be_reloaded(qapp, store, tmp_path):
    """定时器修好后，跨重启仍然要能恢复调度。"""
    def worker():
        store.add(1, "重启后仍要响")

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    _pump(qapp, 0.2)

    fresh = ReminderStore(tmp_path / "reminders.json")
    assert len([i for i in fresh.list() if not i.done]) == 1, "落盘后应能读回"
    fresh.reschedule.emit()
    _pump(qapp, 0.2)
