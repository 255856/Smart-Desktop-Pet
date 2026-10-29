"""Stage 2.1：长期记忆**按 query 召回**（RRF 融合）的验收测试。

背景（PLAN 2.1 的核心 bug）：
    注入路径 _select_for_injection() 用 _score(i, 0.0) 打分 —— semantic 恒为 0，
    于是注入的 20 条跟这一轮主人说了什么**毫无关系**（固定 top-N 倾倒）。

这里钉死的行为：
    A1 相关问题 → 注入 ≤ max_inject 条（Stage 2.8：6 → 4），其中相关 ≥ 2、无关 ≤ 2
    A2 无关问题 → 注入 ≤ 2 条（走 fallback）
    A3 连续 10 轮 → 注入内容随输入变化
    A4 融合路径**不被** _MIN_HIT 预过滤（分数 < 0.10 的候选照样进）
    A5 search() 的默认 min_hit 仍是 0.10（对外行为零变化）
    A6 sentence-transformers 不可用时融合照跑，并报出降级
    A7 fallback 是「按重要性 top-2」，不是 top-20

不需要 API key / 网络 / GUI。
"""
from __future__ import annotations

import inspect

import pytest

from app.brain.memory import MemoryStore

# ----------------------------
# 语料：20 条事实，问题只跟其中 2 条相关
# ----------------------------

#: 20 条事实。前 6 条跟问题有词面关联（其中 3 条是「房租」正主题），
#: 后 14 条跟问题的用词**完全**不沾（连一个相同的字都没有）。
FACTS = [
    "主人明天要交房租",            # 正主题
    "主人每个月底都要交房租",       # 正主题
    "主人每天早上七点起床",         # 词面关联（天）
    "主人下周三要去医院复查",       # 词面关联（要）
    "主人明天要去超市买菜",         # 词面关联（明天）
    "主人说交房租要提前准备好现金",   # 词面关联（交/要）
    "主人的猫叫团子",
    "主人喜欢喝冰美式",
    "主人讨厌香菜",
    "孩子在读小学三年级",
    "主人的老板姓王",
    "主人家里有一只柯基",
    "主人去年去了北海道",
    "主人会弹吉他",
    "主人养了三盆多肉",
    "主人的车是白色的",
    "主人对芒果过敏",
    "主人的狗叫豆豆",
    "主人朋友叫小李",
    "主人的生日是三月十号",
]

QUESTION = "记不记得我明天要交房租"
TOPIC = "房租"


def _rows(block: str) -> list[str]:
    """从记忆块里取出注入的记忆正文（"- [类别★重要性] (时间) 正文"）。"""
    out = []
    for line in block.splitlines():
        if line.startswith("- ["):
            out.append(line.split(") ", 1)[1])
    return out


def _overlaps(text: str, query: str) -> bool:
    """正文跟问题有没有词面关联（共享任何一个字）。"""
    return bool(set(text) & set(query))


@pytest.fixture()
def store(tmp_path):
    s = MemoryStore(tmp_path / "memory.db", backend_name="tfidf")
    for content in FACTS:
        s.add(content, "fact", importance=0.5)
    return s


# ----------------------------
# A1：相关问题 → 6 条，相关 ≥ 2、无关 ≤ 2
# ----------------------------


def test_a1_related_question_injects_capped_relevant_rows(store):
    """Stage 2.8：注入条数 6 → 4（可配置），且只注入「离顶行够近」的行。

    ⚠️ 旧断言是「应注入 6 条」—— 那正是这次改造要废掉的行为：
    6 条里平均只有 ~1.1 条真的跟问题有关（见 scripts/eval_memory_recall.py）。
    新的契约：条数有上限（max_inject=4）、相关行一个不丢、无关行 ≤ 2。
    """
    block = store.system_block(QUESTION)
    rows = _rows(block)
    assert len(rows) == 4, f"应注入 4 条（max_inject），实际 {len(rows)}：{rows}"
    related = [r for r in rows if TOPIC in r]
    irrelevant = [r for r in rows if not _overlaps(r, QUESTION)]
    assert len(related) >= 2, f"相关条目 ≥ 2，实际 {len(related)}：{rows}"
    assert len(irrelevant) <= 2, f"无关条目 ≤ 2，实际 {len(irrelevant)}：{rows}"
    # 正主题必须排在最前面（融合分高的在前）
    assert TOPIC in rows[0]


