# -*- coding: utf-8 -*-
"""真实模型的端到端测试（**默认跳过**）。

为什么要单独一个文件：它要加载 bge-small-zh-v1.5（91MB）+ bge-reranker-base（1.08GB），
冷启动十几秒，而且前提是本机 HF 缓存里已经有两个模型（见 requirements.txt 里的说明）。
日常 pytest 跑 fake 模型那两套就够了；这一套是「真模型确实能召回」的证据。

    DESKTOP_PET_MEMORY_SIDECAR_REAL=1 python -m pytest tests/test_memory_sidecar_real_models.py -q -s
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)

from memory_sidecar.server import MemorySidecarServer  # noqa: E402
from memory_sidecar.store import MemoryStore  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("DESKTOP_PET_MEMORY_SIDECAR_REAL") != "1",
    reason="真实模型测试：要加载 1.1GB 模型，只在 DESKTOP_PET_MEMORY_SIDECAR_REAL=1 时跑",
)

CARDS = [
    {"id": "m1", "kind": "fact", "text": "主人不吃辣", "importance": 0.9},
    {"id": "m2", "kind": "event", "text": "今天下午去爬山了，山顶风很大", "importance": 0.5},
    {"id": "m3", "kind": "topic", "text": "喜欢喝冰美式，不加糖", "importance": 0.6},
    {"id": "m4", "kind": "impression", "text": "最近工作压力有点大，容易失眠", "importance": 0.7},
]


def _dump(tag, res):
    print(
        "[real-model] %s path=%s error=%s"
        % (tag, res["path"], res["error"])
    )
    for r in res["results"]:
        print(
            "    %-3s score=%-8s rel=%-8s logit=%-8s text=%s"
            % (r["id"], r["score"], r["rel"], r["rerankLogit"], r["text"])
        )


def test_semantic_query_finds_card_without_literal_overlap(tmp_path):
    """A2：语义查询「我不能吃什么」必须命中「主人不吃辣，吃辣会胃疼」（无字面重叠）。"""
    store = MemoryStore(tmp_path, retry_cooldown=0)
    store.init()
    assert store.upsert(CARDS) == 4
    # 线上 serve 启动就在后台预热模型；这里也先预热，测的是「模型热着的时候
    # 写完立刻搜」——冷启动第一次检索不再等模型（预算内没跑完就先给词面结果，
    # 见 test_search_does_not_wait_for_a_slow_embedder）。
    store.warmup()

    res = store.search("我不能吃什么", top_k=3)
    _dump("A2 我不能吃什么", res)
    assert res["error"] is None
    assert res["results"][0]["id"] == "m1"
    assert res["results"][0]["rerankLogit"] is not None, "真精排必须给出 logit"
    assert len(res["results"]) >= 2

    # 换一种说法（依然和卡片零字面重叠）也必须找得到
    res2 = store.search("主人有什么忌口", top_k=3)
    _dump("A2 主人有什么忌口", res2)
    assert res2["results"][0]["id"] == "m1"

    # 反方向：问咖啡必须命中冰美式，别被 reranker 一刀切
    res3 = store.search("喝什么咖啡", top_k=3)
    _dump("A2 喝什么咖啡", res3)
    assert res3["results"][0]["id"] == "m3"

    health = store.health()
    assert health["degraded"] is False, health["degradation"]
    assert health["models"] == {"embed": "ready", "rerank": "ready"}
    store.close()


def test_bm25_only_path_still_works_with_real_models(tmp_path):
    """关键词路单独跑（--no-vector）：精确词照样能召回。"""
    store = MemoryStore(tmp_path, use_vector=False, retry_cooldown=0)
    store.init()
    store.upsert(CARDS)
    res = store.search("爬山", top_k=3)
    _dump("BM25-only 爬山", res)
    assert res["path"] == "bm25"
    assert res["results"][0]["id"] == "m2"
    store.close()


def test_health_answers_before_real_models_are_ready(tmp_path):
    """A10：/health 必须在模型加载之前就能回答（并且够快）。"""
    store = MemoryStore(tmp_path, retry_cooldown=0)
    store.init()
    store.upsert(CARDS)  # 注意：这里会加载向量模型（写卡要算向量）
    store.close()

    cold = MemoryStore(tmp_path, retry_cooldown=0)  # 新进程语义：模型都还没加载
    cold.load()
    srv = MemorySidecarServer(cold, port=0, warmup=False)
    port = srv.start()
    try:
        t0 = time.time()
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port, timeout=15) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        elapsed = time.time() - t0
        print("[real-model] /health 首次响应 %.0fms  %s" % (elapsed * 1000, body["models"]))
        assert elapsed < 5.0, "模型没加载时 /health 必须立刻回答（实测 %.2fs）" % elapsed
        assert body["models"] == {"embed": "unloaded", "rerank": "unloaded"}
        assert body["cards"] == 4
    finally:
        srv.stop()
        cold.close()
