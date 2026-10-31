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
    # 功能测试不关心检索延迟：预算给足，别让「进程内第一次 import chromadb（~1s）」
    # 这种一次性启动成本把某个用例的 path 挤成 fallback。预算自己的行为在
    # test_search_does_not_wait_for_a_slow_embedder 里单独测（用真默认值）。
    kwargs.setdefault("model_budget", 30.0)
    # 假向量（按字符 hash）的余弦值跟真实语义没有可比性：语义门禁按默认 0.40 会
    # 把假模型的结果一刀切掉。功能测试关掉门禁；门禁自己的行为在
    # test_gate_* 里用显式阈值测。
    kwargs.setdefault("min_similarity", 0.0)
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
    # 假精排给「主人不吃辣」6.0、给别的卡 1.0（都算正分）：相关度闸门会把 0 分的
    # 行当成「不相关」裁掉，这里要测的是排序，所以让三张卡都有正分。
    store2 = make_store(tmp_path, reranker=FakeRerank(scores={"主人不吃辣": 6.0}, default=1.0))
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

# model_budget=30：这几条测的是「索引坏了能从卡片修好」，不是检索延迟。
# 子进程里第一次 import chromadb 要 ~1s，别让预算把这事变成 flaky。
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
store = MemoryStore(__TMP__, embedder=FakeEmbed(), reranker=FakeRerank(), model_budget=30,
                     min_similarity=0)
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
store = MemoryStore(__TMP__, embedder=FakeEmbed(), reranker=FakeRerank(), model_budget=30,
                     min_similarity=0)
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
store = MemoryStore(__TMP__, embedder=FakeEmbed(), reranker=FakeRerank(), model_budget=30,
                     min_similarity=0)
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
store = MemoryStore(__TMP__, embedder=FakeEmbed(), reranker=FakeRerank(), model_budget=30,
                     min_similarity=0)
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


# ── DEFECT 1：写卡之后第一轮检索不许等模型 ────────────────────────────────
class LoadingEmbedder(FakeEmbedder):
    """前几次调用很慢（= 模型正在加载/很慢），之后很快：模拟真实冷启动。"""

    def __init__(self, delay=3.0, slow_calls=2, dim=8):
        super().__init__(dim=dim)
        self.delay = float(delay)
        self.slow_calls = int(slow_calls)
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        if self.calls <= self.slow_calls:
            time.sleep(self.delay)
        return super().embed(texts)


def _pay_process_startup_once():
    """把**进程级一次性成本**先付掉（jieba 词表 / bm25s 的 import）。

    这些是启动成本（sidecar 的预热线程先付），不属于「写完之后这一次检索」的延迟；
    不先付掉的话，同一个用例单独跑（进程是冷的）会比整套跑慢 1 秒多。
    """
    tokenize("预热")
    store_mod._import_bm25s()


def test_write_then_search_does_not_wait_for_a_slow_embedder(tmp_path):
    """DEFECT 1：upsert 之后第一轮 search 不许等模型（聊天路径的读超时是 2s）。

    实测（假模型每次睡 3 秒、预算 0.8s）：改之前 upsert→search ≈ 3.0s
    （聊天端 2s 就超时了，主人看到的是「刚说完就忘了」），改之后 ≈ 0.8s，
    而且那张刚写的卡照样在结果里（词面兜底腿）。
    """
    _pay_process_startup_once()
    slow = LoadingEmbedder(delay=3.0, slow_calls=99)  # 一直很慢
    store = make_store(tmp_path, embedder=slow, model_budget=0.8)
    store.init()
    assert store.upsert([{"id": "c1", "kind": "fact",
                          "text": "主人的猫叫团子，是个橘猫"}]) == 1

    t0 = time.time()
    res = store.search("我的猫叫什么", top_k=3)
    elapsed = time.time() - t0
    assert elapsed < 2.0, "写完立刻搜不许等模型（实测 %.2fs）" % elapsed
    assert [r["id"] for r in res["results"]] == ["c1"], res
    assert res["modelPending"] is True, "这一轮应当是「模型还没跑完，先给词面结果」"
    assert store.model_timeouts >= 1
    store.close()


