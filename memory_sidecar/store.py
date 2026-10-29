# -*- coding: utf-8 -*-
"""记忆 sidecar 的存储层：JSON 记忆卡（真值）+ Chroma 向量索引 + BM25 关键词索引。

设计要点
--------
* **JSON 卡片是唯一真值**（<data_dir>/cards.json）。Chroma 与 BM25 都只是**派生索引**，
  任何时候都可以从卡片重建 —— 索引坏掉、目录被删、换了嵌入模型，重新跑一次
  rebuild_index() 就好，卡片一条都不会丢。
* **写卡不等模型**（索引是派生的，所以没理由等）：upsert / delete / supersede 只写
  JSON（外加不碰模型的 BM25），向量索引交给「挂起队列」记账，下一次 search、
  预热结束、或显式 sync_index() 时补齐。冷启动时模型要加载十几秒，写卡不能被它拖住。
* 两条粗排腿（向量 / 关键词）各自独立降级，精排（cross-encoder）再独立降级：
  任何一条腿挂了 sidecar 都照常回答，只是 /health 会说自己降级了。
* 模型**懒加载 + 串行化**：本机实测两个 transformers 模型并发加载会炸
  "Cannot copy out of meta tensor"，所以加载和推理都串在一把进程级锁上
  —— 但那把锁只串行「模型」，**不串行「写卡」**（写卡一步都不碰它）。
* 精排模型**按设备自动选**：有 CUDA 用 bge-reranker-v2-m3（实测 41ms / 分离度 2.7x），
  没有就退回 bge-reranker-base（CPU 上 291ms；v2-m3 在 CPU 要 1016ms）。
  环境变量 DESKTOP_PET_MEMORY_RERANK 永远优先。

打分（与参考实现 qq-bridge 一致）
--------------------------------
    base   = RRF(向量排名, BM25 排名)                —— 粗排融合
    rel    = tanh(rerank_logit / 2)                  —— 把 logit 压到 (-1, 1)
    decay  = 0.5 ** (age_days / half_life_days)      —— 时间衰减
    mood   = 1.15 if card.mood == cur_mood else 1.0  —— 情绪一致性
    imp    = 0.5 + 0.5 * importance                  —— 重要度
    final  = rel * (0.4 + 0.6 * decay) * mood * imp  —— 没精排时 rel 退回 min(1, base*10)

（参考实现的文档字符串写的是 final = rerank_logit * decay * mood * imp，
但它的代码把 logit 先压成 rel、并且用 0.4 + 0.6 * decay：直接把可为负的 logit
乘上衰减，会让「老的不相关记忆」排到「新的不相关记忆」前面。这里按它的**代码**实现。）
"""
import os

# ⚠️ 必须在任何 transformers / sentence_transformers 导入之前设置：
#    本机是 TF 2.19 + Keras 3，transformers 4.57 在 import 期就会
#    raise ValueError("Your currently installed version of Keras is Keras 3 ...")。
#    用 setdefault：别的模块先设过就不覆盖。
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")

import json
import math
import re
import shutil
import sys
import threading
import time
from pathlib import Path

# ── 常量与配置 ────────────────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR_ENV = "DESKTOP_PET_MEMORY_DIR"
DEFAULT_DATA_DIR = os.environ.get(DATA_DIR_ENV) or str(PROJECT_ROOT / "data" / "memory_sidecar")
CARDS_FILE = "cards.json"
CHROMA_SUBDIR = "chroma"
COLLECTION_NAME = "desktop_pet_memory"

EMBED_MODEL = os.environ.get("DESKTOP_PET_MEMORY_EMBED") or "BAAI/bge-small-zh-v1.5"

# ── 精排模型：按设备自动选（实测数据见 resolve_rerank_model 的注释） ────────
RERANK_MODEL_ENV = "DESKTOP_PET_MEMORY_RERANK"
RERANK_MODEL_GPU = "BAAI/bge-reranker-v2-m3"
RERANK_MODEL_CPU = "BAAI/bge-reranker-base"
#: 环境变量给了就用它；**空字符串 = 没设，交给自动选择**（resolve_rerank_model）
RERANK_MODEL = os.environ.get(RERANK_MODEL_ENV) or ""

# /list 的默认条数与上限（防止一次拉爆整个卡库）
LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 500

# 各 kind 的半衰期（天）：越短的记得越浅
HALF_LIFE_DAYS = {
    "thought": 2,       # 心里话
    "topic": 7,         # 话题
    "event": 30,        # 发生过的事
    "fact": 90,         # 事实 / 偏好
    "impression": 180,  # 对人的印象
}
DEFAULT_HALF_LIFE = 90
MOOD_BONUS = 1.15
RRF_K = 60
CANDIDATE_N = 30

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"

# ── 分词（CJK 感知：字 + 字 bigram + jieba 搜索词；英文数字按词） ───────────
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[A-Za-z0-9_]+")

_jieba = None
_jieba_lock = threading.Lock()


def _get_jieba():
    """懒加载 jieba；不可用就返回 None（退化成字 + 字 bigram，仍然 CJK 友好）。"""
    global _jieba
    if _jieba is None:
        with _jieba_lock:
            if _jieba is None:
                try:
                    import jieba

                    try:
                        jieba.setLogLevel(60)  # 别往 stderr 刷 "Building prefix dict"
                    except Exception:
                        pass
                    _jieba = jieba
                except Exception:
                    _jieba = False
    return _jieba or None


