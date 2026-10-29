# -*- coding: utf-8 -*-
"""记忆「睡觉」的测试：计划（归组 / 模糊 / 遗忘）、执行、HTTP 端点、客户端、定时。

向量用查表假模型（确定性、零依赖），相似度是设计出来的，不靠真语义。
"""
import datetime
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)

pytest.importorskip("bm25s", reason="bm25s 未安装：BM25 腿会降级，这套用例跳过")

from memory_sidecar.server import MemorySidecarServer  # noqa: E402
from memory_sidecar.store import GRAPH_MIN_SIMILARITY, MemoryStore  # noqa: E402

DAY_MS = 86400000


# ── 假模型：按文本查表的向量 ──────────────────────────────────────────────
def _onehot(text, dim=8):
    idx = int(hashlib.sha1(str(text).encode("utf-8")).hexdigest(), 16) % dim
    vec = [0.0] * dim
    vec[idx] = 1.0
    return vec


class TableEmbedder:
    """表里有的文本用表里的向量；没有的给一个按内容定下来的正交向量。"""

    def __init__(self, table, dim=8):
        self.table = {k: list(v) for k, v in (table or {}).items()}
        self.dim = dim

    def embed(self, texts):
        return [list(self.table.get(t) or _onehot(t, self.dim)) for t in texts]


class FakeRerank:
    def score(self, query, texts):
        return [0.0] * len(texts)


def make_store(data_dir, embedder=None):
    return MemoryStore(
        data_dir,
        embedder=embedder if embedder is not None else TableEmbedder({}),
        reranker=FakeRerank(),
        retry_cooldown=0,
        model_budget=30.0,
        min_similarity=0.0,
    )


def _vecs():
    """五张卡：两张意思一样（同一组），剩下三张各不相干。"""
    return {
        "青柠汽水": [1.0, 0, 0, 0, 0, 0, 0, 0],
        "柠檬汽水": [1.0, 0, 0, 0, 0, 0, 0, 0],
        "爬山": [0, 1.0, 0, 0, 0, 0, 0, 0],
        "旧沙发": [0, 0, 1.0, 0, 0, 0, 0, 0],
        "重要的事": [0, 0, 0, 1.0, 0, 0, 0, 0],
    }


def _cards(now):
    """一组卡：两张近的、一条 unimportant 且旧的（模糊）、一条更旧的（遗忘）、一条重要的。"""
    return [
        {"id": "g1", "kind": "event", "text": "青柠汽水", "importance": 0.6,
         "createdAt": now, "updatedAt": now},
        {"id": "g2", "kind": "event", "text": "柠檬汽水", "importance": 0.6,
         "createdAt": now, "updatedAt": now},
        {"id": "blur1", "kind": "fact", "text": "爬山", "importance": 0.2,
         "createdAt": now - 40 * DAY_MS, "updatedAt": now - 40 * DAY_MS},
        {"id": "forget1", "kind": "fact", "text": "旧沙发", "importance": 0.2,
         "createdAt": now - 200 * DAY_MS, "updatedAt": now - 200 * DAY_MS},
        {"id": "keep1", "kind": "fact", "text": "重要的事", "importance": 0.9,
         "createdAt": now - 300 * DAY_MS, "updatedAt": now - 300 * DAY_MS},
    ]


def _seeded_store(tmp_path, cards=None):
    now = int(time.time() * 1000)
    store = make_store(tmp_path, TableEmbedder(_vecs()))
    store.init()
    store.upsert(cards if cards is not None else _cards(now))
    store.sync_index()          # 把向量写进 Chroma（睡觉靠向量归组）
    return store