def test_a1_injection_is_not_twenty(store):
    """回归：老行为是固定 20 条倾倒；Stage 2.8 之后是 4 条（可配置）。"""
    assert len(_rows(store.system_block(QUESTION))) == 4
    assert store.max_inject == 4


def test_a1_ranking_puts_topic_first(store):
    rows = _rows(store.system_block(QUESTION))
    assert TOPIC in rows[0]


# ----------------------------
# A2 / A7：无关问题 → fallback（top-2 by importance）
# ----------------------------


def test_a2_unrelated_question_injects_at_most_two(store):
    block = store.system_block("今天股市涨了吗 量子力学怎么学")
    rows = _rows(block)
    assert len(rows) <= 2, f"无关问题注入 ≤ 2 条，实际 {len(rows)}：{rows}"


def test_a7_fallback_is_top2_by_importance(tmp_path):
    """fallback = 按重要性取 top-2（不是 top-20）。"""
    s = MemoryStore(tmp_path / "memory.db", backend_name="tfidf")
    for i, content in enumerate(FACTS):
        s.add(content, "fact", importance=0.1 + i * 0.01)   # 越大越重要
    rows = _rows(s.system_block("zzz 完全无关的词 qqq"))
    assert len(rows) == 2, rows
    # 两条必须是重要性最高的两条
    top2 = [i.content for i in sorted(s.all(), key=lambda x: -x.importance)[:2]]
    assert set(rows) == set(top2), (rows, top2)


def test_a7_fallback_fires_when_nothing_matches(store):
    """融合召回一条都没有时，注入数掉到 2（而不是 20）。"""
    rows = _rows(store.system_block("量子力学怎么学"))
    assert len(rows) == 2, rows


# ----------------------------
# A3：连续 10 轮，注入内容随输入变化
# ----------------------------


def test_a3_injection_changes_with_input(store):
    queries = [
        "记不记得我明天要交房租",
        "主人的猫叫什么",
        "主人喜欢喝什么",
        "讨厌吃香菜吗",
        "孩子在几年级",
        "老板姓什么",
        "家里养了几只狗",
        "去年去了哪里旅游",
        "会弹什么乐器",
        "养了多少盆花",
    ]
    seen = []
    for q in queries:
        rows = _rows(store.system_block(q))
        assert rows, f"query {q!r} 应该召回至少一条"
        seen.append(tuple(rows))
    # 不是固定 top-N 倾倒：10 轮里至少 8 种不同的注入内容
    assert len(set(seen)) >= 8, seen
    # 相邻两轮也不能完全一样
    for i in range(len(seen) - 1):
        assert seen[i] != seen[i + 1], f"第 {i + 1}/{i + 2} 轮注入内容相同：{seen[i]}"


def test_a3_consecutive_turns_track_the_topic(store):
    """同一会话连续问两件事，注入内容跟着变（**首条**跟着 query 变）。

    只断言首条：融合结果靠 id 兜底排序，第 6 条（补位那条）是随机 id
    决定的 —— 断言"整组里不含某个词"会随机翻车。
    """
    first = _rows(store.system_block("记不记得我明天要交房租"))
    second = _rows(store.system_block("主人的猫叫什么"))
    assert first != second
    assert "房租" in first[0], first
    assert "猫" in second[0], second


# ----------------------------
# A4：融合路径不受 _MIN_HIT 预过滤
# ----------------------------


def test_a4_fused_path_ignores_min_hit(tmp_path):
    """分数低于 0.10 的候选，在融合路径里照样进（老 search() 会滤掉它）。"""
    s = MemoryStore(tmp_path / "memory.db", backend_name="tfidf")
    target = s.add("主人喜欢喝冰美式", "preference", importance=0.5)
    s.add("主人养了三盆多肉", "other", importance=0.5)
    s.add("主人的猫叫团子", "other", importance=0.5)
    # 这条记忆很长 → query 跟它只共享「主人」时语义分很低（< 0.10）
    s.add("量子力学和弦理论是近代物理学的两大基石也是我的专业方向", "fact",
          importance=0.5)
    query = ("量子力学和弦理论是近代物理学的两大基石也是我的专业方向 "
             "主人明天要交房租顺便去菜市场买排骨和香菜还有水电燃气"
             "物业取暖费宽带费手机费")
    s._ensure_backend_fresh()      # 索引标脏 → 先重建再算分
    sims = s.backend.similarity(s.backend.encode([query])[0],
                                s.backend._doc_vecs)
    score = sims[0]
    assert 0 < score < 0.10, f"前置条件：目标语义分应 < 0.10，实际 {score}"
    # 老路径：被 _MIN_HIT（0.10）预过滤掉
    assert target.id not in [i.id for i in s.search(query)], \
        "老 search() 应该滤掉这条"
    # 融合路径：照样召回（融合输入不被预过滤）
    fused = s._search_fused(query, limit=6)
    assert target.id in [i.id for i in fused], [i.content for i in fused]


