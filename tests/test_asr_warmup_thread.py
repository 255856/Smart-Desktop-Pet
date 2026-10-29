"""ASR 模型预热的线程安全回归测试（P0-10）。

背景：聊天窗原来在 **GUI 主线程** 上预热 faster-whisper 模型：

    QTimer.singleShot(1000, _do_warmup)     # _do_warmup 跑在 GUI 主线程
      -> self.asr._get_model()               # ctranslate2 / OpenMP 初始化

ctranslate2 在主线程初始化 OpenMP 会触发 *Windows fatal exception: access
violation* —— try/except 接不住，整个 pytest 进程直接 exit 139。

修复后：模型加载跑在后台线程，GUI 线程只收到「完成」信号。
本模块只 mock **模型加载**（_get_model），其余（排程 / 后台线程 / 完成信号 /
幂等 / 窗口析构保护）全部走真实代码路径。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".local-packages"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.qt_compat import QThread  # noqa: E402
from app.core.config import CharacterConfig  # noqa: E402
import app.ui.chat_window as cw_mod  # noqa: E402
from app.ui.chat_window import ChatWindow  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_chat_store(tmp_path, monkeypatch):
    """聊天历史写临时目录，不碰仓库里的 data/。"""
    from app.engine.chat_store import ChatStore as _RealChatStore

    def _factory(*args, **kwargs):
        kwargs.pop("data_file", None)
        kwargs["data_file"] = tmp_path / "chat_history.db"
        return _RealChatStore(*args, **kwargs)

    monkeypatch.setattr(cw_mod, "ChatStore", _factory)


def _make_window() -> ChatWindow:
    """造一个聊天窗（asr 默认开启，走真实的预热代码路径）。"""
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    char_cfg = CharacterConfig(name="预热测试娘", tts_enabled=False)
    return ChatWindow(llm_cfg, char_cfg, "assets/sprites")


def _pump_until(app, predicate, timeout: float = 15.0) -> bool:
    """转事件循环直到 predicate() 为真（超时返回 False）。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if predicate():
            return True
        app.processEvents()
        time.sleep(0.01)
    return bool(predicate())


def _delete_qt_object(obj) -> None:
    """真的销毁 Qt 侧的 C++ 对象（模拟窗口被关掉 / deleteLater 生效）。"""
    try:
        from PyQt5 import sip  # type: ignore
        sip.delete(obj)
        return
    except ImportError:
        pass
    try:
        import shiboken6  # type: ignore
        shiboken6.delete(obj)
    except ImportError:
        obj.deleteLater()


def test_warmup_loads_model_off_gui_thread(qapp, monkeypatch):
    """A1/A2：模型加载必须发生在后台线程；GUI 线程只收完成信号。"""
    cw = _make_window()
    if cw.asr is None:
        pytest.skip("ASR 不可用（未安装 sounddevice / faster-whisper）")

    gui_ident = threading.get_ident()
    gui_qthread = QThread.currentThread()

    seen: dict = {}
    loaded = threading.Event()

    def fake_get_model():
        # 注意：这是**实例**属性，不会自动绑定 self
        seen["thread_ident"] = threading.get_ident()
        seen["thread_name"] = threading.current_thread().name
        seen["qthread"] = QThread.currentThread()
        loaded.set()
        return object()          # sentinel：假装模型加载成功

    monkeypatch.setattr(cw.asr, "_get_model", fake_get_model, raising=False)

    # 完成信号必须在 GUI 线程被收到 —— 记录一下
    slot_calls: list[tuple] = []
    original_slot = cw._on_asr_warmup_done

    def spy_slot(ok, error):
        slot_calls.append((threading.get_ident(), ok, error))
        original_slot(ok, error)

    monkeypatch.setattr(cw, "_on_asr_warmup_done", spy_slot, raising=False)

    assert _pump_until(qapp, loaded.is_set), "ASR 预热线程没在超时前跑完"
    assert _pump_until(qapp, lambda: bool(slot_calls)), "GUI 线程没收到完成信号"

    print("[OK] 模型加载线程 = %s(ident=%s) / GUI 线程 = %s(ident=%s)" % (
        seen["thread_name"], seen["thread_ident"],
        threading.current_thread().name, gui_ident))
    print("[OK] QThread 上下文: worker=%r gui=%r" % (seen["qthread"], gui_qthread))

    # --- 核心断言：模型加载不在 GUI 线程上 ---
    assert seen["thread_ident"] != gui_ident, (
        "模型加载跑在 GUI 线程上了 —— ctranslate2 会 Windows access violation")
    assert seen["qthread"] is not gui_qthread, "QThread 上下文也不是后台线程"
    assert seen["thread_name"] == "asr-warmup"

    # --- 完成信号回到 GUI 线程，且成功 ---
    assert len(slot_calls) == 1
    ident, ok, error = slot_calls[0]
    assert ident == gui_ident, "完成信号没在 GUI 线程投递"
    assert ok is True, f"预热应成功：{error}"


