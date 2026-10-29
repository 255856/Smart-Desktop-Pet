"""桌宠长期记忆库：向量检索 + 重要性 + 时间衰减 + 冲突检测。

持久化层使用 SQLite + FTS5（原子写盘、O(log N) 全文检索）。
向量后端（TF-IDF/SBERT）仍驻内存，只持久化元数据。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Protocol

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stage 2.9：向量索引（Chroma + bge）+ cross-encoder 重排
# ---------------------------------------------------------------------------
# 向量索引是**派生索引**，不是真相来源 —— 真相永远是下面这张 SQLite 表。
# 导入失败也不能让 memory.py 挂掉：记忆库是桌宠的核心，索引只是加速器。
try:
    from app.brain.vector_index import (  # noqa: E402
        EMBED_MODEL_ID,
        RERANK_MODEL_ID,
        VectorIndex,
        derive_state,
        digest_rows,
        index_dir_for,
        same_index_dir,
    )
    _VECTOR_INDEX_IMPORT_ERROR: Optional[str] = None
except Exception as _exc:  # noqa: BLE001
    VectorIndex = None            # type: ignore[assignment]
    index_dir_for = None          # type: ignore[assignment]
    derive_state = None           # type: ignore[assignment]
    digest_rows = None            # type: ignore[assignment]
    same_index_dir = None         # type: ignore[assignment]
    EMBED_MODEL_ID = ""           # type: ignore[assignment]
    RERANK_MODEL_ID = ""          # type: ignore[assignment]
    _VECTOR_INDEX_IMPORT_ERROR = str(_exc)
    log.warning("向量索引模块导入失败（记忆检索降级到 substring + TF-IDF）：%s",
                _exc)

CATEGORY_CHOICES = ("preference", "fact", "event", "skill", "person", "shared", "other")

# Stage 2.2：记忆作用域 / 生命周期分类 / 检索辅助字段
SCOPE_CHOICES = ("project", "user")
KIND_CHOICES = ("activeTopic", "pendingThought", "memberImpression", "fact")
STATUS_CHOICES = ("active", "superseded")

#: 老 category → 新 kind 的映射（迁移回填用，见 _migrate_schema）
CATEGORY_TO_KIND = {
    "preference": "memberImpression",   # 偏好 = 对这个人的印象
    "person": "memberImpression",       # 人 = 对这个人的印象
    "event": "activeTopic",             # 事件 = 进行中/已发生的话题
    "shared": "activeTopic",            # 共同经历 = 共同话题
    "fact": "fact",
    "skill": "fact",
    "other": "fact",
}

#: Stage 2.2 新列（老库靠 _migrate_schema 用 ALTER TABLE 补齐）
_NEW_COLUMN_DDL: tuple[tuple[str, str], ...] = (
    ("scope",         "scope TEXT NOT NULL DEFAULT 'project'"),
    ("kind",          "kind TEXT NOT NULL DEFAULT 'fact'"),
    ("topic",         "topic TEXT"),
    ("status",        "status TEXT NOT NULL DEFAULT 'active'"),
    ("superseded_by", "superseded_by TEXT"),
    ("hit_count",     "hit_count INTEGER NOT NULL DEFAULT 0"),
    ("last_hit_at",   "last_hit_at REAL"),
)

#: Stage 2.2 新列的索引（不能放进 _MEMORY_SCHEMA —— 老库还没这些列，
#: executescript 会直接报「no such column」把整个加载打断）
_EXTRA_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope)",
    "CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind)",
    "CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status)",
)

#: Stage 2.1：RRF 融合参数（照抄 qq-bridge memory_sidecar/memory_store.py:462-478）
RRF_K = 60
FUSION_TOP_N = 30

# ---------------------------------------------------------------------------
# Stage 2.8：召回**精度**（注入条数 / 相关度门槛 / 门禁阈值）
# ---------------------------------------------------------------------------
# 背景（scripts/eval_memory_recall.py 实测）：
#   旧行为每轮硬塞 6 条，而 6 条里平均只有 ~1.1 条真的跟这句话有关 ——
#   问「记不记得我明天要交房租」会把「主人怕黑」「主人讨厌香菜」一起塞进来。
#   代价是 token 浪费 + 桌宠**自信说错话**的风险（近似陷阱）。

#: 每轮最多注入多少条记忆。Stage 2.1：20 → 6；Stage 2.8：6 → 4。
#: 为什么是 4 而不是 3：实测（同一把尺子 scripts/eval_memory_recall.py）
#: 注入上限收到 3 会把「答案排在第 4 名」的那条裁掉
#: （「我是做什么工作的」→ 真答案「主人是程序员…」排第 4），
#: recall@6 从 0.857 掉到 0.810 —— 低于 0.85 的红线。4 是保召回的最小值。
DEFAULT_MAX_INJECT = 4

#: 补位行的相关度门槛：融合分 < 顶行融合分 × 该系数 → **不注入**。
#: 顶行（过了门禁的第一名）永远注入；其余行只有「离顶行够近」才值得占 token。
#: 0.30 是量出来的（见 scripts/eval_memory_recall.py --curve 的分数比曲线）：
#:   ≤ 0.28 → 多带噪声行（precision 掉）；≥ 0.32 → 把真答案裁掉（recall 掉到 0.810）。
DEFAULT_FILLER_RATIO = 0.30

#: 语义腿门禁阈值：余弦相似度 **≥** 该值才算「跟记忆库有关系」。
#: 旧判断是「相似度 > 0 即算有关系」—— 真正的 embedding 对几乎任何 query
#: 都会给一个小的正相似度，那个门禁等于没有门禁（安全位置问题）。
SEMANTIC_GATE_THRESHOLD = 0.35

# ---------------------------------------------------------------------------
# Stage 2.9：向量检索（dense 腿）+ cross-encoder 重排 —— 阈值全部**量出来**
# ---------------------------------------------------------------------------
# 语料：scripts/eval_memory_recall.py 的 32 条合成记忆 + 20 条标注查询。
# 工具：python scripts/eval_memory_recall.py --rerank-curve

#: dense 腿（Chroma 语义检索）的门禁阈值：query 与最相似那条记忆的
#: 余弦相似度 **≥** 该值，才算「跟记忆库有关系」。
#: 0.40 是量出来的：
#:   有答案的问题里，最相似那条的相似度**下限** = 0.456；
#:   四个无关问题（今天股市涨了吗 / 量子力学怎么学 / 推荐一部好电影 / 帮我写一首诗）
#:   的相似度**上限** = 0.376。
#: → 0.40 落在中间，两边都留了余量（0.024 / 0.056）。
DENSE_GATE_MIN = 0.40

#: 送进 cross-encoder 的候选数（按融合分取前 K 名）。
#: 实测：每条查询的**标注相关行**在融合排名里都进前 9，15 留了余量；
#: 重排 15 对约 200 ms（GPU）/ 213 ms（CPU）—— 不阻塞聊天。
RERANK_CANDIDATES = 15

#: 补位行的重排分门槛：重排分 < 顶行重排分 × 该系数 → **不注入**。
#: 0.12 是量出来的（--rerank-curve 的曲线给出的可行窗口）：
#:   上界 ≈ 0.19 —— 再高就把「我什么时候交房租还有体检的事」里的体检那条裁掉
#:                   （PLAN Stage 2 的验收行要求「相关 ≥2」）；
#:   下界 ≈ 0.088 —— 再低就把近似陷阱「主人以前的手机是 iPhone 12…」放回来。
#: 0.12 落在窗口中间：recall@6 0.952 / 近似陷阱 1/3 / precision@6 0.83。
RERANK_CUT_RATIO = 0.12

#: 语义腿开门的最低重排分：cross-encoder 说「全都不相关」→ 关门走 fallback。
RERANK_MIN_SCORE = 0.02

#: 强语义信号：重排分 ≥ 该值时，即使 dense 相似度没到阈值也开门
#: （词面完全够不着、但语义上一模一样的场景，例如
#:  「我不能吃什么」→「主人不吃辣」，实测重排分 0.976）。
RERANK_STRONG_SCORE = 0.50

#: **语义地板**（裁剪的第二条规则，量出来的）：
#: 一行如果 ① 与 query **字面命中**（共享 CJK 二元组 / 英文整词）
#: ② 与 query 的 dense 相似度 ≥ 顶行 dense 相似度 × 该系数
#: ③ 且该相似度 ≥ DENSE_GATE_MIN（绝对门槛）
#: → 即使 cross-encoder 给它的分很低，也**不丢掉**。
#:
#: 为什么需要这条：cross-encoder 对**复合问句**偏严 ——
#: 「我什么时候交房租还有体检的事」里那条「下周要去医院做体检复查甲状腺」
#: 的重排分只有 0.0085（它觉得这条跟问题无关），但它明明是问题的一半。
#: 只用重排分裁剪会把真答案裁掉（PLAN Stage 2 的验收行会挂）。
#:
#: 实测（同一批 32 条语料）：
#:   「我什么时候交房租还有体检的事」→ 体检那条 dense 比值 0.827（要**留**）
#:   「我的体检报告放在哪」          → 陷阱「体检复查」dense 比值 0.690（要**丢**）
#: → 0.75 落在两者中间（余量 0.077 / 0.060）。
DENSE_KEEP_RATIO = 0.75

#: 融合分权重（排序 + 裁剪都用它）：
#:   W_REL —— 最强证据那一腿的相对强度（0..1）
#:   W_RRF —— 多路一致性（RRF 相对分，0..1）
#:   W_IMP —— 重要性（0..1，弱先验，只用来在分数接近时定序）
W_REL = 0.6
W_RRF = 0.3
W_IMP = 0.1


def user_memory_dir() -> Path:
    """用户级记忆目录（默认 ~/.desktop-pet）。

    环境变量 DESKTOP_PET_HOME 可覆盖（测试用，避免往真 home 里写）。
    """
    override = (os.environ.get("DESKTOP_PET_HOME") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".desktop-pet"


def user_memory_db_path() -> Path:
    """用户级记忆库路径（scope='user' 的行落在这里）。"""
    return user_memory_dir() / "user_memory.db"


def _sbert_available() -> bool:
    """sentence-transformers 装了吗？

    只查 spec，不真的 import —— 没装就直接降级，不新增任何依赖。
    """
    try:
        import importlib.util
        return importlib.util.find_spec("sentence_transformers") is not None
    except Exception:  # noqa: BLE001
        return False


# SQLite schema：FTS5 虚拟表跟主表 1:1（trigger 同步），全文检索 O(log N)
_MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    content         TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'other',
    importance      REAL NOT NULL DEFAULT 0.5,
    created_at      REAL NOT NULL,
    last_access_ts  REAL NOT NULL,
    access_count    INTEGER NOT NULL DEFAULT 0,
    scope           TEXT NOT NULL DEFAULT 'project',
    kind            TEXT NOT NULL DEFAULT 'fact',
    topic           TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    superseded_by   TEXT,
    hit_count       INTEGER NOT NULL DEFAULT 0,
    last_hit_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at);
CREATE INDEX IF NOT EXISTS idx_memories_category    ON memories(category);
"""

# Tokenize：英文整词 + CJK 单字与相邻二元组（零依赖；中文不引入分词器时的折中）
_TOKEN_RE = re.compile(
    r"[A-Za-z]+|[\u4e00-\u9fff]+",
)
# 简单停用词（中英混合）
_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "in", "on", "at", "for", "with", "by", "from", "as", "into",
    "and", "or", "but", "not", "no", "do", "does", "did", "have", "has", "had",
    "i", "you", "he", "she", "we", "they", "it", "this", "that", "these", "those",
    "的", "了", "和", "是", "在", "我", "你", "他", "她", "它", "们", "就", "都",
    "也", "不", "有", "没", "把", "被", "给", "对", "还", "要", "会", "能",
})


