"""chat.request_timeout_s 配置 + LLMClient 端到端穿透测试。

目的：把单次 chat 超时从硬编码挪到 config.yaml（chat.request_timeout_s, 默认 20）,
让 ProactiveBrain / WerewolfDirector / ChatWindow / _ChatOnceWorker 都从这里读,
而不是各自传 llm_cfg 时忘了设超时。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import ChatConfig, Config, load_config


# ---------- ChatConfig 默认值 ----------

def test_chat_config_default_request_timeout_is_20():
    """ChatConfig 默认 request_timeout_s=20（不能写死成 60 或其它）。"""
    cfg = ChatConfig()
    assert cfg.request_timeout_s == 20


def test_chat_config_can_be_overridden():
    """yaml / 构造都能覆盖默认值。"""
    assert ChatConfig(request_timeout_s=8).request_timeout_s == 8
    assert ChatConfig(request_timeout_s=0).request_timeout_s == 0  # 0=无限


# ---------- Config 顶层暴露 chat ----------

def test_config_top_level_chat_is_chat_config():
    """Config.chat 是 ChatConfig 类型实例,默认值即 ChatConfig()。"""
    cfg = Config()
    assert isinstance(cfg.chat, ChatConfig)
    assert cfg.chat.request_timeout_s == 20


def test_load_config_chat_default(tmp_path: Path):
    """没写 chat 段时,加载出的 Config.chat 也是默认 ChatConfig(20)。"""
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "llm:\n  api_key: ''\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path)
    assert cfg.chat.request_timeout_s == 20


def test_load_config_chat_overridden(tmp_path: Path):
    """yaml 显式写 chat.request_timeout_s 时,加载后生效。"""
    yaml_path = tmp_path / "config.yaml"
    yaml_path.write_text(
        "llm:\n  api_key: ''\nchat:\n  request_timeout_s: 7\n",
        encoding="utf-8",
    )
    cfg = load_config(yaml_path)
    assert cfg.chat.request_timeout_s == 7


# ---------- LLMClient 接收 request_timeout_s 覆盖 cfg.timeout ----------

class _DummyAsyncClient:
    """记录 httpx.AsyncClient 实际收到的 timeout,而不是 mock 掉它。"""
    instances: list[object] = []

    def __init__(self, timeout):
        _DummyAsyncClient.instances.append(timeout)
        self.timeout = timeout


def test_llm_client_uses_explicit_request_timeout(monkeypatch):
    """传入 request_timeout_s 时,httpx.AsyncClient 拿到的是它（覆盖 cfg.timeout）。"""
    import app.brain.llm_client as llm_mod

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _DummyAsyncClient)
    _DummyAsyncClient.instances.clear()

    cfg = llm_mod.LLMConfig(api_key="x", timeout=99)   # cfg.timeout=99,应被覆盖
    client = llm_mod.LLMClient(cfg, "sys", request_timeout_s=15)
    try:
        assert len(_DummyAsyncClient.instances) == 1
        used = _DummyAsyncClient.instances[0]
        # httpx.Timeout 对象,read=传入值,connect=10.0
        assert used.read == 15
        assert used.connect == 10.0
    finally:
        # 避免 client._client.__aexit__ 走真实 httpx
        pass


def test_llm_client_falls_back_to_cfg_timeout(monkeypatch):
    """不传 request_timeout_s 时,httpx.AsyncClient 用 cfg.timeout。"""
    import app.brain.llm_client as llm_mod

    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _DummyAsyncClient)
    _DummyAsyncClient.instances.clear()

    cfg = llm_mod.LLMConfig(api_key="x", timeout=42)
    client = llm_mod.LLMClient(cfg, "sys")
    used = _DummyAsyncClient.instances[0]
    assert used.read == 42
    assert used.connect == 10.0


# ---------- 穿透：ProactiveBrain → _ChatOnceWorker → LLMClient 用 chat 超时 ----------

def _make_brain(chat_cfg=None):
    """构造 ProactiveBrain 但不走 _fire（避开 timer + memory.recent 的副作用）。"""
    from app.brain.proactive import ProactiveBrain

    class FakeMemory:
        def short_term(self): return []
        def recall(self): return ""

    return ProactiveBrain(
        llm_cfg=SimpleNamespace(api_key="x"),
        persona="",
        state=SimpleNamespace(),
        memory=FakeMemory(),
        char_name="测试",
        chat_cfg=chat_cfg,
    )


def test_proactive_brain_stores_chat_timeout():
    """ProactiveBrain(chat_cfg=ChatConfig(8)) 持有 _request_timeout_s == 8。"""
    brain = _make_brain(chat_cfg=ChatConfig(request_timeout_s=8))
    assert brain._request_timeout_s == 8


def test_proactive_brain_chat_cfg_none_means_none():
    """ProactiveBrain 不传 chat_cfg 时,_request_timeout_s 是 None（由 LLMClient fall back）。"""
    brain = _make_brain()
    assert brain._request_timeout_s is None


def test_chat_once_worker_init_saves_request_timeout(monkeypatch):
    """_ChatOnceWorker(__init__) 真的把 request_timeout_s 存到 self 上,后续传给 LLMClient。"""
    from app.brain.proactive import _ChatOnceWorker

    # 不调 start() / run(),只测 __init__ 存住字段
    worker = _ChatOnceWorker(
        llm_cfg=SimpleNamespace(api_key="x"),
        system_prompt="sys",
        messages=[],
        request_timeout_s=11,
    )
    assert worker.request_timeout_s == 11


# ---------- 穿透：WerewolfDirector / ChatWindow 存 chat_timeout_s ----------

def test_werewolf_director_stores_chat_timeout():
    """WerewolfDirector(chat_cfg=ChatConfig(12))._chat_timeout_s == 12。"""
    from app.games.werewolf_director import WerewolfDirector

    d = WerewolfDirector(llm_cfg=SimpleNamespace(api_key="x"),
                         chat_cfg=ChatConfig(request_timeout_s=12))
    assert d._chat_timeout_s == 12


def test_werewolf_director_chat_cfg_none_means_none():
    from app.games.werewolf_director import WerewolfDirector

    d = WerewolfDirector(llm_cfg=SimpleNamespace(api_key="x"))
    assert d._chat_timeout_s is None


def test_werewolf_window_passes_chat_cfg_through(qapp):
    """WerewolfWindow 持有 chat_cfg,构造时存住（不直接传给 director,仅在 _start 里用）。"""
    from app.ui.werewolf_window import WerewolfWindow
    ww = WerewolfWindow(llm_cfg=SimpleNamespace(api_key="x"),
                        chat_cfg=ChatConfig(request_timeout_s=5))
    assert ww.chat_cfg is not None
    assert ww.chat_cfg.request_timeout_s == 5