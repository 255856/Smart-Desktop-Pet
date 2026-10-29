"""Stage 2.8：召回**精度**的验收测试（注入条数 / 相关度裁剪 / 确定性 / 阈值门禁）。

背景（scripts/eval_memory_recall.py 实测）：
    旧行为每轮硬塞 6 条记忆，而 6 条里平均只有 ~1.1 条真的跟这句话有关 ——
    问「记不记得我明天要交房租」会把「主人怕黑」「主人讨厌香菜」一起塞进来。
    代价是 token 浪费 + 桌宠**自信说错话**的风险（近似陷阱）。

这里钉死的行为：
    P1 注入条数：默认 4（可配置），且只注入「离顶行够近」的行
    P2 裁剪：离顶行太远的行 = 噪声 → 不注入；顶行永远注入
    P3 确定性：同一 query + 同一库 → 连续两次结果逐条相同
    P4 顺序不由随机 id 决定（生产里 id 是 uuid4）
    P5 门禁是**阈值**判断：微小的正相似度不算「有关系」
    P6 fallback（无关问题 → 按重要性 top-2）仍然生效
    P7 免责句文案一字不改；记忆块仍然走易变通道（不进 system prompt）

不需要 API key / 网络 / GUI。
"""
from __future__ import annotations

import pytest

from app.brain.memory import (
    DEFAULT_FILLER_RATIO,
    DEFAULT_MAX_INJECT,
    SEMANTIC_GATE_THRESHOLD,
    MemoryStore,
)

# ----------------------------
# 语料
# ----------------------------

#: 「明天」主题（真答案）+ 弱相关（共享一个词）+ 完全不相关
CONTENTS = (
    "主人明天要交房租",       # 真答案
    "主人明天要去超市买菜",    # 弱相关（共享「明天」）
    "主人的猫叫团子",         # 完全不相关
)
QUESTION = "我明天要交房租"


def _install_semantic(store: MemoryStore, sim: float) -> None:
    """给库装上假语义后端（已 fit 好），并冻结后端选择。

    为什么要手动装：生产里 sentence-transformers 没装 → 融合降级到两腿，
    门禁的语义分支根本走不到。要**测**那条分支，只能手工把后端塞进去。
    """
    # 顺序很重要：先让向量索引刷新（add() 之后索引是脏的，
    # 刷新会把 _fusion_ready 置回 False → 假后端被重建掉）
    store._ensure_backend_fresh()
    backend = _TinySemanticBackend(sim)
    backend.fit([i.content for i in store.all()])
    store._fusion_sbert = backend
    store._fusion_ready = True


def _rows(block: str) -> list[str]:
    """从记忆块里取出注入的记忆正文（"- [类别★重要性] (时间) 正文"）。"""
    out = []
    for line in block.splitlines():
        if line.startswith("- ["):
            out.append(line.split(") ", 1)[1])
    return out


def _store(tmp_path, name="m.db", contents=CONTENTS, **kwargs):
    s = MemoryStore(tmp_path / name, backend_name="tfidf", **kwargs)
    for content in contents:
        s.add(content, "fact", importance=0.5)
    return s


class _TinySemanticBackend:
    """假语义后端：对任何 query 都只给一个**极小的正相似度**。

    真 embedding 就是这个脾气：任何 query 都能找到一个「有点像」的邻居，
    相似度还是正的。所以门禁不能写成「> 0 即算有关系」。
    """

    name = "sbert"

    def __init__(self, sim: float = 0.01) -> None:
        self.sim = float(sim)
        self._doc_vecs: list[list[float]] = []

    def fit(self, docs: list[str]) -> None:
        self._doc_vecs = [[1.0] for _ in docs]

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] for _ in texts]

    def similarity(self, query_vec, doc_vecs) -> list[float]:
        return [self.sim for _ in doc_vecs]


# ----------------------------
# P1：注入条数
# ----------------------------


def test_p1_default_max_inject_is_four():
    """默认注入条数从 6 收到 4（实测：3 会把排第 4 的真答案裁掉）。"""
    assert DEFAULT_MAX_INJECT == 4
    assert MemoryStore.__init__.__defaults__[0] == DEFAULT_MAX_INJECT


def test_p1_injection_never_exceeds_max_inject(tmp_path):
    s = _store(tmp_path)
    assert s.max_inject == DEFAULT_MAX_INJECT
    assert len(_rows(s.system_block(QUESTION))) <= DEFAULT_MAX_INJECT


def test_p1_max_inject_is_configurable(tmp_path):
    s = _store(tmp_path, "m2.db", max_inject=2)
    rows = _rows(s.system_block(QUESTION))
    assert len(rows) == 2, rows
    assert rows[0] == "主人明天要交房租"


