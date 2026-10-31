#!/usr/bin/env python3
"""记忆召回评测台：拿 sidecar 的真检索（Chroma + BM25 + cross-encoder）考 32 条语料 + 20 条标注问句。

为什么还留着这把尺子
--------------------
    老版本量的是「进程内 substring + tfidf」够不够用，它是当初把精度调上去的
    证据。进程内记忆库删掉之后，**这套语料与标注照样有用** —— 换成考 sidecar：
    同一批 32 条记忆、同一批 20 条自然问句（含 4 条「库里没有答案」和 3 条
    「词面像、答案不同」的近似陷阱），只看 /search 返回的结果。

它量什么
--------
    recall@k    标注的相关行里，有多少条真的进了前 k
    precision@k 前 k 里有多少条是相关的
    hit-rate    有多少条问句至少命中 1 条相关行（一条都没命中 = 这轮记忆白给）
    无关注入     「库里根本没有答案」的问句，前 k 里混进了几条
    近似陷阱     陷阱行有没有被排进前 k（进了 = 桌宠会自信地说错话）

约束
----
    - **不新增依赖**：只用标准库 + 仓库自己的 HTTP 客户端；
    - **默认不碰主人的记忆库**：自己拉起一个**临时数据目录**的 sidecar
      （memory_sidecar serve --data-dir <tmp>），跑完就关掉、临时目录一并删除；
    - 想考「正在跑的那个 sidecar」就传 --base-url（会把语料写进去、跑完删掉）。

用法
----
    python scripts/eval_memory_recall.py                # 起临时 sidecar，跑一遍
    python scripts/eval_memory_recall.py --top-k 6
    python scripts/eval_memory_recall.py --json out.json
    python scripts/eval_memory_recall.py --base-url http://127.0.0.1:8788

调闸门（标定用；子进程直接继承这两个环境变量）
--------------------------------------------
    DESKTOP_PET_MEMORY_MIN_SCORE_RATIO=0.5 DESKTOP_PET_MEMORY_MIN_SCORE_FLOOR=0.05 \
        python scripts/eval_memory_recall.py
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.brain.memory_client import MemoryClient, category_to_kind  # noqa: E402

# ===========================================================================
# 一、合成语料（⚠️ 全部虚构，不取自 data/ 下的真实记忆）
# ===========================================================================


@dataclass(frozen=True)
class MemoryRow:
    rid: str
    content: str
    category: str = "fact"
    importance: float = 0.5


@dataclass(frozen=True)
class EvalQuery:
    qid: str
    text: str
    relevant: tuple = ()
    trap: Optional[str] = None
    kind: str = "single"


#: 32 条记忆：偏好 / 承诺 / 人物 / 事件 / 琐事，外加 2 条**诱饵**（d01/d02）。
STORE_ROWS: tuple = (
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

#: 20 条标注问句（single / multi / none / nearmiss）
QUERIES: tuple = (
    EvalQuery("A01", "我的猫叫什么名字", ("m01",), None, "single"),
    EvalQuery("A02", "我喜欢喝什么咖啡", ("m02",), None, "single"),
    EvalQuery("A03", "我是做什么工作的", ("m04",), None, "single"),
    EvalQuery("A04", "我的生日是哪天", ("m05",), None, "single"),
    EvalQuery("A05", "我住在哪里", ("m10",), None, "single"),
    EvalQuery("A06", "我的老板姓什么", ("m17",), None, "single"),
    EvalQuery("A07", "我女儿叫什么名字", ("m25",), None, "single"),
    EvalQuery("A08", "我在学什么乐器", ("m09",), None, "single"),
    EvalQuery("A09", "我的车是什么颜色", ("m19",), None, "single"),
    EvalQuery("B01", "我不能吃什么", ("m03", "m14"), None, "multi"),
    EvalQuery("B02", "我的身体最近有什么状况", ("m06", "m27", "m30"), None, "multi"),
    EvalQuery("B03", "我同事和老板分别是谁", ("m12", "m17"), None, "multi"),
    EvalQuery("B04", "我平时喜欢喝什么，讨厌吃什么", ("m02", "m03"), None, "multi"),
    EvalQuery("C01", "今天股市涨了吗", (), None, "none"),
    EvalQuery("C02", "量子力学怎么学", (), None, "none"),
    EvalQuery("C03", "推荐一部好电影", (), None, "none"),
    EvalQuery("C04", "帮我写一首诗", (), None, "none"),
    EvalQuery("D01", "我现在用的是什么手机", ("m26",), "d02", "nearmiss"),
    EvalQuery("D02", "我的体检报告放在哪", ("m20",), "m06", "nearmiss"),
    EvalQuery("D03", "我晚上总是睡不好", ("m30",), "m23", "nearmiss"),
)

#: 零字面重叠的语义对照（打印出来给人看，不计入指标）
SEMANTIC_PAIRS = (
    ("我不能吃什么", "主人讨厌香菜，涮锅也要挑出来", "语义相关（几乎零字面重叠）"),
    ("我晚上总是睡不好", "主人的睡眠最近不太好，晚上容易醒", "语义等价（换了一种说法）"),
)


# ===========================================================================
# 二、检索 → 指标（纯函数：测试可以塞一个假 search 进来）
# ===========================================================================


@dataclass
class QueryResult:
    qid: str
    text: str
    kind: str
    got: list            # 检索到的 row id（按相关度排序）
    relevant: tuple
    trap: Optional[str]
    error: str = ""
    #: 其中真的会被注入 prompt 的那些（score > 0；system_block 就是这么筛的）
    injected: list = None
    #: rid -> {"score", "rel", "logit"}：标定闸门用的原始分数（打印 / 写 --json）
    scores: dict = None

    def __post_init__(self):
        if self.injected is None:
            self.injected = list(self.got)
        if self.scores is None:
            self.scores = {}

    @property
    def top_score(self) -> float:
        """这次检索的最高分（0 = 一条都没有正分）。"""
        return max((float(v.get("score") or 0.0) for v in self.scores.values()), default=0.0)

    @property
    def hits(self) -> list:
        return [rid for rid in self.got if rid in self.relevant]

    @property
    def trap_hit(self) -> bool:
        return bool(self.trap and self.trap in self.got)

    @property
    def trap_beats_answer(self) -> bool:
        """陷阱排在**答案前面**吗？那才是「自信地说错话」。

        （陷阱只是「也进了前 k」不算错 —— 桌宠注入的只是线索，
        真答案排在它前面就不会被带偏。）
        """
        if not self.trap or self.trap not in self.got:
            return False
        if not self.hits:
            return True
        return self.got.index(self.trap) < min(self.got.index(h) for h in self.hits)

    def recall(self) -> float:
        if not self.relevant:
            return 0.0
        return len(self.hits) / float(len(self.relevant))

    def precision(self, k: int) -> float:
        if not self.got:
            return 0.0
        return len(self.hits) / float(min(k, len(self.got)))


def build_cards(rows: Sequence[MemoryRow] = STORE_ROWS) -> dict:
    """语料 → sidecar 卡片 + 「卡片 id ↔ 语料 rid」对照表。"""
    client_card = MemoryClient()   # 只借它造卡（不会发请求）
    cards = {}
    for row in rows:
        card = client_card.make_card(
            row.content, kind=category_to_kind(row.category),
            importance=row.importance, category=row.category)
        cards[row.rid] = card
    return cards


def run_queries(search: Callable, queries: Sequence = QUERIES,
                cards: Optional[dict] = None, top_k: int = 6) -> list:
    """逐条问句打 /search，把结果翻译回语料 rid。

    search: (query, top_k) -> [{"id": ..., "text": ...}, ...]
    """
    cards = cards if cards is not None else build_cards()
    by_card_id = {card["id"]: rid for rid, card in cards.items()}
    by_text = {row.content: row.rid for row in STORE_ROWS}
    out = []
    for q in queries:
        rows = search(q.text, top_k) or []
        got, injected, scores = [], [], {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            rid = by_card_id.get(str(item.get("id") or ""))
            if rid is None:
                rid = by_text.get(str(item.get("text") or ""), "")
            if not rid:
                continue
            got.append(rid)
            try:
                score = float(item.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            try:
                rel = float(item.get("rel") or 0.0)
            except (TypeError, ValueError):
                rel = 0.0
            logit = item.get("rerankLogit")
            scores[rid] = {"score": score, "rel": rel, "logit": logit}
            if score > 0.0:
                injected.append(rid)
        out.append(QueryResult(q.qid, q.text, q.kind, got, q.relevant,
                               q.trap, "", injected, scores))
    return out


def aggregate(results: Sequence[QueryResult], top_k: int = 6) -> dict:
    """汇总成一份指标（问答型 / 无关型 / 陷阱型分开算）。"""
    answered = [r for r in results if r.relevant]
    none_q = [r for r in results if r.kind == "none"]
    traps = [r for r in results if r.trap]
    recall = (sum(r.recall() for r in answered) / len(answered)) if answered else 0.0
    precision = (sum(r.precision(top_k) for r in answered) / len(answered))         if answered else 0.0
    hit = (sum(1 for r in answered if r.hits) / float(len(answered))) if answered else 0.0
    return {
        "queries": len(results),
        "answered": len(answered),
        "recall_at_k": round(recall, 4),
        "precision_at_k": round(precision, 4),
        "hit_rate": round(hit, 4),
        "misses": [r.qid for r in answered if not r.hits],
        "no_answer_queries": len(none_q),
        # 只看**真的会被注入**的行（score > 0）—— 检索回来但被精排判「不相关」的不占 prompt
        "no_answer_leak": round(
            sum(len(r.injected) for r in none_q) / float(len(none_q)), 2)
        if none_q else 0.0,
        "no_answer_leak_max": max((len(r.injected) for r in none_q), default=0),
        "no_answer_rows_returned": round(
            sum(len(r.got) for r in none_q) / float(len(none_q)), 2)
        if none_q else 0.0,
        "trap_queries": len(traps),
        "trap_hits": [r.qid for r in traps if r.trap_hit],
        "trap_beats_answer": [r.qid for r in traps if r.trap_beats_answer],
        "trap_avoid_rate": round(
            sum(0.0 if r.trap_hit else 1.0 for r in traps) / float(len(traps)), 4
        ) if traps else 0.0,
        "top_k": top_k,
    }


# ===========================================================================
# 三、sidecar 的生命周期（自己起一个临时的，跑完就关）
# ===========================================================================


def _free_port() -> int:
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


class TemporarySidecar:
    """在**临时数据目录**里起一个真 sidecar（跑完就关，绝不碰 data/）。

    为什么要起真的：这套指标的整个意义就是「sidecar 真的能召回什么」，
    假实现（关键字重合）测出来的数字没有任何参考价值。
    """

    def __init__(self, data_dir: Optional[Path] = None, wait_seconds: float = 120.0):
        self._tmp = None
        if data_dir is None:
            self._tmp = Path(tempfile.mkdtemp(prefix="pet-eval-sidecar-"))
            data_dir = self._tmp
        self.data_dir = Path(data_dir)
        self.port = _free_port()
        self.base_url = "http://127.0.0.1:%d" % self.port
        self.wait_seconds = float(wait_seconds)
        self.proc = None

    def start(self) -> str:
        cmd = [sys.executable, "-m", "memory_sidecar", "serve",
               "--port", str(self.port), "--data-dir", str(self.data_dir)]
        env = dict(os.environ)
        # 本机 TF 2.19 + Keras 3：transformers 4.x 在 import 期就会炸
        env.setdefault("USE_TF", "0")
        env.setdefault("USE_FLAX", "0")
        self.proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT)
        client = MemoryClient(self.base_url, timeout=2.0, cooldown=0.0)
        deadline = time.time() + self.wait_seconds
        while time.time() < deadline:
            health = client.health(timeout=1.0) or {}
            if health.get("ok"):
                return self.base_url
            if self.proc.poll() is not None:
                tail = ""
                try:
                    tail = (self.proc.stdout.read() or b"").decode("utf-8", "replace")[-800:]
                except Exception:  # noqa: BLE001
                    pass
                raise RuntimeError("sidecar 起不来（退出码 %s）：%s"
                                   % (self.proc.returncode, tail))
            time.sleep(0.25)
        raise RuntimeError("sidecar %.0fs 内没就绪" % self.wait_seconds)

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        if self._tmp is not None:
            # Windows 上 chroma 的句柄可能还没放干净：删不掉就算了（临时目录）
            shutil.rmtree(self._tmp, ignore_errors=True)


def wait_for_models(client: MemoryClient, seconds: float = 240.0) -> dict:
    """等 sidecar 把两个模型加载好（**测量之前**必须等）。

    为什么要等：模型是在后台线程里预热/懒加载的（v2-m3 有 2.1 GB），
    第一个检索请求会排在模型锁后面 —— 不等就开始量，
    **第一条问句**会超时拿到空结果，指标白白变差（这不是召回能力的问题）。
    """
    deadline = time.time() + float(seconds)
    health = {}
    while time.time() < deadline:
        health = client.health(timeout=2.0) or {}
        models = health.get("models") or {}
        if not health.get("ok"):
            return health
        # degraded / missing 也算「终态」：模型坏了就别干等 240 秒，指标照量，
        # 只是要在报告里说明这一轮是降级跑的。
        if all(models.get(k) in ("ready", "absent", "failed", "disabled",
                                 "degraded", "missing")
               for k in ("embed", "rerank")):
            return health
        time.sleep(0.5)
    return health


def seed(client: MemoryClient, cards: dict, rebuild_wait: float = 60.0) -> int:
    """把语料写进 sidecar，并**同步**把两个索引建好（/rebuild）。

    为什么不再靠「触发一次检索」来建索引：检索对模型只给一个时间预算
    （model_budget），冷启动那一轮跑不完就先用词面结果回答 —— 索引留给后台
    线程补。评测要量的是**稳态**召回，所以这里显式重建，别让懒刷新影响指标。
    """
    n = client.upsert(list(cards.values()))
    try:
        report = client.rebuild()
        print("[评测] 重建索引：%s" % (report,))
    except Exception as e:  # noqa: BLE001 - 老 sidecar 没这个端点也能跑
        print("[评测] /rebuild 不可用（%s），退回懒刷新" % e)
        client.search("预热", top_k=1)
    return int(n)


def cleanup(client: MemoryClient, cards: dict) -> int:
    """删掉评测写进去的卡片（--base-url 模式考完不留垃圾）。"""
    return int(client.delete([card["id"] for card in cards.values()]))


# ===========================================================================
# 四、报告
# ===========================================================================


def _fmt(x: float, nd: int = 3) -> str:
    return ("%." + str(nd) + "f") % float(x)


def print_report(results: Sequence[QueryResult], aggs: dict, base_url: str,
                 top_k: int) -> None:
    print("=" * 78)
    print("记忆召回评测 · sidecar = %s" % base_url)
    print("语料 %d 条 · 问句 %d 条 · top-k = %d"
          % (len(STORE_ROWS), len(results), top_k))
    print("=" * 78)
    print("\n%-5s %-24s %-7s %-8s %-7s %-7s %s"
          % ("问句", "内容", "最高分", "命中", "recall", "精度", "Top-%d" % top_k))
    print("-" * 78)
    for r in results:
        flag = "陷阱!" if r.trap_hit else ("空" if not r.got else "")
        print("%-5s %-24s %-7s %-8s %-7s %-7s %s"
              % (r.qid, r.text[:24], _fmt(r.top_score),
                 "%d/%d" % (len(r.hits), len(r.relevant)),
                 _fmt(r.recall(), 2) if r.relevant else "—",
                 _fmt(r.precision(top_k), 2) if r.relevant else "—",
                 ",".join(r.got) + (("  ←" + flag) if flag else "")))
    print("-" * 78)
    print("recall@%d      : %s（%d 条问答型问句）"
          % (top_k, _fmt(aggs["recall_at_k"]), aggs["answered"]))
    print("precision@%d   : %s" % (top_k, _fmt(aggs["precision_at_k"])))
    print("hit-rate      : %s（没命中任何相关行的问句：%s）"
          % (_fmt(aggs["hit_rate"]), "、".join(aggs["misses"]) or "无"))
    print("无关注入      : 平均 %s 行 / 最多 %d 行（%d 条「库里没有答案」的问句）"
          % (aggs["no_answer_leak"], aggs["no_answer_leak_max"],
             aggs["no_answer_queries"]))
    # 标定相关度闸门的两端：无关注句能蹭到的最高分 vs 相关行的最低分
    print("无关注句最高分: %s"
          % ("、".join("%s=%s" % (r.qid, _fmt(r.top_score))
                      for r in results if r.kind == "none") or "无"))
    weakest = [(r.qid, min(float(r.scores.get(h, {}).get("score") or 0.0)
                           for h in r.hits))
               for r in results if r.hits]
    if weakest:
        print("相关行最低分  : %s"
              % "、".join("%s=%s" % (qid, _fmt(sc)) for qid, sc in weakest))
    print("近似陷阱      : 避开率 %s（进了前 k 的陷阱：%s；"
          "压过答案的：%s —— 后者才是真的会说错话）"
          % (_fmt(aggs["trap_avoid_rate"]),
             "、".join(aggs["trap_hits"]) or "无",
             "、".join(aggs["trap_beats_answer"]) or "无"))
    broken = [r.qid for r in results if r.error]
    if broken:
        print("检索报错      : %s（%s）" % ("、".join(broken), results[0].error))


def print_semantic_pairs(search: Callable, cards: dict, top_k: int = 3) -> None:
    """零字面重叠的语义对照：证明「换了说法也找得到」。"""
    by_text = {row.content: row.rid for row in STORE_ROWS}
    print("\n语义对照（问句与目标记忆几乎没有字面重叠）：")
    for query, target, note in SEMANTIC_PAIRS:
        rows = search(query, top_k) or []
        got = []
        for item in rows:
            if isinstance(item, dict):
                got.append(by_text.get(str(item.get("text") or ""), "?"))
        hit = "✓" if by_text.get(target) in got else "✗"
        print("  [%s] %s → %s（%s）：%s"
              % (hit, query, target[:22], note, ",".join(got) or "无"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="记忆召回评测（语料 32 条 / 问句 20 条，全部走 sidecar 的 /search）")
    ap.add_argument("--base-url", default="",
                    help="已有 sidecar 的地址（默认自己起一个临时数据目录的 sidecar）")
    ap.add_argument("--top-k", type=int, default=6, help="每次检索取几条（默认 6）")
    ap.add_argument("--json", default="", help="把机器可读结果写到这个文件")
    ap.add_argument("--keep", action="store_true",
                    help="跑完不删临时数据目录（排查用）")
    args = ap.parse_args(argv)

    top_k = max(1, int(args.top_k))
    cards = build_cards()
    tmp_sidecar = None
    base_url = args.base_url.strip()
    try:
        if not base_url:
            print("[评测] 起一个临时 sidecar（临时数据目录，跑完删除）…")
            tmp_sidecar = TemporarySidecar()
            base_url = tmp_sidecar.start()
            print("[评测] sidecar 就绪：%s" % base_url)
        client = MemoryClient(base_url, timeout=10.0, cooldown=0.0,
                              write_timeout=120.0)
        health = client.health(timeout=2.0) or {}
        if not health.get("ok"):
            print("[评测] sidecar 连不上（%s）：%s"
                  % (base_url, client.last_error or "未知原因"))
            return 2
        print("[评测] 写入 %d 条合成记忆…" % seed(client, cards))
        # 量之前先等模型就绪：第一条问句不该因为「模型还在加载」而超时变空
        print("[评测] 等 sidecar 的模型就绪…")
        ready = wait_for_models(client)
        print("[评测] 模型状态：%s" % ((ready.get("models") or {}),))
        bad = {k: v for k, v in (ready.get("models") or {}).items()
               if v not in ("ready", "disabled")}
        if bad:
            print("[评测] !! 有模型没就绪（%s）：这一轮指标是降级跑的，看报告要说明" % bad)
        knob = (client.stats() or {}).get("search") or {}
        if knob:
            print("[评测] 闸门：minSimilarity=%s minScoreRatio=%s denseKeepRatio=%s "
                  "预算=%sms" % (knob.get("minSimilarity"), knob.get("minScoreRatio"),
                                knob.get("denseKeepRatio"), knob.get("modelBudgetMs")))
            kind_gate = knob.get("kindGate") or {}
            if kind_gate:
                print("[评测] 按类别的闸门（价目表 v%s）：" % knob.get("kindGateVersion"))
                for kind in sorted(kind_gate):
                    row = kind_gate[kind] or {}
                    print("[评测]   %-11s min_score_ratio=%-5s dense_keep_ratio=%s"
                          % (kind, row.get("min_score_ratio"), row.get("dense_keep_ratio")))
        errors = {}

        def _search(q: str, k: int) -> list:
            rows = client.search(q, top_k=k)
            if not rows and client.last_error:
                errors[q] = client.last_error
            return rows

        results = run_queries(_search, QUERIES, cards, top_k)
        for res in results:
            if not res.got and res.text in errors:
                res.error = errors[res.text]
        aggs = aggregate(results, top_k)
        print_report(results, aggs, base_url, top_k)
        print_semantic_pairs(lambda q, k: client.search(q, top_k=k), cards)
        if args.json:
            payload = {
                "base_url": base_url,
                "top_k": top_k,
                "models": health.get("models") or {},
                "degraded": bool(health.get("degraded")),
                "metrics": aggs,
                "gate": knob,
                "results": [
                    {"qid": r.qid, "text": r.text, "kind": r.kind,
                     "got": r.got, "relevant": list(r.relevant),
                     "trap_hit": r.trap_hit, "top_score": round(r.top_score, 4),
                     "scores": r.scores}
                    for r in results],
            }
            Path(args.json).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print("\n[评测] 机器可读结果已写入 %s" % args.json)
        if tmp_sidecar is None:
            # 考的是别人的 sidecar：把语料删掉，别留下 32 条假记忆
            print("[评测] 清理（删掉 %d 条评测卡片）…" % cleanup(client, cards))
        return 0
    finally:
        if tmp_sidecar is not None:
            if args.keep:
                print("[评测] 保留临时数据目录：%s" % tmp_sidecar.data_dir)
            tmp_sidecar.stop()


if __name__ == "__main__":
    raise SystemExit(main())
