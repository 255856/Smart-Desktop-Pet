#!/usr/bin/env python3
"""D1 决策评估：进程内召回（substring + tfidf）到底够不够？要不要接 qq-bridge 的 sidecar？

PLAN 的决策点 D1（PLAN.md「### 🔀 决策点 D1 —— 要不要接 B 的 sidecar?」）：
    Stage 2.1 完成后**用实测数据决定**：
    - 若 top-6 命中率与「用不上就别硬扯」的比例达标 → **不接**，本 Stage 收工
    - 若不达标 → 再评估 qq-bridge 的 sidecar（Chroma + BM25 + Qwen3-Reranker）

这个脚本就是那把「实测数据」的尺子。它：
    1. 用一份**合成但逼真**的中文记忆库（32 条）+ 20 条标注过的自然问句来考当前的召回；
    2. 做**三配置消融**（substring only / tfidf only / substring+tfidf），
       量化「融合到底买到了什么」；
    3. 单独量 fallback（无关问题注入 ≤ 2）与近似陷阱（词面像、答案不同）；
    4. 显式核对 PLAN Stage 2 的验收行（20 条 → 问只涉及 2 条的问题 → 6 条里相关 ≥2、无关 ≤2）。

约束（重要）：
    - **完全离线**：不联网、不要 API key、不开 GUI；
    - **不新增任何第三方依赖**；
    - **不碰 data/**：记忆库落在临时目录；
    - **不依赖真实用户的记忆行**：全部是合成语料（见 STORE_ROWS / QUERIES）。

用法
----
    python scripts/eval_memory_recall.py                  # 打印全部报告
    python scripts/eval_memory_recall.py --json out.json  # 同时落一份机器可读结果

为什么有一个 AblationStore 子类？
    消融要换的**只有「参与 RRF 的路」和「门禁看哪一路」**，
    其余（RRF 公式、fallback、渲染文案、注入条数）必须**一字不改**才公平。
    所以只重写 _fusion_ranked_lists（挑腿）和 _query_matches_anything（换门禁），
    融合本身仍然走父类实现。测试里有一条用例断言：
    「两腿配置」与**直接用 MemoryStore**（生产代码）逐条结果完全一致 —— 保证消融台没走偏。

Stage 2.8（召回精度）新增的能力：
    - **before/after 同框**：--legacy 用老行为（注入 6 条、不裁剪、旧门禁）
      跑同一批语料，两张表并排 —— before/after 不靠嘴说，跑一次就有；
    - **分数比曲线**（--curve）：补位行门槛（顶行融合分 × ratio）从 0 扫到 1，
      precision/recall 怎么动、拐点在哪，一眼看见；
    - **生产真实性检查**（--random-ids N）：评测台默认把 id 钉成语料行号
      （否则「并列靠 id 定序」不可复现），生产里 id 是 uuid4 随机生成的 ——
      这个开关把 id 换回随机，跑 N 次看指标分布（评测台给出的数字偏乐观）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.brain.memory import (  # noqa: E402
    DEFAULT_FILLER_RATIO,
    DEFAULT_MAX_INJECT,
    SEMANTIC_GATE_THRESHOLD,
    MemoryStore,
    _bigrams,
    _has_word_overlap,
    _sbert_available,
)

# ===========================================================================
# 一、合成语料
# ===========================================================================
# ⚠️ 全部是**虚构**的（"主人"是本脚本杜撰的用户），不取自 data/ 下的真实记忆。
#    写法刻意贴近真实记忆行：短、口语、夹着无关琐事。


@dataclass(frozen=True)
class MemoryRow:
    rid: str
    content: str
    category: str = "fact"
    importance: float = 0.5


#: 32 条记忆：偏好 / 承诺 / 人物 / 事件 / 琐事，外加 2 条**诱饵**（d01/d02）。
#: importance 各不相同 → fallback（按重要性 top-2）是**确定的**，不会因 uuid 抖动。
STORE_ROWS: tuple[MemoryRow, ...] = (
    MemoryRow("m01", "主人的猫叫团子，是个橘猫，很胖", "person", 0.85),
    MemoryRow("m02", "主人喜欢喝冰美式，不加糖", "preference", 0.80),
    MemoryRow("m03", "主人讨厌香菜，涮锅也要挑出来", "preference", 0.78),
    MemoryRow("m04", "主人是程序员，在一家做游戏的公司上班", "fact", 0.82),
    MemoryRow("m05", "主人的生日是3月10号", "fact", 0.90),
    MemoryRow("m06", "主人下周要去医院做体检复查甲状腺", "event", 0.76),
    MemoryRow("m07", "主人答应这周末陪妈妈去西湖边散步", "event", 0.74),
    MemoryRow("m08", "主人的工位上摆着一盆仙人掌", "other", 0.30),
    MemoryRow("m09", "主人会弹一点吉他，正在学《睡晚安》", "skill", 0.60),
    MemoryRow("m10", "主人住在杭州西湖区，房子是租的", "fact", 0.72),
    MemoryRow("m11", "主人每个月15号交房租", "fact", 0.71),
    MemoryRow("m12", "主人的同事小李最近在学做饭", "person", 0.40),
    MemoryRow("m13", "主人养了三盆多肉", "other", 0.35),
    MemoryRow("m14", "主人对芒果过敏", "fact", 0.88),
    MemoryRow("m15", "主人去年去了北海道看雪", "event", 0.45),
    MemoryRow("m16", "主人每天早上七点起床", "fact", 0.38),
    MemoryRow("m17", "主人的老板姓王，团队一共六个人", "person", 0.55),
    MemoryRow("m18", "主人上周买了一个新的机械键盘", "event", 0.25),
    MemoryRow("m19", "主人的车是白色的，停在地下车库B2", "fact", 0.65),
    MemoryRow("m20", "主人把体检报告放在玄关抽屉里", "other", 0.62),
    MemoryRow("m21", "主人的大学室友叫阿豪，现在在广州", "person", 0.50),
    MemoryRow("m22", "主人喜欢在雨天散步", "preference", 0.36),
    MemoryRow("m23", "主人最近在减肥，晚上只吃沙拉", "preference", 0.52),
    MemoryRow("m24", "主人打算年底换工作，简历已经改好了", "event", 0.68),
    MemoryRow("m25", "主人的女儿叫朵朵，上幼儿园大班", "person", 0.92),
    MemoryRow("m26", "主人现在用的手机是黑色的iPhone，壳上贴着柴犬", "other", 0.42),
    MemoryRow("m27", "主人上周感冒了，现在已经好了", "event", 0.46),
    MemoryRow("m28", "主人想养一只柴犬，但房东不让养", "fact", 0.58),
    MemoryRow("m29", "主人每天通勤单程一小时，坐地铁1号线", "fact", 0.44),
    MemoryRow("m30", "主人的睡眠最近不太好，晚上容易醒", "fact", 0.56),
    # —— 诱饵：看着相关，其实不是任何问题的答案 ——
    MemoryRow("d01", "主人以前捡过一只土狗，小名叫小黑", "person", 0.33),
    MemoryRow("d02", "主人以前的手机是iPhone 12，前段时间已经卖了", "other", 0.28),
)


@dataclass(frozen=True)
class EvalQuery:
    qid: str
    text: str
    #: 标注：这个问题该由哪些记忆行回答
    relevant: tuple[str, ...] = ()
    #: 近似陷阱：词面很像、但**不是**答案的那一行（命中它 = 桌宠会自信地说错话）
    trap: Optional[str] = None
    #: single（答案在 1 行）/ multi（答案跨 2-3 行）/ none（库里没有答案）/ nearmiss（近似陷阱）
    kind: str = "single"


QUERIES: tuple[EvalQuery, ...] = (
    # —— 答案在恰好 1 行 ——
    EvalQuery("A01", "我的猫叫什么名字", ("m01",), None, "single"),
    EvalQuery("A02", "我喜欢喝什么咖啡", ("m02",), None, "single"),
    EvalQuery("A03", "我是做什么工作的", ("m04",), None, "single"),
    EvalQuery("A04", "我的生日是哪天", ("m05",), None, "single"),
    EvalQuery("A05", "我住在哪里", ("m10",), None, "single"),
    EvalQuery("A06", "我的老板姓什么", ("m17",), None, "single"),
    EvalQuery("A07", "我女儿叫什么名字", ("m25",), None, "single"),
    EvalQuery("A08", "我在学什么乐器", ("m09",), None, "single"),
    EvalQuery("A09", "我的车是什么颜色", ("m19",), None, "single"),
    # —— 答案跨 2~3 行 ——
    EvalQuery("B01", "我不能吃什么", ("m03", "m14"), None, "multi"),
    EvalQuery("B02", "我的身体最近有什么状况", ("m06", "m27", "m30"), None, "multi"),
    EvalQuery("B03", "我同事和老板分别是谁", ("m12", "m17"), None, "multi"),
    EvalQuery("B04", "我平时喜欢喝什么，讨厌吃什么", ("m02", "m03"), None, "multi"),
    # —— 库里根本没有答案（该走 fallback，最多注入 2 条）——
    EvalQuery("C01", "今天股市涨了吗", (), None, "none"),
    EvalQuery("C02", "量子力学怎么学", (), None, "none"),
    EvalQuery("C03", "推荐一部好电影", (), None, "none"),
    EvalQuery("C04", "帮我写一首诗", (), None, "none"),
    # —— 近似陷阱：词面像，答案不同 ——
    EvalQuery("D01", "我现在用的是什么手机", ("m26",), "d02", "nearmiss"),
    EvalQuery("D02", "我的体检报告放在哪", ("m20",), "m06", "nearmiss"),
    EvalQuery("D03", "我晚上总是睡不好", ("m30",), "m23", "nearmiss"),
)

#: PLAN Stage 2 验收行用的那条问题（只涉及 2 行）
ACCEPT_QUESTION = "我什么时候交房租还有体检的事"
ACCEPT_RELEVANT = ("m06", "m11")
#: 验收行里的「无关问题」
ACCEPT_UNRELATED = "今天股市涨了吗"


# ===========================================================================
# 二、消融台
# ===========================================================================


class AblationStore(MemoryStore):
    """只改「参与 RRF 的腿」和「门禁看哪条腿」，其余逻辑全部沿用父类。

    legs     : 参与 RRF 融合的路（"substring" / "tfidf" / "sbert"）
    gate_leg : 门禁（query 跟记忆库有没有关系）看哪条腿
               - "substring"：生产行为（字面/二元组命中即算数）
               - "tfidf"     ：单腿 tfidf 的对应物（tfidf 有正相似度即算数），
                              等价于三腿设计里 sbert 那一路的 semantic_pos 角色
    """

    def __init__(self, *args, legs: Sequence[str] = ("substring", "tfidf"),
                 gate_leg: str = "substring", legacy: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.legs = tuple(legs)
        self.gate_leg = gate_leg
        #: legacy=True → 复刻 Stage 2.8 **之前**的行为（before/after 对照用）
        self.legacy = legacy

    # -- 挑腿：父类算好三路排名，这里只留要参与融合的那些 --
    def _fusion_ranked_lists(self, query, items):
        lists, semantic_sim, leg_rel = super()._fusion_ranked_lists(query, items)
        kept = [(name, ranked) for (name, ranked) in lists if name in self.legs]
        kept_rel = {name: leg_rel.get(name, {}) for name, _ in kept}
        return kept, semantic_sim, kept_rel

    # -- 换门禁：单腿配置下，门禁必须看**它自己那条腿**，否则等于把这条路掐死 --
    def _query_matches_anything(self, query, items, sub_list, semantic_sim):
        if self.gate_leg == "tfidf":
            if self.legacy:
                # 老门禁：向量腿「有正相似度就算有关系」（> 0）
                return self._tfidf_positive_hits(query) > 0
            # 新门禁：最大相似度 **≥ 阈值**（跟生产语义腿同一套判断）
            return self._tfidf_max_sim(query) >= self.semantic_gate_threshold
        if self.legacy:
            return bool(sub_list) or _has_word_overlap(query, items)                 or semantic_sim > 0
        return super()._query_matches_anything(query, items, sub_list, semantic_sim)

    def _tfidf_positive_hits(self, query: str) -> int:
        backend = self._fusion_tfidf
        if backend is None:
            return 0
        try:
            qv = backend.encode([query])[0]
            sims = backend.similarity(qv, backend._doc_vecs)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return 0
        return sum(1 for s in sims if float(s) > 0)

    def _tfidf_max_sim(self, query: str) -> float:
        backend = self._fusion_tfidf
        if backend is None:
            return 0.0
        try:
            qv = backend.encode([query])[0]
            sims = backend.similarity(qv, backend._doc_vecs)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return 0.0
        return max((float(s) for s in sims), default=0.0)

    # -- 老行为（Stage 2.8 之前）：只按 RRF 分排序，不裁剪 --
    def _pick_candidates(self, by_id, fused, leg_rel, max_rows):
        if not self.legacy:
            return super()._pick_candidates(by_id, fused, leg_rel, max_rows)
        ordered = sorted(fused.items(),
                         key=lambda kv: (-kv[1], -by_id[kv[0]].importance, kv[0]))
        return [by_id[mid] for mid, _ in ordered[:max(0, int(max_rows))]]


class LegacyStore(MemoryStore):
    """**老行为**复刻（Stage 2.8 之前）：生产 MemoryStore 的 before 对照。

    - 注入条数：6（老默认）
    - 排序：只按 RRF 分（并列 → 重要性 → id）
    - 裁剪：没有（补位行照旧一路塞满）
    - 门禁：语义腿「有正相似度就算有关系」（老判断）
    """

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_inject", 6)
        super().__init__(*args, **kwargs)

    def _pick_candidates(self, by_id, fused, leg_rel, max_rows):
        ordered = sorted(fused.items(),
                         key=lambda kv: (-kv[1], -by_id[kv[0]].importance, kv[0]))
        return [by_id[mid] for mid, _ in ordered[:max(0, int(max_rows))]]

    def _query_matches_anything(self, query, items, sub_list, semantic_sim):
        if sub_list:
            return True
        if _has_word_overlap(query, items):
            return True
        return semantic_sim > 0


#: 三配置。CONFIGS["both"] = 今天真实跑的两腿（sbert 未安装时）。
CONFIGS: dict[str, dict] = {
    "substring": {
        "label": "substring only（单腿·字面/二元组）",
        "legs": ("substring",),
        "gate_leg": "substring",
    },
    "tfidf": {
        "label": "tfidf only（单腿·TF-IDF 余弦）",
        "legs": ("tfidf",),
        "gate_leg": "tfidf",
    },
    "both": {
        "label": "substring + tfidf（两腿·今天实际跑的）",
        "legs": ("substring", "tfidf"),
        "gate_leg": "substring",
    },
}

PRIMARY_CONFIG = "both"


# ===========================================================================
# 三、跑评测
# ===========================================================================


@dataclass
class QueryResult:
    qid: str
    text: str
    kind: str
    relevant: tuple[str, ...]
    trap: Optional[str]
    injected: list[str] = field(default_factory=list)
    hit_rel: list[str] = field(default_factory=list)
    recall: float = 0.0
    precision: float = 0.0
    hit: bool = False
    fallback: bool = False
    trap_injected: bool = False
    disclaimer: bool = False
    #: 漏掉的相关行里，**字面完全够不着**的那些（只有语义腿才可能捞回来）
    missed_unreachable: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "qid": self.qid, "text": self.text, "kind": self.kind,
            "relevant": list(self.relevant), "trap": self.trap,
            "injected": list(self.injected), "hit_rel": list(self.hit_rel),
            "recall": round(self.recall, 4), "precision": round(self.precision, 4),
            "hit": self.hit, "fallback": self.fallback,
            "trap_injected": self.trap_injected, "disclaimer": self.disclaimer,
            "missed_unreachable": list(self.missed_unreachable),
        }


def _build_store(rows: Iterable[MemoryRow], tmpdir: Path, cfg: dict,
                 plain: bool = False, legacy: bool = False,
                 max_inject: Optional[int] = None,
                 filler_ratio: Optional[float] = None,
                 random_ids: bool = False) -> MemoryStore:
    """建一个只装合成语料的临时记忆库（绝不碰 data/）。

    - legacy=True → 用 LegacyStore 复刻 Stage 2.8 **之前**的行为；
    - random_ids=True → **不钉 id**，让 add() 生成 uuid4（= 生产真实情况）。
    """
    db = tmpdir / "memory.db"
    kwargs: dict = {"backend_name": "tfidf"}
    if legacy:
        # 老行为的注入条数是 6（Stage 2.8 之前的默认）
        kwargs["max_inject"] = 6
    if max_inject is not None:
        kwargs["max_inject"] = max_inject
    if filler_ratio is not None:
        kwargs["filler_ratio"] = filler_ratio
    store: MemoryStore
    if plain:
        store = LegacyStore(db, **kwargs) if legacy else MemoryStore(db, **kwargs)
    else:
        store = AblationStore(db, legs=cfg["legs"], gate_leg=cfg["gate_leg"],
                              legacy=legacy, **kwargs)
    for row in rows:
        item = store.add(row.content, row.category, importance=row.importance)
        if random_ids:
            # 生产真实情况：id 是 uuid4 随机生成的 → 并列时的兜底排序也随机
            continue
        # ⚠️ 固定 id：生产里 id 是 uuid4 随机生成的，而排序的**并列兜底**用的
        #    正是 id。不固定的话，「注入 N 条」里第 2、3 位（补位行）每次跑
        #    都不一样 —— 评测就不可复现了（实测：同一条查询两次跑，补位行完全不同）。
        #    这里把 id 钉成语料里的行号，排序完全确定。
        #    ⚠️ 代价：评测台因此**偏乐观** —— 生产里并列靠随机 id 决胜。
        #    --random-ids 就是专门量这个偏差的。
        item.id = row.rid
    # 把钉过的 id 写回临时库，保持内存与磁盘一致（临时库，用完即删）
    store._save()
    return store


def _rows_of(block: str) -> list[str]:
    """从渲染块里取出注入的记忆正文（"- [类别★重要性] (时间) 正文"）。"""
    out = []
    for line in block.splitlines():
        if line.startswith("- ["):
            out.append(line.split(") ", 1)[1])
    return out


def run_config(rows: Sequence[MemoryRow], queries: Sequence[EvalQuery],
               cfg: dict, tmpdir: Path, plain: bool = False, *,
               legacy: bool = False, max_inject: Optional[int] = None,
               filler_ratio: Optional[float] = None,
               random_ids: bool = False) -> list[QueryResult]:
    """跑一个配置，返回每条查询的逐条结果。"""
    store = _build_store(rows, tmpdir, cfg, plain=plain, legacy=legacy,
                         max_inject=max_inject, filler_ratio=filler_ratio,
                         random_ids=random_ids)
    content_to_rid = {r.content: r.rid for r in rows}
    results: list[QueryResult] = []
    for q in queries:
        block = store.system_block(q.text)
        injected_rows = _rows_of(block)
        injected = [content_to_rid.get(c, "?") for c in injected_rows]
        hit_rel = [rid for rid in injected if rid in q.relevant]
        res = QueryResult(
            qid=q.qid, text=q.text, kind=q.kind, relevant=q.relevant, trap=q.trap,
            injected=injected, hit_rel=hit_rel,
            recall=(len(hit_rel) / len(q.relevant)) if q.relevant else 0.0,
            precision=(len(hit_rel) / len(injected)) if injected else 0.0,
            hit=bool(hit_rel),
            # last_fusion_backends 为空 == 融合一条没召回（走了 fallback）
            fallback=(not store.last_fusion_backends),
            trap_injected=(q.trap is not None and q.trap in injected),
            disclaimer=("用不上就别硬扯" in block),
        )
        # 漏掉的相关行里，字面（二元组）完全够不着的 → 只有语义腿才可能捞回来
        for rid in q.relevant:
            if rid in hit_rel:
                continue
            content = next((r.content for r in rows if r.rid == rid), "")
            if not (_bigrams(q.text) & _bigrams(content)):
                res.missed_unreachable.append(rid)
        results.append(res)
    return results


def aggregate(results: Sequence[QueryResult]) -> dict:
    """汇总指标。

    - recall@6 / precision@6 / hit-rate：**只在有答案的查询上**统计
      （无关问题没有「相关行」，算进去只会稀释指标）；
    - fallback：无关查询的注入条数；
    - near-miss：陷阱行被注入的比例（桌宠会自信说错话的比例）。
    """
    answerable = [r for r in results if r.relevant]
    none_q = [r for r in results if r.kind == "none"]
    near = [r for r in results if r.kind == "nearmiss"]
    total_rel = sum(len(r.relevant) for r in answerable)
    got_rel = sum(len(r.hit_rel) for r in answerable)
    return {
        "n_queries": len(results),
        "n_answerable": len(answerable),
        "recall@6": (got_rel / total_rel) if total_rel else 0.0,
        "precision@6": (sum(r.precision for r in answerable) / len(answerable)) if answerable else 0.0,
        "hit_rate": (sum(1 for r in answerable if r.hit) / len(answerable)) if answerable else 0.0,
        "macro_recall@6": (sum(r.recall for r in answerable) / len(answerable)) if answerable else 0.0,
        # —— 有答案的问题：平均注入了几条（token 成本）——
        "mean_injected": (sum(len(r.injected) for r in answerable) / len(answerable)) if answerable else 0.0,
        "total_relevant": total_rel,
        "total_relevant_hit": got_rel,
        # —— 无关问题 ——
        "n_none": len(none_q),
        "none_mean_injected": (sum(len(r.injected) for r in none_q) / len(none_q)) if none_q else 0.0,
        "none_max_injected": max((len(r.injected) for r in none_q), default=0),
        "none_fallback_ratio": (sum(1 for r in none_q if r.fallback) / len(none_q)) if none_q else 0.0,
        "none_le2_ratio": (sum(1 for r in none_q if len(r.injected) <= 2) / len(none_q)) if none_q else 0.0,
        "none_disclaimer_ratio": (sum(1 for r in none_q if r.disclaimer) / len(none_q)) if none_q else 0.0,
        # —— 近似陷阱 ——
        "n_nearmiss": len(near),
        "nearmiss_trap_injected": sum(1 for r in near if r.trap_injected),
        "nearmiss_trap_ratio": (sum(1 for r in near if r.trap_injected) / len(near)) if near else 0.0,
        "nearmiss_hit_rate": (sum(1 for r in near if r.hit) / len(near)) if near else 0.0,
        # —— 语义腿的上界素材 ——
        "missed_unreachable": sorted({rid for r in answerable for rid in r.missed_unreachable}),
    }


def strip_owner_prefix(rows: Sequence[MemoryRow]) -> tuple[MemoryRow, ...]:
    """敏感性对照：把每行开头的「主人」去掉。

    为什么要做这个对照：本合成语料里**每一行都以「主人」开头**，
    而 tfidf 的 token 里「主人」是一个 df=32/32 的词 → 它给**每一行**都加了相似度，
    于是 tfidf 这一路的排序被稀释、precision@6 被压低。
    去掉前缀 = 排除这种语料形态的影响，看看「低 precision」到底是设计问题还是语料形态问题。
    """
    out = []
    for r in rows:
        c = r.content
        for prefix in ("主人的", "主人"):
            if c.startswith(prefix):
                c = c[len(prefix):]
                break
        out.append(MemoryRow(r.rid, c, r.category, r.importance))
    return tuple(out)


def filler_similarity_spread(rows: Sequence[MemoryRow], queries: Sequence[EvalQuery],
                              tmpdir: Path) -> dict:
    """「补位行」有多并列？—— 衡量 precision@6 低是不是因为第 2~6 名几乎并列。

    对每条查询：取注入的 6 行，算它们在 tfidf 这一路上的相似度 max-min。
    差值越小 → 越并列 → 谁进 top-6 越不是由相关性决定的（生产代码的并列兜底是 id）。
    """
    store = _build_store(rows, tmpdir, CONFIGS[PRIMARY_CONFIG])
    content_to_rid = {r.content: r.rid for r in rows}
    spreads = []
    tail_spreads = []
    for q in queries:
        block = store.system_block(q.text)
        inj_rows = _rows_of(block)
        # ⚠️ 融合后端是**懒建**的：必须先跑一次召回，它才被建出来
        backend = store._fusion_tfidf
        if len(inj_rows) < 2 or backend is None:
            continue
        qv = backend.encode([q.text])[0]
        sims = backend.similarity(qv, backend._doc_vecs)
        rid2sim = {content_to_rid.get(it.content, "?"): float(s)
                   for it, s in zip(store._items, sims, strict=False)}
        vals = [rid2sim.get(content_to_rid.get(c, "?"), 0.0) for c in inj_rows]
        spreads.append(max(vals) - min(vals))
        if len(vals) > 1:
            tail = vals[1:]
            tail_spreads.append(max(tail) - min(tail))
    if not spreads:
        return {"mean_spread": 0.0, "max_spread": 0.0, "n": 0,
                "mean_tail_spread": 0.0, "n_tail": 0}
    return {"mean_spread": sum(spreads) / len(spreads),
            "max_spread": max(spreads),
            "min_spread": min(spreads),
            "n": len(spreads),
            "mean_tail_spread": (sum(tail_spreads) / len(tail_spreads))
                                if tail_spreads else 0.0,
            "n_tail": len(tail_spreads)}


# ===========================================================================
# 四、Stage 2.8：分数比曲线 / before-after 对照 / 生产真实性（随机 id）
# ===========================================================================


def score_ratio_curve(rows: Sequence[MemoryRow], queries: Sequence[EvalQuery],
                      tmpdir: Path, ratios: Optional[Sequence[float]] = None,
                      cfg: Optional[dict] = None) -> list[dict]:
    """分数比曲线：补位行门槛（融合分 ≥ 顶行融合分 × ratio）从 0 扫到 1。

    ratio 越大 → 留的补位行越少 → precision 越高、recall 越低。
    **裁剪规则从这条曲线上选拐点**，不靠猜。
    """
    cfg = cfg or CONFIGS[PRIMARY_CONFIG]
    ratios = list(ratios) if ratios else [
        0.0, 0.05, 0.10, 0.15, 0.20, 0.25, 0.28, 0.30, 0.32, 0.35,
        0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00]
    out: list[dict] = []
    for r in ratios:
        res = run_config(rows, queries, cfg, tmpdir / ("ratio%s" % r),
                         filler_ratio=r)
        a = aggregate(res)
        out.append({
            "ratio": r,
            "recall@6": a["recall@6"],
            "precision@6": a["precision@6"],
            "hit_rate": a["hit_rate"],
            "mean_injected": a["mean_injected"],
            "none_max_injected": a["none_max_injected"],
            "none_fallback_ratio": a["none_fallback_ratio"],
            "nearmiss_trap_ratio": a["nearmiss_trap_ratio"],
            "nearmiss_trap_injected": a["nearmiss_trap_injected"],
            "n_nearmiss": a["n_nearmiss"],
            "total_relevant_hit": a["total_relevant_hit"],
            "total_relevant": a["total_relevant"],
        })
    return out


def legacy_aggs(rows: Sequence[MemoryRow], queries: Sequence[EvalQuery],
                tmpdir: Path, configs: Optional[dict] = None) -> dict:
    """Stage 2.8 **之前**的行为（注入 6 条 + 不裁剪 + 旧门禁）的三配置指标。"""
    configs = configs or CONFIGS
    out: dict = {}
    for key, cfg in configs.items():
        res = run_config(rows, queries, cfg, tmpdir / ("legacy_" + key),
                         legacy=True)
        out[key] = aggregate(res)
    return out


def random_id_realism(rows: Sequence[MemoryRow], queries: Sequence[EvalQuery],
                      tmpdir: Path, trials: int = 20,
                      cfg: Optional[dict] = None,
                      legacy: bool = False) -> dict:
    """生产真实性检查（A10）：**随机 id** 下指标是什么样？

    评测台默认把 id 钉成语料行号 —— 不钉的话「并列靠 id 定序」每次都不一样，
    评测不可复现。但这也让评测**偏乐观**：生产里 id 是 uuid4 随机生成的，
    分数并列时谁进 top-N 一半靠运气。这里把 id 换回随机，跑 N 次看分布。
    """
    cfg = cfg or CONFIGS[PRIMARY_CONFIG]
    # 基准：钉 id 的那一次（评测台平时用的）
    base = run_config(rows, queries, cfg, tmpdir / "base_pinned")
    base_sets = {r.qid: tuple(r.injected) for r in base}
    aggs = []
    same_set = 0
    same_order = 0
    total_q = 0
    for t in range(trials):
        res = run_config(rows, queries, cfg,
                         tmpdir / (("legacy_rand%d" % t) if legacy
                                   else ("rand%d" % t)),
                         random_ids=True, legacy=legacy)
        for r in res:
            total_q += 1
            ref = base_sets.get(r.qid, ())
            if tuple(sorted(r.injected)) == tuple(sorted(ref)):
                same_set += 1
            if tuple(r.injected) == ref:
                same_order += 1
        aggs.append(aggregate(res))

    def _stat(key: str) -> dict:
        vals = [a[key] for a in aggs]
        return {"mean": sum(vals) / len(vals), "min": min(vals),
                "max": max(vals)}

    return {
        "trials": trials,
        "legacy": legacy,
        # 随机 id 下「注入集合」与钉 id 那次完全一致的查询比例
        # （= 钉 id 有没有把「谁被注入」这件事说准）
        "same_injected_set_ratio": (same_set / total_q) if total_q else 0.0,
        # 顺序也完全一致的比例（更严：id 决定次序也算变化）
        "same_injected_order_ratio": (same_order / total_q) if total_q else 0.0,
        "precision@6": _stat("precision@6"),
        "recall@6": _stat("recall@6"),
        "hit_rate": _stat("hit_rate"),
        "mean_injected": _stat("mean_injected"),
        "none_max_injected": _stat("none_max_injected"),
        "none_fallback_ratio": _stat("none_fallback_ratio"),
        "nearmiss_trap_ratio": _stat("nearmiss_trap_ratio"),
        "per_trial": [{"precision@6": a["precision@6"],
                       "recall@6": a["recall@6"],
                       "hit_rate": a["hit_rate"],
                       "mean_injected": a["mean_injected"],
                       "nearmiss_trap_injected": a["nearmiss_trap_injected"]}
                      for a in aggs],
    }


def print_stage28_sections(aggs: dict, legacy: Optional[dict] = None,
                           curve: Optional[list] = None,
                           rand_ids: Optional[dict] = None,
                           rand_ids_legacy: Optional[dict] = None) -> None:
    """打印 Stage 2.8 的三块：before/after、分数比曲线、生产真实性。"""
    if legacy:
        print("-" * 78)
        print("【3b】Stage 2.8 before/after（同一批语料、同一把尺子）")
        print("-" * 78)
        rows = []
        for key in ("substring", "tfidf", "both"):
            for tag, src in (("before（旧行为）", legacy), ("after（现在）", aggs)):
                a = src.get(key)
                if not a:
                    continue
                rows.append([
                    CONFIGS[key]["label"], tag,
                    _fmt(a["recall@6"]), _fmt(a["precision@6"]),
                    _fmt(a["hit_rate"]), _fmt(a.get("mean_injected", 0.0), 2),
                    a["none_max_injected"], _fmt(a["none_fallback_ratio"], 2),
                    "%s/%s" % (a["nearmiss_trap_injected"], a["n_nearmiss"]),
                ])
        print(_table(["配置", "版本", "recall@6", "precision@6", "hit-rate",
                      "平均注入", "无关最大注入", "无关走fallback",
                      "近似陷阱命中"], rows))
        print()
        print("before = 注入 6 条 + 不裁剪 + 旧门禁（语义腿「> 0 即算有关系」）；")
        print("after  = 注入 %d 条 + 相关度裁剪(ratio=%s) + 阈值门禁(%s)。"
              % (DEFAULT_MAX_INJECT, DEFAULT_FILLER_RATIO, SEMANTIC_GATE_THRESHOLD))
        print()

    if curve:
        print("-" * 78)
        print("【3c】分数比曲线（补位行门槛：融合分 ≥ 顶行融合分 × ratio）")
        print("-" * 78)
        rows = []
        for c in curve:
            rows.append([
                _fmt(c["ratio"], 2), _fmt(c["recall@6"]), _fmt(c["precision@6"]),
                _fmt(c["hit_rate"]), _fmt(c["mean_injected"], 2),
                c["none_max_injected"], _fmt(c["none_fallback_ratio"], 2),
                "%s/%s" % (c["nearmiss_trap_injected"], c["n_nearmiss"]),
            ])
        print(_table(["ratio", "recall@6", "precision@6", "hit-rate", "平均注入",
                      "无关最大注入", "无关走fallback", "近似陷阱命中"], rows))
        print()
        print("→ 拐点：recall 掉下去之前的最大 ratio 就是能用的上限"
              "（默认 %s）。" % DEFAULT_FILLER_RATIO)
        print()

    if rand_ids:
        print("-" * 78)
        print("【3d】生产真实性检查：随机 id（生产里 id 是 uuid4 随机生成）")
        print("-" * 78)
        rows = []
        for key, label in (("precision@6", "precision@6"), ("recall@6", "recall@6"),
                           ("hit_rate", "hit-rate"),
                           ("mean_injected", "平均注入")):
            s = rand_ids[key]
            rows.append([label, _fmt(s["mean"], 3), _fmt(s["min"], 3),
                         _fmt(s["max"], 3)])
        print(_table(["指标", "均值", "最小", "最大"], rows))
        s = rand_ids["nearmiss_trap_ratio"]
        print("近似陷阱命中率：均值 %s（最小 %s / 最大 %s）"
              % (_fmt(s["mean"], 3), _fmt(s["min"], 3), _fmt(s["max"], 3)))
        print("→ 跑 %d 次，每次 id 都随机；对比上面「钉 id」的评测台数字 —— "
              "差多少就是评测台的乐观偏差。" % rand_ids["trials"])
        print("→ 随机 id 下「注入哪几行（集合）」与钉 id 那次一致：%s；"
              "「连顺序都一样」：%s"
              % (_fmt(rand_ids.get("same_injected_set_ratio", 0.0), 3),
                 _fmt(rand_ids.get("same_injected_order_ratio", 0.0), 3)))
        if rand_ids_legacy:
            print()
            print("同一检查跑**老行为**（注入 6 条 + 不裁剪 + 排序并列靠 id）：")
            rows2 = []
            for key, label in (("precision@6", "precision@6"),
                               ("recall@6", "recall@6"),
                               ("hit_rate", "hit-rate"),
                               ("mean_injected", "平均注入")):
                s2 = rand_ids_legacy[key]
                rows2.append([label, _fmt(s2["mean"], 3), _fmt(s2["min"], 3),
                              _fmt(s2["max"], 3)])
            print(_table(["指标", "均值", "最小", "最大"], rows2))
            jitter = (s2["min"] != s2["max"])
            print("→ 老行为的指标在 %d 次里%s（最小 %s / 最大 %s）；"
                  "「注入集合」与钉 id 一致的比例 %s、「连顺序都一样」%s。"
                  % (rand_ids_legacy["trials"],
                     "会抖" if jitter else "不抖",
                     _fmt(s2["min"], 3), _fmt(s2["max"], 3),
                     _fmt(rand_ids_legacy.get("same_injected_set_ratio", 0.0), 3),
                     _fmt(rand_ids_legacy.get("same_injected_order_ratio", 0.0), 3)))
            print("→ 结论靠数字说话，不靠断言：id 只在**分数完全并列**时才决定次序，"
                  "所以它影响的是「补位行的顺序」，未必影响「指标」。")
        print()


# ===========================================================================
# 五、PLAN Stage 2 验收行
# ===========================================================================


def acceptance_check(rows: Sequence[MemoryRow], tmpdir: Path,
                     cfg: Optional[dict] = None) -> dict:
    """显式核对 PLAN Stage 2 的验收行。

    原文（PLAN.md Stage 2 验收）：
        - [ ] 写入 20 条 → 问只涉及 2 条的问题 → 注入 6 条中相关 ≥2、无关 ≤2
        - [ ] 问与记忆完全无关的问题 → 注入条数 ≤2（fallback 生效）

    ⚠️ 这条验收行有两种读法，结果差别很大，脚本**两种都报**：
      1. 标注读法：「相关」= 标注的相关行；「无关」= 其余注入行。
         —— 那么「相关 ≥2 且无关 ≤2」在 6 条注入下要求 **≥4 条**相关，
            而题目只涉及 2 条 → **除非注入少于 4 条，否则不可能通过**。
      2. 词面读法（仓库里已有的 tests/test_memory_query_recall.py 就是这么落地的）：
         「无关」= 与问题**连一个字都不沾**的行；词面沾边但不是答案的算「中间态」。
         —— 这是可落地的读法。
    """
    cfg = cfg or CONFIGS[PRIMARY_CONFIG]
    first20 = list(rows[:20])
    store = _build_store(first20, tmpdir, cfg)
    content_to_rid = {r.content: r.rid for r in first20}

    def _run(text: str) -> dict:
        block = store.system_block(text)
        inj_rows = _rows_of(block)
        inj = [content_to_rid.get(c, "?") for c in inj_rows]
        rel = [r for r in inj if r in ACCEPT_RELEVANT]
        # 词面「完全不相干」：与问题连一个字都不沾
        lex_irrelevant = [c for c in inj_rows if not (set(c) & set(text))]
        return {
            "injected": inj, "n_injected": len(inj),
            "related": rel, "n_related": len(rel),
            "n_unrelated_annot": len(inj) - len(rel),
            "n_unrelated_lexical": len(lex_irrelevant),
            "disclaimer": ("用不上就别硬扯" in block),
        }

    rel_run = _run(ACCEPT_QUESTION)
    unrel_run = _run(ACCEPT_UNRELATED)
    strict_pass = (rel_run["n_related"] >= 2 and rel_run["n_unrelated_annot"] <= 2)
    lexical_pass = (rel_run["n_related"] >= 2 and rel_run["n_unrelated_lexical"] <= 2)
    return {
        "question": ACCEPT_QUESTION,
        "relevant": list(ACCEPT_RELEVANT),
        "n_store": len(first20),
        "related_run": rel_run,
        "unrelated_run": unrel_run,
        "strict_pass": strict_pass,
        "lexical_pass": lexical_pass,
        "unrelated_pass": unrel_run["n_injected"] <= 2,
        "disclaimer_present": rel_run["disclaimer"] and unrel_run["disclaimer"],
    }


# ===========================================================================
# 五、报告
# ===========================================================================


def _fmt(x: float, nd: int = 3) -> str:
    return f"{x:.{nd}f}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    lines = ["| " + " | ".join(str(h) for h in headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        lines.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(lines)


def print_report(all_results: dict[str, list[QueryResult]],
                 aggs: dict[str, dict], accept: dict,
                 store_rows: Sequence[MemoryRow], queries: Sequence[EvalQuery],
                 variant: Optional[dict] = None,
                 spread: Optional[dict] = None,
                 legacy: Optional[dict] = None,
                 curve: Optional[list] = None,
                 rand_ids: Optional[dict] = None,
                 rand_ids_legacy: Optional[dict] = None) -> None:
    print("=" * 78)
    print("D1 决策评估：进程内召回（substring + tfidf）够不够？")
    print("=" * 78)
    print("[环境] python=%s  sentence-transformers 可用=%s  → 融合实际参与的路：substring + tfidf（sbert 缺失即降级，不新增依赖）"
          % (sys.version.split()[0], _sbert_available()))
    n_single = sum(1 for q in queries if q.kind == "single")
    n_multi = sum(1 for q in queries if q.kind == "multi")
    n_none = sum(1 for q in queries if q.kind == "none")
    n_near = sum(1 for q in queries if q.kind == "nearmiss")
    print("[语料] 合成记忆 %d 条 / 标注查询 %d 条（单行 %d、跨行 %d、无关 %d、近似陷阱 %d）"
          % (len(store_rows), len(queries), n_single, n_multi, n_none, n_near))
    print("[说明] 语料是**虚构**的，不依赖也不读取 data/ 下真实用户的记忆行。")
    print()

    # ---------- 语料 ----------
    print("-" * 78)
    print("【1】合成记忆库")
    print("-" * 78)
    print(_table(["id", "类别", "重要性", "内容"],
                 [[r.rid, r.category, "%.2f" % r.importance, r.content] for r in store_rows]))
    print()

    # ---------- 逐条明细（主配置） ----------
    primary = all_results[PRIMARY_CONFIG]
    print("-" * 78)
    print("【2】逐条明细 —— %s" % CONFIGS[PRIMARY_CONFIG]["label"])
    print("-" * 78)
    rows = []
    for r in primary:
        rows.append([
            r.qid, r.kind, r.text,
            ",".join(r.relevant) or "—",
            ",".join(r.injected) or "—",
            _fmt(r.recall), _fmt(r.precision),
            "✓" if r.hit else "✗",
            "fallback" if r.fallback else "",
            ("陷阱命中:" + r.trap) if r.trap_injected else "",
        ])
    print(_table(["qid", "类型", "问题", "相关行", "注入行", "recall@6", "precision@6",
                   "hit", "fallback", "近似陷阱"], rows))
    print()

    # ---------- 消融 ----------
    print("-" * 78)
    print("【3】三配置消融（只看有答案的查询）")
    print("-" * 78)
    abl_rows = []
    for key in ("substring", "tfidf", "both"):
        a = aggs[key]
        abl_rows.append([
            CONFIGS[key]["label"],
            _fmt(a["recall@6"]), _fmt(a["precision@6"]), _fmt(a["hit_rate"]),
            "%s/%s" % (a["total_relevant_hit"], a["total_relevant"]),
            _fmt(a["none_mean_injected"], 2), a["none_max_injected"],
            _fmt(a["none_fallback_ratio"], 2),
            "%s/%s" % (a["nearmiss_trap_injected"], a["n_nearmiss"]),
        ])
    print(_table(["配置", "recall@6", "precision@6", "hit-rate", "命中相关行",
                  "无关问题平均注入", "无关最大注入", "无关走fallback", "近似陷阱命中"],
                 abl_rows))
    print()
    print("说明：recall@6 = 标注相关行被注入的比例（微平均）；"
          "precision@6 = 注入行里标注相关的比例（宏平均）；"
          "hit-rate = 至少命中 1 条相关行的查询占比。")
    if spread:
        print("机制诊断（tfidf 这一路）：注入行相似度 max-min 平均 %s；"
              "只看第 2~6 名（补位行）的 max-min 平均 %s"
              % (_fmt(spread.get("mean_spread", 0.0), 4),
                 _fmt(spread.get("mean_tail_spread", 0.0), 4)))
        print("        → 第 1 名（真答案）与补位行差 %s，但补位行内部只差 %s —— "
              "补位行的相对次序基本不可靠，"
              "谁进 top-6 相当程度上由 id 决定，这就是 precision@6 只有 0.19 的机制。"
              % (_fmt(spread.get("mean_spread", 0.0), 4),
                 _fmt(spread.get("mean_tail_spread", 0.0), 4)))
    print()

    # ---------- Stage 2.8：before/after + 曲线 + 生产真实性 ----------
    print_stage28_sections(aggs, legacy=legacy, curve=curve, rand_ids=rand_ids,
                           rand_ids_legacy=rand_ids_legacy)

    # ---------- fallback / near-miss ----------
    print("-" * 78)
    print("【4】fallback 与近似陷阱（主配置：%s）" % CONFIGS[PRIMARY_CONFIG]["label"])
    print("-" * 78)
    a = aggs[PRIMARY_CONFIG]
    none_rows = []
    for r in primary:
        if r.kind != "none":
            continue
        none_rows.append([r.qid, r.text, len(r.injected), ",".join(r.injected) or "—",
                          "✓" if r.fallback else "✗", "✓" if r.disclaimer else "✗"])
    print(_table(["qid", "无关问题", "注入条数", "注入行", "走fallback", "含免责句"],
                 none_rows))
    print("→ 无关问题平均注入 %s 条，最多 %d 条，全部走 fallback=%s，"
          "「用不上就别硬扯」出现率 %s。"
          % (_fmt(a["none_mean_injected"], 2), a["none_max_injected"],
             a["none_fallback_ratio"] == 1.0, _fmt(a["none_disclaimer_ratio"], 2)))
    print()
    near_rows = []
    for r in primary:
        if r.kind != "nearmiss":
            continue
        near_rows.append([r.qid, r.text, ",".join(r.relevant), r.trap,
                          ",".join(r.injected),
                          "✗ 命中了陷阱行" if r.trap_injected else "✓ 没带错行"])
    print(_table(["qid", "问题", "正确行", "陷阱行", "注入行", "近似陷阱"], near_rows))
    print("→ 近似陷阱命中 %s/%s（越低越好：桌宠会自信地说错话）；"
          "这几条查询的 hit-rate = %s。"
          % (a["nearmiss_trap_injected"], a["n_nearmiss"],
             _fmt(a["nearmiss_hit_rate"], 2)))
    print()

    # ---------- A3 验收行 ----------
    print("-" * 78)
    print("【5】PLAN Stage 2 验收行核对")
    print("-" * 78)
    rel = accept["related_run"]
    unrel = accept["unrelated_run"]
    print("问题：「%s」（只涉及 %s 两行），库里有 %d 条"
          % (accept["question"], accept["relevant"], accept["n_store"]))
    print("  注入 %d 条：%s" % (rel["n_injected"], rel["injected"]))
    print("  标注相关 %d 条（要求 ≥2）；按标注算的无关 %d 条（要求 ≤2） → 标注读法：%s"
          % (rel["n_related"], rel["n_unrelated_annot"],
             "通过" if accept["strict_pass"] else "不通过"))
    print("  按词面算的「完全不相干」（与问题连一个字都不沾）%d 条（要求 ≤2） → 词面读法：%s"
          % (rel["n_unrelated_lexical"],
             "通过" if accept["lexical_pass"] else "不通过"))
    print("无关问题「%s」→ 注入 %d 条（要求 ≤2） → %s"
          % (ACCEPT_UNRELATED, unrel["n_injected"],
             "通过" if accept["unrelated_pass"] else "不通过"))
    print("「用不上就别硬扯」免责句：相关问题=%s、无关问题=%s"
          % ("有" if rel["disclaimer"] else "无",
             "有" if unrel["disclaimer"] else "无"))
    print()

    # ---------- 语义腿上界 ----------
    print("-" * 78)
    print("【6】第三腿（语义腿 sbert）能补多少？（上界估计）")
    print("-" * 78)
    missed = aggs[PRIMARY_CONFIG]["missed_unreachable"]
    tot = aggs[PRIMARY_CONFIG]["total_relevant"]
    got = aggs[PRIMARY_CONFIG]["total_relevant_hit"]
    upper = (got + len(missed)) / tot if tot else 0.0
    print("主配置漏掉的相关行里，**字面（二元组）完全够不着**的有 %d 条：%s"
          % (len(missed), missed))
    print("  → 这些只有语义腿（sbert / sidecar 的向量检索）才可能捞回来；")
    print("  → recall@6 上界 = (%d + %d) / %d = %s（当前 %s）"
          % (got, len(missed), tot, _fmt(upper),
             _fmt(aggs[PRIMARY_CONFIG]["recall@6"])))
    print("  → 这是**上界**：语义腿还得真的能判对，现实中达不到 100%。")
    print()

    # ---------- 语料敏感性 ----------
    if variant:
        v = variant["agg"]
        b = aggs[PRIMARY_CONFIG]
        print("-" * 78)
        print("【7】语料敏感性对照：把每行开头的「主人」去掉后（同一批查询）")
        print("-" * 78)
        print("  为什么要对照：本语料里每行都以「主人」开头，「主人」是 df=32/32 的词，"
              "tfidf 这一路给**每一行**都加了相似度 → 排序被稀释。")
        print("  去掉前缀后（%s）：" % variant["label"])
        print("    recall@6     %s → %s" % (_fmt(b["recall@6"]), _fmt(v["recall@6"])))
        print("    precision@6  %s → %s" % (_fmt(b["precision@6"]), _fmt(v["precision@6"])))
        print("    hit-rate     %s → %s" % (_fmt(b["hit_rate"]), _fmt(v["hit_rate"])))
        print("    无关问题注入  %s → %s 条（最大 %s）"
              % (_fmt(b["none_mean_injected"], 2), _fmt(v["none_mean_injected"], 2),
                 v["none_max_injected"]))
        print("    近似陷阱命中  %s/%s → %s/%s"
              % (b["nearmiss_trap_injected"], b["n_nearmiss"],
                 v["nearmiss_trap_injected"], v["n_nearmiss"]))
        if (abs(v["recall@6"] - b["recall@6"]) < 1e-9
                and abs(v["precision@6"] - b["precision@6"]) < 1e-9):
            print("  → 结论：指标**完全没变** → 共享前缀不是低 precision 的原因；"
                  "低 precision 来自 tfidf 这一路本身（中文短句按字符切 token，"
                  "query 与很多行只共享一个字 → 相似度几乎并列）。")
        else:
            print("  → 结论：指标有变化 → 共享前缀对指标有影响（低 precision 与语料形态有关）。")
        print()

def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="D1：进程内召回是否够用（离线评测）")
    ap.add_argument("--json", metavar="PATH", help="把结果写成 JSON（机器可读）")
    ap.add_argument("--config", default="all",
                    choices=["all", "substring", "tfidf", "both"],
                    help="只跑某个配置（默认全部）")
    ap.add_argument("--legacy", action="store_true",
                    help="同时跑 Stage 2.8 之前的老行为，打印 before/after 对照")
    ap.add_argument("--curve", action="store_true",
                    help="打印分数比曲线（补位行门槛扫描）")
    ap.add_argument("--random-ids", type=int, default=0, metavar="N",
                    help="生产真实性检查：随机 id 跑 N 次")
    args = ap.parse_args(argv)

    tmpdir = Path(tempfile.mkdtemp(prefix="d1-memeval-"))
    try:
        keys = list(CONFIGS) if args.config == "all" else [args.config]
        all_results: dict[str, list[QueryResult]] = {}
        aggs: dict[str, dict] = {}
        for key in keys:
            res = run_config(STORE_ROWS, QUERIES, CONFIGS[key], tmpdir / key)
            all_results[key] = res
            aggs[key] = aggregate(res)
        # 一致性自检：两腿配置必须与**生产 MemoryStore** 逐条一致
        plain = run_config(STORE_ROWS, QUERIES, CONFIGS["both"], tmpdir / "plain",
                           plain=True)
        same = ([r.injected for r in plain]
                == [r.injected for r in all_results.get("both", plain)])
        accept = acceptance_check(STORE_ROWS, tmpdir / "accept",
                                  CONFIGS[PRIMARY_CONFIG])
        # 语料敏感性对照：去掉每行的「主人」前缀（同一批查询、同一套代码）
        variant_rows = strip_owner_prefix(STORE_ROWS)
        variant_res = run_config(variant_rows, QUERIES,
                                 CONFIGS[PRIMARY_CONFIG], tmpdir / "variant")
        variant = {"label": "去掉「主人」前缀",
                   "agg": aggregate(variant_res),
                   "queries": [r.to_dict() for r in variant_res]}
        # 「补位行」并列程度（precision@6 低的机制诊断）
        spread = filler_similarity_spread(STORE_ROWS, QUERIES, tmpdir / "spread")
        # Stage 2.8：before/after 对照 + 分数比曲线 + 生产真实性（随机 id）
        legacy = legacy_aggs(STORE_ROWS, QUERIES, tmpdir / "legacy")             if args.legacy else None
        curve = score_ratio_curve(STORE_ROWS, QUERIES, tmpdir / "curve")             if args.curve else None
        rand_ids = random_id_realism(STORE_ROWS, QUERIES, tmpdir / "rand",
                                     args.random_ids) if args.random_ids else None
        rand_ids_legacy = (random_id_realism(STORE_ROWS, QUERIES,
                                             tmpdir / "rand_legacy",
                                             args.random_ids, legacy=True)
                           if (args.random_ids and args.legacy) else None)

        if args.config == "all":
            print_report(all_results, aggs, accept, STORE_ROWS, QUERIES,
                         variant=variant, spread=spread, legacy=legacy,
                         curve=curve, rand_ids=rand_ids,
                         rand_ids_legacy=rand_ids_legacy)
        else:
            print(_table(["qid", "注入行", "recall@6", "precision@6", "hit"],
                         [[r.qid, ",".join(r.injected), _fmt(r.recall),
                           _fmt(r.precision), "✓" if r.hit else "✗"]
                          for r in all_results[args.config]]))
            print(json.dumps(aggs[args.config], ensure_ascii=False, indent=2))
            print_stage28_sections(aggs, legacy=legacy, curve=curve,
                                   rand_ids=rand_ids,
                                   rand_ids_legacy=rand_ids_legacy)

        print("消融台自检：两腿配置与生产 MemoryStore 逐条一致 = %s" % same)

        if args.json:
            payload = {
                "env": {"sbert_available": _sbert_available(),
                        "python": sys.version.split()[0]},
                "configs": {k: {"label": CONFIGS[k]["label"],
                                "legs": list(CONFIGS[k]["legs"]),
                                "gate": CONFIGS[k]["gate_leg"],
                                "agg": aggs[k],
                                "queries": [r.to_dict() for r in all_results[k]]}
                            for k in keys},
                "plain_matches_two_leg": same,
                "acceptance": accept,
                "variant_no_owner_prefix": variant,
                "stage28": {
                    "max_inject": DEFAULT_MAX_INJECT,
                    "filler_ratio": DEFAULT_FILLER_RATIO,
                    "semantic_gate_threshold": SEMANTIC_GATE_THRESHOLD,
                    "legacy_aggs": legacy,
                    "score_ratio_curve": curve,
                    "random_ids": rand_ids,
                    "random_ids_legacy": rand_ids_legacy,
                },
            }
            Path(args.json).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print("[json] 已写出 %s" % args.json)
        return 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())