def _tokenize(text: str) -> list[str]:
    """英文小写词 + CJK 单字 & 相邻二元组；过滤停用词与单字符英文。"""
    out: list[str] = []
    for chunk in _TOKEN_RE.findall(text.lower()):
        if re.match(r"[A-Za-z]", chunk):
            if len(chunk) == 1 or chunk in _STOPWORDS:
                continue
            out.append(chunk)
            continue
        # CJK 连续串：单字 + 相邻二元组
        chars = [c for c in chunk]
        for c in chars:
            if c not in _STOPWORDS:
                out.append(c)
        for i in range(len(chars) - 1):
            bi = chars[i] + chars[i + 1]
            if not (chars[i] in _STOPWORDS and chars[i + 1] in _STOPWORDS):
                out.append(bi)
                out.append(bi)
    return out


def _bigrams(text: str) -> set[str]:
    """CJK 相邻二元组集合（用于判定两个词是否真的相关）。"""
    chars = re.findall(r"[\u4e00-\u9fff]", text)
    return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}


def _has_word_overlap(query: str, items: list) -> bool:
    """query 里的 ASCII 词有没有出现在某条记忆里（纯英文 query 用）。

    CJK 走二元组规则（见 _bigrams 的注释），英文走整词 —— 否则
    "rent" 会跟 "parent" 之类的短词串味。
    """
    q_words = {t for t in _tokenize(query or "") if t.isascii() and len(t) > 1}
    if not q_words:
        return False
    for it in items:
        c_words = {t for t in _tokenize(it.content) if t.isascii() and len(t) > 1}
        if q_words & c_words:
            return True
    return False


def _normalize_scores(scores: dict[str, float]) -> dict[str, float]:
    """把一路的原始分归一化到 0..1（除以本路最高分）。

    归一化的意义：各路的量纲不一样（substring 路 0..3，向量路 0..1），
    归一之后才能比较「哪一路更支持这一行」，也才能把多路合成一个分数。
    """
    if not scores:
        return {}
    top = max(scores.values())
    if top <= 0:
        return {k: 0.0 for k in scores}
    return {k: v / top for k, v in scores.items()}


#  MemoryItem


@dataclass
class MemoryItem:
    id: str
    content: str
    category: str = "other"
    importance: float = 0.5
    created_at: float = field(default_factory=time.time)
    last_access_ts: float = field(default_factory=time.time)
    access_count: int = 0
    # Stage 2.2：作用域 / 生命周期分类 / 检索辅助
    scope: str = "project"
    kind: str = "fact"
    topic: Optional[str] = None
    status: str = "active"
    superseded_by: Optional[str] = None
    hit_count: int = 0
    last_hit_at: Optional[float] = None

    def touch(self) -> None:
        """被命中检索时调用，更新访问热度。"""
        self.last_access_ts = time.time()
        self.access_count += 1


#  向量检索后端（Strategy 模式）


class VectorBackend(Protocol):
    """向量检索后端协议。"""

    name: str

    def fit(self, docs: list[str]) -> None: ...

    def encode(self, texts: list[str]) -> "list[list[float]]": ...

    def similarity(self, query_vec: list[float], doc_vecs: list[list[float]]) -> list[float]: ...


class SubstringBackend:
    """子串匹配（兜底，无依赖）。"""

    name = "substring"

    def __init__(self) -> None:
        self._docs: list[str] = []

    def fit(self, docs: list[str]) -> None:
        self._docs = [d.lower() for d in docs]

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] for _ in texts]

    def similarity(self, query_vec: list[float], doc_vecs: list[list[float]]) -> list[float]:
        # query_vec 在此协议里实际就是 query 文本（见 _search 实现）；此处仅返回 0 占位
        return [0.0] * len(self._docs)