def test_pending_card_is_found_even_without_bm25(tmp_path):
    """挂起队列的卡靠**词面兜底**进候选：连 BM25 都关掉也照样看得见。"""
    _pay_process_startup_once()
    store = make_store(tmp_path, embedder=LoadingEmbedder(delay=3.0, slow_calls=99),
                       use_bm25=False, model_budget=0.8)
    store.init()
    store.upsert([{"id": "c1", "kind": "fact", "text": "主人不吃辣"}])
    res = store.search("不吃辣", top_k=3)
    assert [r["id"] for r in res["results"]] == ["c1"], res
    assert res["path"] == "pending", res["path"]
    store.close()


def test_embedder_catching_up_only_improves_ranking(tmp_path):
    """模型追上来只是**变准**：同一张卡一直在，只是多了向量分。"""
    _pay_process_startup_once()
    emb = LoadingEmbedder(delay=2.0, slow_calls=1)
    store = make_store(tmp_path, embedder=emb, model_budget=0.5)
    store.init()
    store.upsert([{"id": "c1", "kind": "fact", "text": "主人不吃辣"}])

    cold = store.search("不吃辣", top_k=3)
    assert [r["id"] for r in cold["results"]] == ["c1"], cold
    assert cold["modelPending"] is True and cold["results"][0]["sim"] is None

    thread = store._model_thread          # 后台补索引的那个线程
    if thread is not None:
        thread.join(timeout=30)
    store.sync_index()
    warm = store.search("不吃辣", top_k=3)
    assert warm["results"][0]["id"] == "c1"
    assert warm["results"][0]["sim"] is not None, "索引补齐后向量腿参与排序"
    assert warm["modelPending"] is False
    store.close()

# ── DEFECT 2：相关度闸门（库里没有答案就别硬塞） ──────────────────────────
class TableEmbedder:
    """按文本查表的假向量：用来**精确**控制余弦相似度（阈值不靠运气标定）。"""

    def __init__(self, table, default=(1.0, 0.0)):
        self.table = dict(table or {})
        self.default = tuple(default)

    def embed(self, texts):
        out = []
        for text in texts:
            vec = self.table.get(str(text), self.default)
            norm = (sum(x * x for x in vec) ** 0.5) or 1.0
            out.append([float(x) / norm for x in vec])
        return out


def _cos_vec(cos):
    """二维单位向量：它和 [1, 0] 的余弦相似度正好是 cos。"""
    cos = float(cos)
    return (cos, max(0.0, 1.0 - cos * cos) ** 0.5)


def test_gate_gives_nothing_when_nothing_is_similar_enough(tmp_path):
    """DEFECT 2（门禁）：这一问跟记忆库没关系 → 一条都不给。

    阈值是标定出来的（docs/memory-sidecar.md 4.1）：无答案问句的最像相似度上限
    0.376、有答案的下限 0.456 → 0.42。这里用可控假向量把门禁两侧都走一遍。
    """
    table = {
        "主人不吃辣": (1.0, 0.0),
        "今天去爬山了": (1.0, 0.0),
        "不能吃什么": (1.0, 0.0),            # 余弦 1.0 → 门禁开
        "量子力学怎么学": _cos_vec(0.30),    # 余弦 0.30 → 门禁关
    }
    store = MemoryStore(tmp_path, embedder=TableEmbedder(table),
                        reranker=FakeRerank(default=1.0), retry_cooldown=0,
                        model_budget=30.0)
    store.init()
    store.upsert([{"id": "c1", "kind": "fact", "text": "主人不吃辣"},
                  {"id": "c2", "kind": "event", "text": "今天去爬山了"}])

    hit = store.search("不能吃什么", top_k=6)
    assert hit["results"], "有答案的问句必须照常给结果（不许因为闸门丢召回）"
    assert hit["gate"]["topSim"] >= store.min_similarity

    miss = store.search("量子力学怎么学", top_k=6)
    assert miss["results"] == [], miss
    assert miss["gate"]["topSim"] < store.min_similarity
    assert miss["gate"]["dropped"] >= 2
    store.close()


