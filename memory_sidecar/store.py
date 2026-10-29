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
* **检索也不等模型**：模型那一路（补向量索引 + 查向量 + 冷精排）有 0.8s 预算
  （DESKTOP_PET_MEMORY_MODEL_BUDGET），跑不完就先拿词面结果回答 —— 挂起队列里的卡
  会先用同一套分词做**词面兜底**进候选，所以「刚写完立刻搜」永远看得见那张卡；
  模型追上来只让排序更准。聊天路径的读超时是 2s，预算必须明显小于它。
* **相关度闸门**：够不着的问句一条都不给（见 _gate）—— 余弦门禁（绝对，
  min_similarity）+ 补位行门槛（相对，min_score_ratio）+ 语义保命符
  （dense_keep_ratio：复合问句的另外一半别被精排裁掉）。阈值都是量出来的，
  曲线见 docs/memory-sidecar.md 4.1。
* **运行时绝不联网下模型**：默认只读本地 HF 缓存（local_files_only=True），缺模型
  立刻失败并把 prefetch 脚本指给主人；要放开就设
  DESKTOP_PET_VECTOR_ALLOW_DOWNLOAD=1。/health 的 models.* 会区分 missing / degraded。
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

# ── 模型下载策略：运行时**绝不偷偷下载** ──────────────────────────────────
# 模型是 1.1GB 级的资产：悄悄去 HuggingFace 下 2GB 会抢磁盘 / 带宽，离线机器上
# 还会长时间挂着；主人完全不知道发生了什么。所以默认**只读本地缓存**，
# 缺模型就立刻失败并说清「跑哪条命令去备好」。
# 确实想让 sidecar 自己备模型（和当年 app/brain/vector_index.py 同一个口径）：
#     DESKTOP_PET_VECTOR_ALLOW_DOWNLOAD=1
ALLOW_DOWNLOAD_ENV = "DESKTOP_PET_VECTOR_ALLOW_DOWNLOAD"
#: 出错时指给主人的那条命令（prefetch 脚本不在 sidecar 的边界里，这里只做提示）
PREFETCH_HINT = "先跑一次 python scripts/prefetch_models.py 把模型备好"


def allow_runtime_download():
    """运行时允许联网下载模型吗？默认**不允许**。"""
    return str(os.environ.get(ALLOW_DOWNLOAD_ENV, "")).strip().lower() in (
        "1", "on", "yes", "true")


def hf_cache_dir():
    """HuggingFace 缓存目录（**只算磁盘路径**，不导入 huggingface_hub）。

    口径和 huggingface_hub 一致：HF_HUB_CACHE > HF_HOME/hub >
    XDG_CACHE_HOME/huggingface/hub > ~/.cache/huggingface/hub。
    自己算的理由：hub 的常量是**导入时**算的，导入之后再改 HF_HOME 就不生效了。
    """
    explicit = os.environ.get("HF_HUB_CACHE")
    if explicit:
        return Path(os.path.expandvars(os.path.expanduser(explicit)))
    home = os.environ.get("HF_HOME")
    if home:
        return Path(os.path.expandvars(os.path.expanduser(home))) / "hub"
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(os.path.expanduser(xdg)) if xdg else Path.home() / ".cache"
    return base / "huggingface" / "hub"


def model_in_cache(model_id):
    """这个模型在本地缓存里吗？（**只查磁盘，绝不联网**）"""
    model_id = str(model_id or "").strip()
    if not model_id:
        return False
    if os.path.isdir(model_id):  # 直接给了本地路径
        return True
    root = hf_cache_dir() / ("models--" + model_id.replace("/", "--"))
    snaps = root / "snapshots"
    if not snaps.is_dir():
        return False
    try:
        return any(p.is_dir() for p in snaps.iterdir())
    except OSError:
        return False


class ModelUnavailable(RuntimeError):
    """模型用不了。code="missing"（不在本地缓存）/ "failed"（加载出错）。

    /health 靠这个区分「没下载」和「下载了但加载失败」—— 两种情况主人要做的事不一样。
    """

    def __init__(self, message, code="failed"):
        super().__init__(message)
        self.code = code