class TfidfBackend:
    """TF-IDF + 余弦相似度后端（numpy 实现，零外部依赖）。

    适合：
        - < 5000 条记忆；
        - 中文为主；
        - 不下载深度学习模型也能跑语义近邻。
    """

    name = "tfidf"

    def __init__(self) -> None:
        self._vocab: dict[str, int] = {}
        self._idf: list[float] = []
        self._doc_vecs: list[list[float]] = []
        self._norms: list[float] = []

    def fit(self, docs: list[str]) -> None:
        """构建词表 + IDF + 文档向量。"""
        tokenized = [_tokenize(d) for d in docs]
        # 词表
        vocab: dict[str, int] = {}
        for toks in tokenized:
            for t in toks:
                if t not in vocab:
                    vocab[t] = len(vocab)
        self._vocab = vocab
        if not vocab:
            self._idf = []
            self._doc_vecs = []
            self._norms = []
            return
        # df
        df = [0] * len(vocab)
        for toks in tokenized:
            seen = set(toks)
            for t in seen:
                if t in vocab:
                    df[vocab[t]] += 1
        n = max(1, len(docs))
        self._idf = [math.log((n + 1) / (d + 1)) + 1.0 for d in df]   # smooth IDF
        # doc vectors
        self._doc_vecs = []
        self._norms = []
        for toks in tokenized:
            v = [0.0] * len(vocab)
            cnt = Counter(toks)
            for t, c in cnt.items():
                if t in vocab:
                    v[vocab[t]] = (1.0 + math.log(c)) * self._idf[vocab[t]]
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            self._doc_vecs.append(v)
            self._norms.append(norm)

    def encode(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            toks = _tokenize(text)
            v = [0.0] * len(self._vocab)
            cnt = Counter(toks)
            for t, c in cnt.items():
                if t in self._vocab:
                    v[self._vocab[t]] = (1.0 + math.log(c)) * self._idf[self._vocab[t]]
            out.append(v)
        return out

    def similarity(self, query_vec: list[float], doc_vecs: list[list[float]]) -> list[float]:
        q_norm = math.sqrt(sum(x * x for x in query_vec)) or 1.0
        out: list[float] = []
        for dv, dn in zip(doc_vecs, self._norms, strict=False):
            dot = sum(a * b for a, b in zip(query_vec, dv, strict=False))
            out.append(dot / (q_norm * dn))
        return out


class SentenceTransformerBackend:
    """sentence-transformers 后端（可选，依赖 huggingface 模型）。

    自动降级：模型未安装 / 下载失败 → TfidfBackend。
    """

    name = "sbert"

    def __init__(self, model_name: str = "paraphrase-multilingual-MiniLM-L12-v2") -> None:
        self.model_name = model_name
        self._model = None
        self._doc_vecs: list[list[float]] = []

    def _ensure_model(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer  # type: ignore
            log.info("加载 sentence-transformers 模型 %s ...", self.model_name)
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def fit(self, docs: list[str]) -> None:
        try:
            model = self._ensure_model()
            vecs = model.encode(docs, normalize_embeddings=True, show_progress_bar=False)
            self._doc_vecs = [v.tolist() for v in vecs]
        except Exception as e:  # noqa: BLE001
            log.warning("sentence-transformers 加载失败：%s", e)
            raise

    def encode(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure_model()
        vecs = model.encode(texts, normalize_embeddings=True, show_progress_bar=False)
        return [v.tolist() for v in vecs]

    def similarity(self, query_vec: list[float], doc_vecs: list[list[float]]) -> list[float]:
        # vecs 已 normalize，直接点积 = cosine
        out: list[float] = []
        for dv in doc_vecs:
            out.append(sum(a * b for a, b in zip(query_vec, dv, strict=False)))
        return out


def make_backend(name: str = "auto") -> VectorBackend:
    """构造检索后端。auto 模式：sbert 可用则用，否则降级到 tfidf。"""
    name = (name or "auto").lower()
    if name in ("sbert", "sentence-transformer", "sentence_transformer"):
        try:
            return SentenceTransformerBackend()
        except Exception:
            log.info("sbert 不可用，降级到 tfidf")
            return TfidfBackend()
    if name in ("tfidf", "tf-idf"):
        return TfidfBackend()
    if name in ("substring", "none"):
        return SubstringBackend()
    # auto：**不再自动选 sbert**。
    # 老行为是「装了 sentence-transformers 就用 paraphrase-multilingual-MiniLM」，
    # 但那个模型**本机没有缓存**，一启用就是 470 MB 下载（而且在启动时阻塞）。
    # 现在语义检索由**向量索引**负责（bge-small-zh-v1.5 + bge-reranker-base），
    # 索引不可用时退回 TF-IDF —— 对外行为与今天一致（今天那条腿其实也是死的：
    # Keras 3 让 transformers 直接 import 失败）。
    return TfidfBackend()


#  MemoryStore


class MemoryStore:
    """长期记忆：JSON 落盘 + 可插拔向量后端 + 重要性评分 + 时间衰减。

    v31 (2026-10-17): per-character 隔离。传 character_name 后, data_file 自动
    派生为 `data/memory_{name}.db` (老 .json 同步转 .db)。不同 character 各自
    一个 db, 完全隔离。

    不传 character_name (空字符串) 时走老路径, 行为兼容:
      - `data/memory.json` 仍迁到 `data/memory.db` (老记录 shared)
      - `data/memory.db` 留作 legacy shared db, 主人可以手动保留或拷给新角色
    """
    # 注入 system prompt 时各类别的优先级权重
    CATEGORY_PRIORITY = {
        "preference": 0,
        "fact": 1,
        "event": 2,
        "skill": 3,
        "person": 4,
        "other": 5,
    }

    def __init__(self, data_file: str | Path,
                 max_inject: int = DEFAULT_MAX_INJECT,
                 backend_name: str = "auto",
                 character_name: str = "",
                 *,
                 filler_ratio: float = DEFAULT_FILLER_RATIO,
                 semantic_gate_threshold: float = SEMANTIC_GATE_THRESHOLD,
                 vector_index: bool = True,
                 rerank_cut_ratio: float = RERANK_CUT_RATIO):
        """
        Args:
            data_file: JSON 落盘路径 (e.g. "data/memory.json")。
            max_inject: 注入 system prompt 的最大条数（Stage 2.1: 20 → 6；
                Stage 2.8: 6 → 4，见 DEFAULT_MAX_INJECT 的注释）。
            backend_name: "auto" / "tfidf" / "sbert" / "substring"。
            filler_ratio: 补位行的相关度门槛（融合分 < 顶行 × 该值 → 不注入）。
            semantic_gate_threshold: 语义腿门禁阈值（相似度 ≥ 该值才算相关）。
            rerank_cut_ratio: Stage 2.9 — 补位行的**重排分**门槛
                （重排分 < 顶行重排分 × 该系数 → 不注入）。
            vector_index: Stage 2.9 — 是否启用向量索引（Chroma + bge）。
                True（默认）= 索引存在就用，不存在就退回 substring + tfidf；
                False = 完全不用向量索引（消融台 / 降级测试用）。
            character_name: v31 — 角色名。非空时 data_file 派生为
                `data/memory_{name}.db` (per-character 隔离)。空走老路径。
        """
        # 兼容旧的 .json 入参，检测后缀转 .db
        _p = Path(data_file)
        if _p.suffix.lower() == ".json":
            log.info("MemoryStore: 检测到 .json data_file, 自动迁向 %s", _p.with_suffix(".db"))
            data_file = _p.with_suffix(".db")
        # v31: per-character 隔离 — db 文件按 character_name 分文件
        if character_name:
            _pp = Path(data_file)
            data_file = _pp.with_name(f"{_pp.stem}_{character_name}{_pp.suffix}")
            log.info("MemoryStore: character_name=%r → db 路径 %s",
                     character_name, data_file)
        self.data_file = Path(data_file)
        self.character_name = character_name
        self.max_inject = int(max_inject)
        # Stage 2.8：补位行门槛 + 语义门禁阈值（可按角色/场景调）
        self.filler_ratio = float(filler_ratio)
        self.semantic_gate_threshold = float(semantic_gate_threshold)
        self.backend: VectorBackend = make_backend(backend_name)
        self.backend_name = backend_name
        self._items: list[MemoryItem] = []
        self._lock = threading.RLock()
        self._dirty = False
        # 向量索引是否标脏（标脏后延迟到检索前重建，见 _ensure_backend_fresh）
        self._index_dirty = False
        # 最近一次落盘是否成功（remember_fact 据此决定能不能报「已记住」）
        self.last_save_ok = True
        # Stage 2.7：最近一次 add() 是不是「重复」（内容已存在，没新建行）。
        # 工具据此把「已记住」改成「已经记过了」—— 原来去重时返回了一个
        # 从没存进库的 id，桌宠说「已记住」而库里根本没有那条记录。
        self.last_add_was_duplicate = False
        # Stage 2.1：本轮主人说的话（system_block() 无参调用时用它做召回）
        self._current_query: str = ""
        # Stage 2.2：scope='user' 的记忆库（懒建 + 缓存，~/.desktop-pet/user_memory.db）
        self._user_store: Optional["MemoryStore"] = None
        # Stage 2.1：融合用的后端（懒建 + 缓存；sbert 不可用时降级到 tfidf）
        self._fusion_tfidf: Optional[VectorBackend] = None
        self._fusion_sbert: Optional[VectorBackend] = None
        self._fusion_ready: bool = False
        self._fusion_degraded: tuple[str, ...] = ()
        #: 最近一次融合**实际参与**的后端 / **被降级**的后端（可观测：面板与测试都看它）
        self.last_fusion_backends: tuple[str, ...] = ()
        self.last_fusion_degraded: tuple[str, ...] = ()
        #: 最近一次召回的诊断（Stage 2.8）：注入了几条、最弱那条离顶行多近。
        #: 「最弱那条 / 顶行」越接近 1 → 注入里混进了越像噪声的行
        #: （近似陷阱的早期信号，值得盯）。
        self.last_recall_stats: dict = {}
        # ---- Stage 2.9：向量索引（派生索引 · 可重建 · 可降级）----
        # ⚠️ 构造期**什么都不做**：不建目录、不加载模型、不碰 chroma。
        #    ① 桌宠启动不能被索引拖慢；② 记忆库构造**绝不能因为索引失败**；
        #    ③ 测试跑完 data/ 必须字节一致（索引目录只由显式重建创建）。
        self._vector_enabled = bool(vector_index)
        self._vector_index = None            # VectorIndex 实例（懒建）
        self._vector_status: dict = {}        # 最近一次状态（面板/测试看它）
        self._vector_sync_report: dict = {}   # 最近一次漂移检测报告
        self._vector_in_sync = False          # 索引是否与 SQLite 一致
        self._dense_leg_ready = False         # 本轮 dense 腿是否可用
        self._dense_max_sim = 0.0             # 本轮 dense 腿最大相似度（门禁用）
        self._dense_sims: dict = {}           # 本轮 dense 腿的逐行相似度
        self._lexical_hit_ids: set = set()    # 本轮**字面命中**的行 id
        self._rerank_scores: dict = {}        # 本轮重排分（id → 分）
        self._rerank_top = 0.0                # 本轮重排的顶行分
        self._active_query = ""               # 本轮 query（重排要用）
        #: 是否启用 cross-encoder 重排（消融台要能单独关掉它做对照）
        self.rerank_enabled = True
        #: 补位行的重排分门槛（默认 RERANK_CUT_RATIO；评测台扫描这个值）
        self.rerank_cut_ratio = float(rerank_cut_ratio)
        self._load()
        self._refit_backend()
        # 索引目录已经存在 → **后台线程**预热（绝不阻塞启动）
        try:
            if (self._vector_enabled and self.vector_index_dir is not None
                    and self.vector_index_dir.is_dir()):
                self.warmup_vector_index(blocking=False)
        except Exception as e:  # noqa: BLE001
            log.debug("向量索引预热跳过：%s", e)

    def _connect(self) -> sqlite3.Connection:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.data_file))
        # synchronous=NORMAL：默认 synchronous=FULL 每次 commit 都 fsync，
        # 实测单条 insert+commit 要 6.6 ms（整段 add 的耗时几乎全在这）。
        # 记忆库是可丢的本地数据（丢了只是主人少记了几件事），
        # 不值得为每次写入付 fsync。
        # 注意：不加 journal_mode=WAL —— 每次连接都执行这条 pragma 反而更慢
        #（要加锁 + 查文件头），而记忆库没有跨进程读者，用不上 WAL。
        try:
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:  # pragma: no cover - 老 SQLite 不支持
            pass
        conn.executescript(_MEMORY_SCHEMA)
        # Stage 2.2：老库补列（幂等 + 先备份，见 _migrate_schema）
        self._migrate_schema(conn)
        conn.commit()
        return conn

    # ---------------------- Stage 2.2：schema 迁移 ----------------------

    def _backup_db(self) -> Optional[Path]:
        """迁移前把 db 复制成带时间戳的备份（数据红线：先备份，再改 live 库）。

        库还不存在 / 是空文件时不需要备份（返回 None）。
        备份失败 → 抛异常（宁可不迁移，也不能在没备份的情况下改用户数据）。
        """
        src = self.data_file
        try:
            if not src.is_file() or src.stat().st_size == 0:
                return None
        except OSError:
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bak = src.with_name(f"{src.name}.bak-{stamp}")
        n = 1
        while bak.exists():
            bak = src.with_name(f"{src.name}.bak-{stamp}-{n}")
            n += 1
        shutil.copy2(src, bak)
        log.info("MemoryStore: 迁移前备份 %s → %s", src, bak)
        return bak

    def _migrate_schema(self, conn: sqlite3.Connection) -> bool:
        """给老库补 Stage 2.2 的新列并回填。返回是否真的改了库。

        - **幂等**：列都在就直接返回 False（跑第二次不动手，也不会再备份）。
        - **先备份**：改 live 库之前把 db 复制成带时间戳的备份（_backup_db）。
        - **行数不变**：只加列 + UPDATE，不删行、不改老字段。
        """
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
        except sqlite3.Error:
            return False
        if not cols:
            return False
        missing = [(name, ddl) for name, ddl in _NEW_COLUMN_DDL if name not in cols]
        changed = False
        if missing:
            # 数据红线：先备份再改
            self._backup_db()
            for _name, ddl in missing:
                try:
                    conn.execute(f"ALTER TABLE memories ADD COLUMN {ddl}")
                except sqlite3.Error as e:  # pragma: no cover - 老 SQLite 语法差异
                    log.warning("MemoryStore: 加列 %s 失败：%s", _name, e)
                    return False
            # 回填（此刻表里全是老行）：
            #   scope='project' / status='active' / kind 按 category 映射
            conn.execute("UPDATE memories SET scope='project' "
                         "WHERE scope IS NULL OR scope=''")
            conn.execute("UPDATE memories SET status='active' "
                         "WHERE status IS NULL OR status=''")
            conn.execute(
                "UPDATE memories SET kind = CASE category "
                "WHEN 'preference' THEN 'memberImpression' "
                "WHEN 'person' THEN 'memberImpression' "
                "WHEN 'event' THEN 'activeTopic' "
                "WHEN 'shared' THEN 'activeTopic' "
                "ELSE 'fact' END")
            conn.execute("UPDATE memories SET hit_count=0 WHERE hit_count IS NULL")
            conn.commit()
            log.info("MemoryStore: schema 迁移完成（补列 %s）",
                     [n for n, _ in missing])
            changed = True
        # 新列的索引单独建（不能放进 _MEMORY_SCHEMA：老库还没有这些列，
        # executescript 会直接报「no such column」把整个加载打断）
        for sql in _EXTRA_INDEX_SQL:
            try:
                conn.execute(sql)
            except sqlite3.Error as e:  # pragma: no cover
                log.debug("建索引失败（忽略）：%s", e)
        conn.commit()
        return changed

    def _migrate_from_json(self) -> int:
        """从旧 JSON 文件导入记忆到 SQLite(若有)。返回导入条数。"""
        legacy = self.data_file.with_suffix(".json")
        if not legacy.is_file():
            return 0
        try:
            raw = json.loads(legacy.read_text("utf-8"))
            items: list[MemoryItem] = []
            for r in raw:
                r.setdefault("importance", 0.5)
                r.setdefault("last_access_ts", r.get("created_at", time.time()))
                r.setdefault("access_count", 0)
                it = MemoryItem(**r)
                # Stage 2.2：老 JSON 没有 kind → 按 category 映射
                it.kind = CATEGORY_TO_KIND.get(it.category, "fact")
                it.scope = "project"
                it.status = "active"
                items.append(it)
        except Exception as e:  # noqa: BLE001
            log.warning("MemoryStore: legacy JSON 读失败(%s),跳过迁移", e)
            return 0
        if not items:
            legacy.unlink(missing_ok=True)
            return 0
        try:
            conn = self._connect()
            try:
                rows = [(it.id, it.content, it.category, it.importance,
                         it.created_at, it.last_access_ts, it.access_count,
                         it.scope, it.kind, it.topic, it.status,
                         it.superseded_by, it.hit_count, it.last_hit_at)
                        for it in items]
                conn.executemany(
                    "INSERT OR REPLACE INTO memories"
                    "(id, content, category, importance, created_at, "
                    "last_access_ts, access_count, scope, kind, topic, "
                    "status, superseded_by, hit_count, last_hit_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("MemoryStore: 写 DB 失败(%s),保留 JSON", e)
            return 0
        try:
            legacy.unlink(missing_ok=True)
            log.info("MemoryStore: 已迁移 %d 条记忆,删除 %s", len(items), legacy)
        except OSError:
            pass
        return len(items)

    #: 读表用的列顺序（Stage 2.2 加了 scope/kind/topic/status/...）
    _SELECT_COLUMNS = (
        "id", "content", "category", "importance", "created_at",
        "last_access_ts", "access_count", "scope", "kind", "topic",
        "status", "superseded_by", "hit_count", "last_hit_at",
    )

    @staticmethod
    def _row_to_item(row) -> MemoryItem:
        """一行 DB 记录 → MemoryItem（老行缺字段时用默认值兜底）。"""
        return MemoryItem(
            id=row[0], content=row[1], category=row[2], importance=row[3],
            created_at=row[4], last_access_ts=row[5], access_count=row[6],
            scope=row[7] or "project", kind=row[8] or "fact", topic=row[9],
            status=row[10] or "active", superseded_by=row[11],
            hit_count=int(row[12] or 0), last_hit_at=row[13],
        )

    def _load(self) -> None:
        if not self.data_file.is_file():
            # JSON 还在 + DB 不在 → 迁
            self._migrate_from_json()
            if not self.data_file.is_file():
                return
        try:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "SELECT " + ", ".join(self._SELECT_COLUMNS) +
                    " FROM memories"
                )
                self._items = [
                    self._row_to_item(row)
                    for row in cur.fetchall()
                ]
            finally:
                conn.close()
            log.info("MemoryStore: 载入 %d 条记忆(backend=%s)",
                     len(self._items), self.backend.name)
        except Exception as e:  # noqa: BLE001
            log.warning("载入记忆失败:%s", e)

    def _save(self) -> bool:
        """整表重写。返回是否成功。

        原来只 log.warning 吞掉异常,调用方(remember_fact 工具)无从得知,
        于是磁盘只读/写满时,桌宠照样对主人说「已记住」——重启即消失。

        注意：单条增删走 _insert_item / _delete_item（只动一行），
        这里留给「批量清理 / 合并」这类一次性重写。
        """
        try:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM memories")
                conn.executemany(
                    "INSERT INTO memories"
                    "(id, content, category, importance, created_at, "
                    "last_access_ts, access_count, scope, kind, topic, "
                    "status, superseded_by, hit_count, last_hit_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [(it.id, it.content, it.category, it.importance,
                      it.created_at, it.last_access_ts, it.access_count,
                      it.scope, it.kind, it.topic, it.status,
                      it.superseded_by, it.hit_count, it.last_hit_at)
                     for it in self._items],
                )
                conn.commit()
                return True
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("保存记忆失败:%s", e)
            return False

    def _insert_item(self, item: MemoryItem) -> bool:
        """只 INSERT 这一条。

        原来每次 add() 都 DELETE 全表 + 全量 re-INSERT（O(N) 写），
        实测累积成本 0.39s@50 → 2.01s@200 → 5.37s@400（超线性）。
        """
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO memories"
                    "(id, content, category, importance, created_at, "
                    "last_access_ts, access_count, scope, kind, topic, "
                    "status, superseded_by, hit_count, last_hit_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (item.id, item.content, item.category, item.importance,
                     item.created_at, item.last_access_ts, item.access_count,
                     item.scope, item.kind, item.topic, item.status,
                     item.superseded_by, item.hit_count, item.last_hit_at),
                )
                conn.commit()
                return True
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("保存记忆失败:%s", e)
            return False

    def _delete_item(self, mid: str) -> bool:
        """只 DELETE 这一条。"""
        try:
            conn = self._connect()
            try:
                conn.execute("DELETE FROM memories WHERE id=?", (mid,))
                conn.commit()
                return True
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("删除记忆失败:%s", e)
            return False

    def _update_items(self, ids: list[str]) -> bool:
        """只 UPDATE 命中的几行（访问热度）。

        原来每次检索（touch 之后）都整表重写一遍。
        """
        if not ids:
            return True
        try:
            conn = self._connect()
            try:
                by_id = {i.id: i for i in self._items}
                rows = []
                for mid in ids:
                    it = by_id.get(mid)
                    if it is None:
                        continue
                    rows.append((it.last_access_ts, it.access_count, it.id))
                if not rows:
                    return True
                conn.executemany(
                    "UPDATE memories SET last_access_ts=?, access_count=? "
                    "WHERE id=?", rows,
                )
                conn.commit()
                return True
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("更新记忆访问热度失败:%s", e)
            return False

    def _bump_hits(self, ids: list[str]) -> None:
        """记录命中次数（Stage 2.2：hit_count / last_hit_at）。

        召回命中时才计数 —— 这是「这条记忆真的被用上了」的证据，
        跟 access_count（被检索到就 +1）不是一回事。
        """
        if not ids:
            return
        now = time.time()
        with self._lock:
            by_id = {i.id: i for i in self._items}
            rows = []
            for mid in ids:
                it = by_id.get(mid)
                if it is None:
                    continue
                it.hit_count += 1
                it.last_hit_at = now
                rows.append((it.hit_count, it.last_hit_at, it.id))
            if not rows:
                return
        try:
            conn = self._connect()
            try:
                conn.executemany(
                    "UPDATE memories SET hit_count=?, last_hit_at=? WHERE id=?",
                    rows,
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("更新记忆命中次数失败：%s", e)

    def _ensure_backend_fresh(self) -> None:
        """索引标脏时重建一次（延迟到真正要用之前）。

        原来每次 add() 都立刻 _refit_backend()：重新分词 + 重建 N×V 稠密
        向量（tfidf 后端下 O(N·V) 纯 Python 点积）。连着加 10 条就是重建
        10 次。改成「标脏 + 用之前才重建」：检索结果与原来完全一致
        （search 之前一定会先刷新），但连着加 N 条只重建 1 次。
        """
        with self._lock:
            if not self._index_dirty:
                return
            self._refit_backend()
            self._index_dirty = False

    def _refit_backend(self) -> None:
        """重新拟合向量后端（在 load / add / remove 之后调用）。"""
        # Stage 2.1：后端换了 → 融合用的后端缓存也要重建
        self._fusion_ready = False
        docs = [i.content for i in self._items]
        if not docs:
            return
        try:
            self.backend.fit(docs)
        except Exception as e:  # noqa: BLE001
            log.warning("向量后端 fit 失败：%s，降级到 tfidf", e)
            self.backend = TfidfBackend()
            try:
                self.backend.fit(docs)
            except Exception:  # noqa: BLE001
                self.backend = SubstringBackend()

    def set_backend(self, backend_name: str) -> None:
        """运行时切换检索后端。"""
        with self._lock:
            self.backend = make_backend(backend_name)
            self.backend_name = backend_name
            self._refit_backend()
            self._index_dirty = False

    def add(self, content: str, category: str = "other",
            importance: float = 0.5, scope: str = "project",
            kind: str = "", topic: str = "") -> MemoryItem:
        """添加一条记忆。importance ∈ [0, 1]。

        Stage 2.2：scope 决定落在哪个库（project=本库 / user=用户级库），
        kind 是生命周期分类（不给就按 category 映射），topic 是可选检索标签。
        """
        content = content.strip()
        if category not in CATEGORY_CHOICES:
            category = "other"
        importance = max(0.0, min(1.0, float(importance)))
        scope = (scope or "project").strip().lower()
        if scope not in SCOPE_CHOICES:
            scope = "project"
        kind = (kind or "").strip() or CATEGORY_TO_KIND.get(category, "fact")
        if kind not in KIND_CHOICES:
            kind = "fact"
        item = MemoryItem(
            id=f"m{uuid.uuid4().hex[:10]}",
            content=content,
            category=category,
            importance=importance,
            scope=scope,
            kind=kind,
            topic=(topic or "").strip() or None,
        )
        with self._lock:
            # Stage 2.7：完全相同的文本不重复记。
            # ⚠️ 原来这里 return 的是**刚造出来、从来没存进库**的 item ——
            #    工具据此回「已记住」，可数据库里根本没有那条记录
            #    （主人重启就没了，模型拿到的 id 也查不到）。
            # 现在返回**库里真存在的那条**（id 真实有效），
            # 工具据此说「已经记过了」，并且**不新建行**。
            for existing in self._items:
                if existing.content == content:
                    log.debug("记忆去重：%s", content[:30])
                    self.last_save_ok = True   # 已存在，无需再写盘
                    self.last_add_was_duplicate = True
                    return existing
            self.last_add_was_duplicate = False
            self._items.append(item)
            # 只写这一行（原来 DELETE 全表 + 全量 re-INSERT）
            self.last_save_ok = self._insert_item(item)
            # 索引标脏：延迟到下次检索前再重建（见 _ensure_backend_fresh）
            self._index_dirty = True
            self._fusion_ready = False
        log.info("记忆新增 [%s] importance=%.2f: %s", category, importance, content[:60])
        return item

    def add_shared(self, content: str, importance: float = 0.7) -> MemoryItem:
        """v56: 加一条「共同经历」记忆（我们之间发生过的事）。

        比 fact/person 多了一层关系叙事 —— 「上周三一起下棋你连输三盘」、
        「上次那个方案你吐槽了最后还是采纳了」。主动搭话时会注入到
        context，主人也能通过 recall_memory 按 shared 类别检索。
        """
        return self.add(content, category="shared", importance=importance)

    def recent_shared(self, n: int = 3) -> list:
        """v56: 最近 N 条共同经历（按 created_at 倒序）。"""
        items = [i for i in self._active_items() if i.category == "shared"]
        items.sort(key=lambda i: i.created_at, reverse=True)
        return items[:max(0, int(n))]

    def remove(self, mid: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if i.id != mid]
            removed = len(self._items) != before
            if removed:
                self._delete_item(mid)
                self._index_dirty = True
                self._fusion_ready = False
        return removed

    def all(self) -> list[MemoryItem]:
        with self._lock:
            return list(self._items)

    def get(self, mid: str) -> Optional[MemoryItem]:
        with self._lock:
            for i in self._items:
                if i.id == mid:
                    return i
        return None

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def search(
        self,
        keyword: str = "",
        category: str = "",
        limit: int = 20,
        use_semantic: bool = True,
        min_hit: Optional[float] = None,
    ) -> list[MemoryItem]:
        """检索记忆。

        流程：
            1. 用 backend 对 (keyword) 与所有 item.content 计算相似度
            2. 若 backend=substring：用 keyword 在 content 中的子串位置做软排序
            3. category 过滤后按「语义分 + importance + 时新度」综合排序
            4. 取 top limit，并 touch() 这些 item

        Args:
            keyword: 检索关键词（可空，空时按 importance×时新度 返回最近/最重要的）
            category: 按类别过滤（可选）
            limit: 返回条数
            use_semantic: False 时退回子串匹配（更快，CPU 友好）
            min_hit: 命中阈值（按**原始语义分**过滤）。
                None（默认）= 老行为：非 substring 后端 0.10、substring 后端 0.0。
                **融合路径必须传 0** —— 否则 _MIN_HIT 会把「某一路没命中」
                的候选在进融合排序之前就预过滤掉了（PLAN 2.1 的核心 bug）。
        """
        # 索引标脏时先重建（否则 scores 与 items 长度对不上，会错位）
        self._ensure_backend_fresh()
        # Stage 2.4：已过时的记忆（superseded）不再参与召回
        items = self._active_items()
        if not items:
            return []

        if category:
            items = [i for i in items if i.category == category]

        kw = keyword.strip()
        if not kw:
            # 空关键词 → 按 importance×时新度 排序
            results = sorted(items, key=lambda i: self._score(i, 0.0), reverse=True)
            touched = []
            for r in results[:limit]:
                r.touch()
                touched.append(r.id)
            self._maybe_save_dirty(touched)
            return results[:limit]

        # 关键词非空：先算相似度
        scores: list[float] = []
        if use_semantic and not isinstance(self.backend, SubstringBackend):
            try:
                qv = self.backend.encode([kw])[0]
                sims = self.backend.similarity(qv, self.backend._doc_vecs)  # type: ignore[attr-defined]
                scores = sims
            except Exception as e:  # noqa: BLE001
                log.warning("向量检索失败：%s，回退子串匹配", e)
                scores = []
        if not scores:
            # substring fallback
            kw_l = kw.lower()
            scores = []
            for i in items:
                idx = i.content.lower().find(kw_l)
                scores.append(1.0 if idx >= 0 else 0.0)

        # 多字中文 query 必须共享至少一个二元组才算命中（避免「量子力学」与「小学三年级」共享单字的伪命中）
        q_bigrams = _bigrams(kw)
        if len(q_bigrams) >= 1:
            scores = [
                (s if (_bigrams(i.content) & q_bigrams) else 0.0)
                for s, i in zip(scores, items, strict=False)
            ]

        # 命中阈值（按原始语义分过滤，不是综合分）—— 实测 0.10 能保留真实命中并过滤无关召回
        # Stage 2.1：min_hit=None → 老行为不变（对外行为零变化）；
        # 融合路径显式传 0（不预过滤，候选要能进 RRF 排序）。
        if min_hit is None:
            _MIN_HIT = 0.0 if isinstance(self.backend, SubstringBackend) else 0.10
        else:
            _MIN_HIT = float(min_hit)
        scored = [(s, i) for s, i in zip(scores, items, strict=False) if s > _MIN_HIT]
        # 综合排序
        scored = [(self._score(i, s), i) for s, i in scored]
        scored.sort(key=lambda x: x[0], reverse=True)

        # 取 top
        top = [i for _, i in scored[:limit]]
        touched = []
        for r in top:
            r.touch()
            touched.append(r.id)
        self._maybe_save_dirty(touched)
        return top

    def _score(self, item: MemoryItem, semantic: float) -> float:
        """综合分 = 语义相关度 × 类别权重 × (1 - 时间衰减) × importance。"""
        # 时间衰减：30 天前的记忆权重减半（指数衰减）
        age_days = max(0.0, (time.time() - item.last_access_ts) / 86400.0)
        recency = math.exp(-age_days / 30.0)
        cat_w = 1.0 / (1.0 + self.CATEGORY_PRIORITY.get(item.category, 5))
        imp = max(0.05, item.importance)
        # 访问热度加成（最多 ×1.5）
        hot = 1.0 + min(0.5, item.access_count * 0.05)
        return (semantic + 0.01) * cat_w * recency * imp * hot

    def _maybe_save_dirty(self, touched_ids: Optional[list[str]] = None) -> None:
        """把访问热度落盘。

        给了 touched_ids 就只 UPDATE 这几行（原来每次检索都整表重写）；
        没给（外部 mark_dirty）就整表重写。
        """
        with self._lock:
            if not self._dirty:
                return
            if touched_ids:
                self._update_items(touched_ids)
            else:
                self._save()
            self._dirty = False

    def mark_dirty(self) -> None:
        with self._lock:
            self._dirty = True

    def recall(self, keyword: str = "", category: str = "",
               limit: int = 5) -> list[MemoryItem]:
        """便捷接口：search 的简化版本，给 LLM tool 用。"""
        return self.search(keyword=keyword, category=category, limit=limit)

    def forget_by_keyword(self, keyword: str) -> int:
        """按关键词删除（保留：keyword 命中的全部删）。

        数据红线：删行是**破坏性**写入 → 删之前先备份（_backup_db）。
        （增量写入 add() 不备份：高频、丢一条只是少记一件事；
         删行 / 改写这种「主人可能后悔」的操作必须留副本。）
        """
        kw = keyword.strip()
        if not kw:
            return 0
        to_remove: list[str] = []
        with self._lock:
            for i in self._items:
                if kw.lower() in i.content.lower():
                    to_remove.append(i.id)
        if to_remove:
            self._backup_db()
        for mid in to_remove:
            self.remove(mid)
        return len(to_remove)

    # ------------------ Stage 2.4：按 id 删 / 按 id 改写 ------------------

    def forget_by_id(self, mid: str) -> bool:
        """按 id 删一条记忆（Stage 2.4）。

        为什么要有这条路：forget_by_keyword 是**子串**匹配，模型必须先
        知道旧措辞才能删；记错了、或者旧措辞跟模型记的不一样时，就删不掉。
        召回结果里带着 id（recall_memory 每次都回 id），按 id 删才精确。

        数据红线：删行之前先备份（_backup_db）。
        """
        mid = (mid or "").strip()
        if not mid:
            return False
        if self.get(mid) is None:
            return False
        self._backup_db()
        return self.remove(mid)

    def detect_conflicts(self, new_content: str,
                         threshold: float = 0.7) -> list[tuple[MemoryItem, float]]:
        """检测新内容与已有记忆的冲突（语义相似度高于阈值）。

        Returns:
            list of (item, similarity_score)
        """
        # 索引标脏时先重建（否则 _doc_vecs 与 items 长度对不上，会错位）
        self._ensure_backend_fresh()
        with self._lock:
            items = list(self._items)
        if not items or isinstance(self.backend, SubstringBackend):
            # 简单子串重叠（向后兼容）
            words = set(_tokenize(new_content))
            if not words:
                return []
            out: list[tuple[MemoryItem, float]] = []
            for i in items:
                iw = set(_tokenize(i.content))
                if i.category == "other":
                    continue
                overlap = len(words & iw) / max(len(words | iw), 1)
                if overlap > 0.3:
                    out.append((i, overlap))
            return out

        try:
            qv = self.backend.encode([new_content])[0]
            sims = self.backend.similarity(qv, self.backend._doc_vecs)  # type: ignore[attr-defined]
        except Exception:
            return []
        return [(i, s) for i, s in zip(items, sims, strict=False) if s >= threshold and i.category != "other"]

    # ------------------ Stage 2.4：事实改写（supersede） ------------------

    def supersede(self, old_id: str, new_content: str, *,
                  category: str = "", importance: Optional[float] = None,
                  scope: str = "", kind: str = "", topic: str = "",
                  conflict_threshold: float = 0.7) -> Optional[MemoryItem]:
        """把 old_id 那条记忆**改写**成 new_content（事实变了 → 旧的标过时）。

        - 旧行 status='superseded' + superseded_by=新行 id；
          **两条不并存为 active**（召回时旧行被过滤，见 _active_items）。
        - 复用 detect_conflicts()：跟新说法**同样矛盾**的其它 active 行
          一起标过时（否则库里会同时挂着两个互相矛盾的事实）。
        - 数据红线：改 live 库之前先备份（_backup_db，Stage 2.2 的约定）。
        - old_id 不存在 → 返回 None（调用方负责报错，别假装改写成功）。
        """
        content = (new_content or "").strip()
        if not content:
            return None
        old = self.get(old_id)
        if old is None:
            return None
        # 新行字段：没显式给的沿用旧行（改写不该顺手改掉分类 / 重要性）
        cat = category if category in CATEGORY_CHOICES else old.category
        imp = (old.importance if importance is None
               else max(0.0, min(1.0, float(importance))))
        sc = (scope or old.scope or "project").strip().lower()
        if sc not in SCOPE_CHOICES:
            sc = old.scope
        kd = (kind or old.kind or "fact").strip()
        if kd not in KIND_CHOICES:
            kd = old.kind
        tp = topic if topic else old.topic

        # 数据红线：改 live 库之前先备份
        self._backup_db()

        # 矛盾检测：跟新说法冲突的其它 active 行一起标过时
        superseded_ids: list[str] = [old.id]
        try:
            conflicts = self.detect_conflicts(content,
                                              threshold=conflict_threshold)
        except Exception as e:  # noqa: BLE001
            log.warning("supersede：冲突检测失败（只改写指定那条）：%s", e)
            conflicts = []
        for item, _score in conflicts:
            if item.id == old.id or item.status == "superseded":
                continue
            superseded_ids.append(item.id)

        new_item = MemoryItem(
            id="m" + uuid.uuid4().hex[:10],
            content=content,
            category=cat,
            importance=imp,
            scope=sc,
            kind=kd,
            topic=tp,
        )
        with self._lock:
            self._items.append(new_item)
            self.last_save_ok = self._insert_item(new_item)
            for mid in superseded_ids:
                self._mark_superseded(mid, new_item.id)
            # 索引标脏：旧行还在向量库里，检索前重建
            self._index_dirty = True
            self._fusion_ready = False
        log.info("记忆改写：%s → %s（旧条目标过时 %d 条）",
                 old_id, new_item.id, len(superseded_ids))
        return new_item

    def _mark_superseded(self, mid: str, new_id: str) -> bool:
        """把一条记忆标成过时（status='superseded' + superseded_by=new_id）。"""
        with self._lock:
            for it in self._items:
                if it.id == mid:
                    it.status = "superseded"
                    it.superseded_by = new_id
                    break
        try:
            conn = self._connect()
            try:
                conn.execute(
                    "UPDATE memories SET status='superseded', "
                    "superseded_by=? WHERE id=?", (new_id, mid))
                conn.commit()
                return True
            finally:
                conn.close()
        except Exception as e:  # noqa: BLE001
            log.warning("标记记忆过时失败(%s)：%s", mid, e)
            return False

    def _active_items(self) -> list[MemoryItem]:
        """active 的记忆（status != 'superseded'）。

        过时的记忆**不再**参与召回 / 注入 —— 否则模型会同时看到
        「主人喜欢冰美式」和「主人喜欢拿铁」两个互相矛盾的事实。
        """
        with self._lock:
            return [i for i in self._items if i.status != "superseded"]

    def forget_if_expired(self, max_age_days: int = 90,
                          min_importance: float = 0.3) -> int:
        """删除超过 max_age_days 天 且 importance < min_importance 的记忆。

        默认参数下：只清理 importance < 0.3 且 > 90 天的（其他保留）。
        """
        cutoff = time.time() - max_age_days * 86400
        removed = 0
        with self._lock:
            before = len(self._items)
            self._items = [
                i for i in self._items
                if i.created_at > cutoff or i.importance >= min_importance
            ]
            removed = before - len(self._items)
            if removed:
                self._save()
                self._index_dirty = True   # 索引延迟重建
        return removed

    def merge_similar(self, threshold: float = 0.95) -> int:
        """合并高度相似（默认 ≥ 0.95）的记忆，保留 importance 最高那条。

        ⚠️ 索引可能标脏：add() 只标脏，_ensure_backend_fresh() 只在**检索**
        路径上调 ⇒ 这里拿到的向量索引跟 items 对不上，相似度恒为 0 ⇒
        **什么都合并不了**。所以合并前必须自己刷新一次索引。
        """
        self._ensure_backend_fresh()
        with self._lock:
            if len(self._items) < 2:
                return 0
            try:
                keep: list[MemoryItem] = []
                merged = 0
                used: set[str] = set()
                # 逐个比对
                for i, it in enumerate(self._items):
                    if it.id in used:
                        continue
                    group = [it]
                    for j in range(i + 1, len(self._items)):
                        other = self._items[j]
                        if other.id in used:
                            continue
                        sim = self._similarity(it, other)
                        if sim >= threshold:
                            group.append(other)
                            used.add(other.id)
                    # 保留 importance 最高 + 最近访问的
                    group.sort(key=lambda x: (x.importance, x.last_access_ts), reverse=True)
                    keep.append(group[0])
                    if len(group) > 1:
                        merged += len(group) - 1
                if merged:
                    self._items = keep
                    self._save()
                    self._index_dirty = True   # 索引延迟重建
                return merged
            except Exception as e:  # noqa: BLE001
                log.warning("merge_similar 失败：%s", e)
                return 0

    def _similarity(self, a: MemoryItem, b: MemoryItem) -> float:
        """计算两条记忆的相似度（用 backend）。"""
        if isinstance(self.backend, SubstringBackend):
            wa, wb = set(_tokenize(a.content)), set(_tokenize(b.content))
            return len(wa & wb) / max(len(wa | wb), 1)
        try:
            vecs = self.backend.encode([a.content, b.content])
            va, vb = vecs[0], vecs[1]
            dot = sum(x * y for x, y in zip(va, vb, strict=False))
            na = math.sqrt(sum(x * x for x in va)) or 1.0
            nb = math.sqrt(sum(x * x for x in vb)) or 1.0
            return dot / (na * nb)
        except Exception:
            return 0.0

    def recent(self, n: int | None = None) -> list[MemoryItem]:
        # Stage 2.4：过时的记忆不再进注入列表（避免新旧两条一起出现）
        items = self._active_items()
        items.sort(key=lambda i: i.created_at)
        return items[-(n or self.max_inject):]

    # ------------------ Stage 2.9：向量索引（派生索引）------------------

    @property
    def vector_index_dir(self) -> Optional[Path]:
        """本库对应的向量索引目录（data/vectors/<角色>/）。"""
        if index_dir_for is None:
            return None
        try:
            return index_dir_for(self.data_file, self.character_name)
        except Exception:  # noqa: BLE001
            return None

    def _vector_source_rows(self) -> list[tuple]:
        """从 **SQLite** 读当前行（真相来源），列顺序同 _SELECT_COLUMNS。

        ⚠️ 不能用内存里的 self._items：app/brain/memory_merge.py 是用**裸 SQL**
        直接写库的（不经过 MemoryStore），写完不动内存 → 内存旧、库新。
        漂移检测必须以库为准，否则索引会「静默过期」。
        """
        try:
            conn = sqlite3.connect(
                "file:" + self.data_file.as_posix() + "?mode=ro", uri=True)
        except Exception:  # noqa: BLE001
            return []
        try:
            cur = conn.execute(
                "SELECT " + ", ".join(self._SELECT_COLUMNS) + " FROM memories")
            return list(cur.fetchall())
        except Exception:  # noqa: BLE001
            return []
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _source_rows_for_index(rows: list[tuple]) -> list[dict]:
        """库里的行 → 向量索引要的字段（id / content / 元数据）。"""
        return [
            {"id": r[0], "content": r[1], "category": r[2],
             "scope": r[7], "kind": r[8], "topic": r[9], "status": r[10]}
            for r in rows
        ]

    def _reload_items_if_changed(self, rows: list[tuple]) -> bool:
        """库里的行跟内存里不一样 → 以库为准重载。

        谁会这样：app/brain/memory_merge.py 用裸 SQL 直接写 live 库
        （合并 legacy 库），写完不动运行中的 MemoryStore 的内存。
        返回 True = 确实重载过。
        """
        with self._lock:
            mem_ids = {i.id for i in self._items}
            db_ids = {str(r[0]) for r in rows}
            if mem_ids == db_ids:
                return False
            self._items = [self._row_to_item(r) for r in rows]
            self._index_dirty = True
            self._fusion_ready = False
        log.info("MemoryStore: 库里的行变了（%d → %d），已按库重载",
                 len(mem_ids), len(db_ids))
        return True

    def _ensure_vector_index(self) -> Optional["VectorIndex"]:
        """打开/同步向量索引（**漂移检测**）。不可用时返回 None（调用方降级）。

        - 索引目录不存在 → 没有索引（**不自动建**）→ 走既有 substring + tfidf；
        - 索引存在但跟库对不上 → 增量同步（只补差的那几行）；
        - 模型还没就绪 → 本轮**不用**可能过期的索引（状态里报出来）。
        """
        if not self._vector_enabled or VectorIndex is None:
            self._vector_in_sync = False
            return None
        if self._vector_index is None:
            d = self.vector_index_dir
            if d is None or not d.is_dir():
                # 索引**只由显式重建创建**（scripts/rebuild_vector_index.py / 面板按钮）
                self._vector_status = {
                    "available": False, "state": "not_built",
                    "path": str(d) if d else None, "error": None,
                }
                self._vector_in_sync = False
                return None
            # ⚠️ 用**共享实例**（同目录 → 同一个 chroma 客户端、同一个 collection
            #    句柄）：两个 store 各开一个客户端的话，谁重建都会把对方的
            #    视图弄瞎（而且会同时占着同一个 sqlite 文件）。
            self._vector_index = VectorIndex.for_dir(d)
        idx = self._vector_index
        if not idx.open():
            self._vector_status = idx.status()
            self._vector_in_sync = False
            return None
        rows = self._vector_source_rows()
        if not rows:
            self._vector_status = idx.status()
            self._vector_in_sync = False
            return None
        self._reload_items_if_changed(rows)
        report = idx.sync(self._source_rows_for_index(rows), blocking=False)
        self._vector_sync_report = report
        try:
            self._vector_status = idx.status()
        except Exception:  # noqa: BLE001
            self._vector_status = {}
        self._vector_status["sync"] = report
        self._vector_in_sync = report.get("action") in ("in_sync", "synced")
        if not self._vector_in_sync:
            log.info("向量索引未就绪（%s）：本轮走 substring + tfidf",
                     report.get("action"))
        return idx if self._vector_in_sync else None

    def rebuild_vector_index(self, blocking: bool = True) -> dict:
        """**显式**重建向量索引（幂等：重建两次结果一样）。

        这是索引的**唯一**创建方式 —— store 自己不会建索引目录，
        所以「跑一遍测试」不会在 data/ 里留下任何东西（见 A9）。
        """
        if not self._vector_enabled or VectorIndex is None:
            return {"ok": False, "error": "向量索引不可用（模块导入失败）"}
        d = self.vector_index_dir
        if d is None:
            return {"ok": False, "error": "算不出索引目录"}
        # 复用**共享**的索引实例（别每次重建都新开一个 chroma 客户端 ——
        # 客户端会一直占着索引目录里的 sqlite 文件；而且新客户端 = 新句柄，
        # 重建时会让别人的句柄失效）。
        idx = self._vector_index
        if idx is None or not same_index_dir(idx.index_dir, d):
            idx = VectorIndex.for_dir(d)
            self._vector_index = idx
        rows = self._vector_source_rows()
        report = idx.rebuild(self._source_rows_for_index(rows), blocking=blocking)
        self._vector_sync_report = report
        try:
            self._vector_status = idx.status()
        except Exception:  # noqa: BLE001
            self._vector_status = {}
        self._vector_status["sync"] = report
        self._vector_in_sync = bool(report.get("ok"))
        return report

    def vector_index_status(self) -> dict:
        """向量索引状态（面板 / 测试）。**不抛异常**，**不返回缓存**。

        ⚠️ 状态必须**每次现算**，不能端缓存：
            - 别的实例（memory_merge 的 reindex_live、面板的重建按钮、
              另一个 store）可能刚重建过索引 → 缓存的 count / digest 全是旧的；
            - 源库可能被裸 SQL 改过（合并脚本就是）→ 摘要要现算。

        §state§ 只从事实推出来（见 vector_index.derive_state）：
        not_built / built / drifted / unavailable / disabled ——
        **绝不跟同一份 payload 里的 n_indexed / built / in_sync 打架**。
        """
        d = self.vector_index_dir
        out: dict = {
            "enabled": self._vector_enabled,
            "available": False,
            "built": False,
            "state": "disabled" if not self._vector_enabled else "not_built",
            "state_reason": "",
            "path": str(d) if d is not None else None,
            "embed_model": EMBED_MODEL_ID,
            "rerank_model": RERANK_MODEL_ID,
            "n_indexed": 0,
            "n_source": 0,
            "in_sync": False,
            "drift": False,
            "sync": {},
            "error": _VECTOR_INDEX_IMPORT_ERROR,
        }
        if not self._vector_enabled:
            out["state_reason"] = "本 store 关掉了向量索引（vector_index=False）"
            return out
        idx = self._vector_index
        if idx is None and d is not None and VectorIndex is not None:
            # 只读地看一眼（不写任何东西）：共享实例，句柄也是共享的。
            # 目录还没建也没关系 —— status() 不打开 chroma，只读状态文件。
            try:
                idx = VectorIndex.for_dir(d)
            except Exception as e:  # noqa: BLE001
                out["error"] = str(e)
        if idx is not None:
            try:
                out.update(idx.status())
            except Exception as e:  # noqa: BLE001
                out["error"] = str(e)
        # —— 源库（真相来源）现算 ——
        src_rows = self._vector_source_rows()
        out["n_source"] = len(src_rows)
        src_digest = ""
        if digest_rows is not None:
            try:
                src_digest = digest_rows(self._source_rows_for_index(src_rows))
            except Exception:  # noqa: BLE001
                src_digest = ""
        idx_digest = str(out.get("digest") or "")
        # 「现在对得上吗」= 索引里记的摘要 == 源库现算的摘要
        in_sync = bool(idx_digest) and idx_digest == src_digest
        report = dict(self._vector_sync_report or {})
        # drift = 「上次同步发现过漂移」**或**「现在还对不上」——
        # 后者保证「别的实例改过 / 裸 SQL 改过」不会被缓存掩盖。
        drift = bool(report.get("drift")) or (bool(out.get("built")) and not in_sync)
        out["in_sync"] = in_sync
        out["drift"] = drift
        out["sync"] = report
        # 硬错误（chroma 打不开 / 模型加载失败）→ 索引用不了
        hard = (out.get("open_error") or out.get("embed_error")
                or out.get("rerank_error"))
        if hard:
            out["error"] = hard
        if derive_state is not None:
            state, reason = derive_state(
                enabled=True, built=bool(out.get("built")),
                n_indexed=int(out.get("n_indexed") or 0),
                n_source=int(out.get("n_source") or 0),
                in_sync=in_sync, drift=drift, error=hard)
            out["state"] = state
            out["state_reason"] = reason
        return out

    def warmup_vector_index(self, blocking: bool = False) -> dict:
        """预热向量索引（加载向量模型 + 重排模型）。

        blocking=False → **后台线程**加载（不阻塞启动、不阻塞聊天）；
        blocking=True  → 等加载完（脚本 / 评测用）。
        """
        if not self._vector_enabled or VectorIndex is None:
            return {"ok": False, "error": "向量索引不可用"}
        d = self.vector_index_dir
        if d is None:
            return {"ok": False, "error": "算不出索引目录"}
        if self._vector_index is None:
            self._vector_index = VectorIndex.for_dir(d)
        idx = self._vector_index
        if blocking:
            emb = idx.ensure_embedder(blocking=True)
            rr = idx.ensure_reranker(blocking=True)
            return {"ok": bool(emb) and bool(rr),
                    "embed_ready": emb is not None,
                    "rerank_ready": rr is not None}
        # 后台：模型缓存自己会开线程加载，这里只负责触发（不等待）
        idx.ensure_embedder(blocking=False)
        idx.ensure_reranker(blocking=False)
        return {"ok": True, "background": True}

    # ------------------ Stage 2.1：按 query 召回（RRF 融合） ------------------

    def _ensure_fusion_backends(self, docs: list[str]) -> None:
        """准备融合用的后端（懒建 + 缓存）。

        三路的可用性：
            - substring：永远可用（字面 / 二元组，自己算，不占后端对象）
            - tfidf：永远可用（纯 Python 实现，零外部依赖）
            - sbert：**不新增依赖** —— 装了 sentence-transformers 才用；
              没装 / 加载失败 → 降级到 tfidf（融合只剩两路，这是预期行为）

        降级必须**可观测**：写日志 + 记在 last_fusion_degraded 上，
        面板和测试都能看到「这一轮到底哪几路参与了」。
        """
        if self._fusion_ready:
            return
        tfidf: VectorBackend
        if isinstance(self.backend, TfidfBackend):
            tfidf = self.backend            # 已经在同一批 docs 上 fit 过了
        else:
            tfidf = TfidfBackend()
            try:
                tfidf.fit(docs)
            except Exception as e:  # noqa: BLE001
                log.warning("融合用 tfidf 后端 fit 失败：%s", e)
        self._fusion_tfidf = tfidf
        self._fusion_sbert = None
        degraded: list[str] = []
        # ⚠️ 老的那条 sbert 腿（paraphrase-multilingual-MiniLM-L12-v2）**退役**：
        #   1. 它的权重本机没缓存，一启用就是 470 MB 下载（还会在启动时阻塞）；
        #   2. 语义检索改由**向量索引**负责（bge-small-zh-v1.5 + bge-reranker-base）；
        #   3. 今天它其实也是死的（Keras 3 让 transformers import 失败），
        #      所以退役**不改变任何对外行为**。
        # 仍然报「降级」，让面板 / 测试看得到「语义腿现在由谁负责」。
        # （测试可以手工塞一个假后端进 _fusion_sbert，这条腿本身仍然可用。）
        if _sbert_available():
            log.info("sbert 腿已退役（语义腿改由向量索引负责），融合用 substring + tfidf")
        else:
            log.info("sentence-transformers 未安装，融合用 substring + tfidf")
        degraded.append("sbert")
        self._fusion_degraded = tuple(degraded)
        self._fusion_ready = True

    def _rank_substring(self, query: str, items: list[MemoryItem]
                        ) -> tuple[list[int], dict[str, float]]:
        """字面 / 二元组这一路的排名 + 每行的原始分数。

        返回 (下标列表（按相关度降序）, {mid: 原始分数})：
            - 原始分数 = 字面命中 2.0 + 二元组重叠比例（0..3）；
            - 字面命中（query 是 content 的子串）权重更高；
            - 其次按二元组重叠比例（CJK 相邻二元组，避免单字伪命中）。
        并列时按 id 排序 → 结果可复现。
        """
        q_low = (query or "").lower()
        q_bigrams = _bigrams(query or "")
        scored: list[tuple[float, int]] = []
        scores: dict[str, float] = {}
        for idx, it in enumerate(items):
            score = 0.0
            if q_low and q_low in it.content.lower():
                score += 2.0
            if q_bigrams:
                score += len(_bigrams(it.content) & q_bigrams) / len(q_bigrams)
            scores[it.id] = score
            if score > 0:
                scored.append((score, idx))
        scored.sort(key=lambda t: (-t[0], items[t[1]].id))
        return [idx for _, idx in scored], scores

    def _rank_vector(self, query: str, items: list[MemoryItem],
                     backend: VectorBackend, top_n: int = FUSION_TOP_N
                     ) -> tuple[list[int], float, dict[str, float]]:
        """向量这一路的排名。

        返回 (下标列表, 最大相似度, {mid: 相似度})：
            - 下标列表 = 按相似度降序取 top_n（**含 0 分**：PLAN 写的是
              「每路取 top-30」，0 分的行也参与 RRF，并列时靠 id 定序）；
            - 最大相似度 = 这一路对 query 最「有信心」的那条有多像
              （门禁用，**阈值**判断，见 _query_matches_anything）。
        """
        try:
            qv = backend.encode([query])[0]
            sims = backend.similarity(qv, backend._doc_vecs)  # type: ignore[attr-defined]
        except Exception as e:  # noqa: BLE001
            log.warning("向量后端 %s 检索失败：%s", backend.name, e)
            return [], 0.0, {}
        n = min(len(items), len(sims))
        order = sorted(range(n), key=lambda i: (-float(sims[i]), items[i].id))
        max_sim = max((float(sims[i]) for i in range(n)), default=0.0)
        sim_by_id = {items[i].id: float(sims[i]) for i in range(n)}
        return order[:top_n], max_sim, sim_by_id

    def _fusion_ranked_lists(self, query: str,
                             items: list[MemoryItem]
                             ) -> tuple[list[tuple[str, list[int]]], float,
                                        dict[str, dict[str, float]]]:
        """三路排名（substring / tfidf / sbert）。

        返回 (每路的 (名字, 下标列表), 语义腿最大相似度, 每路的相对强度分)：
            - 语义腿最大相似度 → 门禁（**阈值**判断，不是「> 0 即算」）；
            - 相对强度分 = 该路原始分 / 该路最高分（0..1）
              → 排序 + 裁剪用（见 _candidate_scores）。
        """
        lists: list[tuple[str, list[int]]] = []
        rel: dict[str, dict[str, float]] = {}
        semantic_sim = 0.0

        sub_order, sub_scores = self._rank_substring(query, items)
        lists.append(("substring", sub_order))
        rel["substring"] = _normalize_scores(sub_scores)

        if self._fusion_tfidf is not None:
            idx, _sim, sims = self._rank_vector(query, items, self._fusion_tfidf)
            lists.append(("tfidf", idx))
            rel["tfidf"] = _normalize_scores(sims)
        if self._fusion_sbert is not None:
            idx, semantic_sim, sims = self._rank_vector(query, items,
                                                         self._fusion_sbert)
            lists.append(("sbert", idx))
            rel["sbert"] = _normalize_scores(sims)
        # Stage 2.9：dense 腿（Chroma 语义检索）。
        # 索引不可用 / 还没同步 / 模型没就绪 → 这一腿为空（整体退回两腿）。
        dense_order, _dense_sim, dense_sims = self._dense_ranked(query, items)
        if dense_order:
            lists.append(("dense", dense_order))
            rel["dense"] = _normalize_scores(dense_sims)
        return lists, semantic_sim, rel

    def _dense_ranked(self, query: str, items: list[MemoryItem]
                      ) -> tuple[list[int], float, dict[str, float]]:
        """dense 腿：Chroma 语义检索。

        返回 (下标列表, 最大相似度, {mid: 相似度})，跟 _rank_vector 同形状。
        - 只取 status != 'superseded' 的行（过时的记忆不该被召回）；
        - 索引没就绪 → 空（调用方整体降级，**绝不**用过期索引）。
        """
        self._dense_leg_ready = False
        self._dense_max_sim = 0.0
        idx = self._vector_index
        if idx is None or not self._vector_in_sync:
            return [], 0.0, {}
        pool_ids = {i.id for i in items if i.status != "superseded"}
        if not pool_ids:
            return [], 0.0, {}
        try:
            hits = idx.query(query, k=FUSION_TOP_N, where={"status": "active"})
        except Exception as e:  # noqa: BLE001
            log.warning("dense 检索失败（降级）：%s", e)
            return [], 0.0, {}
        pos = {i.id: n for n, i in enumerate(items)}
        order: list[int] = []
        sims: dict[str, float] = {}
        for mid, sim in hits:
            mid = str(mid)
            if mid not in pool_ids or mid not in pos:
                continue
            order.append(pos[mid])
            sims[mid] = float(sim)
        max_sim = max(sims.values(), default=0.0)
        self._dense_leg_ready = bool(order)
        self._dense_max_sim = max_sim
        self._dense_sims = sims
        return order, max_sim, sims

    def _query_matches_anything(self, query: str, items: list[MemoryItem],
                                sub_list: list[int], semantic_sim: float) -> bool:
        """query 跟记忆库**有没有关系**（门禁）。

        - CJK query：必须共享至少一个**二元组**（跟老 search() 同一条规则：
          「量子力学」不该命中「小学三年级」那种单字伪命中）；
        - 纯英文 query：必须共享一个整词；
        - 语义腿（sbert）：相似度必须 **≥ 阈值**才算有关系。

        ⚠️ 旧判断是「语义后端有正命中（> 0）就算有关系」。那等于**没有门禁**：
        真 embedding 对几乎任何 query 都会给一个小小的正相似度，
        于是「问一件跟记忆库完全无关的事」也会硬塞一堆记忆进来。
        现在改成阈值判断（semantic_gate_threshold），无关问题老老实实走 fallback。

        全都不沾 → 这次提问跟记忆库无关 → 什么都不召回，交给 fallback
        （按重要性 top-2），而不是硬塞一坨不相关的记忆。
        """
        if sub_list:
            return True
        if _has_word_overlap(query, items):
            return True
        if float(semantic_sim) >= self.semantic_gate_threshold:
            return True
        # Stage 2.9：语义腿（dense）——
        # 老门禁是「CJK 二元组 / 英文整词」的**字面**匹配，正是它毙掉了
        # 「我不能吃什么」→「主人不吃辣」（零字面重叠）。
        # 语义腿可用时，相关性由**语义相似度**判，不再只靠词面。
        if self._dense_leg_ready and self._dense_max_sim >= DENSE_GATE_MIN:
            return True
        return False

    def _search_fused(self, query: str, kinds: Optional[list[str]] = None,
                      limit: Optional[int] = None) -> list[MemoryItem]:
        """按 query 召回（RRF 融合三路排名 + 相关度裁剪）。

        与 search() 的关键差别：
            - **不**走 _MIN_HIT 预过滤 —— 融合的输入必须是「各路的原始排名」，
              否则「某一路没命中」的候选在进融合之前就被滤掉了（PLAN 2.1 的 bug）；
            - 打分用 RRF：score = Σ 1/(60 + rank + 1)，每路取 top-30；
            - Stage 2.8：RRF 只用来**提名**候选，最终注入哪几条由
              _pick_candidates() 按「融合分 + 相关度门槛 + 条数上限」决定。

        Args:
            query: 本轮主人说的话。
            kinds: 只召回这些 kind（可选）。
            limit: 最多返回几条（默认 self.max_inject）。
        """
        q = (query or "").strip()
        if not q:
            return []
        self._ensure_backend_fresh()
        # Stage 2.9：先做向量索引的漂移检测（必要时按库重载内存里的行 ——
        # 合并脚本是裸 SQL 写库的），再取这一轮的快照。
        self._ensure_vector_index()
        with self._lock:
            items = list(self._items)
        if not items:
            return []

        self._ensure_fusion_backends([i.content for i in items])
        # 只在 active 里找（superseded 是 Stage 2.4 的产物，不该再被召回）
        pool = [i for i in items if i.status != "superseded"]
        if kinds:
            want = set(kinds)
            pool = [i for i in pool if i.kind in want]
        if not pool:
            self.last_fusion_backends = ()
            self.last_fusion_degraded = self._fusion_degraded
            return []
        pool_ids = {i.id for i in pool}

        # ---- Stage 2.9：重排（cross-encoder 是最终裁决权）----
        # 融合分只用来「提名」候选，重排分决定真正的顺序与取舍。
        self._active_query = q
        self._rerank_scores = {}
        self._rerank_top = 0.0

        ranked_lists, semantic_sim, leg_rel = self._fusion_ranked_lists(q, items)
        sub_list = dict(ranked_lists).get("substring", [])
        lexical_hit = bool(sub_list) or _has_word_overlap(q, items)
        # 字面命中的行 id（语义地板要用：cross-encoder 对复合问句偏严，
        # 但「字面命中 + 语义一样近」的行是真答案，不能被一票否决）
        self._lexical_hit_ids = {items[i].id for i in sub_list
                                 if 0 <= i < len(items)}

        # RRF 融合（照抄 qq-bridge memory_sidecar/memory_store.py:462-478 的形状）
        fused: dict[str, float] = {}
        used: list[str] = []
        for name, ranked in ranked_lists:
            if ranked:
                used.append(name)
            for rank, idx in enumerate(ranked[:FUSION_TOP_N]):
                mid = items[idx].id
                if mid not in pool_ids:
                    continue
                fused[mid] = fused.get(mid, 0.0) + 1.0 / (RRF_K + rank + 1)
        by_id = {i.id: i for i in items}
        if fused:
            self._rerank_candidates(q, by_id, fused)

        # 门禁：query 跟记忆库完全无关 → 什么都不召回（交给 fallback）
        pre_gate = self._query_matches_anything(q, items, sub_list, semantic_sim)
        gate_open = pre_gate
        if self._rerank_scores:
            if not pre_gate:
                # 门没开：只有「重排给出强信号」才开
                # （词面完全够不着、但语义上一模一样，例如
                #  「我不能吃什么」→「主人不吃辣」）
                gate_open = self._rerank_top >= RERANK_STRONG_SCORE
            elif not lexical_hit and self._rerank_top < RERANK_MIN_SCORE:
                # 门是 dense 腿开的，但 cross-encoder 说「全都不相关」→ 关门
                gate_open = False
        if not gate_open:
            self.last_fusion_backends = ()
            self.last_fusion_degraded = self._fusion_degraded
            self.last_recall_stats = {"gate": "closed", "n_injected": 0}
            log.info("记忆融合召回：query=%r 与记忆库无关，不召回", q[:40])
            return []

        self.last_fusion_backends = tuple(used)
        self.last_fusion_degraded = self._fusion_degraded
        if not fused:
            return []
        picked = self._pick_candidates(
            by_id, fused, leg_rel,
            self.max_inject if limit is None else int(limit))
        log.info("记忆融合召回：query=%r 后端=%s 降级=%s 命中=%d",
                 q[:40], list(self.last_fusion_backends),
                 list(self.last_fusion_degraded), len(picked))
        self._bump_hits([i.id for i in picked])
        return picked

    def _candidate_scores(self, by_id: dict[str, MemoryItem],
                          fused: dict[str, float],
                          leg_rel: dict[str, dict[str, float]]) -> dict[str, float]:
        """每个候选的**融合分**（排序 + 裁剪都用它）。

        融合分 = W_REL×最强证据的相对强度
               + W_RRF×多路一致性（RRF 相对分）
               + W_IMP×重要性

        - 最强证据的相对强度：这一行在**最支持它的那一路**上的相对分
          （substring 路 = 字面/二元组命中；向量路 = 余弦相似度），0..1；
          —— RRF 分只说「排第几」，把幅度信息全丢了（实测第 2~6 名的
          RRF 分只差 ~0.1，几乎并列 → 谁进 top-N 实际由随机 id 决定）；
        - RRF 相对分：多路都排在前面的行更高（0..1）；
        - 重要性：弱先验，只用来在分数接近时定序。

        纯函数、确定：同样的输入 → 同样的分数。
        """
        rrf_top = max(fused.values()) if fused else 0.0
        if rrf_top <= 0:
            rrf_top = 1.0
        out: dict[str, float] = {}
        for mid, rrf in fused.items():
            item = by_id.get(mid)
            if item is None:
                continue
            rel = 0.0
            for scores in leg_rel.values():
                v = scores.get(mid)
                if v is not None and v > rel:
                    rel = v
            out[mid] = (W_REL * rel
                        + W_RRF * (rrf / rrf_top)
                        + W_IMP * float(item.importance))
        return out

    def _pick_candidates(self, by_id: dict[str, MemoryItem],
                         fused: dict[str, float],
                         leg_rel: dict[str, dict[str, float]],
                         max_rows: int) -> list[MemoryItem]:
        """给候选**排序 + 裁剪**，返回真正要注入的行。

        Stage 2.9：语义腿可用时，**cross-encoder 是排序的最终裁决权** ——
        融合分只用来「提名」候选（前 RERANK_CANDIDATES 名），
        真正的顺序与「补位行留不留」由重排分决定（见 _pick_candidates_rerank）。
        重排没跑成（模型没就绪 / 索引不可用）→ 退回融合分排序（老行为）。
        """
        if self.rerank_enabled and self._rerank_scores:
            picked = self._pick_candidates_rerank(by_id, fused, max_rows)
            if picked is not None:
                return picked
        return self._pick_candidates_fusion(by_id, fused, leg_rel, max_rows)

    def _pick_candidates_fusion(self, by_id: dict[str, MemoryItem],
                                fused: dict[str, float],
                                leg_rel: dict[str, dict[str, float]],
                                max_rows: int) -> list[MemoryItem]:
        """给候选**排序 + 裁剪**（融合分版本 —— 老行为）。

        排序：融合分降序 → 重要性降序 → id 升序
            （**完全确定**：跟 uuid 无关。旧实现只按 RRF 分排，
             第 2 名往后几乎并列 → 补位行是谁基本由随机 id 决定）。

        裁剪（Stage 2.8）：
            - 顶行（过了门禁的第一名）**永远注入**；
            - 其余行只有「融合分 ≥ 顶行融合分 × filler_ratio」才注入 ——
              离顶行太远的就是噪声，白占 token，还有让桌宠**说错话**的风险；
            - 最后按条数上限（max_inject）截断。
        """
        cap = max(0, int(max_rows))
        if cap == 0:
            return []
        scores = self._candidate_scores(by_id, fused, leg_rel)
        if not scores:
            return []
        ordered = sorted(scores.items(),
                         key=lambda kv: (-kv[1], -float(by_id[kv[0]].importance),
                                         kv[0]))
        top_score = ordered[0][1]
        floor = top_score * self.filler_ratio
        picked: list[MemoryItem] = []
        picked_scores: list[float] = []
        for mid, sc in ordered:
            if picked and sc < floor:
                break            # 离顶行太远 → 噪声，不注入
            picked.append(by_id[mid])
            picked_scores.append(sc)
            if len(picked) >= cap:
                break
        self.last_recall_stats = {
            "n_candidates": len(scores),
            "n_injected": len(picked),
            "cut_ratio": self.filler_ratio,
            "top_score": round(top_score, 6),
            # 最弱那条注入行离顶行有多近（1.0 = 与顶行并列，0 = 毫无关系）
            "min_injected_ratio": (round(min(picked_scores) / top_score, 4)
                                   if picked_scores and top_score > 0 else 0.0),
        }
        return picked

    # ------------------ Stage 2.9：cross-encoder 重排 ------------------

    def _rerank_candidates(self, query: str, by_id: dict[str, MemoryItem],
                           fused: dict[str, float]) -> dict[str, float]:
        """对融合分的 top-K 候选做 cross-encoder 重排（**一次批量** predict）。

        融合分只用来「提名」；**重排分是排序的最终裁决权**。
        模型没就绪 / 推理失败 → 返回 {}（调用方退回融合分排序）。
        """
        self._rerank_scores = {}
        self._rerank_top = 0.0
        idx = self._vector_index
        if idx is None or not self.rerank_enabled or not fused:
            return {}
        ordered = sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))
        cand = [(mid, by_id[mid].content)
                for mid, _ in ordered[:RERANK_CANDIDATES] if mid in by_id]
        if not cand:
            return {}
        scored = idx.rerank(query, cand)
        if not scored:
            return {}
        self._rerank_scores = {mid: float(s) for mid, s in scored}
        self._rerank_top = max(self._rerank_scores.values(), default=0.0)
        return self._rerank_scores

    def _pick_candidates_rerank(self, by_id: dict[str, MemoryItem],
                                fused: dict[str, float],
                                max_rows: int) -> Optional[list[MemoryItem]]:
        """按**重排分**排序 + 裁剪（Stage 2.9 的最终裁决）。

        - 顺序：重排分降序 → 融合分降序 → 重要性 → id（完全确定）；
        - 裁剪：顶行永远注入；其余行只有「重排分 ≥ 顶行重排分 × RERANK_CUT_RATIO」
          才注入（离顶行太远的就是噪声，白占 token，还有让桌宠**说错话**的风险）；
        - 最后按 max_inject 截断。
        返回 None = 没有重排结果（调用方退回融合分排序）。
        """
        cap = max(0, int(max_rows))
        if cap == 0:
            return []
        scores = self._rerank_scores
        if not scores:
            return None
        ordered = sorted(
            scores.items(),
            key=lambda kv: (-kv[1], -fused.get(kv[0], 0.0),
                            -float(by_id[kv[0]].importance), kv[0]))
        top = ordered[0][1]
        floor = top * self.rerank_cut_ratio
        dense_top = max(self._dense_sims.values(), default=0.0)             if self._dense_sims else 0.0
        picked: list[MemoryItem] = []
        picked_scores: list[float] = []
        for mid, sc in ordered:
            if picked and sc < floor and not self._semantic_floor_hit(mid,
                                                                      dense_top):
                continue        # 离顶行太远 → 噪声，不注入
            picked.append(by_id[mid])
            picked_scores.append(sc)
            if len(picked) >= cap:
                break
        self.last_recall_stats = {
            "n_candidates": len(scores),
            "n_injected": len(picked),
            "cut_ratio": self.rerank_cut_ratio,
            "top_score": round(top, 6),
            "min_injected_ratio": (round(min(picked_scores) / top, 4)
                                   if picked_scores and top > 0 else 0.0),
            "rerank_top": round(self._rerank_top, 6),
            "rerank": True,
        }
        return picked

    def _semantic_floor_hit(self, mid: str, dense_top: float) -> bool:
        """语义地板：这条行该不该在「重排分很低」时仍然保留？

        条件（三个都要满足，阈值都是量出来的，见 DENSE_KEEP_RATIO）：
            1. 与 query **字面命中**（共享 CJK 二元组 / 英文整词）；
            2. 与 query 的 dense 相似度 ≥ 顶行 dense 相似度 × DENSE_KEEP_RATIO；
            3. 该相似度 ≥ DENSE_GATE_MIN（绝对门槛：确实算「有关系」）。

        为什么：cross-encoder 对复合问句偏严（「我什么时候交房租还有体检的事」
        里那条「体检复查」只有 0.0085），一刀切会把真答案裁掉。
        """
        if dense_top <= 0:
            return False
        sim = float(self._dense_sims.get(mid, 0.0))
        if sim < DENSE_GATE_MIN or sim < dense_top * DENSE_KEEP_RATIO:
            return False
        return mid in self._lexical_hit_ids

    def _top_by_importance(self, n: int) -> list[MemoryItem]:
        """按重要性取 top-n（并列时按最近访问、再按 id → 可复现）。"""
        with self._lock:
            pool = [i for i in self._items if i.status != "superseded"]
        pool.sort(key=lambda i: (-i.importance, -i.last_access_ts, i.id))
        return pool[:max(0, int(n))]

    # ------------------ Stage 2.1：注入 system prompt 的记忆块 ------------------

    def system_block(self, query: str = "") -> str:
        """渲染注入 system prompt 的长期记忆块。

        query = 本轮主人说的话（Stage 2.1：按这句话检索，而不是固定 top-N）。
        不传时用 set_current_query() 记下的那句（老调用方就是这么调的）。
        """
        items = self._select_for_injection(query)
        if not items:
            return ""
        lines = [f"- [{i.category}★{i.importance:.1f}] ({_format_time(i.created_at)}) {i.content}"
                 for i in items]
        return (
            "【你记得的事】（按你刚才这句话检索出来的，可能相关也可能不准；用不上就别硬扯）\n"
            "（★ 是重要性 0-1）\n"
            + "\n".join(lines)
        )

    def _select_for_injection(self, query: str = "") -> list[MemoryItem]:
        """选注入 system prompt 的记忆：**按本轮 query 召回**（Stage 2.1）。

        - 有 query：RRF 融合召回（按相关度裁剪），最多 max_inject（4）条；
        - 一条都没召回（问题跟记忆无关）：回退到**按重要性 top-2**（不是 top-20），
          避免「完全无关的问题也硬塞 20 条记忆」；
        - 没有 query（老调用方拿不到 query）：按重要性取 max_inject 条。
        """
        q = (query or "").strip() or self.current_query
        if q:
            fused = self._search_fused(q, limit=self.max_inject)
            if fused:
                return fused
            log.info("记忆召回：query=%r 一条都没命中，回退按重要性取 top-2", q[:40])
            return self._top_by_importance(2)
        return self._top_by_importance(self.max_inject)

    # ------------------ Stage 2.1 / 2.2：query 通道 + scope 分库 ------------------

    def set_current_query(self, text: str) -> None:
        """记下本轮主人说的话。

        system_block() 不带参数调用时用它做召回 —— prompt_layer.get_memory_block()
        就是无参调 provider 的，它拿不到 query。
        """
        with self._lock:
            self._current_query = (text or "").strip()

    @property
    def current_query(self) -> str:
        """最近一次 set_current_query() 记下的话（没有则空串）。"""
        with self._lock:
            return self._current_query

    def fusion_status(self) -> dict:
        """融合召回的状态（面板 / 测试用）。

        - backends：上一次融合**实际参与**的后端
        - degraded：被降级（不可用）的后端
        """
        return {
            "backends": list(self.last_fusion_backends),
            "degraded": list(self.last_fusion_degraded),
            "rrf_k": RRF_K,
            "top_n": FUSION_TOP_N,
            "max_inject": self.max_inject,
            "filler_ratio": self.filler_ratio,
            "semantic_gate_threshold": self.semantic_gate_threshold,
            "last_recall": dict(self.last_recall_stats),
            # Stage 2.9：向量索引 + 重排的旋钮（面板 / 测试看它）
            "dense_gate_min": DENSE_GATE_MIN,
            "dense_keep_ratio": DENSE_KEEP_RATIO,
            "rerank_candidates": RERANK_CANDIDATES,
            "rerank_cut_ratio": self.rerank_cut_ratio,
            "rerank_enabled": self.rerank_enabled,
            "vector_index": self.vector_index_status(),
        }

    def store_for_scope(self, scope: str) -> "MemoryStore":
        """按 scope 返回对应的记忆库。

        - project（本库，默认）：原样返回 self（老行为不变）；
        - user：~/.desktop-pet/user_memory.db（懒建 + 缓存）。
        """
        sc = (scope or "project").strip().lower()
        if sc != "user":
            return self
        with self._lock:
            store = self._user_store
            if store is None:
                store = MemoryStore(user_memory_db_path(),
                                    max_inject=self.max_inject,
                                    backend_name=self.backend_name)
                self._user_store = store
                log.info("MemoryStore: user scope 记忆库 %s",
                         store.data_file)
            return store