def test_gate_trims_the_tail_but_keeps_semantically_close_rows(tmp_path):
    """补位行门槛：分数尾巴丢掉；但「语义上就是同一件事」的行要保下来。"""
    store = make_store(tmp_path)   # min_similarity=0（假向量无意义），ratio 用默认值

    def row(cid, final):
        return (float(final), {"id": cid, "text": "x"}, float(final), 1.0, None)

    scored = [row("top", 0.30), row("close", 0.02), row("tail", 0.001)]
    sims = {"top": 0.60, "close": 0.50, "tail": 0.10}
    kept = [r[1]["id"] for r in store._gate(scored, sims)]
    assert kept == ["top", "close"], kept       # close 靠 0.50 ≥ 0.60×0.75 活下来
    assert store.min_score_ratio > 0 and store.dense_keep_ratio > 0
    # 顶行永远保留；一条正分都没有 → 整轮不给
    assert [r[1]["id"] for r in store._gate([row("only", 0.0001)], {})] == ["only"]
    assert store._gate([row("zero", 0.0), row("neg", -0.1)], {}) == []
    store.close()


def test_gate_knobs_come_from_env_and_kwargs(tmp_path, monkeypatch):
    """闸门 / 预算都可调：显式参数 > 环境变量 > 默认值。"""
    monkeypatch.setenv(store_mod.MIN_SCORE_RATIO_ENV, "0.5")
    monkeypatch.setenv(store_mod.MIN_SIMILARITY_ENV, "0.7")
    monkeypatch.setenv(store_mod.DENSE_KEEP_RATIO_ENV, "0.9")
    monkeypatch.setenv(store_mod.MODEL_BUDGET_ENV, "2.5")
    from_env = MemoryStore(tmp_path, embedder=FakeEmbedder(), reranker=FakeRerank(),
                           retry_cooldown=0)
    assert from_env.min_score_ratio == 0.5
    assert from_env.min_similarity == 0.7
    assert from_env.dense_keep_ratio == 0.9
    assert from_env.model_budget == 2.5
    from_env.close()

    explicit = MemoryStore(tmp_path, embedder=FakeEmbedder(), reranker=FakeRerank(),
                           retry_cooldown=0, min_score_ratio=0.2, min_similarity=0.0,
                           dense_keep_ratio=0.1, model_budget=0)
    assert (explicit.min_score_ratio, explicit.min_similarity) == (0.2, 0.0)
    assert (explicit.dense_keep_ratio, explicit.model_budget) == (0.1, 0.0)
    explicit.close()

    # 环境变量乱填也不许把服务搞挂
    monkeypatch.setenv(store_mod.MIN_SCORE_RATIO_ENV, "不是数字")
    broken = MemoryStore(tmp_path, embedder=FakeEmbedder(), reranker=FakeRerank(),
                         retry_cooldown=0)
    assert broken.min_score_ratio == store_mod.DEFAULT_MIN_SCORE_RATIO
    broken.close()


def test_cli_exposes_the_gate_knobs(tmp_path):
    """--min-score-ratio / --model-budget 要真的接进去（/stats 里能读回来）。"""
    out = subprocess.run(
        [sys.executable, "-m", "memory_sidecar", "stats",
         "--data-dir", str(tmp_path / "cli"), "--min-score-ratio", "0.3",
         "--min-similarity", "0.5", "--model-budget", "1.5"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
    )
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout.strip().splitlines()[-1])
    search = payload["stats"]["search"]
    assert search["minScoreRatio"] == 0.3
    assert search["minSimilarity"] == 0.5
    assert search["modelBudgetMs"] == 1500




