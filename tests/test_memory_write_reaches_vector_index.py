# -*- coding: utf-8 -*-
"""写卡 → 向量索引：**不靠检索**也能进去（记忆写入侧）。

背景
----
模型预热被推后到「第一次真的需要语义检索」，是为了省掉启动那十几秒
（import torch + 加载两个 transformers 模型）。但推后有个副作用：
写进来的卡只进了 cards.json（真值），**向量索引一直空着** ——
没人检索的话，主人看到的就是「这条没进向量库」。

这里锁住三个不变量：
  1. 启动时**不加载任何模型**（推后预热的红利还在）；
  2. **写卡**会触发推后的预热 + 后台补索引 → 卡自己进向量索引；
  3. 写路径**不等模型**（还是 ~2ms），检索时走的是**向量腿**（响应带 path）。

不需要真模型：假 embedder / 假 rerank（跟 tests/test_memory_sidecar_server.py
同一套口径 —— 确定性、零依赖）。
"""
from __future__ import annotations

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

# 同 test_memory_sidecar_server.py：这些用例要真的建 BM25 / Chroma 索引
pytest.importorskip("bm25s", reason="bm25s 未安装：BM25 腿会降级，这套用例跳过")
pytest.importorskip("chromadb", reason="chromadb 未安装：向量腿会降级，这套用例跳过")

from memory_sidecar import store as store_mod  # noqa: E402
from memory_sidecar.server import MemorySidecarServer  # noqa: E402
from memory_sidecar.store import MemoryStore  # noqa: E402


# ── 假模型（确定性、零依赖） ──────────────────────────────────────────────
class CountingEmbedder:
    """假向量模型：记录被调用了几次（证明「启动时一个模型都没加载」）。"""

    def __init__(self, dim=8, delay=0.0):
        self.dim = dim
        self.delay = float(delay)
        self.calls = 0
        self.embedded = []

    def embed(self, texts):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for ch in str(text):
                vec[ord(ch) % self.dim] += 1.0
            norm = sum(x * x for x in vec) ** 0.5 or 1.0
            out.append([x / norm for x in vec])
            self.embedded.append(str(text))
        return out


class FakeRerank:
    def score(self, query, texts):
        return [1.0 for _ in texts]


def make_store(data_dir, embedder=None, **kwargs):
    kwargs.setdefault("retry_cooldown", 0)
    kwargs.setdefault("model_budget", 30.0)
    kwargs.setdefault("min_similarity", 0.0)
    return MemoryStore(
        data_dir,
        embedder=embedder if embedder is not None else CountingEmbedder(),
        reranker=FakeRerank(),
        **kwargs,
    )


