# -*- coding: utf-8 -*-
"""memory_sidecar.store 的测试：JSON 真值、双路索引、精排、全部降级路径。

假模型（FakeEmbedder / FakeRerank）替换掉真实 transformers 模型 —— 这些用例不需要
网络、不需要下模型、也不需要 API key。真模型那一条在
tests/test_memory_sidecar_real_models.py 里（默认跳过）。
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, ROOT)

# 这些用例要真的建 BM25 索引；bm25s 没装时 sidecar 会降级成纯向量，用例整体跳过
# （requirements.txt 里 bm25s 只记了注释，CI 不一定装）。
pytest.importorskip("bm25s", reason="bm25s 未安装：BM25 腿会降级，这套用例跳过")

from memory_sidecar import store as store_mod  # noqa: E402
from memory_sidecar.store import MemoryStore, tokenize  # noqa: E402


# ── 假模型 ────────────────────────────────────────────────────────────────
class FakeEmbedder:
    """按字符 hash 的确定性假向量（8 维，不需要模型）。"""

    def __init__(self, dim=8, fail=False):
        self.dim = dim
        self.fail = fail
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
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
    """按文本查表的假精排：默认给 1.0，可以指定某张卡的高分。"""

    def __init__(self, scores=None, fail=False, default=1.0):
        self.scores = scores or {}
        self.fail = fail
        self.default = default
        self.calls = 0

    def score(self, query, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("精排模型加载失败（模拟）")
        return [float(self.scores.get(t, self.default)) for t in texts]


def make_store(data_dir, embedder=None, reranker=None, **kwargs):
    kwargs.setdefault("retry_cooldown", 0)  # 测试里不许冷却，失败要能立刻重试
    return MemoryStore(
        data_dir,
        embedder=embedder if embedder is not None else FakeEmbedder(),
        reranker=reranker if reranker is not None else FakeRerank(),
        **kwargs,
    )


CARDS = [
    {"id": "c1", "key": "master", "kind": "fact", "text": "主人不吃辣", "importance": 0.9,
     "mood": "happy"},
    {"id": "c2", "key": "master", "kind": "event", "text": "今天去爬山了", "importance": 0.4},
    {"id": "c3", "key": "master", "kind": "topic", "text": "喜欢喝冰美式", "importance": 0.6},
]


def read_cards(data_dir):
    with open(os.path.join(str(data_dir), "cards.json"), encoding="utf-8") as f:
        return json.load(f)


# ── 分词 ──────────────────────────────────────────────────────────────────
def test_tokenize_is_cjk_aware():
    toks = tokenize("主人不吃辣")
    assert "辣" in toks          # 单字
    assert "主人" in toks         # 字 bigram
    assert "吃辣" in toks
    assert "hello" in tokenize("Hello WORLD 42")  # 英文按词、已小写
    assert "42" in tokenize("Hello WORLD 42")
    assert tokenize("") == []
    assert tokenize(None) == []


# ── A1 ────────────────────────────────────────────────────────────────────
def test_init_creates_data_dir_and_empty_store(tmp_path):
    store = make_store(tmp_path / "mem")
    stats = store.init()
    assert stats["cards"] == 0
    assert (tmp_path / "mem").is_dir()
    assert read_cards(tmp_path / "mem") == []

    # 幂等：再跑一次不炸，也不会把已有卡片清掉
    store.upsert([{"id": "c1", "text": "主人不吃辣"}])
    again = make_store(tmp_path / "mem").init()
    assert again["cards"] == 1
    assert [c["id"] for c in read_cards(tmp_path / "mem")] == ["c1"]


def test_cli_init_is_idempotent(tmp_path):
    data_dir = str(tmp_path / "cli")
    def run():
        return subprocess.run(
            [sys.executable, "-m", "memory_sidecar", "init", "--data-dir", data_dir],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        )
    first, second = run(), run()
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert json.loads(first.stdout.strip())["ok"] is True
    assert os.path.isdir(data_dir)
    assert read_cards(data_dir) == []


# ── A2（假模型：只验打分链路与「没有字面重叠也能命中」） ────────────────────
def test_upsert_then_search_without_literal_overlap(tmp_path):
    store = make_store(tmp_path)
    store.init()
    assert store.upsert(CARDS) == 3

    # 查询词一个都不在卡片的 BM25 词表里 → 只能靠向量路召回
    res = store.search("zzz qqq", top_k=3)
    assert res["error"] is None
    assert res["path"] == "vector"
    assert [r["id"] for r in res["results"]]

    # 精排给「主人不吃辣」高分 → 它必须排第一
    store2 = make_store(tmp_path, reranker=FakeRerank(scores={"主人不吃辣": 6.0}, default=0.0))
    store2.load()
    res2 = store2.search("我不能吃什么", top_k=3)
    assert res2["results"][0]["id"] == "c1"
    assert res2["results"][0]["rerankLogit"] == 6.0
    assert res2["results"][0]["score"] > res2["results"][1]["score"]
    store.close()
    store2.close()


def test_search_scoring_decay_and_mood(tmp_path):
    store = make_store(tmp_path, reranker=FakeRerank(default=2.0))
    store.init()
    now = int(time.time() * 1000)
    old = now - 100 * 86400000  # 100 天前
    store.upsert([
        {"id": "fresh", "kind": "fact", "text": "主人喜欢猫", "mood": "happy",
         "importance": 0.5, "updatedAt": now},
        {"id": "old", "kind": "fact", "text": "主人喜欢猫", "mood": "happy",
         "importance": 0.5, "updatedAt": old},
        {"id": "sad", "kind": "fact", "text": "主人喜欢猫", "mood": "sad",
         "importance": 0.5, "updatedAt": now},
    ])
    res = store.search("主人喜欢猫", top_k=3, mood="happy")
    by_id = {r["id"]: r for r in res["results"]}
    assert res["results"][0]["id"] == "fresh"
    # 同一条 100 天前的记忆：fact 半衰期 90 天 → decay≈0.46，分数必须更低
    assert by_id["old"]["decay"] < 0.5
    assert by_id["old"]["score"] < by_id["fresh"]["score"]
    # 情绪不一致的那张没有 1.15 倍加成
    assert by_id["sad"]["score"] == pytest.approx(by_id["fresh"]["score"] / 1.15, rel=0.02)
    store.close()


def test_search_filters_kind_and_mood(tmp_path):
    store = make_store(tmp_path)
    store.init()
    store.upsert(CARDS)
    res = store.search("爬山", top_k=5, kind="event")
    assert [r["id"] for r in res["results"]] == ["c2"]
    assert store.search("爬山", kind="thought")["results"] == []
    store.close()


# ── A3 ────────────────────────────────────────────────────────────────────
def test_delete_removes_from_json_and_both_indexes(tmp_path):
    store = make_store(tmp_path)
    store.init()
    store.upsert(CARDS)
    store.search("爬山")  # 把索引建起来
    assert store._chroma_count() == 3
    assert set(store._bm25_ids) == {"c1", "c2", "c3"}

    assert store.delete(["c2"]) == 1
    assert [c["id"] for c in read_cards(tmp_path)] == ["c1", "c3"]      # JSON
    store.sync_index()  # BM25 是派生索引：写卡只标脏，追平由 sync_index / search 负责
    assert set(store._bm25_ids) == {"c1", "c3"}                          # BM25
    assert store._chroma_count() == 2                                    # Chroma
    assert store.delete(["不存在"]) == 0                                  # 幂等
    assert store.stats()["active"] == 2
    store.close()


# ── A4 ────────────────────────────────────────────────────────────────────
def test_supersede_keeps_one_active_card(tmp_path):
    store = make_store(tmp_path)
    store.init()
    store.upsert(CARDS)
    assert store.supersede("c2", {"id": "c2b", "kind": "event", "text": "上周去爬山了"}) == 1

    cards = {c["id"]: c for c in read_cards(tmp_path)}
    assert cards["c2"]["status"] == "superseded"      # 旧卡是标记，不是悄悄删掉
    assert cards["c2"]["supersededBy"] == "c2b"
    assert cards["c2b"]["status"] == "active"
    assert cards["c2b"]["supersedes"] == "c2"

    stats = store.stats()
    assert stats["active"] == 3 and stats["superseded"] == 1
    ids = [r["id"] for r in store.search("爬山", top_k=5)["results"]]
    assert "c2" not in ids and "c2b" in ids

    reloaded = make_store(tmp_path)
    reloaded.load()
    assert reloaded.stats()["superseded"] == 1
    reloaded.close()
    store.close()


# ── A5：JSON 卡片是真值，索引删了能重建 ────────────────────────────────────
def test_cards_json_is_source_of_truth_for_both_indexes(tmp_path):
    """先用「没有向量腿」的 store 灌卡（等价于索引从未建过），再开向量腿读回来。"""
    writer = make_store(tmp_path, use_vector=False)
    writer.init()
    writer.upsert(CARDS)
    writer.close()
    assert not (tmp_path / "chroma").exists()

    reader = make_store(tmp_path)
    assert reader.load() == 3
    res = reader.search("zzz qqq", top_k=3)  # 只有向量路能命中
    assert res["path"] == "vector"
    assert {r["id"] for r in res["results"]} == {"c1", "c2", "c3"}
    assert reader._chroma_count() == 3       # 索引已从卡片重建
    reader.close()


_SUBPROCESS_PREAMBLE = '''
import json, sys
sys.path.insert(0, __ROOT__)
from memory_sidecar.store import MemoryStore


class FakeEmbed:
    def embed(self, texts):
        out = []
        for t in texts:
            v = [0.0] * 8
            for ch in str(t):
                v[ord(ch) % 8] += 1.0
            n = sum(x * x for x in v) ** 0.5 or 1.0
            out.append([x / n for x in v])
        return out


class FakeRerank:
    def score(self, q, texts):
        return [1.0 for _ in texts]
'''


def _run_in_subprocess(data_dir, body):
    """在**独立进程**里跑一段脚本（模拟重启：进程退出后 Windows 才会放开 sqlite 句柄）。"""
    code = _SUBPROCESS_PREAMBLE.replace("__ROOT__", repr(ROOT)) + body.replace("__TMP__", repr(data_dir))
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, encoding="utf-8"
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_restart_after_deleting_chroma_dir_rebuilds_index(tmp_path):
    """A5：把 Chroma 目录整个删掉，重启后 search 照样能用（索引从 JSON 卡重建）。"""
    first = _run_in_subprocess(
        str(tmp_path),
        '''
store = MemoryStore(__TMP__, embedder=FakeEmbed(), reranker=FakeRerank())
store.init()
store.upsert([{"id": "c1", "kind": "fact", "text": "主人不吃辣"}])
res = store.search("zzz qqq")
count = store.stats()["vector"]["count"]
store.close()
print(json.dumps({"count": count, "ids": [r["id"] for r in res["results"]]}))
''',
    )
    assert first["ids"] == ["c1"]
    assert os.path.isdir(tmp_path / "chroma")

    shutil.rmtree(tmp_path / "chroma")
    assert not (tmp_path / "chroma").exists()

    second = _run_in_subprocess(
        str(tmp_path),
        '''
store = MemoryStore(__TMP__, embedder=FakeEmbed(), reranker=FakeRerank())
store.load()
res = store.search("zzz qqq")
count = store.stats()["vector"]["count"]
store.close()
print(json.dumps({"count": count, "path": res["path"], "ids": [r["id"] for r in res["results"]],
                  "error": res["error"]}))
''',
    )
    assert second["error"] is None
    assert second["path"] == "vector", "只有向量腿能命中，说明索引真的被重建了"
    assert second["ids"] == ["c1"]
    assert second["count"] == 1


def test_corrupt_chroma_file_is_repaired_from_cards(tmp_path):
    """索引文件被写坏 → 重建后照常服务（卡片一条不丢）。"""
    _run_in_subprocess(
        str(tmp_path),
        '''
store = MemoryStore(__TMP__, embedder=FakeEmbed(), reranker=FakeRerank())
store.init()
store.upsert([{"id": "c1", "kind": "fact", "text": "主人不吃辣"}])
store.search("zzz qqq")
store.close()
print(json.dumps({"ok": True}))
''',
    )
    broken = 0
    for dirpath, _dirs, files in os.walk(tmp_path / "chroma"):
        for name in files:
            if name.endswith(".sqlite3"):
                with open(os.path.join(dirpath, name), "wb") as f:
                    f.write(b"this is not a database")
                broken += 1
    assert broken == 1

    after = _run_in_subprocess(
        str(tmp_path),
        '''
store = MemoryStore(__TMP__, embedder=FakeEmbed(), reranker=FakeRerank())
store.load()
res = store.search("zzz qqq")
print(json.dumps({"ids": [r["id"] for r in res["results"]], "path": res["path"],
                  "count": store.stats()["vector"]["count"]}))
''',
    )
    assert after["ids"] == ["c1"]
    assert after["path"] == "vector"
    assert after["count"] == 1


# ── A7：降级路径 ──────────────────────────────────────────────────────────
def test_degradation_reranker_unavailable_falls_back_to_rrf(tmp_path):
    store = make_store(tmp_path, reranker=FakeRerank(fail=True))
    store.init()
    store.upsert(CARDS)
    res = store.search("主人不吃辣", top_k=3)
    assert res["error"] is None
    assert res["results"], "精排挂了也必须靠 RRF 粗排给结果"
    assert all(r["rerankLogit"] is None for r in res["results"])
    assert res["degraded"] is True
    health = store.health()
    assert health["models"]["rerank"] == "degraded"
    assert any("精排" in r for r in health["degradation"])
    store.close()


def test_degradation_bm25_unavailable_uses_vector_only(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.init()
    store.upsert(CARDS)

    def boom():
        raise ImportError("bm25s 没装（模拟）")

    monkeypatch.setattr(store_mod, "_import_bm25s", boom)
    store.load()
    res = store.search("主人不吃辣", top_k=3)
    assert res["path"] == "vector"
    assert res["error"] is None
    assert res["results"]
    assert store.health()["bm25"] == "degraded"
    assert any("BM25" in r for r in store.health()["degradation"])
    store.close()


def test_degradation_embeddings_unavailable_uses_bm25_only(tmp_path):
    """向量模型挂掉 → 只用 BM25，sidecar 照样回答（这是最容易忽略的一条）。"""
    store = make_store(tmp_path, embedder=FakeEmbedder(fail=True))
    store.init()
    store.upsert(CARDS)
    res = store.search("主人不吃辣", top_k=3)
    assert res["error"] is None
    assert res["path"] == "bm25"
    assert [r["id"] for r in res["results"]] == ["c1"]
    health = store.health()
    assert health["models"]["embed"] == "degraded"
    assert health["degraded"] is True
    store.close()


def test_degradation_chroma_unavailable_still_serves(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.init()
    store.upsert(CARDS)
    store.search("主人不吃辣")  # 先建索引

    def boom():
        raise ImportError("chromadb 没装（模拟）")

    monkeypatch.setattr(store_mod, "_import_chromadb", boom)
    store.close()  # 丢掉已打开的 collection，逼它重新打开 → 失败
    res = store.search("主人不吃辣", top_k=3)
    assert res["path"] == "bm25"
    assert res["error"] is None
    assert [r["id"] for r in res["results"]] == ["c1"]
    assert any("Chroma" in r for r in store.health()["degradation"])
    store.close()


def test_degradation_chroma_open_failure_repairs_from_cards(tmp_path):
    """Chroma 打不开 → 清空重建，索引从 JSON 卡补齐，检索继续。"""
    store = make_store(tmp_path)
    store.init()
    store.upsert(CARDS)
    store.search("主人不吃辣")
    store.close()

    calls = {"open": 0}
    real_open = store._open_chroma

    def flaky(recreate=False):
        if not recreate:
            calls["open"] += 1
            raise RuntimeError("索引损坏（模拟）")
        return real_open(recreate=True)

    store._open_chroma = flaky
    res = store.search("zzz qqq", top_k=3)
    assert calls["open"] >= 1, "必须走到重建分支"
    assert res["error"] is None
    assert res["results"]
    store.close()


def test_degradation_nothing_available_returns_empty_with_error(tmp_path, monkeypatch):
    store = make_store(tmp_path, embedder=FakeEmbedder(fail=True))
    store.init()
    store.upsert(CARDS)

    def boom():
        raise ImportError("bm25s 没装（模拟）")

    monkeypatch.setattr(store_mod, "_import_bm25s", boom)
    store.load()
    res = store.search("主人不吃辣", top_k=3)
    assert res["results"] == []
    assert res["error"] and "不可用" in res["error"]
    assert res["degraded"] is True
    store.close()


# ── 其它 ──────────────────────────────────────────────────────────────────
def test_corrupt_cards_json_is_quarantined(tmp_path):
    (tmp_path / "cards.json").write_text("{ 这不是 JSON", encoding="utf-8")
    store = make_store(tmp_path)
    assert store.load() == 0
    assert (tmp_path / "cards.json.bad").exists(), "坏文件要留档，不能静默覆盖"
    store.close()


def test_health_reports_counts_and_states(tmp_path):
    store = make_store(tmp_path)
    store.init()
    # 什么都没干之前：两个模型都还没加载，但也不能算降级
    assert store.health()["models"] == {"embed": "unloaded", "rerank": "unloaded"}
    assert store.health()["degraded"] is False

    store.upsert(CARDS)
    health = store.health()
    assert health["cards"] == 3
    # 写卡**不等模型**（改这一条就是因为这个）：向量模型还没被叫醒，
    # 3 条卡的向量挂在队列上，等检索 / 预热结束再补
    assert health["models"]["embed"] == "unloaded"
    assert health["models"]["rerank"] == "unloaded"  # 精排只在检索时才加载
    assert health["pendingIndex"] == 3
    store.search("主人不吃辣")
    health = store.health()
    assert health["models"] == {"embed": "ready", "rerank": "ready"}
    assert health["pendingIndex"] == 0              # 检索前已经把队列补干净了
    store.close()


def test_health_never_loads_models(tmp_path):
    """A10 的前提：/health 必须在模型就绪之前就能回答（不自作主张加载）。"""

    class TripwireEmbedder:
        def embed(self, texts):
            raise AssertionError("health 不该触发模型加载")

    class TripwireRerank:
        def score(self, query, texts):
            raise AssertionError("health 不该触发模型加载")

    store = make_store(tmp_path, embedder=TripwireEmbedder(), reranker=TripwireRerank())
    store.init()
    # 直接落卡片再 load（upsert 会顺手算向量，那就碰到 tripwire 了）
    with open(tmp_path / "cards.json", "w", encoding="utf-8") as f:
        json.dump([{"id": "c1", "text": "主人不吃辣"}], f, ensure_ascii=False)
    store.load()
    health = store.health()
    assert health["models"] == {"embed": "unloaded", "rerank": "unloaded"}
    store.close()


def test_rebuild_index_restores_both_legs(tmp_path):
    store = make_store(tmp_path)
    store.init()
    store.upsert(CARDS)
    store.close()
    store = make_store(tmp_path)
    store.load()
    report = store.rebuild_index()
    assert report["vector"] == 3 and report["bm25"] == 3
    assert report["errors"] == []
    store.close()


# ── 写卡不等模型（A1）：JSON 是真值，索引是派生的，所以没理由等 ────────────
class SlowEmbedder(FakeEmbedder):
    """模拟冷启动：**第一次** embed 时握着进程级 _HF_LOCK 慢慢「加载」。

    真模型就是这样（TransformersEmbedder.load/embed 都串在 _HF_LOCK 上），
    所以这个假模型能忠实复现「写卡排在一把十几秒的模型锁后面」那种事故。
    """

    def __init__(self, delay=2.0, dim=8):
        super().__init__(dim=dim)
        self.delay = float(delay)
        self.started = threading.Event()  # 预热线程真的进到 embed 里了

    def embed(self, texts):
        if self.delay:
            with store_mod._HF_LOCK:
                self.started.set()
                time.sleep(self.delay)
                self.delay = 0.0  # 只慢第一次（= 加载那一次）
        return super().embed(texts)


def test_upsert_delete_supersede_do_not_wait_for_the_model(tmp_path):
    """A1：模型正在慢慢加载时，写 / 改 / 删都必须立刻返回（毫秒级）。"""
    slow = SlowEmbedder(delay=2.0)
    store = make_store(tmp_path, embedder=slow)
    store.init()

    # 后台预热：把「模型锁」占住 2 秒，模拟冷启动
    warm = threading.Thread(target=store.warmup, daemon=True)
    warm.start()
    assert slow.started.wait(10), "预热线程没进到 embed"

    t0 = time.time()
    assert store.upsert([{"id": "c1", "kind": "fact", "text": "主人不吃辣"}]) == 1
    upsert_ms = int((time.time() - t0) * 1000)
    assert upsert_ms < 500, "写卡必须不等模型（实测 %dms）" % upsert_ms

    # 真值（JSON）已经落地了，索引只是欠着
    assert [c["id"] for c in read_cards(tmp_path)] == ["c1"]
    assert store.health()["cards"] == 1
    assert store.health()["pendingIndex"] == 1

    # 写完之后立刻检索：卡**必须**能被找到（这时向量模型还在加载）
    res = store.search("不吃辣", top_k=3)
    assert res["error"] is None
    assert [r["id"] for r in res["results"]] == ["c1"], res

    t0 = time.time()
    assert store.supersede("c1", {"id": "c1b", "kind": "fact", "text": "主人不吃辣，吃辣胃疼"}) == 1
    assert int((time.time() - t0) * 1000) < 500, "supersede 也不许等模型"
    t0 = time.time()
    assert store.delete(["c1b"]) == 1
    assert int((time.time() - t0) * 1000) < 500, "delete 也不许等模型"

    warm.join(timeout=30)
    # 预热只排空「它结束那一刻」的挂起项；之后的新写自然又欠着，等下次 search / sync_index
    store.sync_index()
    assert store.health()["pendingIndex"] == 0, "挂起队列必须随时能排空"
    assert store.list_cards() == []  # c1 被 c1b 取代、c1b 又删了
    store.close()


def test_warmup_drains_the_pending_vector_queue(tmp_path):
    """写卡只写 JSON；预热结束（模型就绪）时把欠的向量一次性补齐。"""
    store = make_store(tmp_path)
    store.init()
    store.upsert(CARDS)
    assert store.pending_index_count() == 3, "写卡之后向量是欠着的"
    assert store._chroma_count() in (None, 0)
    store.warmup()
    assert store.pending_index_count() == 0
    assert store._chroma_count() == 3
    store.close()


def test_health_is_fast_while_the_model_is_loading(tmp_path):
    """A2（store 层）：模型在加载时，health() 必须毫秒级返回。"""
    slow = SlowEmbedder(delay=2.0)
    store = make_store(tmp_path, embedder=slow)
    store.init()
    store.upsert(CARDS)
    warm = threading.Thread(target=store.warmup, daemon=True)
    warm.start()
    assert slow.started.wait(10), "预热线程没进到 embed"

    samples = []
    health = None
    for _ in range(5):
        t0 = time.time()
        health = store.health()
        samples.append(round((time.time() - t0) * 1000.0, 3))
    assert health["ok"] is True and health["cards"] == 3
    assert min(samples) < 50, "health 不该排在模型加载后面（实测 %r ms）" % samples
    assert max(samples) < 500, "health 偶尔慢一点可以，但不能被模型加载拖住（实测 %r ms）" % samples
    warm.join(timeout=30)
    store.close()


def test_search_right_after_write_sees_the_card_without_a_warm_index(tmp_path):
    """A1 的另一半：索引从没建过，写完立刻搜也得命中。"""
    store = make_store(tmp_path)
    store.init()
    store.upsert(CARDS)
    assert store._chroma_count() in (None, 0), "写卡不该顺手建向量索引"
    res = store.search("zzz qqq", top_k=3)  # 只有向量腿能命中（无字面重叠）
    assert res["path"] == "vector"
    assert {r["id"] for r in res["results"]} == {"c1", "c2", "c3"}
    store.close()


# ── /list 的存储层（A3） ──────────────────────────────────────────────────
def test_list_cards_orders_by_importance_then_recency(tmp_path):
    store = make_store(tmp_path)
    store.init()
    now = int(time.time() * 1000)
    store.upsert([
        {"id": "low_new", "kind": "fact", "text": "低重要度但很新", "importance": 0.2,
         "updatedAt": now},
        {"id": "high_old", "kind": "fact", "text": "高重要度但很旧", "importance": 0.9,
         "updatedAt": now - 86400000},
        {"id": "high_new", "kind": "event", "text": "高重要度且很新", "importance": 0.9,
         "updatedAt": now},
        {"id": "mid", "kind": "topic", "text": "中等重要度", "importance": 0.5, "updatedAt": now},
    ])
    ids = [c["id"] for c in store.list_cards()]
    # 重要度 desc 优先，同重要度按 updatedAt desc
    assert ids == ["high_new", "high_old", "mid", "low_new"]
    first = store.list_cards()[0]
    assert {"id", "kind", "text", "importance", "updatedAt"} <= set(first)
    store.close()


def test_list_cards_kind_filter_limit_and_empty_store(tmp_path):
    store = make_store(tmp_path)
    store.init()
    assert store.list_cards() == []          # 空库
    assert store.count_cards() == 0

    store.upsert(CARDS)
    assert store.count_cards() == 3
    assert store.count_cards("fact") == 1
    assert [c["id"] for c in store.list_cards(kind="event")] == ["c2"]
    assert store.list_cards(kind="thought") == []
    assert len(store.list_cards(limit=2)) == 2
    assert len(store.list_cards(limit=0)) == 3, "limit<=0 当没传（默认 50）"
    assert len(store.list_cards(limit="不是数字")) == 3
    assert len(store.list_cards(limit=10 ** 9)) == 3, "超过上限也只是把卡全列出来"
    store.close()


def test_list_cards_can_include_superseded(tmp_path):
    """默认只列 active；include_superseded=True 时连历史卡一起列（面板要看全部）。"""
    store = make_store(tmp_path)
    store.init()
    store.upsert([{"id": "c1", "kind": "fact", "text": "主人不吃辣", "importance": 0.9}])
    store.supersede("c1", {"id": "c1b", "kind": "fact", "text": "主人不吃辣，吃辣胃疼"})
    assert [c["id"] for c in store.list_cards()] == ["c1b"]
    assert store.count_cards() == 1
    both = store.list_cards(include_superseded=True)
    # c1 重要度 0.9 > c1b 的 0.5：历史的 c1 反而排在前面（排序口径不变）
    assert [c["id"] for c in both] == ["c1", "c1b"]
    assert store.count_cards(include_superseded=True) == 2
    assert {c["id"]: c["status"] for c in both} == {"c1": "superseded", "c1b": "active"}
    store.close()


# ── 精排模型自动选择（A4） ────────────────────────────────────────────────
def test_rerank_model_auto_selects_by_cuda(monkeypatch):
    monkeypatch.delenv(store_mod.RERANK_MODEL_ENV, raising=False)
    model, why = store_mod.resolve_rerank_model(cuda_ok=True)
    assert model == store_mod.RERANK_MODEL_GPU == "BAAI/bge-reranker-v2-m3"
    assert "CUDA" in why
    model, why = store_mod.resolve_rerank_model(cuda_ok=False)
    assert model == store_mod.RERANK_MODEL_CPU == "BAAI/bge-reranker-base"
    assert "没有 CUDA" in why


def test_rerank_model_env_override_still_wins(monkeypatch):
    monkeypatch.setenv(store_mod.RERANK_MODEL_ENV, "someone/other-reranker")
    for cuda in (True, False):
        model, why = store_mod.resolve_rerank_model(cuda_ok=cuda)
        assert model == "someone/other-reranker"
        assert "环境变量" in why
    # 显式参数优先级最高
    assert store_mod.resolve_rerank_model("x/y", cuda_ok=True)[0] == "x/y"


def test_store_picks_the_auto_reranker_and_says_why(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(store_mod.RERANK_MODEL_ENV, raising=False)
    monkeypatch.setattr(store_mod, "_cuda_available", lambda: True)
    store = MemoryStore(tmp_path, embedder=FakeEmbedder(), reranker=None, retry_cooldown=0)
    assert store.rerank_model == "BAAI/bge-reranker-v2-m3"
    assert store._reranker.model_name == "BAAI/bge-reranker-v2-m3"
    assert "CUDA" in capsys.readouterr().err, "必须把「选了哪个、为什么」记下来"
    store.close()

    monkeypatch.setattr(store_mod, "_cuda_available", lambda: False)
    store = MemoryStore(tmp_path, embedder=FakeEmbedder(), reranker=None, retry_cooldown=0)
    assert store.rerank_model == "BAAI/bge-reranker-base"
    assert "没有 CUDA" in capsys.readouterr().err
    store.close()


def test_store_keeps_injected_and_env_reranker(tmp_path, monkeypatch, capsys):
    """注入假精排 → 用它；设了环境变量 → 用环境变量那个。"""
    fake = FakeRerank()
    store = MemoryStore(tmp_path, embedder=FakeEmbedder(), reranker=fake, retry_cooldown=0)
    assert store._reranker is fake
    store.close()

    monkeypatch.setenv(store_mod.RERANK_MODEL_ENV, "someone/other-reranker")
    store = MemoryStore(tmp_path, embedder=FakeEmbedder(), retry_cooldown=0)
    assert store.rerank_model == "someone/other-reranker"
    assert store._reranker.model_name == "someone/other-reranker"
    assert store.stats()["rerank"]["model"] == "someone/other-reranker"
    store.close()
