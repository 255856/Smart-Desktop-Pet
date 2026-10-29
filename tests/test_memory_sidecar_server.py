# -*- coding: utf-8 -*-
"""memory_sidecar.server 的测试：HTTP 端点、/health、优雅关闭、以及「/search 绝不 500」。"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)

# 同 test_memory_sidecar.py：这些用例要真的建 BM25 索引
pytest.importorskip("bm25s", reason="bm25s 未安装：BM25 腿会降级，这套用例跳过")

from memory_sidecar import store as store_mod  # noqa: E402
from memory_sidecar.server import MemorySidecarServer  # noqa: E402
from memory_sidecar.store import MemoryStore  # noqa: E402


# ── 假模型（和 tests/test_memory_sidecar.py 同一套口径：确定性、零依赖） ────
class FakeEmbedder:
    def __init__(self, dim=8, fail=False):
        self.dim = dim
        self.fail = fail

    def embed(self, texts):
        if self.fail:
            raise RuntimeError("向量模型加载失败（模拟）")
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for ch in str(text):
                vec[ord(ch) % self.dim] += 1.0
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            out.append([x / norm for x in vec])
        return out


class FakeRerank:
    def __init__(self, scores=None, fail=False, default=1.0):
        self.scores = scores or {}
        self.fail = fail
        self.default = default

    def score(self, query, texts):
        if self.fail:
            raise RuntimeError("精排模型加载失败（模拟）")
        return [float(self.scores.get(t, self.default)) for t in texts]


CARDS = [
    {"id": "c1", "kind": "fact", "text": "主人不吃辣", "importance": 0.9, "mood": "happy"},
    {"id": "c2", "kind": "event", "text": "今天去爬山了", "importance": 0.4},
    {"id": "c3", "kind": "topic", "text": "喜欢喝冰美式", "importance": 0.6},
]


def make_store(data_dir, embedder=None, reranker=None, **kwargs):
    kwargs.setdefault("retry_cooldown", 0)
    return MemoryStore(
        data_dir,
        embedder=embedder if embedder is not None else FakeEmbedder(),
        reranker=reranker if reranker is not None else FakeRerank(),
        **kwargs,
    )


# ── HTTP 小工具 ───────────────────────────────────────────────────────────
def http_get(port, path):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # 4xx/5xx 也要能被解析
        return e.code, json.loads(e.read().decode("utf-8"))


def http_post(port, path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path), data=data,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:  # 4xx/5xx 也要能被解析
        return e.code, json.loads(e.read().decode("utf-8"))


@pytest.fixture()
def sidecar(tmp_path):
    store = make_store(
        tmp_path, reranker=FakeRerank(scores={"主人不吃辣": 6.0}, default=0.0)
    )
    store.init()
    store.upsert(CARDS)
    srv = MemorySidecarServer(store, port=0, warmup=False)
    port = srv.start()
    try:
        yield srv, store, port
    finally:
        srv.stop()
        store.close()


def wait_until_up(port, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return http_get(port, "/health")
        except Exception:
            time.sleep(0.05)
    raise AssertionError("sidecar 没起来")


# ── A6 ────────────────────────────────────────────────────────────────────
def test_health_reports_cards_models_and_degraded(sidecar):
    srv, store, port = sidecar
    status, body = http_get(port, "/health")
    assert status == 200
    assert body["ok"] is True
    assert body["cards"] == 3
    assert set(body["models"]) == {"embed", "rerank"}
    assert body["degraded"] is False
    assert body["port"] == port
    assert body["dataDir"] == store.data_dir


def test_health_is_fast_and_does_not_load_models(tmp_path):
    class TripwireEmbedder:
        def embed(self, texts):
            raise AssertionError("/health 不该加载模型")

    class TripwireRerank:
        def score(self, query, texts):
            raise AssertionError("/health 不该加载模型")

    store = make_store(tmp_path, embedder=TripwireEmbedder(), reranker=TripwireRerank())
    store.init()
    srv = MemorySidecarServer(store, port=0, warmup=False)
    port = srv.start()
    try:
        t0 = time.time()
        status, body = http_get(port, "/health")
        elapsed = time.time() - t0
        assert status == 200
        assert elapsed < 1.0, "模型还没加载时 /health 也必须立刻回答（实测 %.3fs）" % elapsed
        assert body["models"] == {"embed": "unloaded", "rerank": "unloaded"}
    finally:
        srv.stop()
        store.close()


def test_stats_endpoint(sidecar):
    _srv, _store, port = sidecar
    status, body = http_get(port, "/stats")
    assert status == 200
    stats = body["stats"]
    assert stats["active"] == 3
    assert stats["byKind"] == {"fact": 1, "event": 1, "topic": 1}
    assert stats["vector"]["enabled"] is True and stats["bm25"]["enabled"] is True
    assert stats["cardsFile"].endswith("cards.json")


def test_root_lists_endpoints(sidecar):
    _srv, _store, port = sidecar
    status, body = http_get(port, "/")
    assert status == 200 and any("/search" in k for k in body["endpoints"])


# ── 端点行为 ──────────────────────────────────────────────────────────────
def test_search_endpoint_returns_ranked_results(sidecar):
    _srv, _store, port = sidecar
    status, body = http_post(port, "/search", {"query": "我不能吃什么", "top_k": 3})
    assert status == 200
    assert body["ok"] is True
    assert body["path"] == "hybrid"
    assert body["results"][0]["id"] == "c1"
    assert body["results"][0]["score"] > 0
    assert {"id", "kind", "text", "mood", "importance", "score"} <= set(body["results"][0])
    # 兼容 topK 这种驼峰写法
    _status, camel = http_post(port, "/search", {"query": "爬山", "topK": 1})
    assert len(camel["results"]) == 1


def test_upsert_and_delete_endpoints(sidecar):
    _srv, store, port = sidecar
    status, body = http_post(port, "/upsert", {"cards": [{"id": "c9", "text": "新卡"}]})
    assert status == 200 and body["upserted"] == 1
    assert http_get(port, "/health")[1]["cards"] == 4

    status, body = http_post(port, "/delete", {"ids": ["c9"]})
    assert status == 200 and body["deleted"] == 1
    assert http_get(port, "/health")[1]["cards"] == 3
    # 逗号字符串也接受
    assert http_post(port, "/delete", {"ids": "c2"})[1]["deleted"] == 1
    assert store.stats()["active"] == 2


def test_supersede_and_rebuild_endpoints(sidecar):
    _srv, store, port = sidecar
    status, body = http_post(
        port, "/supersede", {"oldId": "c2", "card": {"id": "c2b", "kind": "event", "text": "上周去爬山"}}
    )
    assert status == 200 and body["upserted"] == 1
    assert store.stats()["superseded"] == 1

    status, body = http_post(port, "/rebuild", {})
    assert status == 200
    assert body["rebuilt"]["vector"] == 3 and body["rebuilt"]["bm25"] == 3


# ── A7：/search 永不 500 ─────────────────────────────────────────────────
def test_search_never_500_when_everything_is_down(tmp_path, monkeypatch):
    store = make_store(tmp_path, embedder=FakeEmbedder(fail=True))
    store.init()
    store.upsert(CARDS)

    def boom():
        raise ImportError("bm25s 没装（模拟）")

    monkeypatch.setattr(store_mod, "_import_bm25s", boom)
    store.load()

    srv = MemorySidecarServer(store, port=0, warmup=False)
    port = srv.start()
    try:
        status, body = http_post(port, "/search", {"query": "主人不吃辣", "top_k": 3})
        assert status == 200, "两条腿全挂也必须返回 200 + 可解析 JSON"
        assert body["ok"] is False
        assert body["results"] == []
        assert body["degraded"] is True
        assert body["error"] and "不可用" in body["error"]
    finally:
        srv.stop()
        store.close()


def test_search_with_bad_payload_is_parseable(sidecar):
    _srv, _store, port = sidecar
    status, body = http_post(port, "/search", {"query": "爬山", "top_k": "不是数字"})
    assert status == 200
    assert body["ok"] is False and body["results"] == [] and body["error"]
    status, body = http_post(port, "/search", {"query": ""})
    assert status == 200 and body["results"] == []


def test_unknown_endpoint_and_bad_json(sidecar):
    _srv, _store, port = sidecar
    status, body = http_get(port, "/nope")
    assert status == 404 and body["ok"] is False
    status, body = http_post(port, "/nope", {})
    assert status == 404 and body["ok"] is False

    data = "{ 这不是 JSON".encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d/upsert" % port, data=data,
        headers={"content-type": "application/json"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req, timeout=15)
    assert excinfo.value.code == 400
    assert json.loads(excinfo.value.read().decode("utf-8"))["ok"] is False


# ── 优雅关闭 / 启动日志 ───────────────────────────────────────────────────
def test_graceful_shutdown_and_idempotent_stop(tmp_path):
    store = make_store(tmp_path)
    store.init()
    srv = MemorySidecarServer(store, port=0, warmup=False)
    port = srv.start()
    assert http_get(port, "/health")[0] == 200
    srv.stop()
    srv.stop()  # 再关一次不能炸
    with pytest.raises(Exception):
        http_get(port, "/health")
    store.close()


def test_serve_forever_prints_port_and_data_dir(tmp_path, capsys):
    store = make_store(tmp_path)
    store.init()
    srv = MemorySidecarServer(store, port=0, warmup=False)
    srv.bind()
    port = srv.port
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        wait_until_up(port)
    finally:
        srv.stop()
        thread.join(timeout=5)
        store.close()
    out = capsys.readouterr().out
    assert str(port) in out, "启动行必须打印端口：%r" % out
    assert store.data_dir in out, "启动行必须打印数据目录：%r" % out


# ── 写卡不等模型（A1 / HTTP 层） ──────────────────────────────────────────
class SlowEmbedder(FakeEmbedder):
    """第一次 embed 时握着进程级 _HF_LOCK 慢慢「加载」（模拟冷启动）。"""

    def __init__(self, delay=2.0):
        super().__init__()
        self.delay = float(delay)
        self.started = threading.Event()

    def embed(self, texts):
        if self.delay:
            with store_mod._HF_LOCK:
                self.started.set()
                time.sleep(self.delay)
                self.delay = 0.0  # 只慢第一次（= 加载那一次）
        return super().embed(texts)


def test_upsert_over_http_is_fast_while_models_load(tmp_path):
    """A1：模型正在加载时 POST /upsert 立刻返回，紧接着 /search 找得到这张卡。"""
    slow = SlowEmbedder(delay=2.0)
    store = make_store(tmp_path, embedder=slow)
    store.init()
    srv = MemorySidecarServer(store, port=0, warmup=False)
    port = srv.start()
    warm = threading.Thread(target=store.warmup, daemon=True)
    warm.start()
    try:
        assert slow.started.wait(10), "预热线程没进到 embed"
        t0 = time.time()
        status, body = http_post(
            port, "/upsert", {"cards": [{"id": "c9", "kind": "fact", "text": "主人不吃辣"}]}
        )
        upsert_ms = (time.time() - t0) * 1000.0
        assert status == 200 and body["upserted"] == 1
        print("[A1] 冷启动期间 POST /upsert 耗时 %.1fms" % upsert_ms)
        assert upsert_ms < 500, "写卡必须不等模型（实测 %.0fms）" % upsert_ms

        _s, health = http_get(port, "/health")
        assert health["cards"] == 1, health
        assert health["pendingIndex"] == 1, "向量欠着（还没算），但卡已经在了"

        status, body = http_post(port, "/search", {"query": "不吃辣", "top_k": 3})
        assert status == 200 and body["ok"] is True
        assert [r["id"] for r in body["results"]] == ["c9"], body
    finally:
        warm.join(timeout=30)
        srv.stop()
        store.close()


def test_health_stays_fast_while_the_models_load(tmp_path):
    """A2：模型正在加载时 /health 也必须毫秒级（/list 同理）。"""
    slow = SlowEmbedder(delay=2.0)
    store = make_store(tmp_path, embedder=slow)
    store.init()
    store.upsert(CARDS)
    srv = MemorySidecarServer(store, port=0, warmup=False)
    port = srv.start()
    warm = threading.Thread(target=store.warmup, daemon=True)
    warm.start()
    try:
        assert slow.started.wait(10), "预热线程没进到 embed"
        samples, list_samples = [], []
        for _ in range(5):
            t0 = time.time()
            status, body = http_get(port, "/health")
            samples.append((time.time() - t0) * 1000.0)
            t0 = time.time()
            _s2, _b2 = http_get(port, "/list")
            list_samples.append((time.time() - t0) * 1000.0)
        samples = [round(x, 2) for x in samples]
        list_samples = [round(x, 2) for x in list_samples]
        print("[A2] 模型加载中 GET /health 耗时(ms) = %r" % samples)
        print("[A2] 模型加载中 GET /list   耗时(ms) = %r" % list_samples)
        assert status == 200 and body["ok"] is True and body["cards"] == 3
        assert min(samples) < 50, "health 不该排在模型加载后面（实测 %r ms）" % samples
        assert max(samples) < 500, "health 不能被模型加载拖住（实测 %r ms）" % samples
        assert min(list_samples) < 50, "/list 也不该等模型（实测 %r ms）" % list_samples
    finally:
        warm.join(timeout=30)
        srv.stop()
        store.close()


# ── GET /list（A3） ──────────────────────────────────────────────────────
def _list_fixture_cards():
    now = int(time.time() * 1000)
    return [
        {"id": "low_new", "kind": "fact", "text": "低重要度但很新", "importance": 0.2,
         "updatedAt": now},
        {"id": "high_old", "kind": "fact", "text": "高重要度但很旧", "importance": 0.9,
         "updatedAt": now - 86400000},
        {"id": "high_new", "kind": "event", "text": "高重要度且很新", "importance": 0.9,
         "updatedAt": now},
        {"id": "mid", "kind": "topic", "text": "中等重要度", "importance": 0.5,
         "updatedAt": now},
    ]


def test_list_endpoint_ordering_kind_filter_and_limit(tmp_path):
    store = make_store(tmp_path)
    store.init()
    store.upsert(_list_fixture_cards())
    srv = MemorySidecarServer(store, port=0, warmup=False)
    port = srv.start()
    try:
        status, body = http_get(port, "/list")
        assert status == 200 and body["ok"] is True
        # 重要度 desc，同重要度按更新时间 desc
        assert [c["id"] for c in body["cards"]] == ["high_new", "high_old", "mid", "low_new"]
        assert body["count"] == 4 and body["total"] == 4
        assert {"id", "kind", "text", "importance", "updatedAt"} <= set(body["cards"][0])

        status, body = http_get(port, "/list?kind=event")
        assert status == 200
        assert [c["id"] for c in body["cards"]] == ["high_new"]
        assert body["count"] == 1 and body["total"] == 1

        status, body = http_get(port, "/list?kind=thought")
        assert status == 200 and body["cards"] == [] and body["total"] == 0

        status, body = http_get(port, "/list?limit=2")
        assert status == 200
        assert [c["id"] for c in body["cards"]] == ["high_new", "high_old"]
        assert body["count"] == 2 and body["total"] == 4, "count=本次返回，total=符合条件的总数"

        # limit 不是数字 / 是负数 → 当没传，绝不许 500
        for bad in ("abc", "0", "-3"):
            status, body = http_get(port, "/list?limit=" + bad)
            assert status == 200 and len(body["cards"]) == 4, bad
    finally:
        srv.stop()
        store.close()


def test_list_endpoint_can_include_superseded_cards(tmp_path):
    """?include_superseded=1 连历史卡一起列（面板要看「全部记忆」时的口径）。"""
    store = make_store(tmp_path)
    store.init()
    store.upsert([{"id": "c1", "kind": "fact", "text": "主人不吃辣", "importance": 0.9}])
    store.supersede("c1", {"id": "c1b", "kind": "fact", "text": "主人不吃辣，吃辣胃疼"})
    srv = MemorySidecarServer(store, port=0, warmup=False)
    port = srv.start()
    try:
        _s, body = http_get(port, "/list")
        assert [c["id"] for c in body["cards"]] == ["c1b"], "默认只列 active"
        assert body["total"] == 1

        _s, body = http_get(port, "/list?include_superseded=1")
        # c1 重要度 0.9 > c1b 的 0.5，所以历史的 c1 反而排在前面（排序口径不变）
        assert [c["id"] for c in body["cards"]] == ["c1", "c1b"], "旧卡也在（历史）"
        assert body["total"] == 2 and body["includeSuperseded"] is True
        assert {c["id"]: c["status"] for c in body["cards"]} == {
            "c1": "superseded", "c1b": "active"
        }

        _s, body = http_get(port, "/list?status=all")
        assert body["total"] == 2, "status=all 也是「连历史一起」"
        _s, body = http_get(port, "/list?status=active")
        assert body["total"] == 1
    finally:
        srv.stop()
        store.close()


def test_list_endpoint_on_empty_store_and_no_model_load(tmp_path):
    class TripwireEmbedder:
        def embed(self, texts):
            raise AssertionError("/list 不该加载模型")

    class TripwireRerank:
        def score(self, query, texts):
            raise AssertionError("/list 不该加载模型")

    store = make_store(tmp_path, embedder=TripwireEmbedder(), reranker=TripwireRerank())
    store.init()
    srv = MemorySidecarServer(store, port=0, warmup=False)
    port = srv.start()
    try:
        status, body = http_get(port, "/list")
        assert status == 200
        assert body == {"ok": True, "kind": None, "includeSuperseded": False,
                        "cards": [], "count": 0, "total": 0}
        # 端点清单里也要有 /list
        _s, root = http_get(port, "/")
        assert any("/list" in k for k in root["endpoints"])
    finally:
        srv.stop()
        store.close()