# ── 按类别的闸门（price_table.py 的「价目表」） ─────────────────────────────
def test_kind_gate_thresholds_exist_and_come_from_the_price_table():
    """A1：每一类都有阈值线，而且**有出处**（不是散在逻辑里的魔数）。"""
    from memory_sidecar import price_table

    table = price_table.KIND_GATE_TABLE
    for kind in ("fact", "impression", "topic", "event", "thought"):
        assert kind in table, kind
        entry = table[kind]
        assert 0.0 <= entry["min_score_ratio"] <= 1.0, entry
        assert 0.0 <= entry["dense_keep_ratio"] <= 1.0, entry
    # fact 这一类是实测收紧的（近似陷阱住在 fact 里）
    assert table["fact"]["min_score_ratio"] > 0.12
    # fact 的语义保命符是**关掉**的：实测陷阱 d02 的余弦是那一轮最高的，
    # 开着就等于把陷阱救回来
    assert table["fact"]["dense_keep_ratio"] == 0.0
    assert price_table.KIND_GATE_TABLE_VERSION


def test_kind_gate_is_used_by_the_gate(tmp_path):
    """按类别的线要真的在闸门里起作用：同样两个分数，不同类别的命运不同。"""
    store = make_store(tmp_path, min_similarity=0.0)

    def row(cid, final, kind):
        return (float(final), {"id": cid, "text": "x", "kind": kind}, float(final), 1.0, None)

    # 同样两个分数，fact 那一类被裁掉、impression 那一类留着：
    #   0.05 < 1.0 x fact 的 0.28      -> 裁掉
    #   0.09 >= 1.0 x impression 的 0.08 -> 留着
    scored = [row("top", 1.0, "fact"), row("f_low", 0.05, "fact"),
              row("i_low", 0.09, "impression")]
    kept = [r[1]["id"] for r in store._gate(scored, {})]
    assert kept == ["top", "i_low"], kept
    store.close()


def test_kind_gate_can_be_overridden(tmp_path, monkeypatch):
    """可覆盖：构造参数、环境变量（JSON）、全局旋钮，三条路都通。"""
    store = make_store(tmp_path, kind_gate={"fact": {"min_score_ratio": 0.01,
                                                       "dense_keep_ratio": 0.0}})
    assert store.kind_gate["fact"]["min_score_ratio"] == 0.01
    assert store.kind_gate["fact"]["dense_keep_ratio"] == 0.0
    store.close()

    from memory_sidecar import price_table as pt
    monkeypatch.setenv(pt.KIND_GATE_ENV,
                       json.dumps({"impression": {"min_score_ratio": 0.42}}))
    from_env = make_store(tmp_path)
    assert from_env.kind_gate["impression"]["min_score_ratio"] == 0.42
    from_env.close()

    # 全局旋钮**显式**给了值 -> 接管所有类别（老用法：一把梭收紧）
    monkeypatch.setenv(store_mod.MIN_SCORE_RATIO_ENV, "0.6")
    global_knob = make_store(tmp_path)
    assert all(v["min_score_ratio"] == 0.6 for v in global_knob.kind_gate.values())
    global_knob.close()


def test_kind_gate_is_reported_in_stats(tmp_path):
    """按类别的表要能从 /stats 读回来（排查用）。"""
    store = make_store(tmp_path)
    search = store.stats()["search"]
    assert search["kindGate"]["fact"]["min_score_ratio"] == 0.28
    assert search["kindGateVersion"]
    store.close()


# ── 冷启动：重 import 不许压在第一次检索上 ─────────────────────────────────
def test_heavy_imports_are_prewarmed_in_the_background():
    """chromadb / bm25s / jieba 的 import 在后台预热，别压在第一次检索上。"""
    store_mod.prewarm_imports()        # 幂等：起过就当起过了
    store_mod._wait_prewarm(timeout=60)
    ready = store_mod.imports_ready()
    assert ready["bm25s"] and ready["jieba"], ready
    assert store_mod.prewarm_imports() is False, "预热跑完之后不该再起一组"


def test_prewarm_can_be_turned_off(monkeypatch):
    monkeypatch.setenv(store_mod.PREWARM_ENV, "0")
    assert store_mod.prewarm_imports() is False


