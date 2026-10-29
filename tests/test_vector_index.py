# -*- coding: utf-8 -*-
"""Stage 2.9：向量索引（Chroma + bge）+ cross-encoder 重排的验收测试。

覆盖（对应任务书的验收标准）：

  V1 索引路径约定：索引住在 data/vectors/<角色>/（跟记忆库并排）
  V2 store **不会自己建**索引目录 —— 索引只由显式重建创建
     （这保证「跑一遍测试，仓库里的 data/ 字节不变」）
  V3 **漂移检测**：绕过 store 直接改 SQLite → 索引发现并修复
     （绝不静默过期）
  V4 **降级**：索引不可用（模型缺失 / chroma 坏了 / 索引损坏）→
     store 照常工作，召回**退回今天的数字**
  V5 **合并路径**：memory_merge 用裸 SQL 写库 → 索引仍然是正确的
  V6 **重建幂等**：重建两次结果一样；一条命令能从零重建
  V7 裁剪行为：重排分的比例门槛 + 语义地板
  V8（可选，要真模型）语义可达：「我不能吃什么」→「主人不吃辣」被召回

不需要 API key / 网络 / GUI。
默认用**假模型**（确定性、零下载）；要跑真模型：
    set DESKTOP_PET_VECTOR_TESTS=1
"""
from __future__ import annotations

import math
import re
import sqlite3
import zlib
from pathlib import Path

import pytest

from app.brain import memory as memory_mod
from app.brain.memory import MemoryStore
from app.brain.vector_index import (
    EMBED_MODEL_ID,
    RERANK_MODEL_ID,
    VectorIndex,
    digest_rows,
    index_dir_for,
    row_hash,
)


# --------------------------------------------------------------------------
# 假模型：确定性、零下载 —— 测试在**任何**机器上都能跑
# --------------------------------------------------------------------------

def _grams(text: str) -> set:
    """中文单字 + 二元组 + 英文整词（跟 memory.py 的切词口径一致）。"""
    text = text or ""
    chars = re.findall(r"[一-鿿]", text)
    grams = set(chars)
    grams |= {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}
    grams |= set(re.findall(r"[A-Za-z]+", text.lower()))
    return grams


class _FakeEmbedder:
    """确定性假向量模型：把词的哈希散到 64 维里，归一化。"""

    dim = 64

    def encode(self, texts, normalize_embeddings=True, show_progress_bar=False):
        out = []
        for text in texts:
            vec = [0.0] * self.dim
            for g in _grams(text):
                vec[zlib.crc32(g.encode("utf-8")) % self.dim] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            out.append([x / norm for x in vec])
        return out


class _FakeReranker:
    """确定性假重排模型：词面重合度（0..1）。

    真实模型是 cross-encoder；这里只用来验证**管线**（顺序 / 裁剪 / 门禁），
    语义能力由 V8（真模型）验证。
    """

    def predict(self, pairs, batch_size=32, show_progress_bar=False):
        out = []
        for query, doc in pairs:
            qg, dg = _grams(query), _grams(doc)
            union = qg | dg
            out.append((len(qg & dg) / len(union)) if union else 0.0)
        return out


class _FakeModelCache:
    """替掉 app.brain.vector_index._MODEL_CACHE（接口一致）。"""

    def __init__(self, embedder=None, reranker=None):
        self._embedder = embedder
        self._reranker = reranker
        self.embed_error = None if embedder is not None else "假模型：向量模型不可用"
        self.rerank_error = None if reranker is not None else "假模型：重排模型不可用"

    def embedder(self, model_id, blocking=True, timeout=None):
        return self._embedder

    def reranker(self, model_id, blocking=True, timeout=None):
        return self._reranker


@pytest.fixture()
def fake_models(monkeypatch):
    """装上假模型（向量 + 重排都能用）。"""
    cache = _FakeModelCache(_FakeEmbedder(), _FakeReranker())
    monkeypatch.setattr("app.brain.vector_index._MODEL_CACHE", cache)
    return cache


@pytest.fixture()
def no_models(monkeypatch):
    """装上「模型都没有」的缓存（模拟没下载 / 下载失败）。"""
    cache = _FakeModelCache(None, None)
    monkeypatch.setattr("app.brain.vector_index._MODEL_CACHE", cache)
    return cache


