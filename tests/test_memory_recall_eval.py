"""D1 决策评测（scripts/eval_memory_recall.py）的回归测试。

背景：
    PLAN.md 的决策点 D1 —— 「要不要接 qq-bridge 的 sidecar（Chroma + BM25 + Qwen3-Reranker）」，
    判据是「用实测数据决定」。scripts/eval_memory_recall.py 就是那把尺子：
    合成记忆库 + 标注查询 + 三配置消融（substring only / tfidf only / substring+tfidf）。

这里钉死的是**尺子本身**（A1~A3 的可复现性与结论），而不是把某个数字抄一遍：
    - 评测是**离线**的：不联网、不要 API key、不新增任何第三方依赖；
    - 评测**可复现**：同一份语料跑两次结果必须逐条一致；
    - 消融台**没有走偏**：两腿配置必须与生产 MemoryStore 逐条一致；
    - PLAN Stage 2 的验收行被**显式**核对（20 条 → 问只涉及 2 条 → 6 条里相关 ≥2、无关 ≤2；
      无关问题注入 ≤2）。
    - 近似陷阱（词面像、答案不同）被**单独测量** —— 这是桌宠最容易「自信说错话」的地方。

不需要 API key / 网络 / GUI。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import pytest  # noqa: E402

import eval_memory_recall as E  # noqa: E402


# ---------------------------------------------------------------------------
# 公共夹具：三个配置各跑一遍（只跑一次，测试之间共用结果）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("d1-eval")
    out = {}
    for key in ("substring", "tfidf", "both"):
        out[key] = E.run_config(E.STORE_ROWS, E.QUERIES, E.CONFIGS[key], tmp / key)
    return out


@pytest.fixture(scope="module")
def aggs(results):
    return {k: E.aggregate(v) for k, v in results.items()}


@pytest.fixture(scope="module")
def accept(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("d1-accept")
    return E.acceptance_check(E.STORE_ROWS, tmp, E.CONFIGS["both"])


# ---------------------------------------------------------------------------
# A1：离线、可复现、不依赖真实用户的记忆行
# ---------------------------------------------------------------------------


def test_a1_corpus_is_synthetic_and_shaped_right():
    """语料是合成的、规模符合要求、覆盖了 4 类查询。"""
    assert 25 <= len(E.STORE_ROWS) <= 35, "记忆行数应在 25~35 之间"
    assert 15 <= len(E.QUERIES) <= 20, "查询数应在 15~20 之间"
    kinds = {q.kind for q in E.QUERIES}
    assert kinds == {"single", "multi", "none", "nearmiss"}, kinds
    # 至少有 2 条诱饵（看着相关但不是答案）
    decoys = [r for r in E.STORE_ROWS if r.rid.startswith("d")]
    assert len(decoys) >= 2, decoys
    # 必须有「答案跨多行」和「库里没有答案」两类
    assert any(len(q.relevant) >= 2 for q in E.QUERIES)
    assert any(not q.relevant for q in E.QUERIES)
    # 近似陷阱必须指向库里真实存在、且**不是**答案的行
    for q in E.QUERIES:
        if q.trap:
            assert q.trap in {r.rid for r in E.STORE_ROWS}, q.trap
            assert q.trap not in q.relevant, (q.qid, q.trap)


def test_a1_script_has_no_network_or_new_dependency():
    """评测脚本不许引入网络/新依赖（这是「要不要装 sidecar」的前提，不能自己先装）。"""
    src = (SCRIPTS / "eval_memory_recall.py").read_text(encoding="utf-8")
    for banned in ("import requests", "import urllib", "import http", "import socket",
                   "import chromadb", "import bm25s", "import torch",
                   "import sentence_transformers", "import httpx", "from urllib"):
        assert banned not in src, f"评测脚本不该出现 {banned}"
    # 记忆库必须落在临时目录，不能写 data/
    assert "tempfile" in src
    assert '"data/"' not in src


def test_a1_evaluation_is_deterministic(tmp_path):
    """同一份语料跑两次 → 逐条注入结果完全一致（可复现）。"""
    a = E.run_config(E.STORE_ROWS, E.QUERIES, E.CONFIGS["both"], tmp_path / "a")
    b = E.run_config(E.STORE_ROWS, E.QUERIES, E.CONFIGS["both"], tmp_path / "b")
    assert [r.injected for r in a] == [r.injected for r in b]
    assert [r.recall for r in a] == [r.recall for r in b]


def test_a1_two_leg_config_matches_production_store(tmp_path):
    """消融台没走偏：两腿配置 ≡ 生产 MemoryStore（逐条注入结果一致）。"""
    plain = E.run_config(E.STORE_ROWS, E.QUERIES, E.CONFIGS["both"],
                         tmp_path / "plain", plain=True)
    abl = E.run_config(E.STORE_ROWS, E.QUERIES, E.CONFIGS["both"], tmp_path / "abl")
    assert [r.injected for r in plain] == [r.injected for r in abl]


# ---------------------------------------------------------------------------
# A2：消融 —— 三配置并排 + fallback + 近似陷阱
# ---------------------------------------------------------------------------


def test_a2_ablation_three_configs_reported(aggs):
    """三个配置都跑出 recall@6 / precision@6 / hit-rate。"""
    for key in ("substring", "tfidf", "both"):
        a = aggs[key]
        assert 0.0 <= a["recall@6"] <= 1.0
        assert 0.0 <= a["precision@6"] <= 1.0
        assert 0.0 <= a["hit_rate"] <= 1.0
        assert a["n_answerable"] == 16, a["n_answerable"]
        assert a["total_relevant"] == 21, a["total_relevant"]


def test_a2_only_two_leg_config_meets_both_bars(aggs):
    """消融的关键结论：只有两腿（今天的配置）同时满足两条判据。

    - 命中率（hit-rate）≥ 0.9
    - 无关问题注入 ≤ 2 条（走 fallback）

    ⚠️ Stage 2.8 改了单腿 tfidf 的**门禁语义**：旧门禁是「向量腿有正相似度
    就算有关系」→ 无关问题硬塞 6 条；新门禁是**阈值**判断 → 无关问题照样
    走 fallback。所以下面两条断言从「> 2 / 0%」改成「≤ 2 / 100%」。
    单腿 substring 精确率最高，但命中率不够。
    """
    both, tfidf, sub = aggs["both"], aggs["tfidf"], aggs["substring"]
    assert both["hit_rate"] >= 0.9, both["hit_rate"]
    assert both["none_max_injected"] <= 2, both["none_max_injected"]
    # 单腿 tfidf：阈值门禁把无关问题也挡在门外 → 走 fallback（≤ 2 条）
    assert tfidf["none_max_injected"] <= 2, tfidf["none_max_injected"]
    assert tfidf["none_fallback_ratio"] == 1.0, tfidf["none_fallback_ratio"]
    # 单腿 substring：精确率更高但召回更低
    assert sub["precision@6"] > both["precision@6"], (sub["precision@6"], both["precision@6"])
    assert sub["recall@6"] < both["recall@6"], (sub["recall@6"], both["recall@6"])


def test_a2_fallback_fires_for_unrelated_questions(results, aggs):
    """无关问题 → 走 fallback（注入 ≤ 2 条）。"""
    for r in results["both"]:
        if r.kind == "none":
            assert r.fallback, (r.qid, r.injected)
            assert len(r.injected) <= 2, (r.qid, r.injected)
    assert aggs["both"]["none_max_injected"] <= 2
    assert aggs["both"]["none_fallback_ratio"] == 1.0


def test_a2_disclaimer_lets_pet_say_i_dont_remember(results):
    """「用不上就别硬扯」免责句必须在渲染块里（无关问题也一样）。"""
    seen = 0
    for r in results["both"]:
        if r.kind == "none":
            assert r.disclaimer, r.qid
            seen += 1
    assert seen == 4, seen


def test_a2_nearmiss_correct_row_is_still_injected(results):
    """近似陷阱查询：正确行必须还在（修陷阱不能把答案也丢了）。"""
    for r in results["both"]:
        if r.kind == "nearmiss":
            assert r.hit, (r.qid, r.injected)


def test_a2_nearmiss_trap_baseline_snapshot(results):
    """近似陷阱的**当前基线**：3/3 命中（已知失败模式）。

    这是桌宠最容易「自信说错话」的地方：d02（旧手机）、m06（体检复查）、m23（减肥）
    都会跟着正确行一起被注入。
    ⚠️ 这是**基线快照**：谁把这条修好了（陷阱数下降），请把断言改成新值并更新注释。
    """
    near = [r for r in results["both"] if r.kind == "nearmiss"]
    assert len(near) == 3
    assert sum(1 for r in near if r.trap_injected) == 3, [
        (r.qid, r.trap, r.injected) for r in near]


# ---------------------------------------------------------------------------
# A3：PLAN Stage 2 验收行 —— 显式核对
# ---------------------------------------------------------------------------


def test_a3_acceptance_related_question(accept):
    """PLAN Stage 2 验收行第 1 条：20 条 → 问只涉及 2 条 → 注入里相关 ≥2、无关 ≤2。

    ⚠️ Stage 2.8：注入条数从 6 收到 4（max_inject），所以「注入 6 条」改成「注入 4 条」。

    ⚠️ 「无关」有两种读法，脚本两种都报：
      - 标注读法：「无关」= 不是标注答案的行 → 旧行为（注入 6 条）要求 6 条里
        至少 4 条是答案，而题目只涉及 2 条 → 除非注入少于 4 条，否则自相矛盾；
        Stage 2.8 把注入收到 4 条之后，这道歧义**自己没了**：相关 2 + 无关 2，
        标注读法也过了；
      - 词面读法（仓库里 tests/test_memory_query_recall.py 的落地方式）：
        「无关」= 与问题连一个字都不沾的行。
    两种读法都断言：词面读法必须过，标注读法现在也过。
    """
    rel = accept["related_run"]
    assert accept["n_store"] == 20, accept["n_store"]
    assert rel["n_injected"] == 4, rel["injected"]
    assert rel["n_related"] >= 2, rel
    assert rel["n_unrelated_lexical"] <= 2, rel
    assert accept["lexical_pass"] is True
    # 标注读法：注入收到 4 条之后，「相关 2 + 无关 2」也满足要求了
    assert accept["strict_pass"] is True, accept["related_run"]


def test_a3_acceptance_unrelated_question(accept):
    """PLAN Stage 2 验收行第 2 条：无关问题 → 注入 ≤2 条（fallback 生效）。"""
    unrel = accept["unrelated_run"]
    assert unrel["n_injected"] <= 2, unrel
    assert accept["unrelated_pass"] is True
    assert accept["disclaimer_present"] is True


# ---------------------------------------------------------------------------
# A4/A5：第三腿（语义腿）能补多少 —— 上界估计 + 不确定性
# ---------------------------------------------------------------------------


def test_a4_semantic_leg_upper_bound_is_derived_from_unreachable_rows(aggs, results):
    """语义腿的**上界**只能来自「字面完全够不着」的漏召回行。

    - 漏掉且字面够不着的相关行 → 只有语义腿（sbert / sidecar 向量检索）才可能捞回来；
    - 漏掉但字面够得着的 → 那是排序问题，语义腿不一定能救。
    """
    a = aggs["both"]
    unreachable = set(a["missed_unreachable"])
    for r in results["both"]:
        for rid in r.missed_unreachable:
            assert rid in unreachable
            assert rid not in r.hit_rel, (r.qid, rid)
    upper = (a["total_relevant_hit"] + len(unreachable)) / a["total_relevant"]
    assert upper >= a["recall@6"]
    assert upper <= 1.0


def test_a4_upper_bound_is_reported_in_json_payload(tmp_path):
    """--json 产物里必须有消融数字、验收行结果、语义腿上界素材。"""
    import json

    out = tmp_path / "d1.json"
    assert E.main(["--json", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data["configs"]) == {"substring", "tfidf", "both"}
    for key in ("substring", "tfidf", "both"):
        agg = data["configs"][key]["agg"]
        assert "recall@6" in agg and "precision@6" in agg and "hit_rate" in agg
        assert len(data["configs"][key]["queries"]) == len(E.QUERIES)
    assert data["plain_matches_two_leg"] is True
    assert data["acceptance"]["unrelated_pass"] is True
    assert "variant_no_owner_prefix" in data


def test_a5_sensitivity_variant_is_measured(tmp_path):
    """语料敏感性对照（去掉每行的「主人」前缀）必须能跑出数字。"""
    variant_rows = E.strip_owner_prefix(E.STORE_ROWS)
    assert len(variant_rows) == len(E.STORE_ROWS)
    assert not any(r.content.startswith("主人") for r in variant_rows)
    res = E.run_config(variant_rows, E.QUERIES, E.CONFIGS["both"], tmp_path / "v")
    agg = E.aggregate(res)
    assert agg["n_queries"] == len(E.QUERIES)
    assert 0.0 <= agg["recall@6"] <= 1.0