# ── store：计划 ───────────────────────────────────────────────────────────
def test_sleep_plan_groups_similar_and_flags_blur_and_forget(tmp_path):
    store = _seeded_store(tmp_path)
    plan = store.sleep_plan()

    assert plan["ok"] is True
    assert plan["counts"]["cards"] == 5
    # 意思一样的两张归一组
    assert plan["counts"]["clusters"] == 1
    cluster = plan["clusters"][0]
    assert set(cluster["ids"]) == {"g1", "g2"}
    assert cluster["texts"] == ["青柠汽水", "柠檬汽水"]
    # 不重要又旧的：模糊；更旧的：遗忘
    assert [r["id"] for r in plan["blur"]] == ["blur1"]
    assert [r["id"] for r in plan["forget"]] == ["forget1"]
    assert plan["blur"][0]["days"] >= 30
    assert plan["forget"][0]["days"] >= 180
    # 重要的、以及归组的，都不进模糊 / 遗忘名单
    assert "keep1" not in [r["id"] for r in plan["blur"] + plan["forget"]]
    assert "g1" not in [r["id"] for r in plan["blur"] + plan["forget"]]
    store.close()


def test_sleep_plan_without_vectors_has_no_clusters(tmp_path):
    """没有向量就归不了组（但不许报错）。"""
    now = int(time.time() * 1000)
    store = make_store(tmp_path)
    store.init()
    store.upsert([{"id": "a", "kind": "fact", "text": "一", "importance": 0.9,
                   "updatedAt": now},
                  {"id": "b", "kind": "fact", "text": "二", "importance": 0.9,
                   "updatedAt": now}])
    plan = store.sleep_plan()
    assert plan["ok"] is True
    assert plan["clusters"] == []
    store.close()


def test_clustering_needs_both_absolute_and_relative_similarity(tmp_path):
    """跟最像的那条差太远的，算不了一回事（绝对下限 + 相对下限两道门槛）。"""
    now = int(time.time() * 1000)
    # f1/f2 很像（0.99），f3 跟 f1 有 0.7（过了绝对下限），但跟 f1 的最像那条差太远
    table = {
        "f1": [1.0, 0, 0, 0, 0, 0, 0, 0],
        "f2": [0.99, 0.141, 0, 0, 0, 0, 0, 0],
        "f3": [0.70, 0.714, 0, 0, 0, 0, 0, 0],
    }
    store = make_store(tmp_path, TableEmbedder(table))
    store.init()
    store.upsert([
        {"id": "f1", "kind": "fact", "text": "f1", "importance": 0.8, "updatedAt": now},
        {"id": "f2", "kind": "fact", "text": "f2", "importance": 0.8, "updatedAt": now},
        {"id": "f3", "kind": "fact", "text": "f3", "importance": 0.8, "updatedAt": now},
    ])
    store.sync_index()
    plan = store.sleep_plan()
    assert len(plan["clusters"]) == 1
    assert set(plan["clusters"][0]["ids"]) == {"f1", "f2"}
    store.close()


# ── store：执行 ───────────────────────────────────────────────────────────
def test_apply_sleep_merges_blurs_and_forgets(tmp_path):
    store = _seeded_store(tmp_path)
    plan = store.sleep_plan()
    cluster = plan["clusters"][0]

    result = store.apply_sleep(clusters=[{"ids": cluster["ids"],
                                          "summary": "青柠和柠檬汽水"}])

    assert result["ok"] is True
    assert result["merged"] == 1
    assert result["blurred"] == 1
    assert result["forgotten"] == 1

    cards = {c["id"]: c for c in store.list_cards(include_superseded=True)}
    # 合并出来一条新的摘要卡
    new_id = result["newCards"][0]["id"]
    assert cards[new_id]["text"] == "青柠和柠檬汽水"
    assert cards[new_id]["status"] == "active"
    assert cards[new_id]["importance"] == 0.6      # 取组里最高的
    # 原来的两张变成历史
    assert cards["g1"]["status"] == "superseded"
    assert cards["g2"]["status"] == "superseded"
    assert store._cards["g1"]["supersededBy"] == new_id
    # 模糊的那条：重要性打折 + 标成模糊
    assert cards["blur1"]["importance"] == pytest.approx(0.09, abs=0.01)
    assert cards["blur1"]["fuzzy"] is True
    assert cards["blur1"]["status"] == "active"
    # 遗忘的那条：没了
    assert "forget1" not in cards
    # 重要的那条：一点没动
    assert cards["keep1"]["importance"] == 0.9
    assert cards["keep1"].get("fuzzy") is not True
    store.close()