def _store(tmp_path, name="memory.db", contents=(), **kwargs):
    store = MemoryStore(tmp_path / name, backend_name="tfidf", **kwargs)
    for content in contents:
        store.add(content, "fact", importance=0.5)
    return store


def _rows(block: str) -> list:
    out = []
    for line in block.splitlines():
        if line.startswith("- ["):
            out.append(line.split(") ", 1)[1])
    return out


# --------------------------------------------------------------------------
# V1 · 路径约定
# --------------------------------------------------------------------------
class TestIndexPath:
    def test_index_lives_next_to_the_db_under_vectors(self):
        assert index_dir_for("data/memory_小汐.db") == Path("data/vectors/小汐")
        assert index_dir_for("data/memory.db") == Path("data/vectors/memory")
        assert index_dir_for(
            "~/.desktop-pet/user_memory.db").as_posix().endswith(
            "vectors/user_memory")

    def test_character_name_wins(self):
        assert index_dir_for("data/memory_x.db", "小汐") == Path("data/vectors/小汐")

    def test_store_reports_its_index_dir(self, tmp_path):
        store = _store(tmp_path)
        assert store.vector_index_dir == tmp_path / "vectors" / "memory"


# --------------------------------------------------------------------------
# V2 · store 不自己建索引（跑完测试 data/ 字节不变）
# --------------------------------------------------------------------------
class TestStoreDoesNotCreateIndex:
    def test_construction_and_recall_do_not_create_the_index(self, tmp_path,
                                                              fake_models):
        store = _store(tmp_path, contents=("主人明天要交房租",))
        assert not (tmp_path / "vectors").exists()
        store.system_block("明天要交房租")
        assert not (tmp_path / "vectors").exists(), \
            "召回不该顺手建索引目录（索引只由显式重建创建）"

    def test_status_reports_not_built(self, tmp_path, fake_models):
        store = _store(tmp_path, contents=("主人明天要交房租",))
        status = store.vector_index_status()
        assert status["state"] == "not_built"
        assert status["available"] is False
        assert status["embed_model"] == EMBED_MODEL_ID
        assert status["rerank_model"] == RERANK_MODEL_ID


# --------------------------------------------------------------------------
# V3 · 漂移检测（绝不静默过期）
# --------------------------------------------------------------------------
class TestDriftDetection:
    def test_row_changed_behind_the_store_is_detected_and_fixed(self, tmp_path,
                                                                 fake_models):
        db = tmp_path / "memory.db"
        store = _store(tmp_path, contents=("主人明天要交房租",))
        report = store.rebuild_vector_index(blocking=True)
        assert report["ok"] is True
        mid = store.all()[0].id

        # —— 绕过 store，直接改 SQLite（合并脚本就是这种裸 SQL 写入）——
        conn = sqlite3.connect(str(db))
        conn.execute("UPDATE memories SET content=? WHERE id=?",
                     ("主人后天要交房租", mid))
        conn.commit()
        conn.close()

        # 下一次召回必须发现漂移
        store.system_block("交房租")
        status = store.vector_index_status()
        assert status["drift"] is True, status
        assert status["sync"]["updated"] == [mid], status["sync"]
        # 而且已经修好了（索引里是新内容）
        hits = store._vector_index.query("后天交房租", k=5)
        assert [h[0] for h in hits] == [mid], hits

    def test_row_deleted_behind_the_store_is_detected(self, tmp_path,
                                                       fake_models):
        db = tmp_path / "memory.db"
        store = _store(tmp_path, contents=("主人明天要交房租",
                                           "主人的猫叫团子"))
        store.rebuild_vector_index(blocking=True)
        mid = store.all()[0].id
        conn = sqlite3.connect(str(db))
        conn.execute("DELETE FROM memories WHERE id=?", (mid,))
        conn.commit()
        conn.close()
        store.system_block("交房租")
        status = store.vector_index_status()
        assert status["drift"] is True, status
        assert status["sync"]["removed"] == [mid], status["sync"]

    def test_digest_changes_when_content_changes(self):
        row = {"id": "m1", "content": "甲", "category": "fact", "scope": "project",
               "kind": "fact", "status": "active", "topic": None}
        other = dict(row, content="乙")
        assert digest_rows([row]) != digest_rows([other])
        # 摘要与顺序无关（多行时不能因为顺序变了就误报漂移）
        assert digest_rows([row, other]) == digest_rows([other, row])

    def test_digest_changes_when_metadata_changes(self):
        row = {"id": "m1", "content": "甲", "category": "fact", "scope": "project",
               "kind": "fact", "status": "active", "topic": None}
        assert row_hash(row) != row_hash(dict(row, status="superseded"))


