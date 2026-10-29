"""向量索引（Chroma 持久化）+ 语义重排（cross-encoder）。

定位（**最重要的一条**）：本模块是**派生索引**，不是真相来源。
    - 真相来源永远是 SQLite 的 `memories` 表（app/brain/memory.py 的 MemoryStore）；
    - 这里只做两件事：
        1. 把 SQLite 的行变成一个「能按语义检索」的索引（Chroma 持久化）；
        2. 在检索末端用 cross-encoder 给候选**重新排序**（最终裁决权）。
    - 索引**永远可重建**、**永远可降级**：任何一步失败（模型没下载 / chroma 坏了 /
      索引目录损坏 / 首次运行没网 / 磁盘满）都只降级成「索引不可用」，
      调用方（MemoryStore）退回既有的 substring + TF-IDF 路径 —— 桌宠照常工作，
      **绝不因为索引把聊天路径搞崩**。

为什么用 Chroma 而不是自己写 HNSW：
    - 现成、持久化、自带元数据过滤（scope / kind / status / category）；
    - 索引可以整个删掉重建 —— 真相在 SQLite，索引丢了不心疼。

⚠️ USE_TF=0（**必须在 import transformers 之前设**）：
    本机装了 tensorflow 2.19 + Keras 3，而 transformers 4.x 见到 Keras 3 会直接
    raise ValueError("Your currently installed version of Keras is Keras 3 ...")。
    本项目自己从不 import transformers（app/brain/memory.py 里那条老的 MiniLM
    语义腿已经退役，见该文件的说明），所以在这里设 USE_TF=0 是安全的。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import sys
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

# ⚠️ 必须早于任何 transformers / sentence_transformers 的 import
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")

log = logging.getLogger(__name__)

#: 向量模型（512 维，中文检索）
EMBED_MODEL_ID = "BAAI/bge-small-zh-v1.5"
#: 重排模型（cross-encoder，最终排序裁决权）
RERANK_MODEL_ID = "BAAI/bge-reranker-base"
#: Chroma collection 名（一个角色一个目录，目录里一个 collection）
COLLECTION_NAME = "memories"
#: 索引自身的状态文件（记录摘要 → 漂移检测用）
STATE_FILENAME = "_index_state.json"
STATE_VERSION = 1
#: 默认取多少个候选
DEFAULT_TOP_K = 30
#: 批量大小（嵌入 / 重排共用）
BATCH_SIZE = 32
#: 等待模型加载的默认上限（秒）
MODEL_LOAD_TIMEOUT = 180.0

# ---------------------------------------------------------------------------
# 索引状态词汇（面板 / 测试 / 脚本**共用同一套**）
# ---------------------------------------------------------------------------
#: 本 store 关掉了向量索引
STATE_DISABLED = "disabled"
#: 索引还没建过（没有行，也没有建过）
STATE_NOT_BUILT = "not_built"
#: 索引建好了，且跟源库一致
STATE_BUILT = "built"
#: 源库改过了，索引还没重建（**漂移**）
STATE_DRIFTED = "drifted"
#: 索引**用不了**：chroma 打不开，或模型加载失败
STATE_UNAVAILABLE = "unavailable"


def derive_state(*, enabled: bool, built: bool, n_indexed: int,
                 n_source: int, in_sync: bool, drift: bool,
                 error: Optional[str] = None) -> tuple[str, str]:
    """索引状态**只从事实推出来** —— 返回 (state, 理由一句话)。

    事实字段（调用方给）：built / n_indexed / n_source / in_sync / drift / error。
    推导顺序（**从确定到不确定**）：
        disabled    —— 本 store 关掉了向量索引；
        unavailable —— 索引**用不了**（chroma 打不开 / 模型加载失败）；
        not_built   —— 索引**还没建过**（既没有行，也没有建过）；
        drifted     —— 源库跟索引**对不上**（源库改了、索引没重建）；
        built       —— 索引建好了，且跟源库一致。

    ⚠️ 不变式（**不许跟同一份 payload 里的别的字段打架**）：
        state == "not_built"   ⇒  built is False 且 n_indexed == 0
        state == "built"       ⇒  built is True 且 in_sync is True
        state == "drifted"     ⇒  (built 或 n_indexed > 0) 且 in_sync is False
        state == "unavailable" ⇒  有具体的错误原因（error 非空）
        state == "disabled"    ⇒  enabled is False
    """
    if not enabled:
        return STATE_DISABLED, "本 store 关掉了向量索引（vector_index=False）"
    if error:
        return STATE_UNAVAILABLE, "索引用不了：%s" % error
    if not built and n_indexed <= 0:
        return STATE_NOT_BUILT, "索引还没建过（重建一次就有了）"
    if not in_sync:
        return STATE_DRIFTED, ("源库 %d 行 / 索引 %d 行：源库改过了，索引还没重建"
                               % (n_source, n_indexed))
    return STATE_BUILT, "索引 %d 行，跟源库一致" % n_indexed


class VectorIndexError(RuntimeError):
    """向量索引相关错误（调用方应该降级，而不是往上抛）。"""


# ---------------------------------------------------------------------------
# Windows DLL 陷阱：PyQt5 与 chromadb 的 Rust 绑定会抢 MSVC 运行时
# ---------------------------------------------------------------------------
# PyQt5 自带一份**旧版** MSVC 运行时（PyQt5/Qt5/bin/msvcp140.dll 等），
# 并把那个目录加进 DLL 搜索路径。于是**后**加载的原生扩展会捡到那份旧运行时：
#     chromadb_rust_bindings.pyd 依赖 MSVCP140.dll / VCRUNTIME140*.dll
#     → 拿到旧版本 → ABI 不匹配 → Windows fatal exception: access violation
#     → **整进程崩溃**（桌宠直接消失）。
#
# 办法：在 PyQt5 **之前**把系统目录里那份 MSVC 运行时预加载进进程，
# 全进程就都用系统那份（Qt 也会复用它）→ 不再抢 → 不会崩。
# （项目里已经有同类问题的先例：app/main.py 为了 ctranslate2 也要求
#   「必须在 PyQt5 之前 import」。）

_MSVC_RUNTIME_DLLS = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")


def preload_system_msvc_runtime() -> bool:
    """把**系统目录**里的 MSVC 运行时提前加载（Windows only）。

    必须在 **import PyQt5 之前**调用才有效（晚了 Qt 已经占住旧版本）。
    返回是否成功。
    """
    if os.name != "nt":
        return True
    root = os.environ.get("SystemRoot") or "C:/Windows"
    ok = True
    for dll in _MSVC_RUNTIME_DLLS:
        try:
            ctypes.WinDLL(os.path.join(root, "System32", dll))
        except Exception:  # noqa: BLE001
            ok = False
    return ok


def _loaded_module_path(name: str) -> str:
    """某个**已加载** DLL 的完整路径（拿不到就返回空串）。"""
    if os.name != "nt":
        return ""
    try:
        from ctypes import wintypes
        handle = wintypes.HMODULE()
        get = ctypes.windll.kernel32.GetModuleHandleExW
        get.argtypes = [wintypes.DWORD, wintypes.LPCWSTR,
                        ctypes.POINTER(wintypes.HMODULE)]
        get.restype = wintypes.BOOL
        # 0x2 = GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT
        if not get(0x2, name, ctypes.byref(handle)):
            return ""
        buf = ctypes.create_unicode_buffer(1024)
        length = wintypes.DWORD(1024)
        ctypes.windll.kernel32.GetModuleFileNameW(
            handle, buf, ctypes.byref(length))
        return buf.value
    except Exception:  # noqa: BLE001
        return ""


def system_msvc_runtime_loaded() -> bool:
    """系统目录里的 MSVC 运行时是不是已经被加载进本进程了？

    是 → 全进程都用系统那份（PyQt5 的旧版本被挤掉）→ chroma 的 Rust 绑定安全。
    """
    path = _loaded_module_path("msvcp140.dll")
    if not path:
        return False
    return "system32" in os.path.normcase(path)


def chroma_runtime_safe() -> bool:
    """chromadb 的 Rust 绑定在这个进程里**能不能**安全加载？

    判据：PyQt5 已经加载 **且** 系统那份 MSVC 运行时没被预加载
    → Qt 的旧运行时会跟 chroma 抢 → 访问冲突 → 整进程崩。
    这种情况下**宁可不启用索引**（降级回 substring + TF-IDF），
    也不能让桌宠崩掉。
    """
    if os.name != "nt":
        return True
    if "PyQt5" not in sys.modules:
        return True          # Qt 还没加载 → chroma 先加载，没问题
    return system_msvc_runtime_loaded()


#: 本模块被导入时 Qt 是不是已经加载了（= 预加载还来不来得及）
_QT_LOADED_AT_IMPORT = "PyQt5" in sys.modules
if not _QT_LOADED_AT_IMPORT:
    # 趁 Qt 还没加载：把系统那份 MSVC 运行时先拉进来（为 chroma 铺路）
    preload_system_msvc_runtime()


# ---------------------------------------------------------------------------
# 路径约定：索引住在记忆库旁边，data/vectors/<角色>/
# ---------------------------------------------------------------------------


def _character_from_stem(stem: str) -> str:
    """从库文件名推出角色名（memory_小汐.db → 小汐）。"""
    if stem.startswith("memory_") and len(stem) > len("memory_"):
        return stem[len("memory_"):]
    return ""


def index_dir_for(db_path: str | Path, character_name: str = "") -> Path:
    """给定记忆库路径，算出它的向量索引目录。

    约定（跟项目既有的 per-character 隔离一致）：
        data/memory_小汐.db  →  data/vectors/小汐/
        data/memory.db       →  data/vectors/memory/
        ~/.desktop-pet/user_memory.db → ~/.desktop-pet/vectors/user_memory/
    """
    p = Path(db_path)
    name = (character_name or "").strip() or _character_from_stem(p.stem) or p.stem
    return p.parent / "vectors" / name


# ---------------------------------------------------------------------------
# 摘要（漂移检测）
# ---------------------------------------------------------------------------

#: 参与摘要的字段（content + 检索用的元数据；元数据变了索引也得跟着改）
_HASH_FIELDS = ("content", "category", "scope", "kind", "status", "topic")


def row_hash(row: dict) -> str:
    """单行的指纹（内容 + 元数据）。"""
    h = hashlib.sha1()
    h.update(str(row.get("id") or "").encode("utf-8", "replace"))
    for field in _HASH_FIELDS:
        h.update(b"\x1f")
        h.update(str(row.get(field) or "").encode("utf-8", "replace"))
    return h.hexdigest()


def digest_rows(rows: Iterable[dict]) -> str:
    """整库指纹：行数 + 每行指纹的汇总（与顺序无关）。"""
    hashes = sorted(row_hash(r) for r in rows)
    h = hashlib.sha256()
    h.update(str(len(hashes)).encode())
    for x in hashes:
        h.update(b"\x1e")
        h.update(x.encode())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# 模型缓存（进程级）
# ---------------------------------------------------------------------------


class _ModelCache:
    """进程级模型缓存 —— 模型只加载一次，多个索引实例共用。

    为什么必须进程级：MemoryStore 会被反复创建（每个角色一份、每次会话一份），
    每个实例都重新 load 一遍模型 = 白白烧几十秒 CPU + 内存翻倍。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._embedder: Any = None
        self._reranker: Any = None
        self._embed_error: Optional[str] = None
        self._rerank_error: Optional[str] = None
        self._embed_thread: Optional[threading.Thread] = None
        self._rerank_thread: Optional[threading.Thread] = None
        self._embed_done = threading.Event()
        self._rerank_done = threading.Event()

    # -- 向量模型 --
    def embedder(self, model_id: str, blocking: bool = True,
                 timeout: Optional[float] = MODEL_LOAD_TIMEOUT) -> Any:
        with self._lock:
            if self._embedder is not None:
                return self._embedder
            if self._embed_error is not None:
                return None
            if self._embed_thread is None:
                self._embed_thread = threading.Thread(
                    target=self._load_embedder, args=(model_id,),
                    daemon=True, name="VectorIndex-embedder")
                self._embed_thread.start()
        if not blocking:
            return None
        self._embed_done.wait(timeout)
        return self._embedder

    def _load_embedder(self, model_id: str) -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
            log.info("加载向量模型 %s ...", model_id)
            self._embedder = SentenceTransformer(model_id)
        except Exception as e:  # noqa: BLE001
            self._embed_error = str(e)
            log.warning("向量模型 %s 加载失败（降级，不影响聊天）：%s", model_id, e)
        finally:
            self._embed_done.set()

    # -- 重排模型 --
    def reranker(self, model_id: str, blocking: bool = True,
                 timeout: Optional[float] = MODEL_LOAD_TIMEOUT) -> Any:
        with self._lock:
            if self._reranker is not None:
                return self._reranker
            if self._rerank_error is not None:
                return None
            if self._rerank_thread is None:
                self._rerank_thread = threading.Thread(
                    target=self._load_reranker, args=(model_id,),
                    daemon=True, name="VectorIndex-reranker")
                self._rerank_thread.start()
        if not blocking:
            return None
        self._rerank_done.wait(timeout)
        return self._reranker

    def _load_reranker(self, model_id: str) -> None:
        try:
            from sentence_transformers import CrossEncoder  # type: ignore
            log.info("加载重排模型 %s ...", model_id)
            self._reranker = CrossEncoder(model_id)
        except Exception as e:  # noqa: BLE001
            self._rerank_error = str(e)
            log.warning("重排模型 %s 加载失败（降级，不影响聊天）：%s", model_id, e)
        finally:
            self._rerank_done.set()

    @property
    def embed_error(self) -> Optional[str]:
        return self._embed_error

    @property
    def rerank_error(self) -> Optional[str]:
        return self._rerank_error