def test_apply_sleep_without_summary_keeps_cards(tmp_path):
    """没有摘要就不合并（宁可少做，也不要把内容丢了）。"""
    store = _seeded_store(tmp_path)
    before = len(store.list_cards())
    result = store.apply_sleep(clusters=[{"ids": ["g1", "g2"], "summary": "  "}],
                                blur=False, forget=False)
    assert result["merged"] == 0
    assert len(store.list_cards()) == before
    store.close()


def test_apply_sleep_can_skip_blur_and_forget(tmp_path):
    store = _seeded_store(tmp_path)
    result = store.apply_sleep(clusters=[], blur=False, forget=False)
    assert result["blurred"] == 0 and result["forgotten"] == 0
    cards = {c["id"]: c for c in store.list_cards()}
    assert "blur1" in cards and "forget1" in cards
    store.close()


def test_fuzzy_card_is_not_blurred_again(tmp_path):
    """已经模糊过的卡不用再模糊一遍（省得每次睡觉都把它算成一条）。"""
    now = int(time.time() * 1000)
    cards = _cards(now)
    for card in cards:
        if card["id"] == "blur1":
            card["fuzzy"] = True
    store = _seeded_store(tmp_path, cards)
    plan = store.sleep_plan()
    assert "blur1" not in [r["id"] for r in plan["blur"]]
    # 但它够旧的话，还是会被忘掉
    store.close()


# ── HTTP 端点 ────────────────────────────────────────────────────────────
def _http(port, path, payload=None):
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path), data=data,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def test_sleep_endpoints_over_http(tmp_path):
    store = _seeded_store(tmp_path)
    srv = MemorySidecarServer(store, port=0, warmup=False)
    port = srv.start()
    try:
        status, plan = _http(port, "/sleep/plan")
        assert status == 200 and plan["ok"] is True
        assert plan["counts"]["clusters"] == 1
        assert [r["id"] for r in plan["forget"]] == ["forget1"]

        cluster = plan["clusters"][0]
        status, result = _http(port, "/sleep/apply", {
            "clusters": [{"ids": cluster["ids"], "summary": "青柠和柠檬汽水"}],
            "blur": True, "forget": True,
        })
        assert status == 200 and result["ok"] is True
        assert result["merged"] == 1
        assert result["forgotten"] == 1
    finally:
        srv.stop()
        store.close()


def test_graph_mode_all_returns_every_similar_pair(tmp_path):
    store = _seeded_store(tmp_path)
    near = store.graph(limit=50, top_k=2)
    every = store.graph(limit=50, top_k=2, all_pairs=True)
    assert near["ok"] is True and every["ok"] is True
    assert near["mode"] == "neighbors"
    assert every["mode"] == "all"
    # 全连接模式把所有够格的连线都画出来（近邻模式只留最像的几个）
    assert len(every["edges"]) >= len(near["edges"])
    # 低于相似度下限的边最多每个点一条（它最像的那条，保证没有孤点）
    below = [e for e in every["edges"] if e["w"] < GRAPH_MIN_SIMILARITY]
    assert len(below) <= len(every["nodes"])
    # 每个点都连上了
    touched = set()
    for edge in every["edges"]:
        touched.add(edge["a"])
        touched.add(edge["b"])
    assert touched == set(n["id"] for n in every["nodes"])
    store.close()


def test_graph_endpoint_mode_all(tmp_path):
    store = _seeded_store(tmp_path)
    srv = MemorySidecarServer(store, port=0, warmup=False)
    port = srv.start()
    try:
        status, body = _http(port, "/graph?limit=50&top_k=2&mode=all")
        assert status == 200 and body["ok"] is True
        assert body["mode"] == "all"
    finally:
        srv.stop()
        store.close()