def test_a4_search_with_min_hit_zero_keeps_low_score(tmp_path):
    """对照组：search(min_hit=0) 不做预过滤 —— 融合路径就是这一档。"""
    s = MemoryStore(tmp_path / "memory.db", backend_name="tfidf")
    s.add("主人喜欢喝冰美式", "preference", importance=0.5)
    s.add("主人养了三盆多肉", "other", importance=0.5)
    s.add("主人的猫叫团子", "other", importance=0.5)
    s.add("量子力学和弦理论是近代物理学的两大基石也是我的专业方向", "fact",
          importance=0.5)
    query = ("量子力学和弦理论是近代物理学的两大基石也是我的专业方向 "
             "主人明天要交房租顺便去菜市场买排骨和香菜还有水电燃气"
             "物业取暖费宽带费手机费")
    s._ensure_backend_fresh()
    sims = s.backend.similarity(s.backend.encode([query])[0],
                                s.backend._doc_vecs)
    assert sims[0] < 0.10
    assert s.add("主人喜欢喝冰美式", "preference").id not in [
        i.id for i in s.search(query)]
    assert s.search(query, min_hit=0.0) != []


# ----------------------------
# A5：search() 的默认行为不变
# ----------------------------


def test_a5_search_default_min_hit_is_still_010(tmp_path):
    """老调用方不传 min_hit → 行为跟以前一模一样（0.10 阈值）。"""
    s = MemoryStore(tmp_path / "memory.db", backend_name="tfidf")
    s.add("主人的猫叫团子", "fact", importance=0.5)
    s.add("孩子在读小学三年级", "fact", importance=0.5)
    # 无关 query：老行为 = 一条都不召回
    assert s.search("量子力学") == []
    assert s.search("zzzz不存在的词") == []
    # 签名默认值就是 None（= 老行为）
    sig = inspect.signature(MemoryStore.search)
    assert sig.parameters["min_hit"].default is None


def test_a5_search_default_keeps_related_hits(tmp_path):
    s = MemoryStore(tmp_path / "memory.db", backend_name="tfidf")
    s.add("主人的猫叫团子", "fact", importance=0.5)
    res = s.search("团子")
    assert any("团子" in r.content for r in res)


# ----------------------------
# A6：sbert 不可用 → 降级到 tfidf，融合照跑
# ----------------------------


def test_a6_fusion_works_without_sentence_transformers(tmp_path, monkeypatch):
    monkeypatch.setattr("app.brain.memory._sbert_available", lambda: False)
    s = MemoryStore(tmp_path / "memory.db", backend_name="tfidf")
    s.add("主人明天要交房租", "fact", importance=0.5)
    s.add("主人的猫叫团子", "fact", importance=0.5)
    fused = s._search_fused("明天要交房租", limit=6)
    assert fused, "sbert 不可用时融合仍应召回"
    assert "sbert" in s.last_fusion_degraded
    assert "tfidf" in s.last_fusion_backends
    assert "substring" in s.last_fusion_backends


def test_a6_real_environment_reports_contributing_backends(store):
    """本机（没装 sentence-transformers）应报出实际参与的后端。"""
    store._search_fused(QUESTION, limit=6)
    assert store.last_fusion_backends, "应报出参与融合的后端"
    assert "tfidf" in store.last_fusion_backends
    # 没装 sbert 时必须报降级
    try:
        import sentence_transformers  # noqa: F401
    except Exception:
        assert "sbert" in store.last_fusion_degraded