_MODEL_CACHE = _ModelCache()


def _dist_to_sim(distance: float, space: str) -> float:
    """Chroma 的距离 → 余弦相似度（越大越像）。"""
    if space == "l2":
        # 归一化向量的 L2 距离：||a-b||² = 2 - 2cos  ⇒  cos = 1 - d²/2
        return 1.0 - (distance ** 2) / 2.0
    return 1.0 - distance


# ---------------------------------------------------------------------------
# 进程级索引注册表：一个索引目录 → **一个** VectorIndex 实例
# ---------------------------------------------------------------------------
# 为什么必须共享（这不是优化，是**正确性**要求）：
#   1. Chroma 的持久化客户端会**一直占着**索引目录里的 sqlite 文件；
#      同一目录开两个客户端 = 两个句柄各看各的，还容易撞写锁。
#   2. 更要命的是：delete_collection() 会让**别的句柄**全部失效 ——
#      别的实例随即 count()==0，索引在别人眼里「凭空消失」。
#      （reindex_live 会新建一个 MemoryStore → 新的 VectorIndex → 新开一个
#        客户端 → 重建时删掉 collection → 原来的 store 看不到自己的索引。）
#   所以：同目录（同 collection、同模型）→ **同一个实例**，
#   同一个客户端、同一个 collection 句柄 → 谁重建都不会弄坏别人。
#
# 用**弱引用**登记：没人再用它时自动回收（sqlite 文件句柄也跟着放）。
_INDEX_REGISTRY = weakref.WeakValueDictionary()
_REGISTRY_LOCK = threading.Lock()


