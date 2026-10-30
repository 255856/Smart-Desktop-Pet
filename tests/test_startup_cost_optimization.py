# -*- coding: utf-8 -*-
"""启动性能优化的单元测试（帧率节流 / 桥就绪握手 / Chromium 参数 / sidecar 懒预热）。

覆盖的东西都是**行为契约**，不是实现细节：
* 静置帧率不能超过用户给的帧率上限（max_fps 是硬上限）；
* 帧率上限要真的降得下来（PIXI v7 的 maxFPS setter 只会取大 —— 回归护栏）；
* 模型加载不再盲等 400ms（桥就绪即加载，且只加载一次）；
* Chromium 瘦身 flag 存在，且不覆盖调用方已经设好的环境变量；
* sidecar 的模型预热**推迟到第一次检索**（BM25 立刻可用，功能不减）。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# Live2D 渲染器：帧率节流
# --------------------------------------------------------------------------
class _FakePage:
    """记录 runJavaScript 调用的假页面。"""

    def __init__(self):
        self.calls: list[str] = []

    def runJavaScript(self, code, callback=None):
        self.calls.append(code)
        if callback is not None:
            callback(None)
        return None


def _make_renderer(max_fps: int = 30, idle_fps: int = 12):
    """造一个不启动 Chromium 的 Live2DRenderer（只测纯逻辑）。"""
    from app.animation.live2d_renderer import Live2DRenderer

    r = object.__new__(Live2DRenderer)
    r.max_fps = max_fps
    r.idle_fps = max(5, min(int(idle_fps), max_fps))
    r._page = _FakePage()
    r._load_attempted = False
    return r


def test_idle_fps_never_exceeds_max_fps():
    """静置帧率是「静置时的上限」，不能超过用户的 max_fps（硬上限）。"""
    from app.animation.live2d_renderer import Live2DRenderer, DEFAULT_IDLE_FPS

    assert DEFAULT_IDLE_FPS >= 5
    r = _make_renderer(max_fps=30, idle_fps=30)
    r.set_idle_fps(60)
    assert r.idle_fps == 30, "静置帧率不能超过 max_fps"

    r2 = _make_renderer(max_fps=10, idle_fps=12)
    r2.set_idle_fps(12)
    assert r2.idle_fps == 10


def test_set_idle_fps_pushes_to_js():
    """set_idle_fps 要真的下发到 JS（否则改了设置也没用）。"""
    r = _make_renderer(max_fps=30, idle_fps=12)
    r.set_idle_fps(8)
    assert r.idle_fps == 8
    joined = "\n".join(r._page.calls)
    assert "setIdleFps(8)" in joined


def test_set_max_fps_also_clamps_idle_fps():
    """把 max_fps 调小之后，静置帧率不能还停在更高的值（否则反而更快）。"""
    r = _make_renderer(max_fps=60, idle_fps=12)
    r.set_max_fps(10)
    assert r.max_fps == 10
    assert r.idle_fps == 10
    joined = "\n".join(r._page.calls)
    assert "setMaxFps(10)" in joined
    assert "setIdleFps(10)" in joined


def test_bridge_sets_min_fps_before_max_fps():
    """回归护栏：PIXI v7 的 maxFPS setter 只会取大，必须先压 minFPS。

    只设 maxFPS 的话，从 30fps 降到 12fps 是**不生效**的
    （setter 里 Math.max(this.minFPS, t)，minFPS 还是 30）。
    """
    bridge = _PROJECT_ROOT / "app" / "animation" / "live2d_bridge.html"
    content = bridge.read_text(encoding="utf-8")
    assert "_setFps" in content, "缺少统一的帧率设置入口 _setFps()"
    i_min = content.find("t.minFPS = v")
    i_max = content.find("t.maxFPS = v")
    assert i_min != -1 and i_max != -1, "_setFps 里必须同时设 minFPS 与 maxFPS"
    assert i_min < i_max, "必须先设 minFPS 再设 maxFPS（PIXI setter 只会取大）"
    # 除 _setFps 外不允许再有裸的 ticker.maxFPS / ticker.minFPS 赋值
    start = content.find("_setFps(fps) {")
    end = content.find("\n    },", start)
    assert start != -1 and end != -1
    outside = content[:start] + content[end:]
    # 先去掉 JS 注释（注释里也会提到 ticker.maxFPS）
    stripped = chr(10).join(line.split("//")[0] for line in outside.split(chr(10)))
    assert "ticker.maxFPS =" not in stripped, "帧率只能经 _setFps 设置"
    assert "ticker.minFPS =" not in stripped, "帧率只能经 _setFps 设置"


def test_bridge_loads_model_with_idle_fps():
    """loadModel 要把静置帧率一起传下去。"""
    bridge = _PROJECT_ROOT / "app" / "animation" / "live2d_bridge.html"
    content = bridge.read_text(encoding="utf-8")
    assert "loadModel(httpBase, settingsName, fitFactor, hideWm, maxFps, idleFps)" in content
    assert "setIdleFps(fps)" in content


# --------------------------------------------------------------------------
# Live2D 渲染器：桥就绪握手（不再盲等 400ms）
# --------------------------------------------------------------------------
def test_bridge_ready_triggers_load_once(monkeypatch):
    """JS 报桥就绪 → 立刻 loadModel；重复通知只加载一次。"""
    import app.animation.live2d_renderer as mod

    r = _make_renderer()
    calls: list[int] = []
    monkeypatch.setattr(r, "_js_load_model", lambda: calls.append(1))

    r.on_bridge_ready()
    r.on_bridge_ready()
    assert len(calls) == 1, "桥就绪只能触发一次 loadModel"


def test_page_loaded_does_not_blind_wait(monkeypatch):
    """页面加载完成不再排 400ms 的盲等（只留一个兜底定时器）。"""
    import app.animation.live2d_renderer as mod

    delays: list[int] = []
    monkeypatch.setattr(mod.QTimer, "singleShot",
                        staticmethod(lambda ms, cb: delays.append(ms)))
    r = _make_renderer()
    r.on_bridge_ready = lambda: None
    r._on_page_loaded(True)
    assert 400 not in delays, "不该再有 400ms 盲等"
    assert delays and delays[0] >= 1000, "兜底定时器要留够时间等桥就绪"


def test_load_model_passes_idle_fps(monkeypatch):
    """loadModel 的 JS 里要带上静置帧率。"""
    r = _make_renderer(max_fps=30, idle_fps=12)
    r._http_base = "http://127.0.0.1:1"
    r._settings_name = "x.model3.json"
    r.hide_watermark = True
    r.profile = type("P", (), {"watermark_mode": "none"})()
    r._js_load_model()
    joined = "\n".join(r._page.calls)
    assert "loadModel(" in joined
    assert "30, 12)" in joined, "loadModel 没带上 idleFps"


# --------------------------------------------------------------------------
# Chromium 参数
# --------------------------------------------------------------------------
def test_webengine_flags_include_trim():
    """瘦身 flag 要在（少进程 / 少后台任务）。"""
    from app.core.webengine_env import chromium_flags

    flags = chromium_flags()
    for flag in ("--disable-breakpad", "--disable-background-networking",
                 "--disable-component-update", "--renderer-process-limit=1"):
        assert flag in flags, f"缺少 Chromium 瘦身 flag: {flag}"
    # GPU flag 必须在（不然 Chromium 走软渲染，CPU 反而更凶）
    assert "--use-gl=angle" in flags


def test_webengine_trim_can_be_disabled():
    """trim=False 时只留 GPU flag（量"改造前"基线时用）。"""
    from app.core.webengine_env import chromium_flags

    flags = chromium_flags(trim=False)
    assert "--use-gl=angle" in flags
    assert "--disable-breakpad" not in flags


def test_configure_webengine_env_does_not_override():
    """外部已经设过 QTWEBENGINE_CHROMIUM_FLAGS 时不要覆盖（用户的优先）。"""
    from app.core.webengine_env import configure_webengine_env

    env = {"QTWEBENGINE_CHROMIUM_FLAGS": "--mine"}
    out = configure_webengine_env(env)
    assert env["QTWEBENGINE_CHROMIUM_FLAGS"] == "--mine"
    assert out == "--mine"


# --------------------------------------------------------------------------
# 设置项
# --------------------------------------------------------------------------
def test_settings_schema_has_idle_fps():
    """设置面板要能改静置帧率（只加一个开关，不铺开一堆）。"""
    from app.core.pet_settings import SETTING_SCHEMA, GROUP_ORDER

    spec = SETTING_SCHEMA.get("live2d_idle_fps")
    assert spec is not None, "缺少 live2d_idle_fps 设置项"
    assert spec["type"] == "int"
    assert spec["group"] in GROUP_ORDER
    assert spec["min"] >= 5


# --------------------------------------------------------------------------
# sidecar：预热推迟到第一次检索
# --------------------------------------------------------------------------
class _FakeStore:
    """只记录 warmup 调用次数的假 store。"""

    def __init__(self):
        self.warmup_calls = 0
        self.data_dir = "/tmp/fake"
        self._cards = []

    def _active_cards(self):
        return []

    def warmup(self):
        self.warmup_calls += 1
        return {"embed": "ok", "rerank": "ok", "vectorIndex": "ok"}

    def search(self, query, top_k=8, mood=None, kind=None, kinds=None, key=None):
        # 不依赖模型的词面腿（BM25）—— 模型没加载也要能回答
        return {"results": [{"id": "c1", "text": query, "score": 1.0}],
                "path": "bm25", "degraded": False, "error": None}


def test_sidecar_warmup_is_deferred_to_first_search():
    """起服务**不**预热；第一次检索才预热（BM25 立刻可用，功能不减）。"""
    from memory_sidecar.server import MemorySidecarServer

    store = _FakeStore()
    srv = MemorySidecarServer(store, port=0, warmup=True)
    srv.start()
    try:
        time.sleep(0.3)
        assert store.warmup_calls == 0, "起服务时不该预热（那正是启动 CPU 的大头）"
        # 第一次检索 → 触发预热
        srv._on_search()
        deadline = time.time() + 5
        while store.warmup_calls == 0 and time.time() < deadline:
            time.sleep(0.05)
        assert store.warmup_calls == 1, "第一次检索后应该预热"
        # 幂等：再来一次检索不会重复预热
        srv._on_search()
        time.sleep(0.2)
        assert store.warmup_calls == 1
    finally:
        srv.stop()


def test_sidecar_no_warmup_never_warms_up():
    """--no-warmup：永远不预热（与"推迟预热"是两个不打架的语义）。"""
    from memory_sidecar.server import MemorySidecarServer

    store = _FakeStore()
    srv = MemorySidecarServer(store, port=0, warmup=False)
    srv.start()
    try:
        srv._on_search()
        time.sleep(0.3)
        assert store.warmup_calls == 0
    finally:
        srv.stop()


def test_sidecar_search_still_answers_without_models():
    """不预热时检索仍然有结果（BM25 那条腿不依赖模型）。"""
    from memory_sidecar.server import MemorySidecarServer

    store = _FakeStore()
    srv = MemorySidecarServer(store, port=0, warmup=True)
    srv.start()
    try:
        import urllib.request

        req = urllib.request.Request(
            f"{srv.url()}/search",
            data=json.dumps({"query": "你好", "top_k": 3}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        assert body.get("ok") is True
        assert "results" in body
    finally:
        srv.stop()