def http_get(port, path):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
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
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def wait_until_up(port, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return http_get(port, "/health")
        except Exception:
            time.sleep(0.05)
    raise AssertionError("sidecar 没起来")


def vector_ids(store):
    """向量索引里现在有哪些 id（直接问 Chroma）。"""
    try:
        col = store._ensure_chroma()
        return set(col.get(include=["documents"])["ids"])
    except Exception:
        return set()


def vector_count(port):
    _s, body = http_get(port, "/stats")
    stats = body.get("stats") or {}
    return int((stats.get("vector") or {}).get("count") or 0)


def wait_for_vector_count(port, want, timeout=30.0):
    """等后台把卡补进向量索引（返回最后一次看到的条数）。"""
    deadline = time.time() + timeout
    n = 0
    while time.time() < deadline:
        n = vector_count(port)
        if n >= want:
            return n
        time.sleep(0.05)
    return n


def _card(cid="c1", text="主人最喜欢喝柠檬茶"):
    return {"id": cid, "kind": "fact", "text": text, "importance": 0.9}


# ── 1) 启动不加载模型（推后预热的红利还在） ────────────────────────────────
def test_startup_loads_no_model_at_all(tmp_path):
    """启动后 /health 说两个模型都是 unloaded，而且一个模型都没被调用过。"""
    embedder = CountingEmbedder()
    store = make_store(tmp_path, embedder=embedder)
    store.init()
    srv = MemorySidecarServer(store, port=0, warmup=True)
    port = srv.start()
    try:
        wait_until_up(port)
        _s, health = http_get(port, "/health")
        assert health["models"]["embed"] == "unloaded", health
        assert health["models"]["rerank"] == "unloaded", health
        assert embedder.calls == 0, "启动时不该加载模型（实测调用了 %d 次）" % embedder.calls
    finally:
        srv.stop()
        store.close()


# ── 2) 写卡自己把卡送进向量索引（没有 /search 参与） ───────────────────────
def test_a_write_alone_puts_the_card_into_the_vector_index(tmp_path):
    """写一张卡 → 向量索引条数增长（**全程没有检索**）。"""
    embedder = CountingEmbedder()
    store = make_store(tmp_path, embedder=embedder)
    store.init()
    srv = MemorySidecarServer(store, port=0, warmup=True)
    port = srv.start()
    try:
        wait_until_up(port)
        assert vector_count(port) == 0

        status, body = http_post(port, "/upsert", {"cards": [_card()]})
        assert status == 200 and body["upserted"] == 1, body

        # 写卡触发了推后的预热 + 后台补索引 → 卡自己进向量索引
        n = wait_for_vector_count(port, 1)
        assert n >= 1, "写卡之后卡片必须自己进向量索引（向量索引条数=%d）" % n
        assert embedder.calls >= 1, "写卡应该把模型叫醒（预热被触发了）"

        # 真值里也在
        _s, health = http_get(port, "/health")
        assert health["cards"] == 1, health
        assert health["pendingIndex"] == 0, "索引补齐后不该还有欠账的卡"
    finally:
        srv.stop()
        store.close()


def test_search_answers_through_the_vector_leg_after_a_write(tmp_path):
    """写完之后检索走**向量腿**（响应里的 path 说了算），不是只有 BM25。"""
    store = make_store(tmp_path)
    store.init()
    srv = MemorySidecarServer(store, port=0, warmup=True)
    port = srv.start()
    try:
        wait_until_up(port)
        http_post(port, "/upsert", {"cards": [_card()]})
        assert wait_for_vector_count(port, 1) >= 1

        status, body = http_post(port, "/search", {"query": "柠檬茶", "top_k": 5})
        assert status == 200 and body["ok"] is True, body
        # path 说这一轮是谁给的候选：vector = 只有向量腿，hybrid = 向量腿 + BM25
        assert (body.get("path") or "") in ("vector", "hybrid"), (
            "写完就该走向量腿（path=%r）" % (body.get("path"),))
        assert [r["id"] for r in body["results"]] == ["c1"], body
        assert body["results"][0]["text"] == "主人最喜欢喝柠檬茶", body
        # sim 只有**向量腿**答的时候才有值 —— 这是「不是只有 BM25」的硬证据
        assert body["results"][0].get("sim") is not None, (
            "结果里没有向量相似度 → 这一轮其实是词面腿答的：%r" % body)
    finally:
        srv.stop()
        store.close()


def test_search_still_triggers_the_warmup(tmp_path):
    """老行为不能丢：第一次检索也会把预热叫醒。"""
    embedder = CountingEmbedder()
    store = make_store(tmp_path, embedder=embedder)
    store.init()
    srv = MemorySidecarServer(store, port=0, warmup=True)
    port = srv.start()
    try:
        wait_until_up(port)
        http_post(port, "/search", {"query": "柠檬茶", "top_k": 3})
        deadline = time.time() + 30
        while time.time() < deadline and embedder.calls == 0:
            time.sleep(0.05)
        assert embedder.calls >= 1, "检索也该触发预热"
    finally:
        srv.stop()
        store.close()


# ── 3) 写路径不等模型（还是 ~2ms） ────────────────────────────────────────
def test_write_stays_fast_while_the_models_load(tmp_path):
    """模型正在加载时 POST /upsert 立刻返回（写卡不许排在模型后面）。"""
    class SlowEmbedder(CountingEmbedder):
        def __init__(self, delay=2.0):
            super().__init__()
            self.delay = float(delay)
            self.started = threading.Event()

        def embed(self, texts):
            if self.delay:
                with store_mod._HF_LOCK:
                    self.started.set()
                    time.sleep(self.delay)
                    self.delay = 0.0
            return super().embed(texts)

    slow = SlowEmbedder(delay=2.0)
    store = make_store(tmp_path, embedder=slow)
    store.init()
    srv = MemorySidecarServer(store, port=0, warmup=True)
    port = srv.start()
    try:
        wait_until_up(port)
        t0 = time.time()
        status, body = http_post(port, "/upsert", {"cards": [_card()]})
        upsert_ms = (time.time() - t0) * 1000.0
        assert status == 200 and body["upserted"] == 1, body
        print("[写卡触发预热] POST /upsert 耗时 %.1fms" % upsert_ms)
        assert upsert_ms < 500, "写卡必须不等模型（实测 %.0fms）" % upsert_ms
        assert slow.started.wait(10), "写卡应该把预热叫醒"
    finally:
        srv.stop()
        store.close()


# ── 4) 改写 / 删除同样要触发索引刷新 ───────────────────────────────────────
def test_supersede_and_delete_also_refresh_the_index(tmp_path):
    """改写（supersede）与删除同样走 on_write → 索引跟着变。"""
    store = make_store(tmp_path)
    store.init()
    srv = MemorySidecarServer(store, port=0, warmup=True)
    port = srv.start()
    try:
        wait_until_up(port)
        http_post(port, "/upsert", {"cards": [_card("c1", "主人最喜欢喝柠檬茶")]})
        assert wait_for_vector_count(port, 1) >= 1

        # 改写：新卡进来
        status, body = http_post(
            port, "/supersede",
            {"oldId": "c1", "card": {"id": "c2", "kind": "fact",
                                     "text": "主人最喜欢喝桂花乌龙", "importance": 0.9}},
        )
        assert status == 200 and body["upserted"] == 1, body
        # 改写 = 旧卡下线 + 新卡上线（条数不变，id 换了）
        deadline = time.time() + 30
        ids = set()
        while time.time() < deadline:
            ids = vector_ids(store)
            if "c2" in ids and "c1" not in ids:
                break
            time.sleep(0.05)
        assert "c2" in ids and "c1" not in ids, (
            "改写后向量索引应该是新卡 c2、下线旧卡 c1（实际 %r）" % (sorted(ids),))

        # 删除：向量索引里那条要下线
        status, body = http_post(port, "/delete", {"ids": ["c2"]})
        assert status == 200 and body["deleted"] == 1, body
        deadline = time.time() + 30
        n = vector_count(port)
        while time.time() < deadline and n >= 1:
            time.sleep(0.05)
            n = vector_count(port)
        assert n == 0, "删掉的卡不该还在向量索引里（count=%d）" % n
    finally:
        srv.stop()
        store.close()