def model_missing_error(role, model_id):
    return ModelUnavailable(
        "%s不在本地缓存：%s（运行时不会联网下载）。%s；"
        "或者设 %s=1 允许 sidecar 自己下载。" % (
            role, model_id, PREFETCH_HINT, ALLOW_DOWNLOAD_ENV),
        code="missing",
    )


def model_failed_error(role, model_id, exc):
    return ModelUnavailable(
        "%s加载失败：%s：%s。%s（或 python scripts/prefetch_models.py --force 重新下载）。"
        % (role, model_id, exc, PREFETCH_HINT),
        code="failed",
    )


# ── 精排模型：按设备自动选（实测数据见 resolve_rerank_model 的注释） ────────
RERANK_MODEL_ENV = "DESKTOP_PET_MEMORY_RERANK"
RERANK_MODEL_GPU = "BAAI/bge-reranker-v2-m3"
RERANK_MODEL_CPU = "BAAI/bge-reranker-base"
#: 环境变量给了就用它；**空字符串 = 没设，交给自动选择**（resolve_rerank_model）
RERANK_MODEL = os.environ.get(RERANK_MODEL_ENV) or ""

# /list 的默认条数与上限（防止一次拉爆整个卡库）
LIST_DEFAULT_LIMIT = 50
LIST_MAX_LIMIT = 500

# /graph 的默认规模：节点上限、每个节点的近邻数、相似度下限
GRAPH_DEFAULT_LIMIT = 300
GRAPH_MAX_LIMIT = 800
GRAPH_DEFAULT_TOP_K = 4
GRAPH_MIN_SIMILARITY = 0.15
#: 相对阈值：只连「跟自己最像的」达到这个比例的近邻
GRAPH_RELATIVE_CUT = 0.72

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

# ── 检索的两个旋钮（构造函数 / 环境变量都能改） ────────────────────────────
#: 「模型那一路」（补向量索引 + 查向量 + 冷精排）的时间预算（秒）。
#: 聊天路径的读超时是 2s —— 预算必须明显小于它：跑不完就先拿词面结果回答，
#: 索引/精排在后台补齐，下一次检索自然是热的。
#: 0 = 不限时（老行为：检索会一直等模型）。
MODEL_BUDGET_ENV = "DESKTOP_PET_MEMORY_MODEL_BUDGET"
DEFAULT_MODEL_BUDGET = 0.8

#: 相关度闸门 ①：最终分低于「最高分 × 比例」的尾巴丢掉（补位行门槛）。
MIN_SCORE_RATIO_ENV = "DESKTOP_PET_MEMORY_MIN_SCORE_RATIO"
DEFAULT_MIN_SCORE_RATIO = 0.12
#: 相关度闸门 ②（门禁）：query 与**最像的那条卡**的余弦相似度低于它 →
#: 这一问跟记忆库没有关系，一条都不给。
#: 0.42 是量出来的（bge-small-zh-v1.5 + 评测台 32 条语料 / 20 条问句）：
#:   有答案的问句，最像那条的相似度**下限** = 0.456；
#:   四条无答案问句的相似度**上限** = 0.376
#: → 0.42 落在中间，两边各留 0.036 / 0.044。曲线见 docs/memory-sidecar.md 4.1。
MIN_SIMILARITY_ENV = "DESKTOP_PET_MEMORY_MIN_SIMILARITY"
DEFAULT_MIN_SIMILARITY = 0.42
#: 补位行的「语义保命符」：字面/语义上明显是同一件事（cos ≥ 顶行 × 该系数）
#: 的行即使精排分低也不丢 —— 复合问句的另外一半经常是这个下场。
DENSE_KEEP_RATIO_ENV = "DESKTOP_PET_MEMORY_DENSE_KEEP_RATIO"
DEFAULT_DENSE_KEEP_RATIO = 0.75
#: 绝对下限：连最高分都不到它 → 整轮一条都不给（0 = 不设，默认靠 ①② 两条规则）。
MIN_SCORE_FLOOR_ENV = "DESKTOP_PET_MEMORY_MIN_SCORE_FLOOR"
DEFAULT_MIN_SCORE_FLOOR = 0.0

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


