"""verify_features.py 的记忆检查必须**真的问一次** sidecar（不是 grep 源码）。

老的检查是 grep 源码文本（detect_conflicts 在不在），功能被改坏了照样「通过」。
进程内的记忆库删掉之后，这一项改成对 sidecar 的真调用（/health + /stats + /search），
这里钉住：

- 没在跑 → NOTE（离线检查不该判红），而且**不碰仓库 data/ 一个字节**
- 在跑但答得不对 → FAIL（不是「名字还在就绿」）
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def _load_verify_module():
    """按路径加载 scripts/verify_features.py（它不是包）。"""
    path = ROOT / "scripts" / "verify_features.py"
    spec = importlib.util.spec_from_file_location("verify_features_probe", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _snapshot_data_dir() -> dict:
    """data/ 下每个文件的 (mtime_ns, size)。"""
    out = {}
    if not DATA_DIR.is_dir():
        return out
    for p in sorted(DATA_DIR.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(DATA_DIR))] = (st.st_mtime_ns, st.st_size)
    return out


def _install_fake_http(monkeypatch, handler):
    """把 memory_client 的默认 IO 换掉（探测自己会 new 一个客户端）。"""
    from app.brain import memory_client

    monkeypatch.setattr(memory_client, "_urllib_http", handler)


def _json(obj, status=200):
    return status, json.dumps(obj, ensure_ascii=False).encode("utf-8")


def _healthy_handler(method, url, payload, timeout):
    if url.endswith("/health"):
        return _json({"ok": True, "cards": 3, "cardsTotal": 3,
                      "models": {"embed": "ready", "rerank": "ready"},
                      "degraded": False})
    if url.endswith("/stats"):
        return _json({"ok": True, "stats": {"cards": 3, "active": 3,
                                            "byKind": {"fact": 3}}})
    if url.endswith("/search"):
        return _json({"ok": True, "results": [{"id": "mem_x", "text": "x",
                                               "score": 0.5}]})
    return _json({"ok": False, "error": "not found"}, status=404)


# --------------------------------------------------------------------------
# A1：sidecar 没在跑 → NOTE（离线检查不该判红）
# --------------------------------------------------------------------------

def test_probe_reports_note_when_sidecar_is_absent(monkeypatch):
    """端口上没人听 → 立刻给 NOTE（不白等 90s）。"""
    def _refused(method, url, payload, timeout):
        raise ConnectionRefusedError("nothing on 8788")

    _install_fake_http(monkeypatch, _refused)
    mod = _load_verify_module()
    monkeypatch.setattr(mod, "_port_is_open", lambda host, port: False)
    status, detail = mod._probe_memory_sidecar()
    assert status == "NOTE", detail
    assert "sidecar" in detail and "没在跑" in detail


def test_port_is_open_helper_is_honest(monkeypatch):
    """端口探测只连一下：没人听就是 False（不抛、不等）。"""
    import socket

    mod = _load_verify_module()
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()          # 立刻释放 → 这个端口上没人听
    assert mod._port_is_open("127.0.0.1", port) is False
    assert mod._sidecar_port() == 8788
    assert mod._sidecar_host() == "127.0.0.1"


def test_probe_passes_when_sidecar_answers(monkeypatch):
    _install_fake_http(monkeypatch, _healthy_handler)
    mod = _load_verify_module()
    monkeypatch.setattr(mod, "_port_is_open", lambda host, port: True)
    status, detail = mod._probe_memory_sidecar()
    assert status == "OK", detail
    assert "/search" in detail


def test_probe_fails_when_stats_is_broken(monkeypatch):
    """sidecar 在跑但 /stats 给不出卡片数 → FAIL（不是「能连上就绿」）。"""
    def _half_broken(method, url, payload, timeout):
        if url.endswith("/health"):
            return _json({"ok": True, "cards": 3})
        if url.endswith("/stats"):
            return _json({"ok": True, "stats": {}})
        return _json({"ok": True, "results": []})

    _install_fake_http(monkeypatch, _half_broken)
    mod = _load_verify_module()
    monkeypatch.setattr(mod, "_port_is_open", lambda host, port: True)
    status, detail = mod._probe_memory_sidecar()
    assert status == "FAIL", detail


# --------------------------------------------------------------------------
# A2：探测过程 data/ 不变（记忆只在 sidecar 里，探一次不该留任何痕迹）
# --------------------------------------------------------------------------

def test_probe_does_not_modify_repo_data(monkeypatch):
    """跑一次探测，data/ 的 mtime + size 一个都不能变。"""

    def _refused(method, url, payload, timeout):
        raise ConnectionRefusedError("nothing on 8788")

    _install_fake_http(monkeypatch, _refused)
    mod = _load_verify_module()
    monkeypatch.setattr(mod, "_port_is_open", lambda host, port: False)
    before = _snapshot_data_dir()
    status, _detail = mod._probe_memory_sidecar()
    assert status in ("NOTE", "FAIL")
    assert _snapshot_data_dir() == before, "探测把仓库 data/ 改了"