# MemoryCurator


class MemoryCurator:
    """后台定时整理记忆：摘要 / 合并 / 清理 / 重要性衰减。

    与原 MemoryEvolution 的差异：
        - 按 importance 阈值清理（而不是按类别一刀切）
        - 调用 LLM 对「事实类」记忆做摘要压缩（可选）
        - 输出统计信息（按类别、importance 分布）
    """

    def __init__(self, memory_store: MemoryStore,
                 check_interval_hours: int = 12,
                 llm_client=None):
        self.memory = memory_store
        self.check_interval_hours = check_interval_hours
        self.llm = llm_client
        self._running = False
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._curator_loop, daemon=True, name="MemoryCurator"
        )
        self._thread.start()
        log.info("MemoryCurator 启动（每 %d 小时整理一次）", self.check_interval_hours)

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def _curator_loop(self) -> None:
        while self._running:
            stopped = self._stop_event.wait(self.check_interval_hours * 3600)
            if stopped or not self._running:
                break
            try:
                self.run_once()
            except Exception as e:
                log.warning("MemoryCurator 出错：%s", e)

    def run_once(self) -> dict:
        """执行一次整理，返回统计信息。"""
        log.info("MemoryCurator: 开始整理...")
        # 1. 过期清理（importance < 0.3 且 > 90 天）
        expired = self.memory.forget_if_expired(max_age_days=90, min_importance=0.3)
        # 2. 合并高度相似
        merged = self.memory.merge_similar(threshold=0.95)
        # 3. 统计
        items = self.memory.all()
        cats: dict[str, int] = {}
        for it in items:
            cats[it.category] = cats.get(it.category, 0) + 1
        avg_imp = sum(i.importance for i in items) / max(1, len(items))
        stats = {
            "total": len(items),
            "by_category": cats,
            "avg_importance": round(avg_imp, 3),
            "expired_removed": expired,
            "merged_removed": merged,
        }
        log.info("MemoryCurator 完成：%s", stats)
        return stats


#  helpers


def _format_time(ts: float) -> str:
    """时间戳转简短时间标记（相对时间）。"""
    import datetime
    dt = datetime.datetime.fromtimestamp(ts)
    now = datetime.datetime.now()
    delta = (now - dt).days
    if delta == 0:
        return "今天"
    elif delta == 1:
        return "昨天"
    elif delta < 7:
        return f"{delta}天前"
    elif delta < 30:
        return f"{delta // 7}周前"
    elif delta < 365:
        return f"{delta // 30}月前"
    else:
        return f"{delta // 365}年前"
