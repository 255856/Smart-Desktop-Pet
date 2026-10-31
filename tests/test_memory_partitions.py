# -*- coding: utf-8 -*-
"""记忆分区（身份 / 价值 / 程序性 + 情感权重 + 检索路由）的测试。

为什么这个文件盯这几件事
------------------------
这三块是**常量块** —— 它们进 system prompt，所以「每轮一样」这件事必须被
测住：文件没变 → 三块逐字节相同；文件变了 → 才变。系统提示词的前缀缓存
（最长公共前缀、128-token 页粒度）就靠这个（实测命中率 98.9% → 1.5%）。

覆盖的五件事
------------
  ① 身份记忆   data/identity.json —— 独立文件、**不进 sidecar**、常量块
  ② 价值记忆   data/values.json —— 3~7 条分层 + **每天最多改 2 条**（硬要求）
  ③ 程序性记忆 data/procedures.json —— 手动维护 + 从工具调用记录统计
  ④ 情感权重   arousal / valence —— 默认中性（旧卡不用迁移）、衰减慢
  ⑤ 检索路由   时间 / 事实 / how-to → **只调权重，不硬过滤**

全程离线：不联网、不要 API key、不下模型（情感/路由都是纯函数）。
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.brain import memory_partitions as mp  # noqa: E402
from app.brain import prompt_layer  # noqa: E402
from app.brain.memory_client import MemoryClient, category_to_kind  # noqa: E402


# --------------------------------------------------------------------------
# 夹具
# --------------------------------------------------------------------------

@pytest.fixture
def parts(tmp_path, monkeypatch):
    """把三个分区文件指到临时目录（**不碰**仓库里的 data/）。"""
    monkeypatch.setenv(mp.DATA_DIR_ENV, str(tmp_path))
    mp.reset_cache()
    yield tmp_path
    mp.reset_cache()


def _bump_mtime(path: Path, seconds: int = 2) -> None:
    """把 mtime 往前推 —— 避开「同一刻度写两次」导致缓存不刷新的坑。"""
    st = path.stat()
    t = st.st_mtime_ns + seconds * 10 ** 9
    os.utime(path, ns=(t, t))


def _fake_client(rows):
    """一个只会回固定检索结果的 MemoryClient（不联网）。"""
    def http(method, url, payload, timeout):
        return 200, json.dumps({"ok": True, "results": rows}).encode("utf-8")
    return MemoryClient(http=http)


# ==========================================================================
# ① 身份记忆
# ==========================================================================

def test_identity_block_reads_its_own_file(parts):
    """身份在**独立文件**里（不是记忆卡）：data/identity.json。"""
    mp.save_identity([{"id": "self.name", "text": "我叫小豆"}], root=parts)
    block = mp.identity_block(parts)
    assert block.startswith(mp.IDENTITY_HEADER)
    assert "我叫小豆" in block


def test_identity_block_only_changes_when_the_file_changes(parts):
    """**常量**：文件没变 → 逐字节相同；文件变了 → 才变（mtime 检查）。"""
    mp.save_identity([{"id": "a", "text": "第一版"}], root=parts)
    first = mp.identity_block(parts)
    assert mp.identity_block(parts) == first
    mp.save_identity([{"id": "a", "text": "第二版"}], root=parts)
    _bump_mtime(parts / mp.IDENTITY_FILE)
    second = mp.identity_block(parts)
    assert second != first
    assert "第二版" in second


def test_identity_falls_back_to_defaults_and_never_crashes(parts):
    """文件不存在 / 坏了 → 用兜底身份，**绝不让聊天崩**。"""
    assert mp.identity_block(parts)          # 没有文件
    (parts / mp.IDENTITY_FILE).write_text("{ 这不是 json", encoding="utf-8")
    _bump_mtime(parts / mp.IDENTITY_FILE)
    block = mp.identity_block(parts)
    assert block.startswith(mp.IDENTITY_HEADER)
    assert block.strip()


# ==========================================================================
# ② 价值记忆
# ==========================================================================

def test_values_are_layered_and_ordered(parts):
    """三个层级，顺序固定：底线 → 偏好 → 风格。"""
    mp.save_values([
        {"id": "v3", "level": "style", "content": "风格那条"},
        {"id": "v1", "level": "bottom_line", "content": "底线那条"},
        {"id": "v2", "level": "preference", "content": "偏好那条"},
    ], root=parts)
    rows = mp.load_values(parts)
    assert [r["level"] for r in rows] == ["bottom_line", "preference", "style"]
    block = mp.values_block(parts)
    assert block.index("底线那条") < block.index("偏好那条") < block.index("风格那条")


def test_values_count_is_bounded(parts):
    """3~7 条：超过 7 条直接拒绝（并说清为什么）。"""
    rows = [{"id": "v%d" % i, "level": "preference", "content": "第 %d 条" % i}
            for i in range(mp.MAX_VALUES)]
    mp.save_values(rows, root=parts)
    with pytest.raises(ValueError) as exc:
        mp.add_value("第八条", root=parts)
    assert "最多 %d 条" % mp.MAX_VALUES in str(exc.value)


def test_value_third_edit_in_one_day_is_rejected_with_a_clear_reason(parts):
    """**硬要求**：同一天第 3 次编辑被拒，且错误要说清为什么。"""
    mp.save_values([
        {"id": "v1", "level": "bottom_line", "content": "底线那条"},
        {"id": "v2", "level": "preference", "content": "偏好那条"},
        {"id": "v3", "level": "style", "content": "风格那条"},
    ], root=parts)
    day = datetime.datetime(2026, 11, 5, 10, 0, 0)
    mp.update_value("v1", "底线改了", root=parts, now=day)
    mp.update_value("v2", "偏好改了", root=parts, now=day)
    with pytest.raises(mp.ValueEditRejected) as exc:
        mp.update_value("v3", "风格也改", root=parts, now=day)
    msg = str(exc.value)
    assert "每天最多改 %d 条" % mp.MAX_VALUE_EDITS_PER_DAY in msg
    assert "被拒绝" in msg
    assert "2026-11-05" in msg
    # 被拒的那次**没有**改到文件里
    contents = {r["id"]: r["content"] for r in mp.load_values(parts)}
    assert contents["v3"] == "风格那条"
    assert mp.value_edit_count("2026-11-05", root=parts) == 2


def test_value_edit_budget_resets_the_next_day(parts):
    """第二天重新有额度（上限是「每天」，不是「一辈子」）。"""
    mp.save_values([
        {"id": "v1", "level": "bottom_line", "content": "底线那条"},
        {"id": "v2", "level": "preference", "content": "偏好那条"},
        {"id": "v3", "level": "style", "content": "风格那条"},
    ], root=parts)
    day1 = datetime.datetime(2026, 11, 5, 10, 0, 0)
    mp.update_value("v1", "底线改了", root=parts, now=day1)
    mp.update_value("v2", "偏好改了", root=parts, now=day1)
    mp.update_value("v3", "风格改了",
                    root=parts, now=day1 + datetime.timedelta(days=1))
    assert mp.value_edit_count("2026-11-05", root=parts) == 2
    assert mp.value_edit_count("2026-11-06", root=parts) == 1


def test_adding_a_value_also_counts_as_an_edit(parts):
    """「加一条」也是一次改动 —— 否则日限额形同虚设。"""
    mp.save_values([
        {"id": "v1", "level": "bottom_line", "content": "底线那条"},
        {"id": "v2", "level": "preference", "content": "偏好那条"},
    ], root=parts)
    day = datetime.datetime(2026, 11, 5, 10, 0, 0)
    mp.add_value("新加一条", root=parts, now=day, level="style")
    mp.add_value("再来一条", root=parts, now=day, level="style")
    with pytest.raises(mp.ValueEditRejected):
        mp.add_value("第三条", root=parts, now=day, level="style")


def test_editing_a_value_on_a_fresh_install_keeps_the_defaults(parts):
    """新装的机器上文件还不存在：改一条**不能**把兜底的那几条弄丢。"""
    day = datetime.datetime(2026, 11, 5, 10, 0, 0)
    mp.update_value("v.truth", "新的底线", root=parts, now=day)
    rows = mp.load_values(parts)
    assert len(rows) == len(mp.DEFAULT_VALUES)
    contents = {r["id"]: r["content"] for r in rows}
    assert contents["v.truth"] == "新的底线"
    defaults = {v["id"]: v["content"] for v in mp.DEFAULT_VALUES}
    assert contents["v.tone"] == defaults["v.tone"]


def test_values_block_is_a_constant(parts):
    """价值块只随文件变：改一次文件，内容跟着变。"""
    mp.save_values([{"id": "v1", "level": "bottom_line", "content": "旧内容"}],
                   root=parts)
    first = mp.values_block(parts)
    assert "旧内容" in first
    mp.save_values([{"id": "v1", "level": "bottom_line", "content": "新内容"}],
                   root=parts)
    _bump_mtime(parts / mp.VALUES_FILE)
    second = mp.values_block(parts)
    assert "新内容" in second
    assert second != first


# ==========================================================================
# ③ 程序性记忆
# ==========================================================================

def test_procedures_block_is_an_overview_of_tool_sequences(parts):
    """程序性记忆 = 工具序列 + 成功率；注入时**只放总览**。"""
    mp.upsert_procedure("查天气", ["web_search", "mcp__web-search-safe__live_query"],
                        root=parts, success_rate=0.9, samples=10)
    block = mp.procedures_block(parts)
    assert "查天气" in block
    assert "web_search → mcp__web-search-safe__live_query" in block
    assert "90%" in block
    assert block.count("\n") <= 3      # 总览：几行就够


def test_procedures_can_be_derived_from_tool_call_records(parts):
    """自动那条路：从工具调用记录里数成功率。"""
    calls = [
        {"task": "查天气", "tools": ["web_search", "live_query"], "ok": True},
        {"task": "查天气", "tools": ["web_search", "live_query"], "ok": True},
        {"task": "查天气", "tools": ["web_search", "live_query"], "ok": False},
        {"task": "记一件事", "tools": ["remember_fact"], "ok": True},
    ]
    rows = mp.procedures_from_calls(calls)
    by_task = {r["task"]: r for r in rows}
    assert by_task["查天气"]["samples"] == 3
    assert by_task["查天气"]["success_rate"] == pytest.approx(2 / 3, abs=0.01)
    assert by_task["记一件事"]["success_rate"] == 1.0


def test_refresh_procedures_writes_an_offline_snapshot(parts):
    """统计结果**落回快照文件**（注入只读快照 ⇒ 提示词不会每轮变）。"""
    mp.upsert_procedure("手动套路", ["open_app"], root=parts)
    calls = [{"task": "查天气", "tools": ["web_search"], "ok": True}]
    mp.refresh_procedures(calls, root=parts)
    by_task = {r["task"]: r for r in mp.load_procedures(parts)}
    assert "手动套路" in by_task            # 手动维护的没被统计覆盖
    assert by_task["查天气"]["success_rate"] == 1.0
    assert by_task["查天气"]["source"] == "derived"


def test_procedures_manual_path_and_call_records_path_both_exist(parts):
    """两条路都在：手动维护 + 从调用记录统计（接口都留着）。"""
    mp.upsert_procedure("手动套路", ["open_app"], root=parts)
    assert "手动套路" in mp.procedures_block(parts)
    records = parts / "calls.jsonl"
    records.write_text(
        json.dumps({"task": "查天气", "tools": ["web_search"], "ok": True}) + "\n",
        encoding="utf-8")
    calls = mp.load_call_records(records)
    assert len(calls) == 1
    assert mp.procedures_from_calls(calls)[0]["task"] == "查天气"


# ==========================================================================
# ④ 情感权重
# ==========================================================================

def test_neutral_cards_are_untouched(parts):
    """**默认 0 = 中性**：旧卡没有这两个字段 ⇒ 权重恒为 1.0（不用迁移）。"""
    assert mp.affective_weight() == 1.0
    assert mp.affective_weight(None, None) == 1.0
    assert mp.affective_weight(0.0, 0.0) == 1.0
    assert mp.affective_weight(0.0, 0.0, query_arousal=0.9, query_valence=-0.9) == 1.0


def test_affective_half_life_is_slow(parts):
    """衰减要慢：半衰期 ≥ 30 天（要求），30 天时还剩一大半。"""
    assert mp.AFFECT_HALF_LIFE_DAYS >= 30
    assert mp.affect_decay(30) > 0.6
    assert mp.affect_decay(mp.AFFECT_HALF_LIFE_DAYS) == pytest.approx(0.5, abs=0.01)


def test_old_emotional_card_is_not_pushed_out_of_reach(parts):
    """放了很久的情绪卡**不能被压掉**（有下限，且不会被中性卡反超）。"""
    rows = [
        {"id": "emo", "kind": "fact", "text": "情绪卡", "score": 0.5,
         "updatedAt": 0, "arousal": 0.9, "valence": -0.8},
        {"id": "flat", "kind": "fact", "text": "普通卡", "score": 0.5,
         "updatedAt": 0, "arousal": 0.0, "valence": 0.0},
    ]
    year = 365 * 86400000
    out = mp.rerank_rows(rows, "随便问问", now_ms=year, mood="sad")
    weight = {r["id"]: r["score"] / r["baseScore"] for r in out}
    assert weight["emo"] >= mp.AFFECT_MIN_WEIGHT
    assert weight["emo"] > weight["flat"]        # 还是排在前面，没被压没


def test_affective_bias_is_bounded(parts):
    """权重有上下限：再极端也不会把卡顶上天或踩到底。"""
    assert mp.affective_weight(1.0, 1.0, query_arousal=1.0, query_valence=1.0) \
        <= mp.AFFECT_MAX_WEIGHT
    assert mp.affective_weight(1.0, 1.0, query_arousal=-1.0, query_valence=-1.0) \
        >= mp.AFFECT_MIN_WEIGHT


def test_client_keeps_old_cards_neutral(parts):
    """旧卡（没有 arousal/valence）走一遍客户端检索 ⇒ **情感那部分一点不变**。"""
    now_ms = int(time.time() * 1000)
    rows = [{"id": "a", "kind": "fact", "text": "老卡", "score": 0.5,
             "updatedAt": now_ms}]
    out = _fake_client(rows).search("你好呀")       # 不触发路由
    assert out[0]["score"] == 0.5
    assert out[0]["baseScore"] == 0.5
    # 卡上带了情感值 → 才会被加权（这里只会**加**，不会减）
    out2 = _fake_client([dict(rows[0], arousal=0.9, valence=0.9)]).search("你好呀")
    assert out2[0]["score"] > out2[0]["baseScore"]


def test_store_defaults_old_cards_to_neutral_affect(tmp_path):
    """sidecar 侧：老卡没有情感字段 ⇒ 归一化成 0（中性），不用迁移。"""
    from memory_sidecar.store import MemoryStore
    store = MemoryStore(str(tmp_path), use_vector=False, use_bm25=False,
                         use_rerank=False, prewarm=False)
    try:
        card = store._normalize_card({"id": "old1", "kind": "fact", "text": "老卡"})
        assert card["arousal"] == 0.0
        assert card["valence"] == 0.0
        pub = store._public_card(card)
        assert pub["arousal"] == 0.0
        assert pub["valence"] == 0.0
        # 写了情感值的卡会被夹到 [-1, 1]
        card2 = store._normalize_card({"id": "new1", "kind": "fact", "text": "情绪卡",
                                        "arousal": 3.0, "valence": -9.0})
        assert card2["arousal"] == 1.0
        assert card2["valence"] == -1.0
    finally:
        store.close()


def test_client_card_carries_the_affect_fields():
    """写卡时可以带情感（不给就是 0 = 中性）。"""
    card = MemoryClient().make_card("主人今天特别难受", arousal=0.8, valence=-0.9)
    assert card["arousal"] == 0.8
    assert card["valence"] == -0.9
    assert MemoryClient().make_card("普通一条")["arousal"] == 0.0


# ==========================================================================
# ⑤ 检索路由
# ==========================================================================

@pytest.mark.parametrize("query,kind", [
    ("我昨天去哪了", "event"),
    ("上次买的那把键盘呢", "event"),
    ("那天之后发生了什么", "event"),
    ("之前提过的那个功能", "event"),
    ("我的生日是哪天", "fact"),
    ("我老板姓什么", "fact"),
    ("我工资多少", "fact"),
    ("怎么做番茄钟", "procedural"),
    ("如何查天气", "procedural"),
    ("给我讲讲步骤", "procedural"),
])
def test_route_picks_the_right_kind(query, kind):
    """时间词 → 事件；事实词 → 事实；how-to → 程序性。"""
    weights = mp.route_weights(query)
    assert weights[kind] == mp.ROUTE_BOOST
    others = [k for k in weights if k != kind]
    assert all(weights[k] == 1.0 for k in others)


def test_route_does_not_fire_on_a_plain_greeting():
    assert mp.route_intent("你好呀") == ""
    assert all(w == 1.0 for w in mp.route_weights("你好呀").values())


def test_routing_only_weighs_never_filters(parts):
    """**不硬过滤**：问事实也照样能查到事件（只是排在后面）。"""
    rows = [
        {"id": "f", "kind": "fact", "text": "事实卡", "score": 0.50, "updatedAt": 0},
        {"id": "e", "kind": "event", "text": "事件卡", "score": 0.48, "updatedAt": 0},
        {"id": "p", "kind": "procedural", "text": "做法卡", "score": 0.40, "updatedAt": 0},
    ]
    out = mp.rerank_rows(rows, "我的生日是哪天")
    assert len(out) == 3
    assert {r["id"] for r in out} == {"f", "e", "p"}
    assert out[0]["id"] == "f"          # 事实类问句 → 事实排前面


def test_client_search_applies_the_route_weight(parts):
    """客户端检索里路由真的在调顺序（事件卡被提到前面）。"""
    rows = [
        {"id": "f", "kind": "fact", "text": "事实卡", "score": 0.50, "updatedAt": 0},
        {"id": "e", "kind": "event", "text": "事件卡", "score": 0.48, "updatedAt": 0},
    ]
    out = _fake_client(rows).search("我昨天去哪了")
    assert [r["id"] for r in out] == ["e", "f"]
    assert out[0]["baseScore"] == 0.48          # 原分留着（能查是谁调的权）


def test_procedural_kind_is_a_real_kind():
    """程序性记忆有自己的 kind（闸门里也有一行）。"""
    from memory_sidecar import price_table
    assert "procedural" in price_table.KIND_GATE_TABLE
    assert category_to_kind("skill", "procedural") == "procedural"
    card = MemoryClient().make_card("查天气：先搜索再查实时", kind="procedural")
    assert card["kind"] == "procedural"


# ==========================================================================
# 验收：三块是**常量**（逐字节相同 + 落在稳定段）
# ==========================================================================

def _seed_partitions(root):
    mp.save_identity([{"id": "self.name", "text": "我叫小豆"}], root=root)
    mp.save_values([
        {"id": "v1", "level": "bottom_line", "content": "不编造事实"},
        {"id": "v2", "level": "preference", "content": "说短句"},
        {"id": "v3", "level": "style", "content": "像朋友"},
    ], root=root)
    mp.upsert_procedure("查天气", ["web_search", "live_query"], root=root,
                        success_rate=0.8, samples=5)


def test_two_blocks_are_byte_identical_across_three_queries(parts):
    """**验收**：连续三轮不同 query，身份 / 价值两块逐字节相同。

    而且它们落在**稳定段**（system prompt），不是易变段（独立 user 消息）。
    程序性（procedural）条目多、变化快 → **不进稳定段**（走易变段按需召回）。
    """
    _seed_partitions(parts)
    import app.brain.self_memory as sm

    class _NoStore:
        """假客户端（取不到 → 走文件兜底，不联网）。"""
        last_error = ""

        def list_cards(self, limit=200, **kw):
            return []

    sm.invalidate_cache()
    blocks = sm.stable_blocks(_NoStore())
    identity, values = blocks[0], blocks[1]
    assert identity and values, (identity, values)

    old_self = prompt_layer._self_block_provider
    old_provider = prompt_layer._memory_block_provider
    # 自我记忆块（稳定段）+ 记忆块按 query 变（易变通道）
    prompt_layer.register_self_block_provider(lambda: sm.stable_blocks(_NoStore()))
    prompt_layer.register_memory_block_provider(
        lambda query="": "【你记得的事】%s" % (query or ""))
    try:
        systems = []
        volatiles = []
        for query in ("今天天气怎么样", "我上次说的那个项目", "怎么做番茄钟"):
            systems.append(prompt_layer.build_system_prompt(
                persona="人设", style_guide="", tool_handbook="", dynamic=""))
            volatiles.append(prompt_layer.build_turn_context(query=query))
    finally:
        prompt_layer.register_self_block_provider(old_self)
        prompt_layer.register_memory_block_provider(old_provider)

    # ① system 串三轮逐字节相同
    assert systems[0].encode("utf-8") == systems[1].encode("utf-8")
    assert systems[1].encode("utf-8") == systems[2].encode("utf-8")

    # ② 两块都在 system 串里，且逐字节相同
    for block in (identity, values):
        for sp in systems:
            assert block in sp
            assert sp.encode("utf-8").count(block.encode("utf-8")) == 1

    # ③ 两块**不在**易变段里（易变段每轮都变）
    assert len(set(volatiles)) == 3
    for block in (identity, values):
        for ctx in volatiles:
            assert block not in ctx

    # ④ 程序性**不进稳定段**（条目多、变化快 → 走易变段按需召回）
    procedures = mp.procedures_block()
    assert procedures
    for sp in systems:
        assert procedures not in sp


def test_blocks_do_not_leak_a_timestamp(parts):
    """常量块里不许有「当前时间」—— 那东西每轮变，会打掉前缀缓存。"""
    _seed_partitions(parts)
    sp = prompt_layer.build_system_prompt(
        persona="人设", style_guide="", tool_handbook="", dynamic="")
    assert "[当前时间:" not in sp
    assert "星期" not in sp


def test_backend_parity_still_holds_with_the_new_blocks(parts):
    """两个后端拿到的 system 串仍然**逐字节相同**（含新加的三块）。"""
    _seed_partitions(parts)
    from app.brain.agent import AgentLoop
    from app.brain.langchain_agent import LangChainAgent
    from app.brain.llm_client import LLMConfig

    class _Reg:
        def names(self, level=None):
            return ["open_app", "open_website"]

    persona = "你是小汐，是住在主人身边的女孩。"
    reg = _Reg()
    llm = LLMConfig(api_key="sk-test", model="test-model", stream=False)
    # 自我记忆块（身份/价值）要进 system —— 注册一个提供者（测试里不联网）
    import app.brain.self_memory as sm

    class _NoStore:
        last_error = ""

        def list_cards(self, limit=200, **kw):
            return []

    sm.invalidate_cache()
    old_self = prompt_layer._self_block_provider
    prompt_layer.register_self_block_provider(lambda: sm.stable_blocks(_NoStore()))
    react = AgentLoop(client=None, registry=reg, level_getter=lambda: 3)
    standard = LangChainAgent(llm_cfg=llm, registry=reg, persona=persona,
                              level_getter=lambda: 3)
    try:
        a = react.assemble_system_prompt(persona=persona).encode("utf-8")
        b = standard.assemble_system_prompt(persona=persona).encode("utf-8")
    finally:
        prompt_layer.register_self_block_provider(old_self)
    assert a == b
    assert "【我是谁】" in a.decode("utf-8")