# --------------------------------------------------------------------------
# V4 · 降级（索引不可用 → 退回今天的数字）
# --------------------------------------------------------------------------
class TestDegradation:
    def _legacy_reference(self, tmp_path, contents, query):
        """今天的行为：只有 substring + tfidf 两腿。"""
        ref = MemoryStore(tmp_path / "ref.db", backend_name="tfidf",
                          vector_index=False)
        for content in contents:
            ref.add(content, "fact", importance=0.5)
        return _rows(ref.system_block(query))

    def test_model_missing_falls_back_to_todays_numbers(self, tmp_path,
                                                         no_models):
        contents = ("主人明天要交房租", "主人的猫叫团子", "主人喜欢喝冰美式")
        query = "明天要交房租"
        store = _store(tmp_path / "a.db", contents=contents)
        store.rebuild_vector_index(blocking=True)
        got = _rows(store.system_block(query))
        want = self._legacy_reference(tmp_path, contents, query)
        assert got == want, (got, want)

    def test_chroma_broken_falls_back_to_todays_numbers(self, tmp_path,
                                                         fake_models,
                                                         monkeypatch):
        contents = ("主人明天要交房租", "主人的猫叫团子", "主人喜欢喝冰美式")
        query = "明天要交房租"
        store = _store(tmp_path / "b.db", contents=contents)
        store.rebuild_vector_index(blocking=True)
        monkeypatch.setattr(type(store._vector_index), "open",
                            lambda self: False)
        got = _rows(store.system_block(query))
        want = self._legacy_reference(tmp_path, contents, query)
        assert got == want, (got, want)

    def test_index_corrupt_falls_back_and_never_raises(self, tmp_path,
                                                        fake_models):
        contents = ("主人明天要交房租", "主人的猫叫团子")
        store = _store(tmp_path / "c.db", contents=contents)
        store.rebuild_vector_index(blocking=True)
        # 把索引的状态文件写坏（模拟索引损坏）
        (store.vector_index_dir / "_index_state.json").write_text(
            "{ not json", encoding="utf-8")
        block = store.system_block("明天要交房租")
        assert block  # 召回照常工作，不抛异常
        assert store.last_recall_stats.get("gate") in (None, "closed")

    def test_store_construction_never_fails_because_of_the_index(self,
                                                                 tmp_path):
        # 索引模块整个坏掉 → store 照常构造
        import app.brain.memory as mm
        original = mm.VectorIndex
        try:
            mm.VectorIndex = None            # type: ignore[assignment]
            store = MemoryStore(tmp_path / "d.db", backend_name="tfidf")
            store.add("主人明天要交房租", "fact", importance=0.5)
            assert store.system_block("明天要交房租")
        finally:
            mm.VectorIndex = original         # type: ignore[assignment]