def lexical_score(query_tokens, text_tokens):
    """查询与一段文本的**词面重合度**（给「还没进向量索引的卡」兜底用）。

    只认多字词项（CJK 字 bigram / jieba 词 / 英文单词）：单字重合太容易成立
    （「我」「的」「了」满地都是），拿它当相关等于把无关的卡硬塞进来。
    返回 0 = 不相关；命中几个词项就是几分（重复命中不重复计分）。
    """
    strong = {t for t in query_tokens if len(t) >= 2}
    if not strong:
        return 0.0
    return float(len(strong & set(text_tokens)))


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
        """加载向量模型。**只读本地缓存**（缺模型立刻失败，不去 HuggingFace 下）。"""
        with _HF_LOCK:
            if self._model is not None:
                return self._model
            download = allow_runtime_download()
            # 先查磁盘再 import：缺模型时连 transformers 都不必加载（毫秒级失败）
            if not download and not model_in_cache(self.model_name):
                raise model_missing_error("向量模型", self.model_name)
            from sentence_transformers import SentenceTransformer

            try:
                model = SentenceTransformer(
                    self.model_name,
                    device=self.device or _pick_device(),
                    local_files_only=not download,  # True = 只读本地缓存，不联网
                )
            except Exception as e:
                if not download:
                    raise model_failed_error("向量模型", self.model_name, e) from e
                raise
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
        """加载精排模型。同样**只读本地缓存**（缺模型立刻失败，不联网）。"""
        with _HF_LOCK:
            if self._model is not None:
                return self._model
            download = allow_runtime_download()
            if not download and not model_in_cache(self.model_name):
                raise model_missing_error("精排模型", self.model_name)
            from sentence_transformers import CrossEncoder

            try:
                self._model = CrossEncoder(
                    self.model_name,
                    device=self.device or _pick_device(),
                    max_length=self.max_length,
                    local_files_only=not download,  # True = 只读本地缓存，不联网
                )
            except Exception as e:
                if not download:
                    raise model_failed_error("精排模型", self.model_name, e) from e
                raise
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


def _env_float(name, default):
    """读一个浮点环境变量；没设 / 不是数字都退回默认值（环境变量不该把服务搞挂）。"""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return float(default)
    try:
        return float(raw)
    except Exception:
        return float(default)


def _env_or(value, name, default):
    """显式参数 > 环境变量 > 默认值（三个旋钮统一走这条优先级）。"""
    return float(value) if value is not None else _env_float(name, default)