# ----------------------------
# P2：相关度裁剪
# ----------------------------


def test_p2_far_filler_rows_are_not_injected(tmp_path):
    """离顶行太远的行 = 噪声 → 不注入（省 token + 防止桌宠说错话）。"""
    s = _store(tmp_path)
    rows = _rows(s.system_block(QUESTION))
    assert "主人的猫叫团子" not in rows, rows
    assert rows[0] == "主人明天要交房租"


def test_p2_top_row_is_always_injected(tmp_path):
    """门槛调到 1.0（只认完全并列的）→ 顶行仍然注入（不能一条都不给）。"""
    s = _store(tmp_path, "m2.db", filler_ratio=1.0)
    rows = _rows(s.system_block(QUESTION))
    assert rows, "顶行永远注入"
    assert rows[0] == "主人明天要交房租"


def test_p2_filler_ratio_is_configurable(tmp_path):
    """门槛 = 0 → 不裁剪（回到「有多少候选就塞多少」的老行为）。"""
    loose = _store(tmp_path, "loose.db", filler_ratio=0.0)
    tight = _store(tmp_path, "tight.db", filler_ratio=DEFAULT_FILLER_RATIO)
    assert len(_rows(loose.system_block(QUESTION))) > \
        len(_rows(tight.system_block(QUESTION)))


def test_p2_cut_does_not_drop_the_answer(tmp_path):
    """裁剪不能把真答案裁掉（只有噪声该被裁）。"""
    s = _store(tmp_path)
    rows = _rows(s.system_block(QUESTION))
    assert "主人明天要交房租" in rows
    assert rows.index("主人明天要交房租") == 0


# ----------------------------
# P3 / P4：确定性 + 顺序不由 id 决定
# ----------------------------


def test_p3_same_query_same_store_same_rows(tmp_path):
    """同一 query + 同一库 → 连续两次注入结果逐条相同。"""
    s = _store(tmp_path)
    first = _rows(s.system_block(QUESTION))
    second = _rows(s.system_block(QUESTION))
    assert first == second, (first, second)


def _inject_with_ids(db, contents, ids) -> list[str]:
    s = MemoryStore(db, backend_name="tfidf")
    for content, mid in zip(contents, ids, strict=False):
        item = s.add(content, "fact", importance=0.5)
        item.id = mid
    s._save()
    return _rows(s.system_block(QUESTION))


def test_p4_injected_order_is_not_by_id(tmp_path):
    """id 随机（生产是 uuid4）时，注入顺序依然稳定、且不是按 id 排的。

    做法：故意把 id 排成「相关性倒序」（最不相关的行拿最小 id），
    两套 id 完全颠倒 → 注入顺序必须一模一样。
    """
    ids = ("ffffffffffffffff", "eeeeeeeeeeeeeeee", "0000000000000000")
    order_a = _inject_with_ids(tmp_path / "a.db", CONTENTS, ids)
    order_b = _inject_with_ids(tmp_path / "b.db", CONTENTS, tuple(reversed(ids)))
    assert order_a == order_b, (order_a, order_b)
    assert order_a[0] == "主人明天要交房租", order_a


def test_p4_repeated_runs_with_random_ids_are_stable(tmp_path):
    """把 id 换成随机 uuid 跑多次 → 注入内容与顺序都稳定。"""
    seen = []
    for i in range(5):
        s = _store(tmp_path, "m%d.db" % i)
        for item, content in zip(s.all(), CONTENTS, strict=False):
            item.id = "%032x" % (i * 7919 + hash(content) % 100000)
        seen.append(tuple(_rows(s.system_block(QUESTION))))
    assert len(set(seen)) == 1, seen


# ----------------------------
# P5：门禁是阈值判断（不是「> 0」）
# ----------------------------


def test_p5_tiny_positive_similarity_is_not_relevant(tmp_path):
    """一个**微小正相似度**不算「跟记忆库有关系」。"""
    s = _store(tmp_path)
    items = list(s.all())
    # 语义腿给 0.01 的正相似度（真 embedding 的典型值）
    assert s._query_matches_anything("今天股市涨了吗", items, [], 0.01) is False
    # 达到阈值才算有关系
    assert s._query_matches_anything("今天股市涨了吗", items, [],
                                     SEMANTIC_GATE_THRESHOLD) is True
    assert SEMANTIC_GATE_THRESHOLD > 0