# --------------------------------------------------------------------------
# V5 · 合并路径（裸 SQL 写库 → 索引仍然正确）
# --------------------------------------------------------------------------
class TestMergePath:
    def test_reindex_after_raw_sql_write(self, tmp_path, fake_models):
        from app.brain.memory_merge import reindex_live
        db = tmp_path / "memory.db"
        store = _store(tmp_path, contents=("主人明天要交房租",))
        store.rebuild_vector_index(blocking=True)
        before = store.vector_index_status()["n_indexed"]

        # —— 模拟 memory_merge 的裸 SQL 插入 ——
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO memories (id, content, category, importance, "
            "created_at, last_access_ts, access_count, scope, kind, status) "
            "VALUES (?, ?, ?, ?, 0, 0, 0, 'project', 'fact', 'active')",
            ("mmerge01", "主人的生日是三月十号", "fact", 0.5))
        conn.commit()
        conn.close()

        report = reindex_live(db, blocking=True)
        assert report["ok"] is True, report
        after = store.vector_index_status()
        assert after["n_indexed"] == before + 1, after
        hits = store._vector_index.query("生日", k=5)
        assert "mmerge01" in [h[0] for h in hits], hits

    def test_store_detects_the_merge_drift_on_next_recall(self, tmp_path,
                                                          fake_models):
        db = tmp_path / "memory.db"
        store = _store(tmp_path, contents=("主人明天要交房租",))
        store.rebuild_vector_index(blocking=True)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO memories (id, content, category, importance, "
            "created_at, last_access_ts, access_count, scope, kind, status) "
            "VALUES (?, ?, ?, ?, 0, 0, 0, 'project', 'fact', 'active')",
            ("mmerge02", "主人的生日是三月十号", "fact", 0.5))
        conn.commit()
        conn.close()
        store.system_block("交房租")
        status = store.vector_index_status()
        assert status["sync"]["added"] == ["mmerge02"], status["sync"]
        # store 自己也看到了新行（内存按库重载）
        assert any(i.id == "mmerge02" for i in store.all())


# --------------------------------------------------------------------------
# V6 · 重建幂等 / 一条命令能重建
# --------------------------------------------------------------------------
class TestRebuild:
    def test_rebuild_is_idempotent(self, tmp_path, fake_models):
        store = _store(tmp_path, contents=("主人明天要交房租", "主人的猫叫团子"))
        first = store.rebuild_vector_index(blocking=True)
        digest1 = store.vector_index_status()["digest"]
        second = store.rebuild_vector_index(blocking=True)
        digest2 = store.vector_index_status()["digest"]
        assert first["ok"] and second["ok"]
        assert digest1 == digest2, (digest1, digest2)
        assert store.vector_index_status()["n_indexed"] == 2

    def test_rebuild_is_from_scratch(self, tmp_path, fake_models):
        """重建是「从零」：索引里多出来的行会被清掉。"""
        store = _store(tmp_path, contents=("主人明天要交房租",))
        store.rebuild_vector_index(blocking=True)
        # 往索引里塞一条**库里没有**的行（模拟索引脏了）
        idx = store._vector_index
        idx._collection.upsert(
            ids=["mghost"], embeddings=[[0.1] * 64], documents=["幽灵行"],
            metadatas=[{"chash": "ghost", "category": "fact", "scope": "project",
                        "kind": "fact", "status": "active", "topic": ""}])
        assert idx.count() == 2
        report = store.rebuild_vector_index(blocking=True)
        assert report["ok"] is True, report
        assert store.vector_index_status()["n_indexed"] == 1
        assert idx.count() == 1, "重建必须清掉索引里多余的行"

    def test_cli_rebuild_is_idempotent(self, tmp_path, fake_models):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "rebuild_vector_index_cli",
            Path(__file__).resolve().parent.parent / "scripts" /
            "rebuild_vector_index.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        db = tmp_path / "memory.db"
        store = _store(tmp_path, contents=("主人明天要交房租",))
        r1 = mod.rebuild_one(db, quiet=True)
        r2 = mod.rebuild_one(db, quiet=True)
        assert r1["ok"] and r2["ok"], (r1, r2)
        assert r1["count"] == r2["count"] == 1


# --------------------------------------------------------------------------
# V7 · 裁剪行为（重排分的比例门槛 + 语义地板）
# --------------------------------------------------------------------------
class TestCutRules:
    def test_rerank_orders_by_score(self, tmp_path, fake_models):
        store = _store(tmp_path, contents=("主人明天要交房租",
                                           "主人的猫叫团子"))
        store.rebuild_vector_index(blocking=True)
        rows = _rows(store.system_block("明天要交房租"))
        assert rows and "房租" in rows[0], rows

    def test_cut_ratio_knob_is_honoured(self, tmp_path, fake_models):
        store = _store(tmp_path, contents=("主人明天要交房租",
                                           "主人的猫叫团子"))
        store.rebuild_vector_index(blocking=True)
        store.rerank_cut_ratio = 0.0
        wide = _rows(store.system_block("明天要交房租"))
        store.rerank_cut_ratio = 1.0
        narrow = _rows(store.system_block("明天要交房租"))
        assert len(narrow) <= len(wide)

    def test_semantic_floor_keeps_a_lexical_hit_that_reranker_hates(self,
                                                                      tmp_path,
                                                                      fake_models):
        """复合问句：cross-encoder 对第二条腿给分很低，但它是问题的一半。"""
        store = _store(tmp_path, contents=("主人每个月15号交房租",
                                           "主人下周要去医院做体检复查甲状腺"))
        store.rebuild_vector_index(blocking=True)
        rows = _rows(store.system_block("我什么时候交房租还有体检的事"))
        assert any("体检" in r for r in rows), rows