def test_graph_all_mode_leaves_no_node_isolated(tmp_path):
    """全连接模式下每个点都至少有一条线（不许有点孤零零地挂着）。"""
    now = int(time.time() * 1000)
    table = {}
    cards = []
    for i in range(6):
        text = "卡%d" % i
        table[text] = _onehot(text, 64)
        cards.append({"id": "c%d" % i, "kind": "fact", "text": text,
                      "importance": 0.5, "updatedAt": now})
    store = make_store(tmp_path, TableEmbedder(table, dim=64))
    store.init()
    store.upsert(cards)
    store.sync_index()
    graph = store.graph(limit=50, top_k=4, all_pairs=True)
    assert graph["ok"] is True
    touched = set()
    for edge in graph["edges"]:
        touched.add(edge["a"])
        touched.add(edge["b"])
    assert touched == set(c["id"] for c in cards)
    store.close()


# ── 客户端 ────────────────────────────────────────────────────────────────
def test_client_graph_mode_and_sleep_endpoints(tmp_path):
    from app.brain.memory_client import MemoryClient
    store = _seeded_store(tmp_path)
    srv = MemorySidecarServer(store, port=0, warmup=False)
    port = srv.start()
    try:
        client = MemoryClient("http://127.0.0.1:%d" % port, timeout=15.0)
        every = client.graph(limit=50, top_k=2, mode="all")
        assert every["ok"] is True
        assert every["mode"] == "all"

        plan = client.sleep_plan()
        assert plan["ok"] is True
        assert plan["counts"]["clusters"] == 1

        cluster = plan["clusters"][0]
        result = client.apply_sleep(clusters=[{"ids": cluster["ids"],
                                                "summary": "青柠和柠檬汽水"}])
        assert result["ok"] is True
        assert result["merged"] == 1
        assert result["forgotten"] == 1
    finally:
        srv.stop()
        store.close()


# ── 睡觉服务（面板这层） ─────────────────────────────────────────────────
def _service(tmp_path, summarizer=None, clock=None):
    from app.web.memory_sleep import MemorySleepService
    return MemorySleepService(tmp_path, sidecar_url="http://127.0.0.1:1",
                              summarizer=summarizer, clock=clock)


def test_sleep_service_applies_plan_with_summaries(tmp_path):
    store = _seeded_store(tmp_path / "sidecar")
    srv = MemorySidecarServer(store, port=0, warmup=False)
    port = srv.start()
    try:
        from app.web.memory_sleep import MemorySleepService
        seen = {}

        def summarizer(clusters):
            seen["clusters"] = clusters
            return {c["id"]: "青柠和柠檬汽水" for c in clusters}

        svc = MemorySleepService(tmp_path / "panel",
                                 sidecar_url="http://127.0.0.1:%d" % port,
                                 summarizer=summarizer)
        report = svc.run()
        assert report["ok"] is True
        assert report["merged"] == 1
        assert report["blurred"] == 1
        assert report["forgotten"] == 1
        assert report["summaries"] == ["青柠和柠檬汽水"]
        # 摘要函数拿到的组就是计划里的那一组
        assert seen["clusters"][0]["ids"] == ["g1", "g2"]

        # 跑完状态里留了记录
        state = svc.state()
        assert state["lastReport"]["merged"] == 1
        assert state["lastRunAt"] > 0
    finally:
        srv.stop()
        store.close()


def test_sleep_service_dry_run_changes_nothing(tmp_path):
    store = _seeded_store(tmp_path / "sidecar")
    srv = MemorySidecarServer(store, port=0, warmup=False)
    port = srv.start()
    try:
        from app.web.memory_sleep import MemorySleepService
        svc = MemorySleepService(tmp_path / "panel",
                                 sidecar_url="http://127.0.0.1:%d" % port,
                                 summarizer=lambda cs: {c["id"]: "摘要" for c in cs})
        before = len(store.list_cards())
        report = svc.run(dry_run=True)
        assert report["ok"] is True and report["dryRun"] is True
        assert report["merged"] == 1
        assert len(store.list_cards()) == before
    finally:
        srv.stop()
        store.close()