def test_p5_unrelated_query_with_tiny_similarity_still_falls_back(tmp_path):
    """端到端：语义腿一直给正相似度 → 无关问题照样走 fallback。"""
    s = _store(tmp_path)
    _install_semantic(s, 0.01)
    rows = _rows(s.system_block("今天股市涨了吗"))
    assert len(rows) == 2, rows              # fallback：按重要性 top-2
    assert s.last_fusion_backends == (), s.last_fusion_backends


def test_p5_semantic_similarity_above_threshold_opens_the_gate(tmp_path):
    """语义相似度够高 → 门禁放行（语义腿是有用的，不是摆设）。"""
    s = _store(tmp_path)
    _install_semantic(s, 0.9)
    rows = _rows(s.system_block("今天股市涨了吗"))
    assert rows, rows
    assert "sbert" in s.last_fusion_backends, s.last_fusion_backends


# ----------------------------
# P6：fallback 仍然生效
# ----------------------------


def test_p6_unrelated_question_injects_at_most_two(tmp_path):
    s = _store(tmp_path)
    rows = _rows(s.system_block("量子力学怎么学"))
    assert len(rows) == 2, rows


def test_p6_fallback_is_top2_by_importance(tmp_path):
    s = MemoryStore(tmp_path / "m.db", backend_name="tfidf")
    for i, content in enumerate(CONTENTS):
        s.add(content, "fact", importance=0.1 + i * 0.1)
    rows = _rows(s.system_block("zzz 完全无关的词 qqq"))
    assert len(rows) == 2, rows
    top2 = [i.content for i in sorted(s.all(), key=lambda x: -x.importance)[:2]]
    assert set(rows) == set(top2), (rows, top2)


# ----------------------------
# P7：文案与通道不变
# ----------------------------


def test_p7_disclaimer_wording_unchanged(tmp_path):
    s = _store(tmp_path)
    block = s.system_block(QUESTION)
    assert block.startswith(
        "【你记得的事】（按你刚才这句话检索出来的，可能相关也可能不准；用不上就别硬扯）")


def test_p7_memory_block_stays_in_volatile_channel(tmp_path):
    """记忆块走易变通道（独立 user 消息），不进 system prompt。

    Stage 2.1 + P0-11 不变量：记忆块每轮都变，放 system prompt 会打掉
    MiniMax 的前缀缓存。见 tests/test_prompt_cache_stability.py。
    """
    from app.brain import prompt_layer

    s = _store(tmp_path)
    old_provider = prompt_layer._memory_block_provider
    prompt_layer.register_memory_block_provider(
        lambda query="": s.system_block(query))
    try:
        stable = prompt_layer.build_dynamic_block()
        volatile = prompt_layer.build_turn_context(query=QUESTION)
    finally:
        prompt_layer.register_memory_block_provider(old_provider)
    assert "房租" not in stable
    assert "房租" in volatile
    assert "【你记得的事】" in volatile


def test_p7_fusion_status_reports_the_new_knobs(tmp_path):
    s = _store(tmp_path)
    status = s.fusion_status()
    assert status["max_inject"] == DEFAULT_MAX_INJECT
    assert status["filler_ratio"] == DEFAULT_FILLER_RATIO
    assert status["semantic_gate_threshold"] == SEMANTIC_GATE_THRESHOLD


def test_p7_last_recall_stats_expose_the_noise_floor(tmp_path):
    """可观测：注入了几条、最弱那条离顶行多近（近似陷阱的早期信号）。"""
    s = _store(tmp_path)
    rows = _rows(s.system_block(QUESTION))
    stats = s.last_recall_stats
    assert stats["n_injected"] == len(rows), stats
    assert 0.0 < stats["min_injected_ratio"] <= 1.0, stats
    assert s.fusion_status()["last_recall"]["n_injected"] == stats["n_injected"]


def test_p7_gate_closed_is_visible(tmp_path):
    """门禁把无关问题挡掉时，也要能从状态里看出来（走了 fallback）。"""
    s = _store(tmp_path)
    s.system_block("量子力学怎么学")
    assert s.last_recall_stats.get("gate") == "closed", s.last_recall_stats


def test_p7_candidate_scores_are_a_pure_function(tmp_path):
    """融合分是纯函数：同样的候选 → 同样的分数（确定性打分，不含随机）。"""
    s = _store(tmp_path)
    s._search_fused(QUESTION, limit=4)
    items = list(s.all())
    fused = {i.id: 1.0 / 61.0 for i in items}
    leg_rel = {"substring": {i.id: 0.5 for i in items}}
    by_id = {i.id: i for i in items}
    first = s._candidate_scores(by_id, fused, leg_rel)
    second = s._candidate_scores(by_id, fused, leg_rel)
    assert first == second
    assert all(isinstance(v, float) for v in first.values())