# --------------------------------------------------------------------------
# V9 · 共享索引 + 状态一致性（**回归测试**）
# --------------------------------------------------------------------------
# 这两条钉的是 Stage 2.9 修掉的两个 bug：
#   ① 一个 store 重建索引，把**另一个** store 的视图弄瞎了
#      （rebuild() 以前 delete_collection() → 别的句柄失效 → count()==0）；
#   ② 状态字段 §state§ 跟同一份 payload 里的 §n_indexed§ 打架
#      （state 是「本实例建没建过」推出来的，不是索引的真实状态）。
class TestSharedIndexAndState:
    @staticmethod
    def _assert_consistent(status: dict) -> None:
        """状态字段**不许跟同一份 payload 里的事实字段打架**。"""
        state = status["state"]
        n = int(status["n_indexed"] or 0)
        if n > 0:
            assert state != "not_built", status
        if state == "not_built":
            assert status["built"] is False and n == 0, status
        if state == "built":
            assert status["built"] is True, status
            assert status["in_sync"] is True, status
        if state == "drifted":
            assert status["in_sync"] is False, status
        if state == "disabled":
            assert status["enabled"] is False, status
        if state == "unavailable":
            assert status["error"], status

    def test_rebuild_by_one_store_keeps_the_other_store_view(self, tmp_path,
                                                              fake_models):
        """**回归**：A2 —— 一个 store 重建，不能把另一个 store 的索引弄瞎。

        根因：rebuild() 以前会 delete_collection() → **别的句柄全部失效** →
        那个 store 的 count() 变成 0（索引「凭空消失」）。
        """
        db = tmp_path / "memory.db"
        store_a = MemoryStore(db, backend_name="tfidf")
        store_a.add("主人明天要交房租", "fact", importance=0.5)
        store_b = MemoryStore(db, backend_name="tfidf")      # 第二个实例
        store_a.rebuild_vector_index(blocking=True)
        assert store_a.vector_index_status()["n_indexed"] == 1
        assert store_b.vector_index_status()["n_indexed"] == 1

        # b 重建（= memory_merge 的 reindex_live 干的事）
        store_b.add("主人的猫叫团子", "fact", importance=0.5)
        report = store_b.rebuild_vector_index(blocking=True)
        assert report["ok"] is True, report

        # a 的视图**必须**还是对的（不是 0！）
        st_a = store_a.vector_index_status()
        assert st_a["n_indexed"] == 2, st_a
        assert store_a._vector_index.count() == 2, "a 看到的索引不能是空的"
        assert store_b.vector_index_status()["n_indexed"] == 2
        # 两边用的是**同一个**索引实例（同一个客户端、同一个 collection 句柄）
        assert store_a._vector_index is store_b._vector_index
        # 重建是幂等的：a 再重建一次，视图还是 2
        store_a.rebuild_vector_index(blocking=True)
        assert store_a.vector_index_status()["n_indexed"] == 2

    def test_rebuild_keeps_the_collection_handle(self, tmp_path, fake_models):
        """重建**不删 collection** → collection 的身份（句柄）不变。"""
        store = _store(tmp_path, contents=("主人明天要交房租",))
        store.rebuild_vector_index(blocking=True)
        idx = store._vector_index
        handle = idx._collection
        assert handle is not None
        store.rebuild_vector_index(blocking=True)
        assert idx._collection is handle, "重建不该换掉 collection 句柄"
        assert idx.count() == 1

    def test_state_never_says_not_built_while_rows_are_indexed(self, tmp_path,
                                                                fake_models):
        """**回归**：A3 —— state='not_built' 与 n_indexed>0 不能同时出现。"""
        db = tmp_path / "memory.db"
        store = _store(tmp_path, contents=("主人明天要交房租",))
        # ① 还没建索引 → not_built + n_indexed == 0（自洽）
        st = store.vector_index_status()
        assert st["state"] == "not_built", st
        assert st["n_indexed"] == 0, st
        self._assert_consistent(st)
        # ② 建好之后 → **绝不能**再是 not_built
        store.rebuild_vector_index(blocking=True)
        st = store.vector_index_status()
        assert st["n_indexed"] == 1, st
        assert st["state"] != "not_built", st
        self._assert_consistent(st)
        # ③ 别的 store 重建之后也一样
        other = MemoryStore(db, backend_name="tfidf")
        other.add("主人的猫叫团子", "fact", importance=0.5)
        other.rebuild_vector_index(blocking=True)
        st = store.vector_index_status()
        assert st["n_indexed"] == 2, st
        assert st["state"] != "not_built", st
        self._assert_consistent(st)

    def test_status_does_not_serve_a_stale_count(self, tmp_path, fake_models):
        """**回归**：别的 store 重建之后，本 store 的状态**不能**是缓存的旧值。"""
        db = tmp_path / "memory.db"
        store_a = _store(tmp_path, contents=("主人明天要交房租",))
        store_b = MemoryStore(db, backend_name="tfidf")
        store_a.rebuild_vector_index(blocking=True)
        assert store_a.vector_index_status()["n_indexed"] == 1
        store_b.add("主人的猫叫团子", "fact", importance=0.5)
        store_b.rebuild_vector_index(blocking=True)
        st = store_a.vector_index_status()
        assert st["n_indexed"] == 2, st          # 不是缓存的 1
        assert st["n_source"] == 2, st
        assert st["in_sync"] is True, st
        assert st["state"] == "built", st

    def test_drift_is_reported_not_silently_stale(self, tmp_path, fake_models):
        """源库被裸 SQL 改过 → 状态必须说「漂移」，不能装作没事。"""
        db = tmp_path / "memory.db"
        store = _store(tmp_path, contents=("主人明天要交房租",))
        store.rebuild_vector_index(blocking=True)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO memories (id, content, category, importance, "
            "created_at, last_access_ts, access_count, scope, kind, status) "
            "VALUES (?, ?, ?, ?, 0, 0, 0, 'project', 'fact', 'active')",
            ("mraw01", "主人的生日是三月十号", "fact", 0.5))
        conn.commit()
        conn.close()
        st = store.vector_index_status()
        assert st["drift"] is True, st
        assert st["in_sync"] is False, st
        assert st["state"] == "drifted", st
        self._assert_consistent(st)
        # 下一次召回必须把它修好（漂移绝不静默过期）
        store.system_block("交房租")
        st = store.vector_index_status()
        assert st["in_sync"] is True, st
        assert st["sync"]["added"] == ["mraw01"], st["sync"]


# --------------------------------------------------------------------------
# V8 · 语义可达（真模型；默认跳过）
# --------------------------------------------------------------------------
def _real_models_available() -> bool:
    if not memory_mod:          # pragma: no cover
        return False
    try:
        from huggingface_hub import constants as hf_constants
    except Exception:           # pragma: no cover
        return False
    cache = Path(hf_constants.HF_HUB_CACHE)
    return all((cache / ("models--" + m.replace("/", "--"))).is_dir()
               for m in (EMBED_MODEL_ID, RERANK_MODEL_ID))


@pytest.mark.skipif(
    not _real_models_available(),
    reason="需要本地已缓存 bge-small-zh-v1.5 / bge-reranker-base")
def test_v8_semantic_case_is_recalled(tmp_path):
    """「我不能吃什么」→「主人不吃辣」：零字面重叠，但语义上一模一样。"""
    store = _store(tmp_path, contents=("主人不吃辣", "主人的猫叫团子"))
    store.rebuild_vector_index(blocking=True)
    rows = _rows(store.system_block("我不能吃什么"))
    assert any("不吃辣" in r for r in rows), rows
