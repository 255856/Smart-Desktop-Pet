"""纯聊天（无工具）路径的 usage 接线测试（Stage 0.2 补 C1/C2）。

C1: 纯聊天路径的 usage 事件必须进 _on_meta —— 状态栏 token 数会增加,
    Trace 里也会多一条 "usage" 事件。
C2: 轻量路径写进台账的 session_id 必须跟标准路径用的是同一个
    （聊天窗的 self._session_id，标准路径的 LangChainAgent._session_id）。

全部离线: HTTP 层用 httpx.MockTransport 打桩, 不需要 API Key、不发网络包。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".local-packages"))
sys.path.insert(0, str(ROOT))

from app.core.qt_compat import QApplication  # noqa: E402
_app = QApplication.instance() or QApplication(sys.argv)

import pytest  # noqa: E402

from app.brain.llm_client import LLMClient, LLMConfig  # noqa: E402
from app.core.config import CharacterConfig  # noqa: E402


@pytest.fixture(autouse=True)
def _no_asr_warmup(monkeypatch):
    """聊天窗构造完会用 QTimer 预热 faster-whisper 模型 —— 测试里别碰它
    （本机 ctranslate2 加载会直接崩进程）。"""
    from app.ui.chat_window import ChatWindow
    monkeypatch.setattr(ChatWindow, "_warmup_asr_model", lambda self: None)


_SSE = (
    'data: {"choices":[{"delta":{"content":"你好"}}]}\n'
    "\n"
    'data: {"choices":[],"usage":{"prompt_tokens":120,"completion_tokens":7,'
    '"total_tokens":127,"prompt_tokens_details":{"cached_tokens":100}}}\n'
    "\n"
    "data: [DONE]\n"
    "\n"
)


def _handler(request: httpx.Request) -> httpx.Response:
    """打桩的 HTTP 层：一个包都不发出去。"""
    return httpx.Response(200, content=_SSE.encode("utf-8"))


class _FakeTrace:
    def __init__(self):
        self.events = []
        self.runs = []

    def begin_run(self, goal, mode="react", meta=None):
        self.runs.append(goal)
        return "rtest123"

    def record(self, rid, kind, payload):
        self.events.append((rid, kind, payload))

    def end_run(self, rid, *args, **kwargs):
        pass


def _make_window():
    char_cfg = CharacterConfig(name="台账测试娘", tts_enabled=False)
    llm_cfg = LLMConfig(model="test", api_key="sk-test-key",
                        base_url="http://127.0.0.1:9/v1")
    from app.ui.chat_window import ChatWindow
    return ChatWindow(llm_cfg, char_cfg, "assets/sprites")


def _stub_llm_client(monkeypatch):
    """把聊天窗用的 LLMClient 换成打桩 HTTP 的版本。"""
    import app.ui.chat_window as cw

    class _StubLLMClient(LLMClient):
        def __init__(self, cfg, system_prompt, request_timeout_s=None):
            super().__init__(cfg, system_prompt, request_timeout_s)
            self._client = httpx.AsyncClient(
                transport=httpx.MockTransport(_handler))

    monkeypatch.setattr(cw, "LLMClient", _StubLLMClient)
    return _StubLLMClient


def _use_tmp_ledger(monkeypatch, tmp_path):
    from app.brain import token_ledger
    path = tmp_path / "token-usage.jsonl"
    monkeypatch.setattr(token_ledger, "_SINGLETON",
                        token_ledger.TokenLedger(path))
    return path


def _pump_until_worker_done(win, timeout_s=15.0):
    """等 QThread 跑完并把排队中的信号送出去。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        _app.processEvents()
        worker = getattr(win, "_worker", None)
        if worker is not None and worker.isFinished():
            break
        time.sleep(0.01)
    for _ in range(40):
        _app.processEvents()
        time.sleep(0.005)


def test_plain_chat_feeds_status_bar_and_trace(tmp_path, monkeypatch):
    """C1: 纯聊天跑一轮 → 状态栏 token 数增加 + Trace 收到 "usage" 事件。"""
    _stub_llm_client(monkeypatch)
    ledger_path = _use_tmp_ledger(monkeypatch, tmp_path)

    win = _make_window()
    trace = _FakeTrace()
    win.trace = trace
    win._accum_tokens = 0
    win._accum_prompt = 0
    win._accum_completion = 0

    win._kickoff_llm()
    _pump_until_worker_done(win)

    # 状态栏的 token 计数真的涨了（以前这条路径永远是 0）
    assert win._accum_tokens == 127
    assert win._accum_prompt == 120
    assert win._accum_completion == 7

    # Trace 里有一条 "usage" 事件
    usage_events = [e for e in trace.events if e[1] == "usage"]
    assert len(usage_events) == 1
    rid, _kind, payload = usage_events[0]
    assert rid == "rtest123"
    assert payload["event"] == "usage"
    assert payload["total_tokens"] == 127
    assert payload["cache_hit_tokens"] == 100
    assert payload["cache_miss_tokens"] == 20

    # 用户看到的文本没变（"你好"）
    texts = [e for e in trace.events if e[1] == "text"]
    assert texts, "纯聊天必须照常产出文本事件"


def test_plain_chat_ledger_row_carries_window_session_id(tmp_path, monkeypatch):
    """C2: 轻量路径的台账行带的是聊天窗的 session_id。"""
    _stub_llm_client(monkeypatch)
    ledger_path = _use_tmp_ledger(monkeypatch, tmp_path)

    win = _make_window()
    win.trace = _FakeTrace()
    win._kickoff_llm()
    _pump_until_worker_done(win)

    rows = [json.loads(line) for line in
            ledger_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert rows
    row = rows[-1]
    assert row["backend"] == "lightweight"
    assert row["session_id"] == win._session_id
    assert row["session_id"] == "char:台账测试娘:main"
    assert row["total_tokens"] == 127
    assert row["cache_hit_tokens"] == 100


def test_stream_worker_emits_usage_meta_signal(tmp_path, monkeypatch):
    """C1 的下层: _StreamWorker 把 usage 事件发到 meta 通道（文本通道不变）。"""
    from app.ui.chat_window.worker import _StreamWorker

    cfg = LLMConfig(model="test", api_key="sk-test-key",
                    base_url="http://127.0.0.1:9/v1")
    client = LLMClient(cfg, "人设")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))

    worker = _StreamWorker(client, [__import__(
        "app.brain.llm_client", fromlist=["ChatMessage"]).ChatMessage(
            role="user", content="你好")])
    metas, dones, fails = [], [], []
    worker.meta.connect(lambda kind, payload: metas.append((kind, payload)))
    worker.done.connect(lambda text: dones.append(text))
    worker.failed.connect(lambda msg: fails.append(msg))
    worker.start()
    assert worker.wait(20000)
    # 跨线程的信号是排队投递的 -> 阻塞的 wait() 期间不会执行,
    # 这里把事件循环转一下把排队的信号送出来。
    for _ in range(40):
        _app.processEvents()
        time.sleep(0.005)

    assert not fails
    # 文本通道一个字都没变
    assert dones == ["你好"]
    # usage 走了 meta 通道
    assert len(metas) == 1
    kind, payload = metas[0]
    assert kind == "meta"
    assert payload["event"] == "usage"
    assert payload["prompt_tokens"] == 120
    assert payload["cache_hit_tokens"] == 100