def _norm_dir(path: str | Path) -> str:
    """索引目录的规范化形式（同一个目录的不同写法 → 同一个键）。"""
    try:
        p = os.path.realpath(str(path))
    except Exception:  # noqa: BLE001 - 路径有问题也别炸
        p = str(path)
    return os.path.normcase(p)


def _registry_key(index_dir: str | Path, collection_name: str,
                  embed_model: str, rerank_model: str) -> tuple:
    return (_norm_dir(index_dir), str(collection_name),
            str(embed_model), str(rerank_model))


def same_index_dir(a: str | Path, b: str | Path) -> bool:
    """两个路径是不是**同一个**索引目录（规范化后比较）。"""
    return _norm_dir(a) == _norm_dir(b)


# ---------------------------------------------------------------------------
# VectorIndex
# ---------------------------------------------------------------------------


class VectorIndex:
    """一个角色一份的向量索引（Chroma 持久化目录）。

    用法：
        idx = VectorIndex(index_dir_for("data/memory_小汐.db"))
        idx.rebuild(rows)                   # 从 SQLite 的行重建
        hits = idx.query("我不能吃什么")      # → [(id, 余弦相似度), ...]
        scored = idx.rerank("我不能吃什么", [(id, text), ...])

    任何一步失败都只体现在 status() 里（available=False / error=...），
    方法本身返回空结果而不是抛异常 —— 调用方据此降级。
    """

    def __init__(self,
                 index_dir: str | Path,
                 *,
                 embed_model: str = EMBED_MODEL_ID,
                 rerank_model: str = RERANK_MODEL_ID,
                 collection_name: str = COLLECTION_NAME,
                 top_k: int = DEFAULT_TOP_K) -> None:
        self.index_dir = Path(index_dir)
        self.embed_model = embed_model
        self.rerank_model = rerank_model
        self.collection_name = collection_name
        self.top_k = int(top_k)
        self._client: Any = None
        self._collection: Any = None
        self._open_attempted = False
        self._open_error: Optional[str] = None
        self._state: dict = {}
        self._last_error: Optional[str] = None
        self._lock = threading.RLock()

    # ---------------- 进程级共享 ----------------

    @classmethod
    def for_dir(cls, index_dir: str | Path, *,
                embed_model: str = EMBED_MODEL_ID,
                rerank_model: str = RERANK_MODEL_ID,
                collection_name: str = COLLECTION_NAME,
                top_k: int = DEFAULT_TOP_K) -> "VectorIndex":
        """拿这个索引目录的**共享实例**（同目录 → 同一个对象）。

        为什么不是每次 new 一个：
            - Chroma 持久化客户端会一直占着索引目录里的 sqlite 文件；
            - delete_collection 会让别的句柄失效（别的实例随即 count()==0）。

        实例是**弱引用**登记的：没人用它时自动回收（sqlite 句柄也跟着放）。
        """
        key = _registry_key(index_dir, collection_name, embed_model,
                            rerank_model)
        with _REGISTRY_LOCK:
            inst = _INDEX_REGISTRY.get(key)
            if inst is not None:
                return inst
            inst = cls(index_dir, embed_model=embed_model,
                       rerank_model=rerank_model,
                       collection_name=collection_name, top_k=top_k)
            _INDEX_REGISTRY[key] = inst
            return inst

    # ---------------- 路径 / 状态 ----------------

    @property
    def state_path(self) -> Path:
        return self.index_dir / STATE_FILENAME

    def _read_state(self) -> dict:
        p = self.state_path
        if not p.is_file():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
        return data if isinstance(data, dict) else {}

    def _write_state(self, **fields) -> None:
        # 以**磁盘上**的状态为底（别的实例可能刚写过）→ 别把人家的字段抹掉
        base = self._read_state() or self._state
        state = dict(base)
        state.update(fields)
        state["version"] = STATE_VERSION
        state["updated_at"] = time.time()
        self._state = state
        try:
            self.index_dir.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as e:  # pragma: no cover - 磁盘问题
            log.warning("写索引状态文件失败：%s", e)

    # ---------------- 打开 ----------------

    def open(self) -> bool:
        """打开（必要时创建）Chroma 持久化客户端与 collection。失败返回 False。"""
        with self._lock:
            if self._open_attempted:
                if self._collection is not None:
                    return True
                if self.index_dir.is_dir():
                    # 上次失败是因为目录不存在？目录现在有了（显式重建）→ 再试一次
                    self._open_attempted = False
                    self._open_error = None
                else:
                    return False
            self._open_attempted = True
            if not self.index_dir.is_dir():
                self._open_error = "索引目录不存在（需要显式重建）"
                return False
            if not chroma_runtime_safe():
                # PyQt5 已经加载、而系统 MSVC 运行时没被提前预加载 →
                # chroma 的 Rust 绑定会跟 Qt 抢运行时 → 整进程崩。
                # 这里**直接不启用索引**（降级），宁可召回差一点也不能崩桌宠。
                self._open_error = (
                    "PyQt5 已加载且系统 MSVC 运行时未预加载，"
                    "chromadb 的原生扩展会崩（access violation）—— 本次不启用索引")
                log.warning("向量索引不可用：%s", self._open_error)
                return False
            try:
                import chromadb  # type: ignore
                from chromadb.config import Settings  # type: ignore
                settings = Settings(anonymized_telemetry=False)
                self._client = chromadb.PersistentClient(
                    path=str(self.index_dir), settings=settings)
                try:
                    self._collection = self._client.get_collection(
                        name=self.collection_name)
                except Exception:  # noqa: BLE001 - 还没有这个 collection
                    self._collection = self._client.get_or_create_collection(
                        name=self.collection_name,
                        metadata={"hnsw:space": "cosine"})
            except Exception as e:  # noqa: BLE001 - chroma 坏了 / 目录损坏
                self._open_error = "Chroma 打开失败：%s" % e
                log.warning("向量索引打开失败（降级）：%s", e)
                self._collection = None
                return False
            self._state = self._read_state()
            return True

    # ---------------- 模型 ----------------

    def ensure_embedder(self, blocking: bool = True,
                        timeout: Optional[float] = MODEL_LOAD_TIMEOUT) -> Any:
        """确保向量模型可用。blocking=False 时在**后台线程**加载（不等）。"""
        return _MODEL_CACHE.embedder(self.embed_model, blocking=blocking,
                                     timeout=timeout)

    def ensure_reranker(self, blocking: bool = True,
                        timeout: Optional[float] = MODEL_LOAD_TIMEOUT) -> Any:
        """确保重排模型可用。blocking=False 时在**后台线程**加载（不等）。"""
        return _MODEL_CACHE.reranker(self.rerank_model, blocking=blocking,
                                     timeout=timeout)

    @property
    def embedder_ready(self) -> bool:
        return _MODEL_CACHE.embedder(self.embed_model, blocking=False) is not None

    @property
    def reranker_ready(self) -> bool:
        return _MODEL_CACHE.reranker(self.rerank_model, blocking=False) is not None

    # ---------------- 嵌入 / 重排 ----------------

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """文本 → 归一化向量。模型不可用时抛 VectorIndexError（调用方降级）。"""
        model = self.ensure_embedder(blocking=True)
        if model is None:
            raise VectorIndexError(
                "向量模型不可用：%s" % (_MODEL_CACHE.embed_error or "未知原因"))
        vecs = model.encode(list(texts), normalize_embeddings=True,
                            show_progress_bar=False)
        return [list(map(float, v)) for v in vecs]

    def rerank(self, query: str,
               candidates: Sequence[tuple[str, str]]) -> list[tuple[str, float]]:
        """cross-encoder 重排：一次**批量** predict（不是逐对调用）。

        candidates: [(id, 正文), ...] → [(id, 相关分), ...]（按输入顺序返回）。
        模型不可用 / 推理失败 → 返回 []（调用方退回融合排序）。
        """
        if not candidates:
            return []
        model = self.ensure_reranker(blocking=False)
        if model is None:
            return []
        pairs = [(query, text) for _, text in candidates]
        try:
            scores = model.predict(pairs, batch_size=BATCH_SIZE,
                                   show_progress_bar=False)
        except Exception as e:  # noqa: BLE001
            log.warning("重排失败（退回融合排序）：%s", e)
            self._last_error = "重排失败：%s" % e
            return []
        return [(cid, float(s)) for (cid, _), s in zip(candidates, scores)]

    # ---------------- 索引维护 ----------------

    @staticmethod
    def _meta(row: dict) -> dict:
        """Chroma 元数据（只能是标量 → 全部转成字符串）。"""
        return {
            "chash": row_hash(row),
            "category": str(row.get("category") or "other"),
            "scope": str(row.get("scope") or "project"),
            "kind": str(row.get("kind") or "fact"),
            "status": str(row.get("status") or "active"),
            "topic": str(row.get("topic") or ""),
        }

    def _upsert_rows(self, rows: Sequence[dict]) -> int:
        if not rows:
            return 0
        vecs = self.embed([str(r.get("content") or "") for r in rows])
        self._collection.upsert(
            ids=[str(r.get("id")) for r in rows],
            embeddings=vecs,
            documents=[str(r.get("content") or "") for r in rows],
            metadatas=[self._meta(r) for r in rows],
        )
        return len(rows)

    def rebuild(self, rows: Sequence[dict], blocking: bool = True) -> dict:
        """**重建**索引（幂等：重建两次结果一样）。

        这是索引的**唯一**创建方式 —— store 不会自动建目录，
        所以测试跑完 data/ 仍然是字节一致的（见 A9）。

        ⚠️ 重建**绝不删 collection**：
            delete_collection() 会让**别的实例**手里的句柄全部失效 ——
            它们随即 count()==0，索引在别人眼里「凭空消失」（这正是
            「reindex_live 重建完，原 store 的 count 变成 0」的根因）。
            重建 = **全量 upsert + 删掉源里已经没有的 id**：
            collection 的身份不变 → 别的句柄照常有效；
            结果仍然等于「从零重建」（多余的 id 会被删掉）；仍然幂等。
        """
        report = {"ok": False, "action": "rebuild", "count": 0, "error": None}
        try:
            self.index_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            report["error"] = "建索引目录失败：%s" % e
            return report
        if not self.open():
            report["error"] = self._open_error or "索引打不开"
            return report
        if not self.ensure_embedder(blocking=blocking):
            report["error"] = ("向量模型不可用：%s"
                               % (_MODEL_CACHE.embed_error or "未知原因"))
            return report
        # 重排模型也一起预热：显式重建是「管理员动作」，等它加载完，
        # 重建完的索引立刻就能用（否则第一轮召回还只有融合排序）。
        self.ensure_reranker(blocking=blocking)
        rows = list(rows)
        # 全量 upsert（已存在的 id 就地更新 → collection 身份不变）
        try:
            self._upsert_rows(rows)
        except VectorIndexError as e:
            report["error"] = str(e)
            return report
        except Exception as e:  # noqa: BLE001
            report["error"] = "写入索引失败：%s" % e
            return report
        # 源里已经没有的行 → 删掉（这才叫「从零重建」）
        source_ids = {str(r.get("id")) for r in rows}
        stale = [i for i in self._indexed_ids() if i not in source_ids]
        if stale:
            try:
                self._collection.delete(ids=stale)
            except Exception as e:  # noqa: BLE001
                report["error"] = "清理索引旧行失败：%s" % e
                return report
        self._write_state(count=len(rows), digest=digest_rows(rows),
                          embed_model=self.embed_model,
                          rerank_model=self.rerank_model, space="cosine")
        report.update(ok=True, count=len(rows))
        log.info("向量索引重建完成：%s（%d 行）", self.index_dir, len(rows))
        return report

    def upsert(self, rows: Sequence[dict]) -> dict:
        """增量写入若干行（不删别的行）。"""
        report = {"ok": False, "action": "upsert", "count": 0, "error": None}
        if not self.open():
            report["error"] = self._open_error or "索引打不开"
            return report
        rows = list(rows)
        try:
            report["count"] = self._upsert_rows(rows)
            report["ok"] = True
        except VectorIndexError as e:
            report["error"] = str(e)
        except Exception as e:  # noqa: BLE001
            report["error"] = "写入索引失败：%s" % e
        return report

    def delete(self, ids: Sequence[str]) -> dict:
        """删掉若干行。"""
        report = {"ok": False, "action": "delete", "count": 0, "error": None}
        if not self.open():
            report["error"] = self._open_error or "索引打不开"
            return report
        ids = [str(i) for i in ids]
        if not ids:
            report["ok"] = True
            return report
        try:
            self._collection.delete(ids=ids)
            report.update(ok=True, count=len(ids))
        except Exception as e:  # noqa: BLE001
            report["error"] = "删除失败：%s" % e
        return report

    def sync(self, rows: Sequence[dict], blocking: bool = False) -> dict:
        """把索引对齐到源数据（**漂移检测** + 增量修复）。

        源数据 = SQLite 的行（真相来源）。比对方式：
            1. 整库指纹（行数 + 内容/元数据哈希）与索引里存的对不上 → 漂移；
            2. 逐行比对（索引元数据里的 chash）→ 只补差的那几行。

        blocking=False 时，模型没就绪就返回 action="pending"（**不假装同步好了**），
        调用方据此把语义腿关掉 —— 索引**绝不静默过期**。
        """
        report = {"ok": True, "action": "in_sync", "drift": False,
                  "added": [], "updated": [], "removed": [], "error": None}
        if not self.open():
            report.update(ok=False, action="unavailable",
                          error=self._open_error or "索引打不开")
            return report
        rows = list(rows)
        source = {str(r.get("id")): r for r in rows}
        if self._handle_is_stale():
            # 别的进程重建过索引 → 老句柄指向的 collection 已经没了，换一个
            self._refresh_collection()
        indexed = self._indexed_chash()
        added = [i for i in source if i not in indexed]
        updated = [i for i in source
                   if i in indexed and indexed[i] != row_hash(source[i])]
        removed = [i for i in indexed if i not in source]
        src_digest = digest_rows(rows)
        # 摘要从**磁盘**现读（别的实例可能刚重建过 → 内存里的那份是旧的）
        drift = bool(added or updated or removed) \
            or self._read_state().get("digest") != src_digest
        report["drift"] = drift
        report["added"] = added
        report["updated"] = updated
        report["removed"] = removed
        if not drift:
            report["action"] = "in_sync"
            return report
        if added or updated:
            if not self.ensure_embedder(blocking=blocking):
                report.update(ok=False, action="pending",
                              error="向量模型尚未就绪（后台加载中），本轮不改索引")
                return report
            todo = [source[i] for i in added + updated]
            try:
                self._upsert_rows(todo)
            except Exception as e:  # noqa: BLE001
                report.update(ok=False, action="failed",
                              error="同步索引失败：%s" % e)
                return report
        if removed:
            try:
                self._collection.delete(ids=removed)
            except Exception as e:  # noqa: BLE001
                report.update(ok=False, action="failed",
                              error="删除索引行失败：%s" % e)
                return report
        self._write_state(count=len(rows), digest=src_digest)
        report["action"] = "synced"
        log.info("向量索引已同步：新增 %d / 更新 %d / 删除 %d",
                 len(added), len(updated), len(removed))
        return report

    def _indexed_ids(self) -> list[str]:
        """索引里现有的**所有 id**（重建时用来找「源里已经没有的行」）。"""
        try:
            res = self._collection.get(include=["metadatas"])
        except Exception as e:  # noqa: BLE001
            log.warning("读索引 id 失败：%s", e)
            return []
        return [str(i) for i in (res.get("ids") or [])]

    def _refresh_collection(self) -> bool:
        """句柄失效时（**别的进程**删了 collection 又重建）→ 重新拿一个句柄。

        同进程内不会发生（注册表保证同目录共用一个实例）；
        跨进程才会 —— 这时老句柄指向的 collection 已经不存在了。
        返回是否刷新成功。
        """
        with self._lock:
            if self._client is None:
                return False
            try:
                self._collection = self._client.get_collection(
                    name=self.collection_name)
                log.info("向量索引句柄失效 → 已重新获取 collection 句柄")
                return True
            except Exception as e:  # noqa: BLE001
                log.warning("向量索引句柄失效，重新获取失败：%s", e)
                return False

    def _handle_is_stale(self) -> bool:
        """句柄是不是已经失效（别的进程重建过索引）。"""
        if self._collection is None:
            return True
        try:
            self._collection.count()
            return False
        except Exception:  # noqa: BLE001
            return True

    def _indexed_chash(self) -> dict[str, str]:
        """索引里现有的行 → 指纹。"""
        out: dict[str, str] = {}
        try:
            res = self._collection.get(include=["metadatas"])
        except Exception as e:  # noqa: BLE001
            log.warning("读索引元数据失败：%s", e)
            return out
        ids = res.get("ids") or []
        metas = res.get("metadatas") or []
        for cid, meta in zip(ids, metas):
            out[str(cid)] = str((meta or {}).get("chash") or "")
        return out

    # ---------------- 查询 ----------------

    def query(self, query: str, k: Optional[int] = None,
              where: Optional[dict] = None) -> list[tuple[str, float]]:
        """语义检索：query → [(行 id, 余弦相似度), ...]（按相似度降序）。

        模型没就绪 → 返回 []（**不等**：绝不阻塞聊天路径）。
        """
        if not self.open():
            return []
        model = self.ensure_embedder(blocking=False)
        if model is None:
            return []
        try:
            vec = model.encode([query], normalize_embeddings=True,
                                show_progress_bar=False)[0]
        except Exception as e:  # noqa: BLE001
            log.warning("向量化查询失败：%s", e)
            self._last_error = "向量化查询失败：%s" % e
            return []
        n = max(1, int(k or self.top_k))
        try:
            res = self._collection.query(
                query_embeddings=[list(map(float, vec))], n_results=n,
                where=where, include=["distances"])
        except Exception as e:  # noqa: BLE001
            log.warning("Chroma 查询失败：%s", e)
            self._last_error = "Chroma 查询失败：%s" % e
            return []
        ids = (res.get("ids") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        space = str((getattr(self._collection, "metadata", None) or {})
                    .get("hnsw:space") or "cosine")
        out: list[tuple[str, float]] = []
        for cid, dist in zip(ids, dists):
            out.append((str(cid), _dist_to_sim(float(dist), space)))
        return out

    # ---------------- 状态 ----------------

    def count(self) -> int:
        if self._collection is None:
            return 0
        try:
            return int(self._collection.count())
        except Exception:  # noqa: BLE001
            return 0

    def probe(self) -> dict:
        """只读地看一眼索引状态（**不打开 chroma**，不写任何东西）。

        面板只想知道「索引建了没、建了多少、什么时候建的」时用它 ——
        不该为了看一眼状态就去打开一个持久化客户端。
        """
        state = self._read_state()
        n_indexed = int(state.get("count") or 0)
        built = bool(state.get("digest")) or n_indexed > 0
        return {
            "available": bool(built),
            "path": str(self.index_dir),
            "built": built,
            "count": state.get("count"),
            "digest": state.get("digest"),
            "updated_at": state.get("updated_at"),
            "embed_model": state.get("embed_model") or self.embed_model,
            "rerank_model": state.get("rerank_model") or self.rerank_model,
            "n_indexed": n_indexed,
            "error": None,
        }

    def status(self) -> dict:
        """索引状态（面板 / 测试用）。**不抛异常**，**不返回缓存**。

        ⚠️ 每次都**重读**状态文件 + **重新问** chroma 现在的行数：
           别的实例（memory_merge 的 reindex_live、面板的重建按钮、
           另一个 store）可能刚重建过索引 —— 缓存的 count / digest 全是旧的，
           面板会显示过期数字（这正是「state 与 n_indexed 打架」的来源）。
        """
        state = self._read_state()          # 每次重读（别的实例可能重建过）
        open_ok = self._collection is not None
        stored_count = int(state.get("count") or 0)
        live_count = None
        if open_ok:
            try:
                live_count = int(self._collection.count())
            except Exception as e:  # noqa: BLE001
                # 句柄失效（别的进程删了 collection 又重建）→ 别报 0 行，
                # 用**状态文件**里的行数兜底（那个才是最新的）。
                log.info("读索引行数失败（句柄可能失效）：%s", e)
                self._last_error = "读索引行数失败：%s" % e
        n_indexed = live_count if live_count is not None else stored_count
        built = bool(state.get("digest")) or n_indexed > 0
        embed_error = _MODEL_CACHE.embed_error
        rerank_error = _MODEL_CACHE.rerank_error
        return {
            # 「这里有索引吗」：建过（或者句柄开着）就算有
            "available": bool(open_ok or built),
            "path": str(self.index_dir),
            "collection": self.collection_name,
            "embed_model": self.embed_model,
            "rerank_model": self.rerank_model,
            "embed_ready": self.embedder_ready,
            "rerank_ready": self.reranker_ready,
            # 模型加载失败（硬错误）：索引用不了 → 面板要说「不可用」
            "embed_error": embed_error,
            "rerank_error": rerank_error,
            # chroma 打不开（硬错误）：跟「模型坏了」一样属于「不可用」
            "open_error": self._open_error,
            "n_indexed": n_indexed,
            "count": state.get("count"),
            "digest": state.get("digest"),
            "updated_at": state.get("updated_at"),
            "built": built,
            "error": (self._open_error or embed_error or rerank_error
                      or self._last_error),
        }