def test_sleep_service_falls_back_to_mechanical_summary(tmp_path):
    """模型不给摘要时也要有话说 —— 宁可机械合并，也不要丢内容。"""
    from app.web.memory_sleep import _mechanical_summary
    cluster = {"texts": ["今天天气好", "今天出门了"]}
    text = _mechanical_summary(cluster)
    assert "今天天气好" in text
    assert "2" in text


def test_sleep_service_schedule_state(tmp_path):
    svc = _service(tmp_path / "panel")
    assert svc.state()["enabled"] is False
    state = svc.set_schedule(enabled=True, time="4:05")
    assert state["enabled"] is True
    assert state["time"] == "04:05"
    # 存下来了，重开还在
    again = _service(tmp_path / "panel").state()
    assert again["enabled"] is True and again["time"] == "04:05"


def test_sleep_service_due_window(tmp_path):
    """到点之后两小时内都算数；过了窗口、或者今天跑过，就不再跑。"""
    base = datetime.datetime(2026, 3, 5, 3, 0, 0)
    svc = _service(tmp_path / "panel", clock=lambda: base)
    svc.set_schedule(enabled=True, time="03:00")
    assert svc._due() is True

    # 今天跑过了 → 不再来一次
    svc.set_schedule(enabled=True, time="03:00")
    svc._state["lastRunAt"] = int(base.timestamp() * 1000) + 1000
    assert svc._due() is False

    # 离点太远（面板晚起）→ 今天不补跑
    late = _service(tmp_path / "panel2", clock=lambda: base + datetime.timedelta(hours=5))
    late.set_schedule(enabled=True, time="03:00")
    assert late._due() is False

    # 没开定时 → 不跑
    off = _service(tmp_path / "panel3", clock=lambda: base)
    assert off._due() is False


# ── 面板路由 ─────────────────────────────────────────────────────────────
def test_dashboard_sleep_routes(tmp_path):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    store = _seeded_store(tmp_path / "sidecar")
    srv = MemorySidecarServer(store, port=0, warmup=False)
    port = srv.start()
    try:
        from app.web.dashboard import create_app
        app = create_app(tmp_path / "traces.db",
                         "http://127.0.0.1:%d" % port)
        with fastapi_testclient.TestClient(app) as client:
            r = client.get("/api/memory/sleep")
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True
            assert body["state"]["enabled"] is False
            assert body["plan"]["counts"]["clusters"] == 1

            r = client.post("/api/memory/sleep/schedule",
                            json={"enabled": True, "time": "03:30"})
            assert r.status_code == 200
            assert r.json()["state"]["time"] == "03:30"

            r = client.get("/api/memory/graph?mode=all")
            assert r.status_code == 200
            assert r.json()["ok"] is True
            assert r.json()["mode"] == "all"

            # 记忆 Tab 默认就是全连线（没有「全部连线」按钮了）
            r = client.get("/api/memory")
            assert r.status_code == 200
            assert r.json()["graph"]["mode"] == "all"
    finally:
        srv.stop()
        store.close()


def test_dashboard_memory_tab_has_sleep_panel_and_no_all_edges_button(tmp_path):
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    from app.web.dashboard import create_app
    app = create_app(tmp_path / "traces.db", "http://127.0.0.1:1")
    with fastapi_testclient.TestClient(app) as client:
        html = client.get("/").text
    # 睡觉面板在
    assert "睡觉" in html
    assert 'id="memSleepRun"' in html
    assert 'id="memSleepSave"' in html
    assert 'id="memSleepAt"' in html
    # 「全部连线」按钮没有了（默认就全连）
    assert "memAllEdges" not in html
    assert ">全部连线<" not in html
    # 状态进度条有动效
    assert "barShine" in html
    assert "barBreath" in html
