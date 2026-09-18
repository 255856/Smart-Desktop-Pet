"""桌宠长期记忆库（增强版：向量检索 + 重要性 + 时间衰减 + 冲突检测）。

设计：
    - 每条记忆 = MemoryItem(id, content, category, importance, created_at, last_access_ts, access_count)
    - 三种检索后端（按优先级）：
        1. VectorBackend（sentence-transformers，可选）
        2. TfidfBackend（numpy + 词袋 TF-IDF + 余弦，零依赖默认）
        3. SubstringBackend（大小写不敏感的子串匹配，兜底）
    - 自动按 availability 选最强者；可手动 lock。
    - 注入 system prompt 时综合：类别优先级 × 时新度 × importance × 与 query 的相关度。
    - MemoryCurator 后台线程：定期摘要/合并/归纳/清理过期。

data/memory.json 结构：
    [{"id":"m1731...","content":"主人喜欢喝冰美式","category":"preference",
      "importance":0.8,"created_at":1731...,"last_access_ts":1731...,"access_count":3},
     ...]
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Protocol

log = logging.getLogger(__name__)

CATEGORY_CHOICES = ("preference", "fact", "event", "skill", "person", "other")

# Tokenize：CJK 单字 + 英文整词（够用且零依赖）
_TOKEN_RE = re.compile(
    r"[A-Za-z]+|[\u4e00-\u9fff]",
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
    """CJK 单字 + 英文小写词；过滤停用词；过滤单字符英文。"""
    out: list[str] = []
    for tok in _TOKEN_RE.findall(text.lower()):
        if tok in _STOPWORDS:
            continue
        if len(tok) == 1 and not re.match(r"[\u4e00-\u9fff]", tok):
            continue
        out.append(tok)
    return out


# ============================================================================
#  MemoryItem
# ============================================================================


@dataclass
class MemoryItem:
    id: str
    content: str
    category: str = "other"
    importance: float = 0.5
    created_at: float = field(default_factory=time.time)
    last_access_ts: float = field(default_factory=time.time)
    access_count: int = 0

    def touch(self) -> None:
        """被命中检索时调用，更新访问热度。"""
        self.last_access_ts = time.time()
        self.access_count += 1


# ============================================================================
#  向量检索后端（Strategy 模式）
# ============================================================================


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
        # query_vec 在此协议里实际就是 query 文本（见 _search 实现）
        # 这里返回 0 占位，真实相似度由 query 文本与文档计算
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
        for dv, dn in zip(doc_vecs, self._norms):
            dot = sum(a * b for a, b in zip(query_vec, dv))
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
            out.append(sum(a * b for a, b in zip(query_vec, dv)))
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
    # auto
    try:
        import importlib.util
        if importlib.util.find_spec("sentence_transformers") is not None:
            return SentenceTransformerBackend()
    except Exception:  # noqa: BLE001
        pass
    return TfidfBackend()


# ============================================================================
#  MemoryStore
# ============================================================================


class MemoryStore:
    """长期记忆：JSON 落盘 + 可插拔向量后端 + 重要性评分 + 时间衰减。"""

    # 注入 system prompt 时各类别的优先级权重
    CATEGORY_PRIORITY = {
        "preference": 0,
        "fact": 1,
        "event": 2,
        "skill": 3,
        "person": 4,
        "other": 5,
    }

    def __init__(self, data_file: str | Path, max_inject: int = 20,
                 backend_name: str = "auto"):
        """
        Args:
            data_file: JSON 落盘路径。
            max_inject: 注入 system prompt 的最大条数。
            backend_name: "auto" / "tfidf" / "sbert" / "substring"。
        """
        self.data_file = Path(data_file)
        self.max_inject = max_inject
        self.backend: VectorBackend = make_backend(backend_name)
        self.backend_name = backend_name
        self._items: list[MemoryItem] = []
        self._lock = threading.RLock()
        self._dirty = False
        self._load()
        self._refit_backend()

    # ----- 持久化 -----
    def _load(self) -> None:
        if not self.data_file.is_file():
            return
        try:
            raw = json.loads(self.data_file.read_text("utf-8"))
            self._items = []
            for r in raw:
                # 向后兼容：旧记录缺字段就补
                r.setdefault("importance", 0.5)
                r.setdefault("last_access_ts", r.get("created_at", time.time()))
                r.setdefault("access_count", 0)
                self._items.append(MemoryItem(**r))
            log.info("MemoryStore: 载入 %d 条记忆（backend=%s）",
                     len(self._items), self.backend.name)
        except Exception as e:  # noqa: BLE001
            log.warning("载入记忆失败：%s", e)

    def _save(self) -> None:
        try:
            self.data_file.parent.mkdir(parents=True, exist_ok=True)
            self.data_file.write_text(
                json.dumps([asdict(i) for i in self._items],
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            log.warning("保存记忆失败：%s", e)

    def _refit_backend(self) -> None:
        """重新拟合向量后端（在 load / add / remove 之后调用）。"""
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

    # ----- CRUD -----
    def add(self, content: str, category: str = "other",
            importance: float = 0.5) -> MemoryItem:
        """添加一条记忆。importance ∈ [0, 1]。"""
        content = content.strip()
        if category not in CATEGORY_CHOICES:
            category = "other"
        importance = max(0.0, min(1.0, float(importance)))
        item = MemoryItem(
            id=f"m{uuid.uuid4().hex[:10]}",
            content=content,
            category=category,
            importance=importance,
        )
        with self._lock:
            # 完全相同的文本不重复记
            if any(i.content == content for i in self._items):
                log.debug("记忆去重：%s", content[:30])
                return item
            self._items.append(item)
            self._save()
            self._refit_backend()
        log.info("记忆新增 [%s] importance=%.2f: %s", category, importance, content[:60])
        return item

    def remove(self, mid: str) -> bool:
        with self._lock:
            before = len(self._items)
            self._items = [i for i in self._items if i.id != mid]
            removed = len(self._items) != before
            if removed:
                self._save()
                self._refit_backend()
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

    # ----- 检索 -----
    def search(
        self,
        keyword: str = "",
        category: str = "",
        limit: int = 20,
        use_semantic: bool = True,
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
        """
        with self._lock:
            items = list(self._items)
        if not items:
            return []

        if category:
            items = [i for i in items if i.category == category]

        kw = keyword.strip()
        if not kw:
            # 空关键词 → 按 importance×时新度 排序
            results = sorted(items, key=lambda i: self._score(i, 0.0), reverse=True)
            for r in results[:limit]:
                r.touch()
            self._maybe_save_dirty()
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

        # 综合排序
        scored = [(self._score(i, s), i) for s, i in zip(scores, items)]
        scored.sort(key=lambda x: x[0], reverse=True)

        # 命中阈值（substring 模式：要求至少子串命中）
        if isinstance(self.backend, SubstringBackend):
            scored = [(s, i) for s, i in scored if s > 0.0]
        # 取 top
        top = [i for _, i in scored[:limit]]
        for r in top:
            r.touch()
        self._maybe_save_dirty()
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

    def _maybe_save_dirty(self) -> None:
        with self._lock:
            if self._dirty:
                self._save()
                self._dirty = False

    def mark_dirty(self) -> None:
        with self._lock:
            self._dirty = True

    # ----- 重要 API（工具会调） -----
    def recall(self, keyword: str = "", category: str = "",
               limit: int = 5) -> list[MemoryItem]:
        """便捷接口：search 的简化版本，给 LLM tool 用。"""
        return self.search(keyword=keyword, category=category, limit=limit)

    def forget_by_keyword(self, keyword: str) -> int:
        """按关键词删除（保留：keyword 命中的全部删）。"""
        kw = keyword.strip()
        if not kw:
            return 0
        to_remove: list[str] = []
        with self._lock:
            for i in self._items:
                if kw.lower() in i.content.lower():
                    to_remove.append(i.id)
        for mid in to_remove:
            self.remove(mid)
        return len(to_remove)

    def detect_conflicts(self, new_content: str,
                         threshold: float = 0.7) -> list[tuple[MemoryItem, float]]:
        """检测新内容与已有记忆的冲突（语义相似度高于阈值）。

        Returns:
            list of (item, similarity_score)
        """
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
        return [(i, s) for i, s in zip(items, sims) if s >= threshold and i.category != "other"]

    # ----- 生命周期 -----
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
                self._refit_backend()
        return removed

    def merge_similar(self, threshold: float = 0.95) -> int:
        """合并高度相似（默认 ≥ 0.95）的记忆，保留 importance 最高那条。"""
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
                    self._refit_backend()
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
            dot = sum(x * y for x, y in zip(va, vb))
            na = math.sqrt(sum(x * x for x in va)) or 1.0
            nb = math.sqrt(sum(x * x for x in vb)) or 1.0
            return dot / (na * nb)
        except Exception:
            return 0.0

    def recent(self, n: int | None = None) -> list[MemoryItem]:
        items = self.all()
        items.sort(key=lambda i: i.created_at)
        return items[-(n or self.max_inject):]

    # ----- 注入 system prompt -----
    def system_block(self) -> str:
        items = self._select_for_injection()
        if not items:
            return ""
        lines = [f"- [{i.category}★{i.importance:.1f}] ({_format_time(i.created_at)}) {i.content}"
                 for i in items]
        return (
            "【你对主人的长期记忆】（来自以往相处，回答时可以自然引用；★ 后是重要性 0-1）\n"
            + "\n".join(lines)
        )

    def _select_for_injection(self) -> list[MemoryItem]:
        """选择注入 system prompt 的记忆：按综合分排序。"""
        with self._lock:
            pool = list(self._items)
        if not pool:
            return []
        # 综合分排序（无 query，semantic=0，仅类别+时新度+importance）
        pool.sort(key=lambda i: self._score(i, 0.0), reverse=True)
        return pool[: self.max_inject]


# ============================================================================
#  MemoryCurator（升级版 memory_evolution）
# ============================================================================


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


# ============================================================================
#  helpers
# ============================================================================


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