def tokenize(text):
    """把文本切成 BM25 词项。

    中文同时给**字**和**字 bigram**（参考实现的做法：比整词更稳），再叠加 jieba
    搜索模式的**词**（专名、短语命中更准）。英文数字按整词。全部小写。
    """
    s = str(text or "").lower()
    if not s.strip():
        return []
    toks = _WORD_RE.findall(s)
    cjk = _CJK_RE.findall(s)
    toks.extend(cjk)
    toks.extend(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    jb = _get_jieba()
    if jb is not None:
        try:
            for w in jb.cut_for_search(s):
                w = w.strip()
                # 只要含中文的多字词：纯英文词上面已经给过，单字上面也给过
                if len(w) >= 2 and _CJK_RE.search(w):
                    toks.append(w)
        except Exception:
            pass
    return toks


# ── 模型（懒加载 + 串行化） ────────────────────────────────────────────────
_HF_LOCK = threading.RLock()  # 进程级：加载与推理都串行，避免并发加载炸 meta tensor


def _cuda_available():
    """有没有 CUDA。单独抽成一个函数：测试 monkeypatch 它就等于「换一台机器」。"""
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _pick_device():
    return "cuda" if _cuda_available() else "cpu"


def resolve_rerank_model(explicit=None, env=None, cuda_ok=None):
    """挑精排模型，返回 (模型 id, 为什么选它)。

    同一批 16 对 query/候选在同一台机器上实测：

    | 模型 | GPU | CPU | 语义用例分离度 |
    | --- | --- | --- | --- |
    | BAAI/bge-reranker-base  | 209 ms | 291 ms  | 1.28x（几乎分不开） |
    | BAAI/bge-reranker-v2-m3 |  41 ms | 1016 ms | 2.7x |

    所以规则是：**有 CUDA 用 v2-m3，没有就用 base**。
    优先级：显式参数 > 环境变量 DESKTOP_PET_MEMORY_RERANK > 按设备自动选。
    """
    explicit = str(explicit or "").strip()
    if explicit:
        return explicit, "调用方显式指定"
    env_value = (os.environ.get(RERANK_MODEL_ENV) if env is None else env) or ""
    env_value = str(env_value).strip()
    if env_value:
        return env_value, "环境变量 %s 覆盖" % RERANK_MODEL_ENV
    cuda = _cuda_available() if cuda_ok is None else bool(cuda_ok)
    if cuda:
        return (
            RERANK_MODEL_GPU,
            "检测到 CUDA：v2-m3 在 GPU 上 41ms、分离度 2.7x（base 是 209ms、1.28x）",
        )
    return (
        RERANK_MODEL_CPU,
        "没有 CUDA：v2-m3 在 CPU 上要 1016ms，退回 base（291ms）",
    )


class TransformersEmbedder:
    """BAAI/bge-small-zh-v1.5 之类 sentence-transformers 向量模型（懒加载）。"""

    def __init__(self, model_name=EMBED_MODEL, device=None):
        self.model_name = model_name
        self.device = device
        self.dim = None
        self._model = None

    @property
    def ready(self):
        return self._model is not None

    def load(self):
        with _HF_LOCK:
            if self._model is not None:
                return self._model
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(self.model_name, device=self.device or _pick_device())
            try:
                # sentence-transformers 5.7 把方法改名了，先试新的再退回旧的
                getter = getattr(model, "get_embedding_dimension", None) or getattr(
                    model, "get_sentence_embedding_dimension", None
                )
                self.dim = int(getter()) if getter else None
            except Exception:
                self.dim = None
            self._model = model
            return model

    def embed(self, texts):
        texts = [str(t) for t in texts]
        if not texts:
            return []
        model = self.load()
        with _HF_LOCK:
            vecs = model.encode(
                texts,
                normalize_embeddings=True,
                batch_size=16,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        return [[float(x) for x in row] for row in vecs]


class TransformersReranker:
    """cross-encoder 精排（懒加载）。

    model_name 为空 → 按设备自动选（有 CUDA 用 bge-reranker-v2-m3，否则用
    bge-reranker-base，理由见 resolve_rerank_model）。
    """

    def __init__(self, model_name=None, device=None, max_length=512):
        self.model_name, self.why = resolve_rerank_model(model_name)
        self.device = device
        self.max_length = max_length
        self._model = None

    @property
    def ready(self):
        return self._model is not None

    def load(self):
        with _HF_LOCK:
            if self._model is not None:
                return self._model
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(
                self.model_name, device=self.device or _pick_device(), max_length=self.max_length
            )
            return self._model

    def score(self, query, texts):
        texts = [str(t) for t in texts]
        if not texts:
            return []
        model = self.load()
        with _HF_LOCK:
            raw = model.predict([(str(query), t) for t in texts])
        return [float(x) for x in raw]


# ── 可选依赖的导入（抽成模块函数，测试里可以直接 monkeypatch 成"不可用"） ──
def _import_chromadb():
    import chromadb

    return chromadb


def _import_bm25s():
    import bm25s

    return bm25s


# ── 小工具 ────────────────────────────────────────────────────────────────
def _now_ms():
    return int(time.time() * 1000)


def _as_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default


def _clamp01(v):
    try:
        return max(0.0, min(1.0, float(v)))
    except Exception:
        return 0.5


# ── 存储 ──────────────────────────────────────────────────────────────────
class MemoryStore:
    """记忆卡存储：JSON 真值 + Chroma / BM25 双路索引。

    参数
    ----
    data_dir : str | Path
        数据目录，默认 <repo>/data/memory_sidecar（可用环境变量
        DESKTOP_PET_MEMORY_DIR 覆盖）。里面是 cards.json + chroma/。
    embedder / reranker : object | None
        可注入的假模型（测试 / 离线用）。需要实现 embed(texts) -> list[list[float]]
        与 score(query, texts) -> list[float]。默认用真实 transformers 模型（懒加载）。
    use_vector / use_bm25 / use_rerank : bool
        单独关掉某一条腿（命令行开关）。
    retry_cooldown : float
        模型加载失败后多久内不再重试（秒），避免每次检索都卡在加载上。

    锁的分工（写卡不等模型的关键）
    ------------------------------
    _cards_lock    只护 self._cards 字典 + cards.json 落盘（毫秒级，从不持锁等模型）
    _bm25_lock     只护 BM25 索引引用的原子替换 / 快照（bm25 是本地计算，不等模型）
    _pending_lock  只护「待写向量」的挂起队列
    _index_lock    护 Chroma / 向量索引 / 检索编排（**模型调用发生在这把锁里**）
    写路径只碰前三个，所以写卡永远不会排在某次模型加载后面。
    """

    def __init__(
        self,
        data_dir=None,
        *,
        embedder=None,
        reranker=None,
        use_vector=True,
        use_bm25=True,
        use_rerank=True,
        embed_model=EMBED_MODEL,
        rerank_model=None,
        collection=COLLECTION_NAME,
        candidate_n=CANDIDATE_N,
        retry_cooldown=30.0,
    ):
        self.data_dir = str(data_dir or DEFAULT_DATA_DIR)
        self.cards_path = os.path.join(self.data_dir, CARDS_FILE)
        self.chroma_dir = os.path.join(self.data_dir, CHROMA_SUBDIR)
        self.collection_name = collection or COLLECTION_NAME
        self.candidate_n = max(1, int(candidate_n))
        self.retry_cooldown = max(0.0, float(retry_cooldown))
        self.embed_model = embed_model
        # 精排模型：没指定就按设备自动选（有 CUDA → v2-m3，否则 base）
        self.rerank_model, self.rerank_reason = resolve_rerank_model(rerank_model)

        self.use_vector = bool(use_vector)
        self.use_bm25 = bool(use_bm25)
        self.use_rerank = bool(use_rerank)
        # 走 stderr：命令行子命令的 stdout 必须是**一行 JSON**（脚本要能直接 parse）
        print(
            "[memory_sidecar] 精排模型 = %s（%s）%s"
            % (
                self.rerank_model,
                self.rerank_reason,
                "" if self.use_rerank else "  ·  精排已关闭（--no-rerank）",
            ),
            file=sys.stderr,
            flush=True,
        )

        self._embedder = embedder
        if self._embedder is None and self.use_vector:
            self._embedder = TransformersEmbedder(embed_model)
        self._reranker = reranker
        if self._reranker is None and self.use_rerank:
            self._reranker = TransformersReranker(self.rerank_model)

        self._cards = {}  # id -> card（真值，内存里的一份）
        self._cards_lock = threading.RLock()
        self._bm25_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._index_lock = threading.RLock()
        #: 向量索引的挂起队列：id -> "upsert" / "delete"（写卡时记账，检索前补齐）
        self._pending_vectors = {}
        #: 卡片代次：每次写卡 +1；BM25 索引记着自己是从哪一代建出来的（脏了就重建）
        self._cards_gen = 0
        self._bm25_gen = 0
        #: /health 用的计数快照（写卡时刷新，health 因此不用抢任何锁）
        self._counts = {"total": 0, "active": 0}

        # 索引与状态
        self._bm25 = None
        self._bm25_ids = []
        self._bm25_error = None
        self._client = None
        self._col = None
        self._vector_dim = None
        self._vector_error = None
        self._chroma_error = None
        self._embed_error = None
        self._embed_ready = False
        self._embed_failed_at = 0.0
        self._rerank_error = None
        self._rerank_ready = False
        self._rerank_failed_at = 0.0
        self._load_error = None
        self.last_search_ms = 0

    # -- 生命周期 --------------------------------------------------------
    def init(self):
        """建数据目录 + 空卡库（幂等）。返回 stats。"""
        os.makedirs(self.data_dir, exist_ok=True)
        if not os.path.exists(self.cards_path):
            self._save()
        self.load()
        return self.stats()

    def load(self):
        """读 cards.json 并重建 BM25。文件坏了就改名留档，从空库继续。"""
        with self._cards_lock:
            self._cards = {}
            self._load_error = None
            raw = []
            if os.path.exists(self.cards_path):
                try:
                    with open(self.cards_path, encoding="utf-8") as f:
                        raw = json.load(f)
                except Exception as e:  # 坏文件：留档而不是静默覆盖
                    self._load_error = str(e)
                    try:
                        os.replace(self.cards_path, self.cards_path + ".bad")
                    except OSError:
                        pass
                    raw = []
            if isinstance(raw, dict):
                raw = raw.get("cards") or []
            for item in raw or []:
                card = self._normalize_card(item)
                if card:
                    self._cards[card["id"]] = card
            # 重新读了真值 → 挂起队列作废（索引差多少由 ensure_vector_index 兜底）
            with self._pending_lock:
                self._pending_vectors.clear()
            self._refresh_counts()
            self._rebuild_bm25()
            return len(self._cards)

    def close(self):
        """释放 Chroma 客户端引用（Windows 上删目录前需要）。"""
        with self._index_lock:
            self._col = None
            self._client = None

    # -- 计数快照（/health 用：不加锁、不碰模型） --------------------------
    def _refresh_counts(self):
        """刷新 health 用的计数快照。调用方持有 _cards_lock。"""
        active = 0
        for card in self._cards.values():
            if card["status"] == STATUS_ACTIVE and card["text"]:
                active += 1
        self._counts = {"total": len(self._cards), "active": active}

    # -- 卡片 ------------------------------------------------------------
    def _normalize_card(self, raw):
        if not isinstance(raw, dict):
            return None
        cid = str(raw.get("id") or "").strip()
        if not cid:
            return None
        old = self._cards.get(cid) or {}
        card = dict(raw)  # 保留调用方塞的额外字段
        card["id"] = cid
        card["key"] = str(raw.get("key") or old.get("key") or "")
        card["kind"] = str(raw.get("kind") or old.get("kind") or "fact").strip() or "fact"
        card["text"] = str(raw.get("text") or old.get("text") or "").strip()[:500]
        card["mood"] = str(raw.get("mood") or old.get("mood") or "").strip()[:24]
        card["importance"] = _clamp01(raw.get("importance", old.get("importance", 0.5)))
        card["createdAt"] = _as_int(raw.get("createdAt") or old.get("createdAt") or _now_ms())
        card["updatedAt"] = _as_int(raw.get("updatedAt") or _now_ms())
        card["hitCount"] = _as_int(raw.get("hitCount", old.get("hitCount", 0)))
        card["lastHitAt"] = _as_int(raw.get("lastHitAt", old.get("lastHitAt", 0)))
        card["status"] = str(raw.get("status") or old.get("status") or STATUS_ACTIVE)
        return card

    def _active_cards(self):
        with self._cards_lock:
            return sorted(
                (c for c in self._cards.values() if c["status"] == STATUS_ACTIVE and c["text"]),
                key=lambda c: c["id"],
            )

    def _public_card(self, card):
        """对外暴露的卡片字段（/search 与 /list 同一套口径）。"""
        return {
            "id": card["id"],
            "key": card["key"],
            "kind": card["kind"],
            "text": card["text"],
            "mood": card["mood"],
            "importance": card["importance"],
            "status": card["status"],
            "createdAt": card["createdAt"],
            "updatedAt": card["updatedAt"],
            "hitCount": card["hitCount"],
            "lastHitAt": card["lastHitAt"],
        }

    def _listing(self, kind=None, include_superseded=False):
        """按「重要度 desc → 更新时间 desc（同分按 id 稳定）」排好的卡。

        默认只列 active（被 supersede 的旧卡是历史，检索也不会返回它们）；
        include_superseded=True 时一并列出（面板要看「全部记忆」时用）。
        """
        want = str(kind or "").strip()
        with self._cards_lock:
            rows = [
                c
                for c in self._cards.values()
                if c["text"]
                and (include_superseded or c["status"] == STATUS_ACTIVE)
                and (not want or c["kind"] == want)
            ]
        rows.sort(key=lambda c: (-c["importance"], -c["updatedAt"], c["id"]))
        return rows

    def count_cards(self, kind=None, include_superseded=False):
        """符合条件的卡数（/list 的 total；不碰模型）。"""
        return len(self._listing(kind, include_superseded))

    def list_cards(self, kind=None, limit=LIST_DEFAULT_LIMIT, include_superseded=False):
        """列卡片（GET /list 用）：重要度 desc → 更新时间 desc，缺省 50 条。

        「recall_memory 不带关键词」就是靠它把卡列出来 —— 不检索、不算向量、
        不加载模型，纯内存排序。include_superseded=True 时连历史卡一起列。
        """
        try:
            n = int(limit if limit is not None else LIST_DEFAULT_LIMIT)
        except Exception:
            n = LIST_DEFAULT_LIMIT
        if n <= 0:
            n = LIST_DEFAULT_LIMIT  # 传 0 / 负数当「没传」，别返回空
        n = min(n, LIST_MAX_LIMIT)
        return [self._public_card(c) for c in self._listing(kind, include_superseded)[:n]]

    # -- 挂起队列（写卡记账，检索 / 预热时补索引） ------------------------
    def _mark_pending(self, ids, op):
        with self._pending_lock:
            for cid in ids:
                self._pending_vectors[str(cid)] = op

    def _clear_pending(self, ids):
        with self._pending_lock:
            for cid in ids:
                self._pending_vectors.pop(str(cid), None)

    def pending_index_count(self):
        """还有多少条卡片变更没写进向量索引（/health 用）。"""
        return len(self._pending_vectors)

    def _bm25_dirty(self):
        return self._cards_gen != self._bm25_gen

    def sync_index(self):
        """把两个**派生索引**追平真值：BM25（脏了就重建）+ 挂起的向量变更。

        返回写进向量索引的条数。写卡只写 JSON，索引总是「晚一拍」：由下一次 search、
        预热结束、或显式调用它来补齐。向量腿降级时挂起项**保留**，下次再补
        （卡片本身早已落盘，一条不丢）。
        """
        if self._bm25_dirty():
            self._rebuild_bm25()
        if not self.use_vector:
            with self._pending_lock:
                self._pending_vectors.clear()
            return 0
        with self._index_lock:
            with self._pending_lock:
                pending = dict(self._pending_vectors)
            if not pending:
                return 0
            ids = list(pending)
            with self._cards_lock:
                known = {cid: self._cards.get(cid) for cid in ids}
            # 卡没了 / 状态不是 active 了 → 向量要删；剩下的才是要写
            gone = [cid for cid in ids if known.get(cid) is None
                    or known[cid]["status"] != STATUS_ACTIVE
                    or not known[cid]["text"]]
            live = [known[cid] for cid in ids if cid not in gone]
            done = 0
            if gone and self._vector_delete(gone) >= len(gone):
                self._clear_pending(gone)
            if live:
                try:
                    vecs = self._embed([c["text"] for c in live])
                    self._write_vectors(live, vecs)
                except Exception:
                    return done  # 向量腿降级：挂起项留着，下次再补
                self._vector_error = None
                done = len(live)
                self._clear_pending([c["id"] for c in live])
            return done

    # -- 写（全部不等模型：JSON 落地就返回） -----------------------------
    def upsert(self, cards):
        """写入 / 更新记忆卡。返回实际写入条数。

        **只写 JSON（真值），一步都不碰模型**：BM25 标脏、向量进挂起队列，
        由下一次 search / sync_index() / 预热结束补齐。
        """
        prepared = []
        with self._cards_lock:
            for raw in cards or []:
                card = self._normalize_card(raw)
                if card and card["text"]:
                    prepared.append(card)
            if not prepared:
                return 0
            for card in prepared:
                self._cards[card["id"]] = card
            self._cards_gen += 1
            self._save()
            self._refresh_counts()
        self._mark_pending(
            [c["id"] for c in prepared if c["status"] == STATUS_ACTIVE],
            "upsert",
        )
        self._mark_pending(
            [c["id"] for c in prepared if c["status"] != STATUS_ACTIVE],
            "delete",
        )
        return len(prepared)

    def delete(self, ids):
        """按 id 删除。返回删除条数。JSON 立刻落地，向量索引走挂起队列。"""
        gone = []
        with self._cards_lock:
            for cid in ids or []:
                cid = str(cid)
                if cid in self._cards:
                    del self._cards[cid]
                    gone.append(cid)
            if not gone:
                return 0
            self._cards_gen += 1
            self._save()
            self._refresh_counts()
        self._mark_pending(gone, "delete")
        # 删向量不需要模型：collection 已经打开就顺手删掉（毫秒级），
        # 没打开就留在挂起队列里，别为了删索引去付「打开 Chroma」的钱。
        with self._index_lock:
            if self.use_vector and self._col is not None:
                if self._vector_delete(gone) >= len(gone):
                    self._clear_pending(gone)
        return len(gone)

    def supersede(self, old_id, new_card):
        """UPDATE 语义：旧卡标成 superseded（留在卡库里），并写入新卡。"""
        old_id = str(old_id or "")
        new = dict(new_card or {}) if isinstance(new_card, dict) else {}
        with self._cards_lock:
            old = self._cards.get(old_id)
            if old is not None:
                old["status"] = STATUS_SUPERSEDED
                old["supersededBy"] = str(new.get("id") or "")
                old["updatedAt"] = _now_ms()
                self._cards_gen += 1
                new.setdefault("supersedes", old_id)
        written = self.upsert([new])
        if old is not None:
            self._mark_pending([old_id], "delete")  # 旧卡的向量要下线
        return written

    # -- Chroma ----------------------------------------------------------
    def _open_chroma(self, recreate=False):
        chromadb = _import_chromadb()
        if recreate:
            try:
                shutil.rmtree(self.chroma_dir, ignore_errors=True)
            except Exception:
                pass
        os.makedirs(self.chroma_dir, exist_ok=True)
        try:
            from chromadb.config import Settings

            settings = Settings(anonymized_telemetry=False)
        except Exception:
            settings = None
        client = (
            chromadb.PersistentClient(path=self.chroma_dir, settings=settings)
            if settings is not None
            else chromadb.PersistentClient(path=self.chroma_dir)
        )
        names = []
        try:
            names = [getattr(c, "name", str(c)) for c in client.list_collections()]
        except Exception:
            names = []
        if self.collection_name in names:
            # 已存在：不传 EF，直接用（向量一律我们自己算、自己传，绝不触发 ONNX 下载）
            col = client.get_collection(name=self.collection_name, embedding_function=None)
        else:
            col = client.create_collection(
                name=self.collection_name,
                embedding_function=None,
                metadata={"hnsw:space": "cosine"},
            )
        self._client = client
        self._col = col
        self._chroma_error = None
        return col

    def _ensure_chroma(self):
        """拿到 collection；打不开就清空重建（索引是派生的，可以随便扔）。"""
        if not self.use_vector:
            raise RuntimeError("向量腿未启用")
        if self._col is not None:
            return self._col
        try:
            return self._open_chroma()
        except Exception as e:
            self._chroma_error = str(e)
            return self._open_chroma(recreate=True)

    def _chroma_count(self, open_if_possible=False):
        """索引里有多少条。open_if_possible=False 时绝不主动打开 Chroma（/health 要快）。"""
        with self._index_lock:
            return self._chroma_count_locked(open_if_possible)

    def _chroma_count_locked(self, open_if_possible=False):
        if self._col is None:
            if not (open_if_possible and self.use_vector and os.path.isdir(self.chroma_dir)):
                return None
            try:
                if self._ensure_chroma() is None:
                    return None
            except Exception as e:
                self._chroma_error = str(e)
                return None
        try:
            return int(self._col.count())
        except Exception as e:
            self._chroma_error = str(e)
            return None

    # -- 模型调用（带降级状态） -------------------------------------------
    def _cooling(self, failed_at):
        return (
            self.retry_cooldown > 0
            and bool(failed_at)
            and (time.time() - failed_at) < self.retry_cooldown
        )

    def _embed(self, texts):
        """算向量。失败抛异常并把向量腿标记成 degraded（带冷却，别每次都卡加载）。"""
        if not self.use_vector or self._embedder is None:
            raise RuntimeError("向量腿未启用")
        if self._cooling(self._embed_failed_at):
            raise RuntimeError("向量模型不可用（冷却中）：%s" % self._embed_error)
        try:
            vecs = self._embedder.embed([str(t) for t in texts])
        except Exception as e:
            self._embed_error = str(e)
            self._embed_failed_at = time.time()
            raise
        if not vecs or len(vecs) != len(texts):
            err = "向量数量不匹配：期望 %d 实际 %d" % (len(texts), len(vecs or []))
            self._embed_error = err
            self._embed_failed_at = time.time()
            raise RuntimeError(err)
        self._embed_error = None
        self._embed_failed_at = 0.0
        self._embed_ready = True
        return [[float(x) for x in row] for row in vecs]

    def _rerank(self, query, texts):
        """精排打分。失败返回 None（退回粗排），并把精排标记成 degraded。"""
        if not self.use_rerank or self._reranker is None:
            return None
        if self._cooling(self._rerank_failed_at):
            return None
        try:
            raw = self._reranker.score(query, list(texts))
        except Exception as e:
            self._rerank_error = str(e)
            self._rerank_failed_at = time.time()
            return None
        if not raw or len(raw) != len(texts):
            self._rerank_error = "精排返回数量不匹配"
            self._rerank_failed_at = time.time()
            return None
        self._rerank_error = None
        self._rerank_failed_at = 0.0
        self._rerank_ready = True
        return [float(x) for x in raw]

    # -- 索引维护 --------------------------------------------------------
    def _meta(self, card):
        return {
            "key": card["key"],
            "kind": card["kind"],
            "mood": card["mood"],
            "importance": card["importance"],
            "updatedAt": card["updatedAt"],
        }

    def _write_vectors(self, active, vecs):
        with self._index_lock:
            dim = len(vecs[0]) if vecs else 0
            if self._vector_dim and dim and dim != self._vector_dim:
                # 换了嵌入模型（维度变了）→ 旧 collection 一定不兼容，直接重建
                self._open_chroma(recreate=True)
            col = self._ensure_chroma()
            col.upsert(
                ids=[c["id"] for c in active],
                documents=[c["text"] for c in active],
                metadatas=[self._meta(c) for c in active],
                embeddings=vecs,
            )
            if dim:
                self._vector_dim = dim

    def _vector_delete(self, ids):
        if not self.use_vector or not ids:
            return 0
        with self._index_lock:
            try:
                col = self._ensure_chroma()
                col.delete(ids=[str(i) for i in ids])
                return len(ids)
            except Exception as e:
                self._chroma_error = str(e)
                return 0

    def rebuild_index(self):
        """从 JSON 卡片重建两个索引。返回 {'vector': n, 'bm25': n, 'errors': [...]}。"""
        with self._index_lock:
            with self._pending_lock:
                self._pending_vectors.clear()  # 整体重建 = 不需要再补挂起项
            errors = []
            self._rebuild_bm25()
            if self._bm25_error:
                errors.append("bm25: %s" % self._bm25_error)
            vector_n = 0
            if self.use_vector:
                active = self._active_cards()
                if active:
                    try:
                        vector_n = self._rebuild_vectors(active)
                    except Exception as e:
                        errors.append("vector: %s" % e)
                else:
                    try:
                        self._ensure_chroma()
                    except Exception as e:
                        errors.append("chroma: %s" % e)
            return {"vector": vector_n, "bm25": len(self._bm25_ids), "errors": errors}

    def _rebuild_vectors(self, active, recreate=False):
        """把 active 卡的向量整体重写一遍（先算向量再写，避免写坏后索引是空的）。"""
        if not active:
            return 0
        vecs = self._embed([c["text"] for c in active])
        if recreate:
            self._open_chroma(recreate=True)
        self._write_vectors(active, vecs)
        self._vector_error = None
        return len(active)

    def ensure_vector_index(self, force=False):
        """确保向量索引覆盖所有 active 卡：缺（被删了 / 之前降级没写进去）就重建。"""
        if not self.use_vector:
            return 0
        with self._index_lock:
            active = self._active_cards()
            try:
                col = self._ensure_chroma()
                count = int(col.count())
            except Exception as e:
                self._chroma_error = str(e)
                try:
                    col = self._open_chroma(recreate=True)
                    count = 0
                except Exception as e2:
                    self._chroma_error = str(e2)
                    return 0
            if not active:
                return count
            if force or count < len(active):
                try:
                    return self._rebuild_vectors(active)
                except Exception as e:
                    self._vector_error = str(e)
                    return 0
            return count

    def warmup(self):
        """串行预热：向量模型 → 重排模型 → 补齐向量索引。

        必须**串行**：本机实测两个 transformers 模型并发加载会炸
        "Cannot copy out of meta tensor"。返回 {"embed": ..., "rerank": ..., "vectorIndex": ...}。

        冷启动期间写进来的卡先落在挂起队列里，模型就绪后由这里的 sync_index()
        一次性补进向量索引（所以写卡不用等预热，预热完索引自然追上）。
        """
        report = {"embed": None, "rerank": None, "vectorIndex": None}
        if self.use_vector:
            try:
                self._embed(["预热"])
                report["embed"] = "ok" if not self._embed_error else "degraded"
            except Exception as e:
                report["embed"] = "failed: %s" % e
        if self.use_rerank:
            scores = self._rerank("预热", ["预热"])
            if scores is not None:
                report["rerank"] = "ok"
            else:
                report["rerank"] = "failed: %s" % (self._rerank_error or "unknown")
        if self.use_vector:
            try:
                report["pending"] = self.sync_index()
            except Exception as e:
                report["pending"] = "failed: %s" % e
            try:
                report["vectorIndex"] = self.ensure_vector_index()
            except Exception as e:
                report["vectorIndex"] = "failed: %s" % e
        return report

    # -- BM25 ------------------------------------------------------------
    def _rebuild_bm25(self):
        """从卡片重建 BM25。纯本地计算（不碰模型），写完再原子换上去。

        「脏了才建」靠代次号：建之前记下 _cards_gen，建完把它写进 _bm25_gen；
        建的过程中又有人写卡（gen 变了）→ 下次照样重建，不会漏。
        """
        error = None
        index = None
        ids = []
        with self._cards_lock:
            gen = self._cards_gen
            active = sorted(
                (c for c in self._cards.values() if c["status"] == STATUS_ACTIVE and c["text"]),
                key=lambda c: c["id"],
            )
        if self.use_bm25:
            ids = [c["id"] for c in active]
            if active:
                try:
                    bm25s = _import_bm25s()
                except Exception as e:
                    error = "bm25s 不可用: %s" % e
                else:
                    try:
                        index = bm25s.BM25()
                        index.index(
                            [tokenize(c["text"] + " " + c["kind"]) for c in active],
                            show_progress=False,
                        )
                    except Exception as e:
                        index = None
                        error = str(e)
        with self._bm25_lock:
            self._bm25 = index
            self._bm25_ids = ids
            self._bm25_error = error
            self._bm25_gen = gen
        return len(ids) if index is not None else 0

    def _bm25_search(self, query, k):
        with self._bm25_lock:
            index, ids = self._bm25, list(self._bm25_ids)
        if not self.use_bm25 or index is None:
            return {}
        try:
            vocab = getattr(index, "vocab_dict", None) or {}
            # ⚠️ 必须先按词表过滤：bm25s 会把不在词表里的词映射成越界 token id 直接报错
            toks = [t for t in tokenize(query) if not vocab or t in vocab]
            if not toks:
                return {}
            scores = index.get_scores(toks)
        except Exception as e:
            self._bm25_error = str(e)
            return {}
        order = sorted(range(len(scores)), key=lambda i: -float(scores[i]))
        out = {}
        for i in order[: max(1, int(k))]:
            if float(scores[i]) > 0:
                out[ids[i]] = float(scores[i])
        self._bm25_error = None
        return out

    # -- 向量检索 --------------------------------------------------------
    def _vector_search(self, query, k):
        if not self.use_vector:
            return {}
        with self._index_lock:
            return self._vector_search_locked(query, k)

    def _vector_search_locked(self, query, k):
        hit = self._vector_search_once(query, k)
        if hit is not None:
            return hit
        # 查询失败（多半是维度冲突 / 索引损坏）→ 重建索引后再试一次
        try:
            self._rebuild_vectors(self._active_cards(), recreate=True)
        except Exception as e:
            self._vector_error = str(e)
            return {}
        hit = self._vector_search_once(query, k)
        return hit or {}

    def _vector_search_once(self, query, k):
        """返回 dict 或者 None（None = 出错了，调用方可以重建后重试）。"""
        try:
            col = self._ensure_chroma()
            count = int(col.count())
            if count <= 0:
                return {}
            vecs = self._embed([query])
            res = col.query(query_embeddings=[vecs[0]], n_results=max(1, min(int(k), count)))
        except Exception as e:
            self._vector_error = str(e)
            return None
        self._vector_error = None
        ids = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        out = {}
        for cid, dist in zip(ids, dists, strict=False):
            # cosine distance -> similarity
            out[str(cid)] = 1.0 - float(dist)
        return out

    # -- 检索 ------------------------------------------------------------
    def _degradation(self):
        reasons = []
        if not self.use_vector:
            reasons.append("向量腿已关闭（--no-vector）")
        elif self._embed_error or self._vector_error:
            reasons.append("向量腿降级：%s" % (self._embed_error or self._vector_error))
        if not self.use_bm25:
            reasons.append("BM25 已关闭（--no-bm25）")
        elif self._bm25_error:
            reasons.append("BM25 降级：%s" % self._bm25_error)
        if not self.use_rerank:
            reasons.append("精排已关闭（--no-rerank）")
        elif self._rerank_error:
            reasons.append("精排降级：%s" % self._rerank_error)
        if self._chroma_error:
            reasons.append("Chroma 异常：%s" % self._chroma_error)
        return reasons

    def search(self, query, top_k=8, mood=None, kind=None, kinds=None, key=None):
        """粗排（RRF 融合）→ 精排（cross-encoder）→ 时间 / 情绪 / 重要度加权。

        返回 dict：{'results': [...], 'path': ..., 'degraded': bool, 'error': str|None}。
        path 说明这次是谁给的候选：hybrid / vector / bm25 / fallback / none。
        两条粗排腿全挂时返回空结果 + 明确的 error（绝不抛给 HTTP 层变 500）。
        """
        result = {"results": [], "path": "none", "degraded": False, "error": None}
        q = str(query or "").strip()
        if not q:
            return result
        with self._index_lock:
            t0 = time.time()
            pool = [
                c
                for c in self._active_cards()
                if (not kind or c["kind"] == kind)
                and (not kinds or c["kind"] in kinds)
                and (not key or c["key"] == key)
            ]
            if not pool:
                return result
            allowed = {c["id"] for c in pool}
            by_id = {c["id"]: c for c in pool}

            # ① 派生索引追平真值：BM25（脏了才重建）+ 挂起的向量变更
            #    —— 写卡不等模型，等的是这里；所以「写完立刻搜」也一定看得到新卡
            try:
                self.sync_index()
            except Exception:
                pass
            # ② 索引可能是空的（第一次跑 / 目录被删 / 之前降级）→ 从 JSON 卡补齐
            if self.use_vector:
                try:
                    self.ensure_vector_index()
                except Exception:
                    pass

            bm = self._bm25_search(q, self.candidate_n)
            vec = self._vector_search(q, self.candidate_n)

            fused = {}
            for rank, (cid, _) in enumerate(sorted(bm.items(), key=lambda kv: -kv[1])):
                if cid in allowed:
                    fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
            for rank, (cid, _) in enumerate(sorted(vec.items(), key=lambda kv: -kv[1])):
                if cid in allowed:
                    fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)

            if bm and vec:
                result["path"] = "hybrid"
            elif vec:
                result["path"] = "vector"
            elif bm:
                result["path"] = "bm25"

            reasons = self._degradation()
            result["degraded"] = bool(reasons)

            # 两条粗排腿都不可用 → 空结果 + 明确 error（而不是 500）
            bm_unusable = (
                (not self.use_bm25) or bool(self._bm25_error) or self._bm25 is None
            )
            vec_unusable = (
                (not self.use_vector) or bool(self._embed_error) or bool(self._vector_error)
            )
            if bm_unusable and vec_unusable:
                detail = "；".join(reasons) or "向量与 BM25 均不可用"
                result["error"] = "记忆检索不可用：%s" % detail
                result["path"] = "none"
                result["degraded"] = True
                return result

            if not fused:  # 两路都没命中 → 退回全量让精排挑
                fused = {c["id"]: 1.0 for c in pool[: self.candidate_n]}
                result["path"] = "fallback"
            cands = sorted(fused.items(), key=lambda kv: -kv[1])[: self.candidate_n]

            rerank_scores = {}
            cands = [(cid, base) for cid, base in cands if cid in by_id]
            raw = self._rerank(q, [by_id[cid]["text"] for cid, _ in cands])
            if raw is not None:
                rerank_scores = {cid: s for (cid, _), s in zip(cands, raw, strict=False)}
            # 精排刚刚可能才降级 → 降级标记必须在它之后再算一次
            result["degraded"] = bool(self._degradation())

            now = _now_ms()
            scored = []
            for cid, base in cands:
                card = by_id[cid]
                logit = rerank_scores.get(cid)
                if logit is not None:
                    # logit 压到 (-1, 1)：直接拿可为负的 logit 乘衰减会把老的不相关记忆顶上来
                    clamped = max(-30.0, min(30.0, logit))
                    rel = 2.0 / (1.0 + math.exp(-clamped)) - 1.0
                    primary = rel
                else:
                    primary = min(1.0, base * 10.0)
                age_days = max(0.0, (now - card["updatedAt"]) / 86400000.0)
                half_life = HALF_LIFE_DAYS.get(card["kind"], DEFAULT_HALF_LIFE)
                decay = 0.5 ** (age_days / max(0.5, half_life))
                m = MOOD_BONUS if (mood and card.get("mood") and card["mood"] == mood) else 1.0
                imp = 0.5 + 0.5 * card.get("importance", 0.5)
                if primary > 0:
                    final = primary * (0.4 + 0.6 * decay) * m * imp
                else:
                    final = primary * 0.5  # 不相关的保持相对顺序，但不乘 decay
                scored.append((final, card, primary, decay, logit))
            scored.sort(key=lambda x: -x[0])

            out = []
            for final, card, primary, decay, logit in scored[: max(1, int(top_k))]:
                card["lastHitAt"] = now
                card["hitCount"] = _as_int(card.get("hitCount")) + 1
                out.append(
                    {
                        **self._public_card(card),
                        "score": round(final, 4),
                        "rel": round(primary, 4),
                        "rerankLogit": round(logit, 4) if logit is not None else None,
                        "decay": round(decay, 4),
                    }
                )
            result["results"] = out
            if out:
                self._save()  # hitCount / lastHitAt 变了
            self.last_search_ms = int((time.time() - t0) * 1000)
            return result

    # -- 持久化 ----------------------------------------------------------
    def _save(self):
        """把内存里的卡片落盘（真值）。持 _cards_lock：写卡路径要的是毫秒级、不等模型。"""
        os.makedirs(self.data_dir, exist_ok=True)
        tmp = self.cards_path + ".tmp"
        try:
            with self._cards_lock:
                data = sorted(self._cards.values(), key=lambda c: c["id"])
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.cards_path)
        except Exception as e:
            print("[memory_sidecar] 卡片落盘失败: %s" % e, flush=True)

    # -- 报告 ------------------------------------------------------------
    def _model_state(self, which):
        if which == "embed":
            if not self.use_vector:
                return "disabled"
            if self._embed_error:
                return "degraded"
            return "ready" if self._embed_ready else "unloaded"
        if not self.use_rerank:
            return "disabled"
        if self._rerank_error:
            return "degraded"
        return "ready" if self._rerank_ready else "unloaded"

    def health(self):
        """给 /health 用的轻量状态：**不抢锁、不碰模型**。

        锁的艺术在这里：模型加载（十几秒）在 _HF_LOCK 里，检索编排在 _index_lock 里，
        而 health 只读计数快照 + 几个状态字段 —— 所以模型正在加载时它照样毫秒级回答
        （这是 A2 的前提：写卡 / 预热都不能把 /health 拖住）。
        """
        counts = self._counts  # 整体替换的快照，读它不需要持锁
        reasons = self._degradation()
        bm25_state = "disabled" if not self.use_bm25 else (
            "degraded" if self._bm25_error else "ready"
        )
        return {
            "ok": True,
            "cards": counts["active"],
            "cardsTotal": counts["total"],
            "pendingIndex": len(self._pending_vectors),
            "models": {
                "embed": self._model_state("embed"),
                "rerank": self._model_state("rerank"),
            },
            "bm25": bm25_state,
            "degraded": bool(reasons),
            "degradation": reasons,
            "dataDir": self.data_dir,
        }

    def stats(self):
        with self._index_lock:
            with self._cards_lock:
                cards = list(self._cards.values())
            kinds = {}
            superseded = 0
            for card in cards:
                if card["status"] == STATUS_ACTIVE:
                    kinds[card["kind"]] = kinds.get(card["kind"], 0) + 1
                else:
                    superseded += 1
            reasons = self._degradation()
            bm25_state = "disabled" if not self.use_bm25 else (
                "degraded" if self._bm25_error else "ready"
            )
            return {
                "cards": len(cards),
                "active": sum(kinds.values()),
                "superseded": superseded,
                "byKind": kinds,
                "dataDir": self.data_dir,
                "cardsFile": self.cards_path,
                "loadError": self._load_error,
                "pendingIndex": len(self._pending_vectors),
                "vector": {
                    "enabled": self.use_vector,
                    "state": self._model_state("embed"),
                    "error": self._embed_error or self._vector_error,
                    "count": self._chroma_count(open_if_possible=True),
                    "model": self.embed_model if self.use_vector else None,
                    "dim": self._vector_dim,
                },
                "bm25": {
                    "enabled": self.use_bm25,
                    "state": bm25_state,
                    "error": self._bm25_error,
                    "docs": len(self._bm25_ids),
                    "stale": self._bm25_dirty(),
                },
                "rerank": {
                    "enabled": self.use_rerank,
                    "state": self._model_state("rerank"),
                    "error": self._rerank_error,
                    "model": self.rerank_model if self.use_rerank else None,
                    "why": self.rerank_reason,
                },
                "degraded": bool(reasons),
                "degradation": reasons,
            }