def _pca3(matrix):
    """矩阵（n × d）的主成分投影到 3 维 → (n × 3) ndarray。

    去中心后做 SVD，取前三个右奇异向量。维数不够 3 时补零列，
    免得只有两条记忆时画图直接崩。

    三个轴各自按标准差开方缩放：PCA 的第一主成分常常吃掉绝大部分方差，
    不均衡的话整张图会被压成一条线，看不出簇。
    """
    import numpy as np

    mat = np.asarray(matrix, dtype="float64")
    if mat.ndim != 2 or mat.shape[0] < 2:
        return np.zeros((max(1, mat.shape[0] if mat.ndim == 2 else 0), 3))
    centered = mat - mat.mean(axis=0, keepdims=True)
    try:
        _, _, vt = np.linalg.svd(centered, full_matrices=False)
    except np.linalg.LinAlgError:
        vt = np.eye(centered.shape[1])[:3]
    comps = vt[:3]
    if comps.shape[0] < 3:
        pad = np.zeros((3 - comps.shape[0], comps.shape[1]))
        comps = np.vstack([comps, pad])
    out = centered @ comps.T
    spread = out.std(axis=0)
    for axis in range(3):
        if spread[axis] > 1e-9:
            out[:, axis] = out[:, axis] / (spread[axis] ** 0.5)
    scale = float(np.max(np.abs(out))) or 1.0
    return out / scale * 8.5     # 半径 ~8.5，跟面板里球壳的尺寸一致


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
        model_budget=None,
        min_score_ratio=None,
        min_score_floor=None,
        min_similarity=None,
        dense_keep_ratio=None,
    ):
        self.data_dir = str(data_dir or DEFAULT_DATA_DIR)
        self.cards_path = os.path.join(self.data_dir, CARDS_FILE)
        self.chroma_dir = os.path.join(self.data_dir, CHROMA_SUBDIR)
        self.collection_name = collection or COLLECTION_NAME
        self.candidate_n = max(1, int(candidate_n))
        self.retry_cooldown = max(0.0, float(retry_cooldown))
        # 检索里「模型那一路」的预算（秒）：0 = 不限时
        self.model_budget = max(0.0, _env_or(model_budget, MODEL_BUDGET_ENV, DEFAULT_MODEL_BUDGET))
        # 相关度闸门：相对比例（0 = 不裁尾巴）+ 绝对下限（0 = 不设下限）+ 语义门禁
        self.min_score_ratio = _clamp01(
            _env_or(min_score_ratio, MIN_SCORE_RATIO_ENV, DEFAULT_MIN_SCORE_RATIO))
        self.min_score_floor = max(
            0.0, _env_or(min_score_floor, MIN_SCORE_FLOOR_ENV, DEFAULT_MIN_SCORE_FLOOR))
        self.min_similarity = _clamp01(
            _env_or(min_similarity, MIN_SIMILARITY_ENV, DEFAULT_MIN_SIMILARITY))
        self.dense_keep_ratio = _clamp01(
            _env_or(dense_keep_ratio, DENSE_KEEP_RATIO_ENV, DEFAULT_DENSE_KEEP_RATIO))
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
        self._embed_missing = False   # 模型不在本地缓存（区别于「加载失败」）
        self._embed_ready = False
        self._embed_failed_at = 0.0
        self._rerank_error = None
        self._rerank_missing = False
        self._rerank_ready = False
        self._rerank_failed_at = 0.0
        self._load_error = None
        self.last_search_ms = 0
        #: 上一轮「模型那一路」的线程（还活着 = 模型正在补索引/加载，这一轮别排第二个）
        self._model_thread = None
        #: 有多少次检索没在预算内等到模型（/health、/stats 里能看出来）
        self.model_timeouts = 0

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
        """对外暴露的卡片字段（/search 与 /list 同一套口径）。

        ``category`` 是调用方写卡时塞进来的「老的内容类型」（preference /
        shared / ...）：面板的类别筛、主动发言的「共同经历」都要它。**永远
        带这个键**（没写就是空字符串），调用方不用再回读 cards.json 去补。
        """
        return {
            "id": card["id"],
            "key": card["key"],
            "kind": card["kind"],
            "text": card["text"],
            "category": str(card.get("category") or ""),
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

    # -- 语义图 ------------------------------------------------------------
    def graph(self, limit: int = GRAPH_DEFAULT_LIMIT, top_k: int = GRAPH_DEFAULT_TOP_K,
              min_similarity: float = GRAPH_MIN_SIMILARITY) -> dict:
        """把记忆卡投影成一张语义图：节点 = 卡，边 = 语义上挨着的两张卡。

        位置取自 Chroma 里已存的向量（归一化的 bge-small-zh，512 维），
        用 PCA 降到 3 维 —— 意思相近的记忆在图上自然挨着，边也就有意义了。
        边取每个节点的余弦近邻（向量已归一化，点积即余弦）。

        只读派生索引，不碰模型：向量已经在 Chroma 里，缺模型照样能出图。
        没有向量 / 少于两张卡就返回 ok=False，让调用方退回自己的布局。
        """
        try:
            n = int(limit if limit is not None else GRAPH_DEFAULT_LIMIT)
        except Exception:
            n = GRAPH_DEFAULT_LIMIT
        n = max(2, min(n, GRAPH_MAX_LIMIT))
        try:
            k = max(1, int(top_k or GRAPH_DEFAULT_TOP_K))
        except Exception:
            k = GRAPH_DEFAULT_TOP_K
        try:
            floor = float(min_similarity)
        except Exception:
            floor = GRAPH_MIN_SIMILARITY

        cards = self._listing(include_superseded=False)[:n]
        ids = [str(c["id"]) for c in cards]
        if len(ids) < 2:
            return {"ok": False, "reason": "记忆卡不足两张，画不出语义图",
                    "nodes": [], "edges": []}

        vecs = self._stored_vectors(ids)
        if vecs is None or len(vecs) < 2:
            return {"ok": False, "reason": "向量索引里没有可用的向量",
                    "nodes": [], "edges": []}
        vec_ids, matrix = vecs
        keep = {cid: i for i, cid in enumerate(vec_ids)}
        picked = [(cid, c) for cid, c in zip(ids, cards) if cid in keep]
        if len(picked) < 2:
            return {"ok": False, "reason": "记忆卡和向量对不上",
                    "nodes": [], "edges": []}

        order = [keep[cid] for cid, _ in picked]
        mat = matrix[order]
        positions = _pca3(mat)
        node_ids = [cid for cid, _ in picked]

        edges = []
        sims = mat @ mat.T          # 归一化向量 → 点积就是余弦
        for i in range(len(node_ids)):
            row = sims[i]
            best = sorted(
                (j for j in range(len(node_ids)) if j != i),
                key=lambda j: -row[j],
            )[:k]
            if not best:
                continue
            # 相对阈值：只连「跟自己最像的那几个」，而不是一刀切的绝对线。
            # 绝对线单独用会把弱相似的整簇边砍光，图上只剩孤立的点。
            cut = max(floor, float(row[best[0]]) * GRAPH_RELATIVE_CUT)
            for j in best:
                if row[j] < cut:
                    continue
                if i < j:                      # 无向边只留一份
                    edges.append({
                        "a": node_ids[i], "b": node_ids[j],
                        "w": round(float(row[j]), 4),
                    })

        return {
            "ok": True,
            "projection": "pca",
            "dim": int(mat.shape[1]),
            "nodes": [
                {"id": cid, "x": round(float(positions[i][0]), 5),
                 "y": round(float(positions[i][1]), 5),
                 "z": round(float(positions[i][2]), 5)}
                for i, (cid, _) in enumerate(picked)
            ],
            "edges": edges,
            "total": len(cards),
        }

    def _stored_vectors(self, ids):
        """从 Chroma 取这些 id 的已存向量 → (ids, ndarray) 或 None。

        向量是派生索引里的东西，读不到就算了 —— 不为了画图去加载模型。
        """
        try:
            import numpy as np
        except Exception:  # noqa: BLE001
            return None
        try:
            with self._index_lock:
                col = self._ensure_chroma()
                got = col.get(ids=list(ids), include=["embeddings"])
        except Exception as e:  # noqa: BLE001
            self._chroma_error = str(e)
            return None
        raw_ids = list(got.get("ids") or [])
        raw = got.get("embeddings")
        if raw is None or len(raw_ids) == 0:
            return None
        try:
            mat = np.asarray(raw, dtype="float32")
        except Exception:  # noqa: BLE001
            return None
        if mat.ndim != 2 or mat.shape[0] != len(raw_ids):
            return None
        return raw_ids, mat

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

    def _pending_candidates(self, query):
        """挂起队列里的卡（已落盘、还没进向量索引）用**词面**兜底捞一遍。

        写卡不等模型 → 真值里总有一小批卡「JSON 有了、向量还没有」。检索如果
        只看派生索引，这批卡就「刚说完就忘了」。这里只对**挂起的那几条**做一次
        廉价字面重合（和 BM25 同一套分词），并进候选集 —— 模型追上来之后只是把
        排序调得更准，永远不会让刚写的卡从结果里消失。
        """
        if not self.use_vector:
            return {}  # 向量腿关着就不存在「欠索引」这回事（BM25 已经覆盖全部卡）
        with self._pending_lock:
            ids = [cid for cid, op in self._pending_vectors.items() if op == "upsert"]
        if not ids:
            return {}
        qt = tokenize(query)
        if not any(len(t) >= 2 for t in qt):
            return {}
        with self._cards_lock:
            cards = [
                self._cards[cid] for cid in ids
                if cid in self._cards
                and self._cards[cid]["status"] == STATUS_ACTIVE
                and self._cards[cid]["text"]
            ]
        out = {}
        for card in cards:
            score = lexical_score(qt, tokenize(card["text"] + " " + card["kind"]))
            if score > 0:
                out[card["id"]] = score
        if len(out) > self.candidate_n:
            out = dict(sorted(out.items(), key=lambda kv: -kv[1])[: self.candidate_n])
        return out

    # -- 「模型那一路」的时间预算 ------------------------------------------
    def _vector_phase(self, query):
        """补齐向量索引 + 算查询向量 + 查一次（只在 _run_in_budget 的线程里跑）。"""
        with self._index_lock:
            try:
                self.sync_index()
            except Exception:
                pass
            try:
                self.ensure_vector_index()
            except Exception:
                pass
            return self._vector_search_locked(query, self.candidate_n)

    def _run_in_budget(self, fn):
        """在预算内跑 fn()；跑不完返回 None，线程留着继续跑。

        为什么用线程而不是先估一下「这一步慢不慢」：模型是懒加载的，估不出来。
        索引与向量都是**派生数据** → 补它慢一点没有理由挡在回答前面；跑不完就把
        线程放生，它补完的索引下一次检索直接用得上（所以只会越检索越准）。
        """
        if self.model_budget <= 0:      # 不限时 = 老行为
            return fn()
        prev = self._model_thread
        if prev is not None and prev.is_alive():
            # 上一轮还在跑：模型是串行的，再排一个线程也只是排队等它
            self.model_timeouts += 1
            return None
        box = {}

        def work():
            try:
                box["value"] = fn()
            except Exception as e:      # 派生索引补不上不算检索失败
                box["error"] = str(e)

        th = threading.Thread(target=work, name="memory-sidecar-model", daemon=True)
        self._model_thread = th
        th.start()
        th.join(max(0.0, self.model_budget))
        if th.is_alive():
            self.model_timeouts += 1
            return None
        return box.get("value")

    def _gate(self, scored, sims=None):
        """相关度闸门：这一问够不着就别硬塞（老实现有，迁到 sidecar 时丢了）。

        规则（阈值全部**标定出来的**，曲线见 docs/memory-sidecar.md 4.1）：

        ① 门禁（绝对，看余弦相似度）：最像的那条卡都不够像 → 这一问跟记忆库没
           关系，一条都不给。为什么不用「最终分」当门禁：最终分是**重排分 × 衰减
           × 重要度**，同一条问句换个说法就差一个数量级（实测无关问句的最高分
           0.000~0.110，而有答案的问句里「我不能吃什么」只有 0.034）—— 拿它当
           绝对门槛必然要么放噪声、要么裁掉真答案。
        ② 补位行门槛（相对，看最终分）：低于「顶行 × min_score_ratio」的尾巴
           丢掉；但语义/字面明显是同一件事（cos ≥ 顶行 × dense_keep_ratio）的行
           保留 —— 复合问句的另外一半常被交叉编码器判低。
        ③ 顶行永远保留，最高分 <= 0（一条正分都没有）则整轮不给。

        闸门只做减法：它不会把任何一行排到前面，也不改行内顺序。
        """
        if not scored:
            return []
        sims = sims or {}
        top = float(scored[0][0])
        if top <= 0 or top < self.min_score_floor:
            return []
        top_sim = max(sims.values()) if sims else None
        if top_sim is not None and self.min_similarity > 0 and top_sim < self.min_similarity:
            return []
        if self.min_score_ratio <= 0:
            return list(scored)
        cut = top * self.min_score_ratio
        sim_cut = top_sim * self.dense_keep_ratio if top_sim is not None else None
        kept = []
        for row in scored:
            if not kept:                      # 顶行永远在
                kept.append(row)
                continue
            if float(row[0]) >= cut:
                kept.append(row)
                continue
            sim = sims.get(row[1]["id"])
            if sim is not None and sim_cut is not None and sim >= sim_cut:
                kept.append(row)              # 语义上就是同一件事：别裁
        return kept

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
        # 索引锁正被「模型那一路」占着（在补索引）时**不等它**：挂起队列记着呢，
        # sync_index 迟早会删掉 —— 删除没有理由排在一把模型锁后面。
        if self.use_vector and self._col is not None and self._index_lock.acquire(timeout=0.05):
            try:
                if self._vector_delete(gone) >= len(gone):
                    self._clear_pending(gone)
            finally:
                self._index_lock.release()
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
            self._embed_missing = getattr(e, "code", "") == "missing"
            self._embed_failed_at = time.time()
            raise
        if not vecs or len(vecs) != len(texts):
            err = "向量数量不匹配：期望 %d 实际 %d" % (len(texts), len(vecs or []))
            self._embed_error = err
            self._embed_failed_at = time.time()
            raise RuntimeError(err)
        self._embed_error = None
        self._embed_missing = False
        self._embed_failed_at = 0.0
        self._embed_ready = True
        return [[float(x) for x in row] for row in vecs]

    def _rerank_in_budget(self, query, texts):
        """精排打分，返回 (分数 | None, 这次是否走了预算)。

        已经加载好的精排**同步**跑：那几百毫秒是回答质量的一部分，不该被裁。
        还没加载（冷启动要十几秒）才走预算 —— 超时这一轮先用 RRF 粗排回答，
        模型在后台线程里继续加载，下一个问题就用上了。
        """
        if not self.use_rerank or self._reranker is None or self._rerank_ready:
            return self._rerank(query, texts), False
        return self._run_in_budget(lambda: self._rerank(query, texts)), True

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
            self._rerank_missing = getattr(e, "code", "") == "missing"
            self._rerank_failed_at = time.time()
            return None
        if not raw or len(raw) != len(texts):
            self._rerank_error = "精排返回数量不匹配"
            self._rerank_failed_at = time.time()
            return None
        self._rerank_error = None
        self._rerank_missing = False
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
    def _vector_search_locked(self, query, k):
        """向量检索（**调用方必须已经持有 _index_lock**，见 _vector_phase）。"""
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
        elif self._embed_missing:
            reasons.append("向量腿降级：向量模型不在本地缓存（%s）" % PREFETCH_HINT)
        elif self._embed_error or self._vector_error:
            reasons.append("向量腿降级：%s" % (self._embed_error or self._vector_error))
        if not self.use_bm25:
            reasons.append("BM25 已关闭（--no-bm25）")
        elif self._bm25_error:
            reasons.append("BM25 降级：%s" % self._bm25_error)
        if not self.use_rerank:
            reasons.append("精排已关闭（--no-rerank）")
        elif self._rerank_missing:
            reasons.append("精排降级：精排模型不在本地缓存（%s）" % PREFETCH_HINT)
        elif self._rerank_error:
            reasons.append("精排降级：%s" % self._rerank_error)
        if self._chroma_error:
            reasons.append("Chroma 异常：%s" % self._chroma_error)
        return reasons

    def search(self, query, top_k=8, mood=None, kind=None, kinds=None, key=None):
        """粗排（RRF 融合）→ 精排（cross-encoder）→ 时间 / 情绪 / 重要度加权 → 相关度闸门。

        返回 dict：{'results': [...], 'path': ..., 'degraded': bool, 'error': str|None,
                    'gate': {...}, 'modelPending': bool|None}。
        path 说明这次是谁给的候选：hybrid / vector / bm25 / pending / fallback / none。
        两条粗排腿全挂时返回空结果 + 明确的 error（绝不抛给 HTTP 层变 500）。

        **检索不排队等模型**：答案的源头是 JSON 卡本身（真值），不是派生索引 ——
          ① BM25：脏了就先重建（纯本地计算，一步都不碰模型）；
          ② 挂起队列：还没进向量索引的卡用词面兜底并进候选（刚写的卡立刻看得见）；
          ③ 向量腿 / 冷精排：给一个时间预算（model_budget），跑不完先拿①②回答，
             模型在后台补索引 —— 所以模型只会让排序**变准**，不会让卡**消失**。
        """
        result = {"results": [], "path": "none", "degraded": False, "error": None}
        q = str(query or "").strip()
        if not q:
            return result
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

        # ① BM25：脏了就先重建（本地计算，不等模型）
        if self._bm25_dirty():
            try:
                self._rebuild_bm25()
            except Exception:
                pass
        bm = self._bm25_search(q, self.candidate_n)
        # ② 词面兜底：写卡不等模型，所以总有几条卡「已落盘、还没进向量索引」
        pend = self._pending_candidates(q)
        # ③ 模型那一路（补向量索引 + 查一次）：预算内跑完才合并进候选
        vec = self._run_in_budget(lambda: self._vector_phase(q)) if self.use_vector else {}
        result["modelPending"] = bool(self.use_vector and vec is None)
        vec = vec or {}

        fused = {}
        for leg in (bm, vec, pend):
            for rank, (cid, _) in enumerate(sorted(leg.items(), key=lambda kv: -kv[1])):
                if cid in allowed:
                    fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)

        if vec and (bm or pend):
            result["path"] = "hybrid"
        elif vec:
            result["path"] = "vector"
        elif bm:
            result["path"] = "bm25"
        elif pend:
            result["path"] = "pending"

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

        if not fused:  # 三路都没命中 → 退回全量让精排挑
            fused = {c["id"]: 1.0 for c in pool[: self.candidate_n]}
            result["path"] = "fallback"
        cands = sorted(fused.items(), key=lambda kv: -kv[1])[: self.candidate_n]
        cands = [(cid, base) for cid, base in cands if cid in by_id]

        rerank_scores = {}
        raw, budgeted = self._rerank_in_budget(q, [by_id[cid]["text"] for cid, _ in cands])
        if budgeted and raw is None:
            result["modelPending"] = True
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

        # ④ 相关度闸门：这一问够不着就别硬塞（「用不上就别硬扯」不能只靠免责文案）
        kept = self._gate(scored, vec)
        result["gate"] = {
            "top": round(float(scored[0][0]), 4) if scored else 0.0,
            "topSim": round(max(vec.values()), 4) if vec else None,
            "kept": len(kept),
            "dropped": len(scored) - len(kept),
            "minScoreRatio": self.min_score_ratio,
            "minSimilarity": self.min_similarity,
        }

        out = []
        for final, card, primary, decay, logit in kept[: max(1, int(top_k))]:
            card["lastHitAt"] = now
            card["hitCount"] = _as_int(card.get("hitCount")) + 1
            sim = vec.get(card["id"])
            out.append(
                {
                    **self._public_card(card),
                    "score": round(final, 4),
                    "rel": round(primary, 4),
                    "rerankLogit": round(logit, 4) if logit is not None else None,
                    "decay": round(decay, 4),
                    "sim": round(float(sim), 4) if sim is not None else None,
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
        # 每个线程自己的临时名：检索（会刷新 hitCount）与写卡可能同时落盘，
        # 共用一个 .tmp 会互相截断 —— 卡片文件坏了就是真的把记忆弄丢了。
        tmp = "%s.%d.%d.tmp" % (self.cards_path, os.getpid(), threading.get_ident())
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
        """模型状态：disabled / missing / degraded / ready / unloaded。

        missing = 模型压根不在本地缓存（跑 prefetch 脚本就能解决）；
        degraded = 在缓存里但加载/推理失败（要看日志或重下）。两件事不一样。
        """
        if which == "embed":
            if not self.use_vector:
                return "disabled"
            if self._embed_missing:
                return "missing"
            if self._embed_error:
                return "degraded"
            return "ready" if self._embed_ready else "unloaded"
        if not self.use_rerank:
            return "disabled"
        if self._rerank_missing:
            return "missing"
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
            # 检索里「模型那一路」没等到预算的次数：>0 说明这几轮是拿词面结果回答的
            "modelTimeouts": self.model_timeouts,
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
                    "inCache": model_in_cache(self.embed_model) if self.use_vector else None,
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
                    "inCache": model_in_cache(self.rerank_model) if self.use_rerank else None,
                    "why": self.rerank_reason,
                },
                # 检索的两个旋钮 + 它们实际的表现（标定 / 排查用得上）
                "search": {
                    "modelBudgetMs": int(self.model_budget * 1000),
                    "modelTimeouts": self.model_timeouts,
                    "minScoreRatio": self.min_score_ratio,
                    "minScoreFloor": self.min_score_floor,
                    "minSimilarity": self.min_similarity,
                    "denseKeepRatio": self.dense_keep_ratio,
                    "lastSearchMs": self.last_search_ms,
                },
                "degraded": bool(reasons),
                "degradation": reasons,
            }