def test_cold_search_does_not_wait_for_the_models(tmp_path):
    """冷启动的第一轮检索**不等模型**：模型没热就先用词面结果回答。

    实测：模型要十几秒，等它等于白等预算（跑不完也是白跑）。
    """
    class NotReadyEmbedder(FakeEmbedder):
        """还没加载好的向量模型（ready=False）。"""

        @property
        def ready(self):
            return False

    store = make_store(tmp_path, embedder=NotReadyEmbedder(), model_budget=0.8)
    store.init()
    store.upsert([{"id": "c1", "kind": "fact", "text": "主人的猫叫团子，是个橘猫"}])

    t0 = time.time()
    res = store.search("我的猫叫什么", top_k=3)
    elapsed = time.time() - t0
    assert elapsed < 0.5, "冷启动不该等模型（实测 %.2fs）" % elapsed
    assert res["modelPending"] is True, "这一轮是「模型还没好，先给词面结果」"
    assert [r["id"] for r in res["results"]] == ["c1"], res
    store.close()


def test_cold_rerank_does_not_burn_the_budget(tmp_path):
    """精排模型没热的时候也不许烧掉整个预算。"""

    class NotReadyRerank(FakeRerank):
        @property
        def ready(self):
            return False

    store = make_store(tmp_path, reranker=NotReadyRerank(), model_budget=0.8)
    store.init()
    store.upsert([{"id": "c1", "kind": "fact", "text": "主人的猫叫团子，是个橘猫"}])

    t0 = time.time()
    res = store.search("我的猫叫什么", top_k=3)
    elapsed = time.time() - t0
    assert elapsed < 0.5, "精排没热不该白等预算（实测 %.2fs）" % elapsed
    assert res["results"], "词面腿照常回答"
    store.close()

# ── DEFECT 3：category 要跟着卡一起出去（/list 与 /search 同一套字段） ────
def test_public_card_keeps_category_key_and_status(tmp_path):
    store = make_store(tmp_path, min_similarity=0.0)
    store.init()
    store.upsert([
        {"id": "c1", "key": "master", "kind": "impression", "text": "主人不吃辣",
         "category": "preference", "importance": 0.9},
        {"id": "c2", "kind": "event", "text": "今天去爬山了"},   # 没写 category
    ])
    listed = {c["id"]: c for c in store.list_cards()}
    assert listed["c1"]["category"] == "preference"
    assert listed["c1"]["key"] == "master"
    assert listed["c1"]["status"] == "active"
    assert listed["c2"]["category"] == "", "没写 category 的卡也给空字符串，不是缺键"

    found = {r["id"]: r for r in store.search("不吃辣", top_k=5)["results"]}
    assert found["c1"]["category"] == "preference"
    assert {"id", "key", "kind", "text", "category", "mood", "importance", "status",
            "createdAt", "updatedAt", "hitCount", "lastHitAt"} <= set(found["c1"])
    store.close()

# ── 模型下载策略：只读本地缓存，缺模型快速失败（不联网） ──────────────────
def test_model_in_cache_reads_the_hf_cache_layout(tmp_path, monkeypatch):
    """缓存判据只看磁盘：models--<owner>--<name>/snapshots/<sha>。"""
    cache = tmp_path / "hub"
    snap = cache / "models--BAAI--bge-small-zh-v1.5" / "snapshots" / "abc123"
    snap.mkdir(parents=True)
    (snap / "config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HF_HUB_CACHE", str(cache))
    assert store_mod.model_in_cache("BAAI/bge-small-zh-v1.5") is True
    assert store_mod.model_in_cache("BAAI/bge-reranker-base") is False
    assert store_mod.model_in_cache("") is False
    assert store_mod.model_in_cache(str(snap)) is True, "直接给本地路径也算有"