def test_warmup_only_runs_once_per_window(qapp, monkeypatch):
    """幂等：一个窗口只预热一次（重复调用 _warmup_asr_model 不再触发加载）。"""
    cw = _make_window()
    if cw.asr is None:
        pytest.skip("ASR 不可用（未安装 sounddevice / faster-whisper）")

    calls: list[int] = []

    def fake_get_model():
        calls.append(threading.get_ident())
        return object()

    monkeypatch.setattr(cw.asr, "_get_model", fake_get_model, raising=False)

    # 构造时已经排程过一次；再调两次应该被幂等标记挡掉
    cw._warmup_asr_model()
    cw._warmup_asr_model()

    assert _pump_until(qapp, lambda: bool(calls)), "预热没跑"
    # 多转一会儿，确认没有第二次加载
    t0 = time.time()
    while time.time() - t0 < 0.3:
        qapp.processEvents()
        time.sleep(0.01)
    assert len(calls) == 1, f"预热被跑了 {len(calls)} 次"
    print("[OK] 预热只跑了 1 次（幂等）")


def test_warmup_survives_window_destroyed_during_load(qapp, monkeypatch):
    """A2：窗口在加载途中被销毁，worker 回来后不能碰已删除的 Qt 对象。"""
    from app.voice import asr as asr_mod

    started = threading.Event()
    release = threading.Event()
    returned = threading.Event()

    def slow_get_model(self):
        # 类属性 patch → 会自动绑定 self
        started.set()
        release.wait(timeout=15)
        returned.set()
        return object()

    monkeypatch.setattr(asr_mod.SpeechRecognizer, "_get_model", slow_get_model,
                        raising=False)

    cw = _make_window()
    if cw.asr is None:
        pytest.skip("ASR 不可用（未安装 sounddevice / faster-whisper）")

    # 完成信号若真的投递到已销毁的窗口，异常必须被记录下来（不能崩进程）
    slot_errors: list[str] = []
    original_slot = cw._on_asr_warmup_done

    def spy_slot(ok, error):
        try:
            original_slot(ok, error)
        except Exception as e:  # noqa: BLE001
            slot_errors.append(repr(e))

    monkeypatch.setattr(cw, "_on_asr_warmup_done", spy_slot, raising=False)

    assert _pump_until(qapp, started.is_set), "预热线程没起来"

    # 窗口在加载途中被销毁（Qt 侧 C++ 对象真的没了）
    _delete_qt_object(cw)

    # 放行加载：worker 回来后 emit 完成信号 —— 接收者已经没了，必须安全丢弃
    release.set()
    assert _pump_until(qapp, returned.is_set), "预热线程没返回"

    # 再转一会儿事件循环：排队信号若投递到已销毁对象会抛 RuntimeError
    t0 = time.time()
    while time.time() - t0 < 0.5:
        qapp.processEvents()
        time.sleep(0.01)

    assert slot_errors == [], f"完成信号打到了已销毁的窗口：{slot_errors}"
    print("[OK] 窗口销毁后预热线程安全收尾，没有碰已删除的 Qt 对象")