def test_a6_degradation_is_logged(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr("app.brain.memory._sbert_available", lambda: False)
    import logging
    with caplog.at_level(logging.INFO, logger="app.brain.memory"):
        s = MemoryStore(tmp_path / "memory.db", backend_name="tfidf")
        s.add("主人喜欢喝冰美式", "fact", importance=0.5)
        s._search_fused("冰美式", limit=6)
    assert "降级" in caplog.text, caplog.text


# ----------------------------
# 渲染文案 + 其它
# ----------------------------


def test_disclaimer_wording(store):
    block = store.system_block(QUESTION)
    assert block.startswith(
        "【你记得的事】（按你刚才这句话检索出来的，可能相关也可能不准；用不上就别硬扯）")


def test_empty_store_block_is_empty(tmp_path):
    s = MemoryStore(tmp_path / "memory.db", backend_name="tfidf")
    assert s.system_block("随便问问") == ""


def test_current_query_channel_is_used_when_no_query_given(store):
    """老调用方（无参 system_block）→ 用 set_current_query 记下的那句。"""
    store.set_current_query(QUESTION)
    rows = _rows(store.system_block())
    assert rows, rows
    assert any("房租" in r for r in rows), rows


def test_provider_contract_accepts_optional_query(store):
    """brain_controller 注册的 provider 契约（见注入调用点的改动）。

    - 无参调用（共享层 prompt_layer.get_memory_block() 就是这么调的）→ 不炸；
    - 带 query 调用 → 按这句话检索。
    """
    provider = lambda query="": store.system_block(query)  # noqa: E731
    assert provider() is not None
    assert "房租" in provider("记不记得我明天要交房租")


def test_shared_layer_provider_still_works(store):
    """共享层无参调用 provider → 老路径不炸（回归保护）。"""
    pytest.importorskip("app.brain.prompt_layer")
    from app.brain import prompt_layer
    # 全局 provider：测完必须还原（不然会把临时库的记忆块漏给别的测试）
    old = prompt_layer._memory_block_provider
    try:
        prompt_layer.register_memory_block_provider(
            lambda query="": store.system_block(query))
        block = prompt_layer.get_memory_block()
        assert isinstance(block, str)
        assert block  # 有记忆时非空
    finally:
        prompt_layer.register_memory_block_provider(old)


# --------------------------------------------------------------------------
# 记忆块的通道：易变通道（不是 system prompt）
# --------------------------------------------------------------------------

def test_memory_block_lives_in_the_volatile_channel(store):
    """记忆块走易变通道（独立 user 消息），不进 system prompt。

    Stage 2.1 + 记忆通道搬家：记忆块按本轮 query 召回 ⇒ 每轮都变，
    放 system prompt 会打掉 MiniMax 的 8192-token 前缀缓存。
    """
    from app.brain import prompt_layer

    old_provider = prompt_layer._memory_block_provider
    prompt_layer.register_memory_block_provider(
        lambda query="": store.system_block(query))
    try:
        stable = prompt_layer.build_dynamic_block()
        volatile = prompt_layer.build_turn_context(query=QUESTION)
    finally:
        prompt_layer.register_memory_block_provider(old_provider)

    assert "房租" not in stable, "记忆块不许进稳定段（system prompt）"
    assert "房租" in volatile, "记忆块必须在易变通道里"
    assert "【你记得的事】" in volatile


def test_system_prompt_is_identical_for_two_queries(store):
    """两个不同 query 下，稳定段（system）逐字节相同 —— 缓存不能被打掉。"""
    from app.brain import prompt_layer

    old_provider = prompt_layer._memory_block_provider
    prompt_layer.register_memory_block_provider(
        lambda query="": store.system_block(query))
    try:
        sp1 = prompt_layer.build_dynamic_block()
        ctx1 = prompt_layer.build_turn_context(query=QUESTION)
        sp2 = prompt_layer.build_dynamic_block()
        ctx2 = prompt_layer.build_turn_context(query="主人的猫叫什么")
    finally:
        prompt_layer.register_memory_block_provider(old_provider)

    assert sp1.encode("utf-8") == sp2.encode("utf-8"), (
        "system 串不能随 query 变")
    assert ctx1 != ctx2, "易变段必须随 query 变"


def test_hit_count_is_recorded_on_recall(store):
    """Stage 2.2：召回命中要记 hit_count / last_hit_at。"""
    store._search_fused(QUESTION, limit=6)
    hits = [i for i in store.all() if i.hit_count > 0]
    assert hits, "命中应记录 hit_count"
    assert all(i.last_hit_at for i in hits)