def test_missing_model_fails_fast_and_points_at_prefetch(tmp_path):
    """模型不在本地缓存 → 立刻失败、指向 prefetch 脚本（**不去 HuggingFace 联网**）。"""
    emb = store_mod.TransformersEmbedder("nobody/no-such-model-xyz")
    t0 = time.time()
    with pytest.raises(store_mod.ModelUnavailable) as info:
        emb.load()
    elapsed = time.time() - t0
    assert elapsed < 2.0, "缺模型必须立刻失败（实测 %.2fs）" % elapsed
    assert info.value.code == "missing"
    message = str(info.value)
    assert "prefetch_models.py" in message and "不会联网下载" in message
    assert store_mod.ALLOW_DOWNLOAD_ENV in message, "要告诉主人怎么放开下载"

    rerank = store_mod.TransformersReranker("nobody/no-such-reranker-xyz")
    with pytest.raises(store_mod.ModelUnavailable) as info2:
        rerank.load()
    assert info2.value.code == "missing" and "prefetch_models.py" in str(info2.value)


def test_models_load_with_local_files_only(tmp_path, monkeypatch):
    """SentenceTransformer / CrossEncoder 都必须带 local_files_only=True（只读缓存）。"""
    import types

    seen = {}

    class FakeST:
        def __init__(self, name, device=None, local_files_only=False, **kwargs):
            seen[name] = local_files_only
            self.name = name

        def get_sentence_embedding_dimension(self):
            return 4

        def encode(self, texts, **kwargs):
            return [[0.0, 0.0, 0.0, 1.0] for _ in texts]

    class FakeCE(FakeST):
        def __init__(self, name, device=None, max_length=None, local_files_only=False,
                     **kwargs):
            super().__init__(name, device=device, local_files_only=local_files_only)

        def predict(self, pairs):
            return [0.0 for _ in pairs]

    monkeypatch.setitem(sys.modules, "sentence_transformers",
                        types.SimpleNamespace(SentenceTransformer=FakeST,
                                              CrossEncoder=FakeCE))
    monkeypatch.setattr(store_mod, "model_in_cache", lambda model_id: True)
    monkeypatch.delenv(store_mod.ALLOW_DOWNLOAD_ENV, raising=False)

    store_mod.TransformersEmbedder("BAAI/x").load()
    store_mod.TransformersReranker("BAAI/y").load()
    assert seen == {"BAAI/x": True, "BAAI/y": True}

    # 放开下载 → local_files_only 跟着变 False（显式开关，不是默认行为）
    monkeypatch.setenv(store_mod.ALLOW_DOWNLOAD_ENV, "1")
    store_mod.TransformersEmbedder("BAAI/x").load()
    assert seen["BAAI/x"] is False


def test_allow_download_env_is_opt_in_only(monkeypatch):
    for value, expected in (("", False), ("0", False), ("no", False), ("1", True),
                            ("on", True), ("TRUE", True)):
        monkeypatch.setenv(store_mod.ALLOW_DOWNLOAD_ENV, value)
        assert store_mod.allow_runtime_download() is expected
    monkeypatch.delenv(store_mod.ALLOW_DOWNLOAD_ENV, raising=False)
    assert store_mod.allow_runtime_download() is False


def test_health_distinguishes_missing_from_failed_model(tmp_path):
    """/health 要能区分「模型不在缓存」（跑 prefetch）与「加载失败」（看日志）。"""
    missing = MemoryStore(tmp_path / "missing", embed_model="nobody/no-such-model-xyz",
                          embedder=None, reranker=FakeRerank(), retry_cooldown=0,
                          model_budget=30.0)
    missing.init()
    missing.upsert([{"id": "c1", "kind": "fact", "text": "主人不吃辣"}])
    missing.search("主人不吃辣")   # BM25 照常回答，向量腿缺模型
    health = missing.health()
    assert health["models"]["embed"] == "missing"
    assert any("prefetch" in reason for reason in health["degradation"])
    missing.close()

    broken = make_store(tmp_path / "broken", embedder=FakeEmbedder(fail=True))
    broken.init()
    broken.upsert([{"id": "c1", "kind": "fact", "text": "主人不吃辣"}])
    broken.search("主人不吃辣")
    assert broken.health()["models"]["embed"] == "degraded"
    broken.close()

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
