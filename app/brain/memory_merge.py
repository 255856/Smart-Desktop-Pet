# -*- coding: utf-8 -*-
"""legacy / live 双库合并的**唯一实现**（Stage 2.6 的 CLI 脚本 + Stage 5 的面板共用）。

为什么要有这个模块
------------------
Stage 2.6 写了 §scripts/merge_memory_libraries.py§（CLI），
Stage 5 又在 §app/web/dashboard.py§ 里写了一份（面板的
§/api/memory/merge-preview§ 与 §POST /api/maintenance/merge-memory§）。
两份实现**各写各的**，口径不一致：

* 相似度：CLI 复用记忆库自己的 §MemoryStore.detect_conflicts()§ / §_similarity()§；
  面板自己抄了一份分词（§_sim_tokens§）+ 阈值（0.35 / 0.15）—— 两边**按构造就会打架**。
* 面板那份还漏了三件事：
  1. 冲突行（keep_both）**根本没插进 live 库** —— 那条记忆永远召不回来；
  2. 不写 conflict 标记（§memories.topic='conflict'§ + §memory_conflicts§ 配对表），
     Stage 2.4 的 §supersede()§ 就没有东西可处理；
  3. legacy 库是**复制**一份留底，不是**移动**到 backups/ —— data/ 里永远剩两个记忆库。

这个模块把「判定 + 落库」抽出来，CLI 和面板**都调它** —— 全项目只有一份实现。

判定口径（四类）
----------------
============ ======================================================= ==========
类别          含义                                                  处理
============ ======================================================= ==========
duplicate     语义重复（同一个意思）                                保留 live 那条
conflict      同主题但**取值不同**                                  两条都保留 + 打
                                                                       conflict 标记
invalid       占位符 / 没填完的模板                                  丢弃（报告里
                                                                       明确说明）
unique        live 库里没有                                          并进 live
============ ======================================================= ==========

红线（PLAN 附录 A · 前提 7）
---------------------------
项目红线是「既有用户数据不动：不删除、不覆盖」，所以 legacy 库是**归档**
（**移动**到 <data-dir>/backups/memory_legacy-<时间戳>.db，字节不变、仍可打开），
不是删除。动手之前先备份，并校验备份可还原。
"""
from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    from app.brain import memory as memory_mod
except Exception as exc:  # pragma: no cover - 环境坏了才走
    raise ImportError("导入 app.brain.memory 失败：%s" % exc) from exc

MemoryStore = memory_mod.MemoryStore
MemoryItem = memory_mod.MemoryItem

#: 记忆库自己的 category → kind 映射（拿不到就用一份等价的）
_CATEGORY_TO_KIND = getattr(memory_mod, "CATEGORY_TO_KIND", None) or {
    "preference": "memberImpression",
    "person": "memberImpression",
    "event": "activeTopic",
    "shared": "activeTopic",
    "fact": "fact",
    "skill": "fact",
    "other": "fact",
}

#: 记忆库自己的 schema（拿不到就退回一份等价的最小 schema）
_MEMORY_SCHEMA = getattr(memory_mod, "_MEMORY_SCHEMA", None) or """
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
"""

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

#: 语义匹配阈值。记忆库自带的 detect_conflicts 默认 0.7，那是给 sbert 后端 +
#: 长文本调的；这里是 tfidf 后端 + 十几字的中文短句，实测同主题的两条落在
#: 0.39~0.44，不相关的落在 0.02~0.07，断崖非常清楚，所以用 0.30。
DEFAULT_SIMILARITY_THRESHOLD = 0.30

#: 分组名的规范化词典：**只影响显示名，不参与任何判定**。
#: 判定（重复 / 冲突 / 无效 / 独有）全部来自语义比对，跟这里无关。
GROUP_ALIASES = (
    ("生日", "生日"),
    ("开发", "创造者身份"),
    ("创造", "创造者身份"),
    ("共享", "共同记忆占位"),
)

#: 中文功能字（判定「取值是否一致」时剔除，避免虚词造成假冲突）
_FUNC_CHARS = set(
    "的了是在我你他她它们就都也不有没把被给对还要会能个之其所以但而且并且"
    "呢吗吧啊呀嗯哦这那很最更再又还只挺太非常么什怎样如何因为因此"
)

#: 数字（取值的一部分；记忆库的 _tokenize 不收数字，所以单独抽）
_NUM_RE = re.compile(r"\d+")

#: 拉丁词（代号 / 缩写 / 英文名，取值的一部分）
_LATIN_RE = re.compile(r"[A-Za-z]+")

#: CJK 连续片段
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

#: 没填完的模板槽位：独立的 X / xx / XXX / ? / ?? / _ / N / TBD / TODO / 待填
_SLOT_RE = re.compile(
    r"(?:^|[\s:：,，(（])(?:[XxＸ]{1,3}|[?？]{1,2}|_{1,3}|N|TBD|TODO|待填|占位)"
    r"(?=$|[\s,，。;；)）])"
)

#: 「模板标签 + 冒号 + 空值」：例如 "共享记忆:"
_EMPTY_TEMPLATE_RE = re.compile(r"^[^:：\n]{1,16}[:：]\s*$")

#: 冲突标记表（live 库内新增的旁路表；不改 memories 表的结构）
CONFLICT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS memory_conflicts (
    legacy_id      TEXT NOT NULL,
    live_id        TEXT NOT NULL,
    legacy_content TEXT NOT NULL,
    live_content   TEXT NOT NULL,
    similarity     REAL NOT NULL DEFAULT 0,
    group_label    TEXT,
    detected_at    REAL NOT NULL,
    status         TEXT NOT NULL DEFAULT 'open',
    PRIMARY KEY (legacy_id, live_id)
);
"""

#: 关系（判定结果）的中文标签
RELATION_LABEL = {
    "duplicate": "语义重复 duplicate",
    "conflict": "取值冲突 conflict",
    "unique": "仅 legacy 独有 unique",
    "invalid": "无效占位 invalid",
}

#: 动作（处理方式）的中文标签
ACTION_LABEL = {
    "keep_live": "保留 live 那条，legacy 那条丢弃",
    "keep_both": "两条都保留，并打 conflict 标记",
    "insert": "并进 live 库",
    "discard": "丢弃（内容无效）",
}


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def sha256(path: Path) -> str:
    """文件的 sha256（用于「备份可还原」「归档字节一致」的证明）。"""
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now() -> float:
    return time.time()


def stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _cjk_ngrams(text: str, n: int) -> list:
    """文本里所有长度 n 的 CJK n-gram（按出现顺序）。"""
    out: list = []
    for chunk in _CJK_RE.findall(text or ""):
        for i in range(len(chunk) - n + 1):
            out.append(chunk[i:i + n])
    return out


def _strip_func(text: str) -> str:
    """去掉首尾的功能字（中间的不动）。"""
    s = text or ""
    start, end = 0, len(s)
    while start < end and s[start] in _FUNC_CHARS:
        start += 1
    while end > start and s[end - 1] in _FUNC_CHARS:
        end -= 1
    return s[start:end]


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------

@dataclass
class Row:
    """记忆库的一行（跨 schema 兼容：缺的列用默认值）。"""
    id: str
    content: str
    category: str = "other"
    importance: float = 0.5
    created_at: float = 0.0
    last_access_ts: float = 0.0
    access_count: int = 0
    scope: str = "project"
    kind: str = "fact"
    topic: Optional[str] = None
    status: str = "active"


@dataclass
class Group:
    """一个语义分组：一条 legacy 行 +（可能）它对应的 live 行。"""
    label: str = ""
    keyword: str = ""
    legacy: Optional[Row] = None
    live: Optional[Row] = None
    relation: str = "unique"        # duplicate / conflict / unique / invalid
    action: str = "insert"          # keep_live / keep_both / insert / discard
    similarity: float = 0.0
    note: str = ""
    detail: str = ""


# --------------------------------------------------------------------------
# 读库（只读）
# --------------------------------------------------------------------------

def read_rows(path: Path) -> list:
    """只读打开记忆库，把 memories 表读成 Row 列表。

    用 mode=ro URI 打开：读的时候**绝不会**建 WAL、改 schema、建备份文件。
    """
    path = Path(path)
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    rows: list = []
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)")]
        if not cols or "content" not in cols:
            return []
        want = [c for c in ("id", "content", "category", "importance",
                            "created_at", "last_access_ts", "access_count",
                            "scope", "kind", "topic", "status") if c in cols]
        sql = "SELECT " + ", ".join(want) + " FROM memories ORDER BY created_at"
        for raw in conn.execute(sql).fetchall():
            rec = dict(zip(want, raw))
            rows.append(row_from_dict(rec))
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return rows


def row_from_dict(rec: dict) -> Row:
    """dict → Row（面板那边读出来的是 dict，这里统一成同一份结构）。"""
    category = str(rec.get("category") or "other")
    try:
        importance = float(rec.get("importance") or 0.5)
    except (TypeError, ValueError):
        importance = 0.5
    try:
        created_at = float(rec.get("created_at") or 0.0)
    except (TypeError, ValueError):
        created_at = 0.0
    try:
        last_access_ts = float(rec.get("last_access_ts") or 0.0)
    except (TypeError, ValueError):
        last_access_ts = 0.0
    try:
        access_count = int(rec.get("access_count") or 0)
    except (TypeError, ValueError):
        access_count = 0
    return Row(
        id=str(rec.get("id") or ""),
        content=str(rec.get("content") or ""),
        category=category,
        importance=importance,
        created_at=created_at,
        last_access_ts=last_access_ts,
        access_count=access_count,
        scope=str(rec.get("scope") or "project"),
        # 老库没有 kind 列 → 按记忆库自己的映射从 category 推
        kind=str(rec.get("kind") or ""
                or _CATEGORY_TO_KIND.get(category, "fact")),
        topic=(rec.get("topic") or None),
        status=str(rec.get("status") or "active"),
    )


def rows_to_dicts(rows: list) -> list:
    """Row 列表 → 面板用的 dict 列表（形状跟面板原来读的字段一致）。"""
    out = []
    for row in rows:
        out.append({
            "id": row.id,
            "content": row.content,
            "category": row.category,
            "kind": row.kind,
            "scope": row.scope,
            "importance": row.importance,
            "created_at": row.created_at,
            "last_access_ts": row.last_access_ts,
            "access_count": row.access_count,
        })
    return out


# --------------------------------------------------------------------------
# 语义比对（复用记忆库自己的相似度 / 冲突检测）
# --------------------------------------------------------------------------

def build_union_store(tmpdir: Path, rows: list) -> MemoryStore:
    """把两库的行放进**同一个临时库**，建一个 MemoryStore。

    为什么要合起来：TF-IDF 的 IDF 统计来自语料。只拿 live 那两行当语料，
    词频权重会偏（每行都只出现一次），跨库比对的分数就没法互相比较。
    两库合并当语料，IDF 才覆盖双方，比较才是对称的。

    临时库放在系统临时目录里 —— 仓库里的 data/ 一个字节都不动。
    """
    db = Path(tmpdir) / "union.db"
    conn = sqlite3.connect(str(db))
    try:
        conn.executescript(_MEMORY_SCHEMA)
        ts = now()
        for row in rows:
            conn.execute(
                "INSERT OR REPLACE INTO memories"
                "(id, content, category, importance, created_at, "
                " last_access_ts, access_count, scope, kind, topic, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (row.id, row.content, row.category, row.importance,
                 row.created_at or ts, row.last_access_ts or ts,
                 row.access_count, row.scope, row.kind, row.topic, row.status),
            )
        conn.commit()
    finally:
        conn.close()
    return MemoryStore(db, backend_name="auto")


def _as_item(row: Row) -> MemoryItem:
    return MemoryItem(
        id=row.id, content=row.content, category=row.category,
        importance=row.importance, created_at=row.created_at,
        last_access_ts=row.last_access_ts, access_count=row.access_count,
        scope=row.scope, kind=row.kind, topic=row.topic, status=row.status,
    )


def is_placeholder(content: str) -> bool:
    """这条是不是「没填完的模板」（内容无效，应当丢弃）。

    判据（通用，不针对某一句话）：
      1. 里面有一个没填的槽位（独立的 X / ? / _ / TBD / 待填 ……）；
      2. 或者整条就是「标签 + 冒号 + 空值」。
    """
    text = (content or "").strip()
    if not text:
        return True
    if _SLOT_RE.search(text):
        return True
    if _EMPTY_TEMPLATE_RE.match(text):
        return True
    return False


def _value_markers(text: str) -> tuple:
    """取值标记：数字 + 拉丁词（名字 / 代号 / 英文）。"""
    nums = set(_NUM_RE.findall(text or ""))
    lats = set(w.lower() for w in _LATIN_RE.findall(text or ""))
    return nums, lats


def _residual_chars(text: str, other: str) -> list:
    """text 里有、other 里没有的「有意义的字符」（数字单独算，这里只留字/字母）。

    按出现顺序返回（报告里读起来顺）。
    """
    other_chars = set(other or "")
    out: list = []
    for ch in (text or ""):
        if ch in other_chars or ch in _FUNC_CHARS:
            continue
        if ch.isdigit():
            continue          # 数字由 _value_markers 负责
        # 中文去重（同一个字算一次），拉丁字母不去重（代号 cxxy 要原样显示）
        if not ch.isascii() and ch in out:
            continue
        out.append(ch)
    return out


def _extras(a: str, b: str) -> str:
    """a 相对 b 多出来的取值（数字 / 拉丁词 / 有意义的字），给人看的说明。"""
    an, al = _value_markers(a)
    bn, bl = _value_markers(b)
    parts: list = []
    nums = sorted(an - bn)
    if nums:
        parts.append("、".join(nums))
    lats = sorted(al - bl)
    if lats:
        parts.append("、".join(lats))
    # 拉丁词已经在 lats 里列过了，这里只补汉字部分（免得重复显示）
    chars = "".join(ch for ch in _residual_chars(a, b) if not ch.isascii())
    if chars:
        parts.append(chars)
    return " / ".join(parts) if parts else "无"


def values_agree(a: str, b: str, label_a: str = "a",
                 label_b: str = "b") -> tuple:
    """两条记忆的**取值**是不是同一个。

    返回 (是否一致, 说明, 哪边更详细)。

    规则（纯字面判定，不猜语义）：
      * 数字 / 拉丁词：一方是另一方的子集 → 兼容（live 更简略，不算矛盾）；
        谁都不包含谁 → 取值不同（冲突）。
      * 有意义的字符差集：只有**一方**有 → 另一方是它的子集（更简略）→ 兼容；
        **两边都有** → 各自带了不同的说法（名字 vs 代号）→ 取值不同。

    「哪边更详细」= a / b / same，报告里用来提醒人丢了哪些信息。
    """
    an, al = _value_markers(a)
    bn, bl = _value_markers(b)
    num_ok = (an <= bn) or (bn <= an)
    lat_ok = (al <= bl) or (bl <= al)
    ra = _residual_chars(a, b)
    rb = _residual_chars(b, a)
    if not num_ok or not lat_ok:
        return False, (f"取值不同：{label_a} 多了「{_extras(a, b)}」，"
                       f"{label_b} 多了「{_extras(b, a)}」"), "conflict"
    if ra and rb:
        return False, (f"取值不同：{label_a} 多了「{_extras(a, b)}」，"
                       f"{label_b} 多了「{_extras(b, a)}」"), "conflict"
    if ra or rb:
        richer = "a" if ra else "b"
        rich_label = label_a if richer == "a" else label_b
        thin_label = label_b if richer == "a" else label_a
        extra = _extras(a, b) if richer == "a" else _extras(b, a)
        return True, (f"取值兼容：{rich_label} 那条更详细（多「{extra}」），"
                      f"{thin_label} 那条更简略"), richer
    return True, "取值一致", "same"


def _group_keyword(a: str, b: str, corpus_freq) -> str:
    """两条记忆共同的话题词（自动发现，不写死任何一句话）。

    取两边最长的公共 CJK 短语；一样长时取「在整份语料里出现得更少」的
    （更具体的话题），再一样长时取更靠前的。
    """
    for n in range(6, 1, -1):
        ga = _cjk_ngrams(a, n)
        gb = set(_cjk_ngrams(b, n))
        cands = [_strip_func(g) for g in ga if g in gb]
        cands = [c for c in cands if c]
        if not cands:
            continue
        return sorted(cands, key=lambda c: (corpus_freq(c), a.find(c)))[0]
    return ""


def _own_keyword(text: str, corpus_freq) -> str:
    """没有配对的那条：自己身上最有特点的一段（够长、整份语料里只出现一次）。"""
    for n in range(6, 1, -1):
        cands = []
        for g in _cjk_ngrams(text, n):
            g = _strip_func(g)
            if len(g) == n and corpus_freq(g) <= 1:
                cands.append(g)
        if cands:
            return sorted(cands, key=lambda g: text.find(g))[0]
    # 兜底：取前 16 个字
    return (text or "")[:16]


def normalize_label(label: str) -> str:
    """把自动发现的话题词规范化成中文分组名（只影响显示名）。"""
    for key, canonical in GROUP_ALIASES:
        if key in (label or ""):
            return canonical
    return label


def known_group(a: str, b: str = "") -> str:
    """两条内容属于哪个「已知语义组」（生日 / 创造者身份 / 共同记忆占位）。

    只影响显示名，不参与任何判定 —— 判定全部来自语义比对。
    """
    pair = (a or "") + " " + (b or "")
    for key, canonical in GROUP_ALIASES:
        if key in pair:
            return canonical
    return ""


def _sort_rows(rows: list) -> list:
    """按 (创建时间, id) 排序 —— 判定结果与输入顺序**无关**。

    CLI 按 created_at 读，面板按别的顺序读；不统一的话两边可能因为
    「相似度并列时取先读到的那条」而给出不同结果。排序之后，
    同样的数据 → 同样的判定（CLI 与面板逐行一致）。
    """
    return sorted(rows, key=lambda r: (float(r.created_at or 0.0), str(r.id)))


def classify(live_rows: list, legacy_rows: list, store: MemoryStore,
             threshold: float) -> tuple:
    """把 legacy 每一行分类，并按语义分组。

    返回 (groups, stats)。
    """
    live_rows = _sort_rows(list(live_rows))
    legacy_rows = _sort_rows(list(legacy_rows))
    corpus = [r.content for r in (live_rows + legacy_rows)]

    def corpus_freq(phrase: str) -> int:
        return sum(1 for c in corpus if phrase in c)

    live_ids = {r.id for r in live_rows}
    live_items = {r.id: _as_item(r) for r in live_rows}

    groups: list = []
    matched_scores: list = []
    unmatched_scores: list = []

    for legacy in legacy_rows:
        item = _as_item(legacy)
        g = Group(legacy=legacy)

        # --- ① 无效占位符 -------------------------------------------------
        if is_placeholder(legacy.content):
            g.relation = "invalid"
            g.action = "discard"
            g.keyword = _own_keyword(legacy.content, corpus_freq)
            g.label = normalize_label(g.keyword) or "无效占位符"
            g.note = ("内容是没填完的模板（占位符），**丢弃**——"
                      "它不是一条真实记忆")
            g.detail = "占位符判定：命中未填槽位 / 空模板"
            groups.append(g)
            continue

        # --- ② 用记忆库自己的冲突检测找同主题的 live 行 -------------------
        best_live: Optional[Row] = None
        best_sim = 0.0
        try:
            cands = store.detect_conflicts(legacy.content, threshold=threshold)
        except Exception:  # pragma: no cover - 记忆库内部异常时不猜
            cands = []
        for cand_item, sim in cands:
            if cand_item.id not in live_ids:
                continue
            sim = float(sim)
            if sim > best_sim:
                best_sim = sim
                best_live = next((r for r in live_rows if r.id == cand_item.id), None)
        # detect_conflicts 内部按 backend.similarity 算，和 _similarity 同一个值；
        # 这里再用 _similarity 复核一次（口径统一到记忆库自己的实现）。
        if best_live is not None:
            try:
                best_sim = float(store._similarity(item, live_items[best_live.id]))
            except Exception:  # pragma: no cover
                pass
        if best_sim < threshold:
            best_live = None
            best_sim = 0.0

        if best_live is None:
            # --- ③ live 库里没有 → 并进 live --------------------------------
            g.relation = "unique"
            g.action = "insert"
            g.keyword = _own_keyword(legacy.content, corpus_freq)
            excerpt = legacy.content[:18] + ("…" if len(legacy.content) > 18 else "")
            g.label = normalize_label(excerpt)
            g.note = "live 库里没有对应记录 → **并进 live**"
            g.detail = "没有任何 live 行跟它同主题"
            groups.append(g)
            continue

        # --- ④ 同主题：再看取值是不是同一个 -------------------------------
        agree, why, richer = values_agree(legacy.content, best_live.content,
                                           "legacy", "live")
        g.live = best_live
        g.similarity = best_sim
        g.keyword = _group_keyword(legacy.content, best_live.content, corpus_freq)
        g.label = normalize_label(g.keyword) or g.keyword
        matched_scores.append(best_sim)
        if agree:
            g.relation = "duplicate"
            g.action = "keep_live"
            g.note = ("语义重复 → **保留 live 那条**，legacy 那条不并进去"
                      f"（{why}）")
            g.detail = why
            if richer == "a":
                g.detail += ("；⚠ live 那条更简略，legacy 那条多出来的信息"
                             "（数字 / 名字）没有并进去 —— 想留住就人工补到 live 那条")
        else:
            g.relation = "conflict"
            g.action = "keep_both"
            g.note = ("同主题但**取值不同** → **两条都保留**并打 conflict 标记，"
                      f"交给 2.4 的 supersede 处理（{why}）")
            g.detail = why
        groups.append(g)

    # 未匹配上的分数（用来展示「断崖」）
    for legacy in legacy_rows:
        item = _as_item(legacy)
        for live in live_rows:
            try:
                sim = float(store._similarity(item, live_items[live.id]))
            except Exception:  # pragma: no cover
                continue
            if sim < threshold:
                unmatched_scores.append(sim)

    stats = {
        "matched": sorted(matched_scores, reverse=True),
        "unmatched": sorted(unmatched_scores, reverse=True),
    }
    return groups, stats


def classify_rows(live_rows: list, legacy_rows: list,
                  threshold: float = DEFAULT_SIMILARITY_THRESHOLD) -> tuple:
    """判定（自带临时并集库）→ (groups, stats)。

    临时库建在系统临时目录里 —— 仓库里的 data/ 一个字节都不动。
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="merge-memory-"))
    try:
        store = build_union_store(tmpdir, list(live_rows) + list(legacy_rows))
        return classify(live_rows, legacy_rows, store, float(threshold))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def classify_paths(live_path: Path, legacy_path: Path,
                   threshold: float = DEFAULT_SIMILARITY_THRESHOLD) -> dict:
    """从两个库文件直接判定 → {"groups", "stats", "live_rows", "legacy_rows"}。"""
    live_rows = read_rows(Path(live_path))
    legacy_rows = read_rows(Path(legacy_path))
    groups, stats = classify_rows(live_rows, legacy_rows, threshold)
    return {"groups": groups, "stats": stats,
            "live_rows": live_rows, "legacy_rows": legacy_rows}


def classify_items(live_items: list, legacy_items: list,
                   threshold: float = DEFAULT_SIMILARITY_THRESHOLD) -> list:
    """面板那条路：读出来的 dict → 判定（**同一套口径**）。"""
    live_rows = [row_from_dict(r) for r in (live_items or [])]
    legacy_rows = [row_from_dict(r) for r in (legacy_items or [])]
    groups, _stats = classify_rows(live_rows, legacy_rows, threshold)
    return groups


def group_to_dict(g: Group) -> dict:
    """Group → 面板用的 dict（CLI 的 JSON 里也是这几个字段）。"""
    legacy_content = g.legacy.content if g.legacy else ""
    live_content = g.live.content if g.live else ""
    return {
        "legacy_id": g.legacy.id if g.legacy else None,
        "legacy_content": legacy_content or None,
        "live_id": g.live.id if g.live else None,
        "live_content": live_content or None,
        "similarity": round(float(g.similarity or 0.0), 3),
        "relation": g.relation,
        "action": g.action,
        "group": known_group(legacy_content, live_content),
        "label": g.label,
        "keyword": g.keyword,
        "note": g.note,
        "detail": g.detail,
    }


# --------------------------------------------------------------------------
# 写库（只在真正执行合并时走）
# --------------------------------------------------------------------------

def _ensure_topic_column(conn: sqlite3.Connection) -> bool:
    """live 库没有 topic 列就补一个（跟记忆库自己的迁移 DDL 完全一致）。

    只加列、不改已有数据；老库本来也只多一个 NULL 列。
    返回 True 表示这次是新增的列。
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
    if "topic" in cols:
        return False
    conn.execute("ALTER TABLE memories ADD COLUMN topic TEXT")
    return True


def mark_conflict(conn: sqlite3.Connection, group: Group) -> dict:
    """给一对冲突行打标记：memories.topic='conflict' + memory_conflicts 一行。"""
    assert group.legacy is not None and group.live is not None
    added_topic = _ensure_topic_column(conn)
    ids = [group.legacy.id, group.live.id]
    for mid in ids:
        row = conn.execute("SELECT topic FROM memories WHERE id=?", (mid,)).fetchone()
        cur = (row[0] if row else None) or ""
        parts = [p for p in str(cur).split("|") if p]
        if "conflict" not in parts:
            parts.append("conflict")
        conn.execute("UPDATE memories SET topic=? WHERE id=?", ("|".join(parts), mid))
    conn.execute(
        "INSERT OR REPLACE INTO memory_conflicts"
        "(legacy_id, live_id, legacy_content, live_content, similarity, "
        " group_label, detected_at, status) VALUES (?,?,?,?,?,?,?,'open')",
        (group.legacy.id, group.live.id, group.legacy.content,
         group.live.content, float(group.similarity), group.label, now()),
    )
    return {"ids": ids, "added_topic_column": added_topic}


def insert_row(conn: sqlite3.Connection, row: Row) -> Optional[str]:
    """把一条 legacy 行并进 live 库（保留它原来的 id，方便追溯 + 幂等）。

    live 库里已经有同样内容 → 跳过（不重复插）。
    """
    exist = conn.execute("SELECT id FROM memories WHERE content=?",
                         (row.content,)).fetchone()
    if exist is not None:
        return None
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
    ts = now()
    rec = {
        "id": row.id,
        "content": row.content,
        "category": row.category,
        "importance": row.importance,
        "created_at": row.created_at or ts,
        "last_access_ts": row.last_access_ts or ts,
        "access_count": 0,
        "scope": row.scope,
        "kind": row.kind,
        "topic": row.topic,
        "status": row.status,
    }
    usable = {k: v for k, v in rec.items() if k in cols}
    if "id" not in usable or "content" not in usable:
        return None
    # id 撞车（极低概率）→ 换一个
    if conn.execute("SELECT 1 FROM memories WHERE id=?", (usable["id"],)).fetchone():
        usable["id"] = "m" + hashlib.sha1(
            (row.content + str(ts)).encode("utf-8")).hexdigest()[:10]
    names = ", ".join(usable)
    marks = ", ".join("?" for _ in usable)
    conn.execute(f"INSERT OR IGNORE INTO memories ({names}) VALUES ({marks})",
                 tuple(usable.values()))
    return usable["id"]


def backup_databases(live: Path, legacy: Optional[Path], backup_dir: Path) -> dict:
    """动手之前先备份，并**校验备份可还原**（sha256 + 能打开 + 行数一致）。

    备份**只覆盖真正会被动的两个文件**（live / legacy 记忆库）——
    data/ 里还有 289MB 的 checkpoint 库，整包打 zip 又慢又占地方。
    """
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    result = {"dir": str(backup_dir), "files": [], "ok": True, "error": None}
    targets = [(Path(live), "memory_live")]
    if legacy is not None and Path(legacy).is_file():
        targets.append((Path(legacy), "memory_legacy"))
    for src, tag in targets:
        if not src.is_file():
            continue
        dst = backup_dir / f"{tag}-{src.name}"
        try:
            shutil.copy2(src, dst)
        except OSError as exc:
            result["ok"] = False
            result["error"] = f"备份 {src} 失败：{exc}"
            return result
        src_hash, dst_hash = sha256(src), sha256(dst)
        src_rows = len(read_rows(src))
        dst_rows = len(read_rows(dst))
        ok = (src_hash == dst_hash) and (src_rows == dst_rows)
        result["files"].append({
            "src": str(src), "dst": str(dst),
            "sha256": src_hash, "same_bytes": src_hash == dst_hash,
            "rows": src_rows, "backup_rows": dst_rows, "restorable": ok,
        })
        if not ok:
            result["ok"] = False
            result["error"] = f"备份 {dst} 校验不通过（还原后会不一样）"
            return result
    return result


def backup_databases_zip(live: Path, legacy: Optional[Path],
                         backup_dir: Path) -> dict:
    """面板用的备份：**只打包它会动的文件**（两个记忆库），不碰 data/ 里其它东西。

    面板原来的实现是把整个 data/ 打成 zip —— 里面混着 289MB 的
    §langchain_checkpoints.db§，每次维护操作都又慢又占地方。
    这里只打包 live / legacy 两个记忆库。
    """
    import zipfile
    backup_dir = Path(backup_dir)
    try:
        backup_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"ok": False, "dir": str(backup_dir), "archive": None,
                "bytes": 0, "packed": [], "skipped": [], "error": str(exc)}
    archive = backup_dir / f"memory-{stamp()}.zip"
    packed: list = []
    skipped: list = []
    targets = [Path(live)]
    if legacy is not None and Path(legacy).is_file():
        targets.append(Path(legacy))
    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for src in targets:
                if not src.is_file():
                    continue
                try:
                    zf.write(src, arcname=src.name)
                    packed.append({"name": src.name,
                                   "bytes": src.stat().st_size})
                except OSError as exc:
                    skipped.append({"name": src.name, "error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "dir": str(backup_dir), "archive": str(archive),
                "bytes": 0, "packed": packed, "skipped": skipped,
                "error": str(exc)}
    return {
        "ok": True,
        "dir": str(backup_dir),
        "archive": str(archive),
        "bytes": archive.stat().st_size if archive.exists() else 0,
        "packed": packed,
        "skipped": skipped,
        "error": None,
    }


def archive_legacy(legacy: Path, archive_dir: Path) -> dict:
    """把 legacy 库**移动**（归档，不是删除）到 archive_dir。

    字节必须一模一样、还得能打开 —— 归档不是删除（PLAN 附录 A · 前提 7）。
    """
    legacy = Path(legacy)
    archive_dir = Path(archive_dir)
    if not legacy.is_file():
        # 已经没有 legacy 库了（上一轮归档过了）→ 没什么可归档的，
        # **什么都没删**（幂等：跑第二遍是空操作）。
        return {"ok": True, "from": str(legacy), "to": None,
                "sha256": None, "same_bytes": None, "rows": 0,
                "readable": False, "archived": False, "error": None}
    legacy_hash_before = sha256(legacy)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive_path = archive_dir / f"memory_legacy-{stamp()}.db"
    try:
        shutil.move(str(legacy), str(archive_path))
    except OSError as exc:
        return {"ok": False, "from": str(legacy), "to": str(archive_path),
                "sha256": None, "same_bytes": False, "rows": 0,
                "readable": False, "error": f"归档 legacy 库失败：{exc}"}
    archive_hash = sha256(archive_path)
    archive_rows = len(read_rows(archive_path))
    same_bytes = (legacy_hash_before is not None
                  and archive_hash == legacy_hash_before)
    out = {
        "ok": same_bytes,
        "from": str(legacy),
        "to": str(archive_path),
        "sha256": archive_hash,
        "same_bytes": same_bytes,
        "rows": archive_rows,
        "readable": archive_rows > 0,
        "error": None if same_bytes else "归档文件跟原文件字节不一致，请人工核对",
    }
    return out


def reindex_live(live: Path, blocking: bool = False) -> dict:
    """合并后把 live 库的**向量索引**对齐（尽力而为，失败不影响合并结果）。

    为什么必须处理：合并是**裸 SQL** 写库（不经过 MemoryStore），
    写完索引就跟库对不上了 —— 那是「漂移」。两条防线：
        1. 这里主动重建一次（默认**后台线程**，不阻塞合并/面板响应；
           模型第一次下载可能很久，绝不能把 HTTP 请求挂在那儿）；
        2. 运行中的 MemoryStore 下次召回时会自己发现漂移并修复
           （app/brain/memory.py 的 _ensure_vector_index）。
    索引目录还没建过 → 跳过（索引只由显式重建创建）。

    ⚠️ 这里新建的 MemoryStore 跟运行中那个**共用同一个 VectorIndex 实例**
       （app/brain/vector_index.py 的进程级注册表：同目录 → 同一个 chroma
       客户端、同一个 collection 句柄）。所以：
        - 不会「客户端一直占着索引目录里的 sqlite 文件」（老注释的担心）；
        - 重建**不会**把运行中那个 store 的视图弄瞎
          （以前这里新开客户端 + 重建时删 collection → 它的 count 变 0）。
    """
    out: dict = {"ok": True, "skipped": False, "error": None, "count": 0,
                 "thread": None}
    try:
        from app.brain.vector_index import index_dir_for
    except Exception as exc:  # noqa: BLE001
        out.update(ok=False, error="向量索引模块不可用：%s" % exc)
        return out
    try:
        idx_dir = index_dir_for(Path(live))
    except Exception as exc:  # noqa: BLE001
        out.update(ok=False, error="算不出索引目录：%s" % exc)
        return out
    if not idx_dir.is_dir():
        out.update(skipped=True, reason="索引还没建过（不需要重建）")
        return out

    def _work() -> None:
        try:
            store = MemoryStore(Path(live))
            report = store.rebuild_vector_index(blocking=True)
            out.update(ok=bool(report.get("ok")),
                       error=report.get("error"),
                       count=report.get("count") or 0)
        except Exception as exc:  # noqa: BLE001
            out.update(ok=False, error="重建索引失败：%s" % exc)

    if blocking:
        _work()
    else:
        thread = threading.Thread(target=_work, daemon=True,
                                  name="MemoryMergeReindex")
        thread.start()
        out["thread"] = thread
    return out


def apply_merge(live: Path, legacy: Path, groups: list, archive_dir: Path,
                backup_dir: Path, backup: Optional[dict] = None) -> dict:
    """真正合并：备份 → 写 live（插入 + 标记冲突）→ 归档 legacy。

    backup 为空时自己先备份（CLI 那条路）；面板那条路把**已经打好并校验过**
    的备份传进来（面板的自动备份是 zip 形态），免得备份两遍。
    """
    live = Path(live)
    legacy = Path(legacy)
    out: dict = {"backup": None, "inserted": [], "skipped": [],
                 "marked": [], "archived": None, "ok": True, "error": None}

    # ① 先备份（不动数据也要先把保险打好）
    if backup is None:
        backup = backup_databases(live, legacy, backup_dir)
    out["backup"] = backup
    if not backup.get("ok"):
        out["ok"] = False
        out["error"] = backup.get("error") or "备份失败"
        return out

    # ② 写 live 库（一个事务，出错回滚）
    conn = sqlite3.connect(str(live), timeout=10)
    try:
        conn.execute("PRAGMA synchronous=NORMAL")
        # 冲突标记表先建好（幂等）—— 放在事务外，
        # 免得 executescript 的隐式提交把「一个事务」的保证打破。
        conn.executescript(CONFLICT_TABLE_DDL)
        conn.commit()
        with conn:
            for g in groups:
                if g.relation in ("unique", "conflict") and g.legacy is not None:
                    new_id = insert_row(conn, g.legacy)
                    if new_id:
                        out["inserted"].append({"id": new_id,
                                                "content": g.legacy.content,
                                                "group": g.label})
                    else:
                        out["skipped"].append({"id": g.legacy.id,
                                               "content": g.legacy.content,
                                               "why": "live 库里已经有同样内容"})
                if g.relation == "conflict" and g.live is not None:
                    mark = mark_conflict(conn, g)
                    out["marked"].append({
                        "group": g.label,
                        "ids": mark["ids"],
                        "similarity": round(g.similarity, 3),
                        "contents": [g.legacy.content, g.live.content],
                        "added_topic_column": mark["added_topic_column"],
                    })
    except Exception as exc:  # noqa: BLE001
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        out["ok"] = False
        out["error"] = f"写 live 库失败，已回滚：{exc}"
        return out
    finally:
        conn.close()

    # ③ 向量索引对齐（裸 SQL 写完库 → 索引漂移了）。
    #    后台线程做，失败也不影响合并结果（真相在 SQLite，索引可以再重建）。
    out["reindex"] = reindex_live(live, blocking=False)

    # ④ 归档 legacy（移动，不是删除；字节必须一模一样）
    out["archived"] = archive_legacy(legacy, archive_dir)
    if not out["archived"].get("ok"):
        out["ok"] = False
        out["error"] = out["archived"].get("error") or "归档 legacy 库失败"
        return out
    return out


# --------------------------------------------------------------------------
# 路径解析
# --------------------------------------------------------------------------

def _read_yaml_field(path: Path, key: str) -> str:
    """从 yaml 里粗读一个字段（只为了拿到角色名；不引入 yaml 依赖）。"""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    m = re.search(rf"^\s*{re.escape(key)}\s*:\s*([^\n#]+)", text, re.M)
    return m.group(1).strip().strip("\"'") if m else ""


def character_name(root: Path) -> str:
    """角色名（小汐）—— live 记忆库叫 data/memory_<角色名>.db。"""
    root = Path(root)
    cfg = root / "config.yaml"
    if not cfg.is_file():
        return ""
    name = _read_yaml_field(cfg, "name")
    if name:
        return name
    rel = _read_yaml_field(cfg, "character_file")
    if not rel:
        return ""
    p = Path(rel)
    if not p.is_absolute():
        p = root / p
    return _read_yaml_field(p, "name")


def resolve_paths(root: Path, data_dir: Path, live_arg: Optional[str],
                  legacy_arg: Optional[str]) -> tuple:
    """解析 live / legacy 库路径（跟面板 _memory_stores 一个思路）。"""
    root = Path(root)
    data_dir = Path(data_dir)
    legacy = Path(legacy_arg) if legacy_arg else (data_dir / "memory.db")
    if not legacy.is_absolute():
        legacy = (root / legacy)
    live: Optional[Path] = None
    if live_arg:
        p = Path(live_arg)
        live = p if p.is_absolute() else (root / p)
    else:
        char = character_name(root)
        if char:
            cand = data_dir / f"memory_{char}.db"
            if cand.is_file():
                live = cand
        if live is None:
            cands = sorted(p for p in data_dir.glob("memory_*.db")
                           if p.name != legacy.name)
            live = cands[0] if cands else None
        if live is None:
            live = legacy
    return live, legacy


# --------------------------------------------------------------------------
# 报告（CLI 的人看的报告；面板用的是同一份判定结果）
# --------------------------------------------------------------------------

def render_report(groups: list, stats: dict, live_path: Path, legacy_path: Path,
                  live_count: int, legacy_count: int, threshold: float,
                  mode: str, result: Optional[dict] = None) -> str:
    """人看的报告（中文）。"""
    lines: list = []
    bar = "=" * 78
    sub = "-" * 78
    lines.append(bar)
    lines.append("记忆库合并 · Stage 2.6 —— legacy → live")
    lines.append("模式：试运行（dry-run，默认；**什么都不会被修改**）"
                 if mode == "dry-run"
                 else "模式：**执行合并（--apply）**")
    lines.append(bar)
    lines.append(f"live 库    ：{live_path}   （{live_count} 行）")
    lines.append(f"legacy 库 ：{legacy_path}   （{legacy_count} 行）")
    lines.append(f"相似度阈值：{threshold:.2f}"
                 "（主题匹配判定线；用记忆库自己的 detect_conflicts + _similarity）")
    matched = stats.get("matched") or []
    unmatched = stats.get("unmatched") or []
    if matched:
        if unmatched:
            lines.append(f"实测断崖  ：同主题的两条最低 {min(matched):.3f}，"
                         f"不同主题的最高 {max(unmatched):.3f}")
        else:
            lines.append(f"实测断崖  ：同主题的两条最低 {min(matched):.3f}")
    lines.append("")
    lines.append(sub)
    lines.append(f"【自动发现的语义分组】共 {len(groups)} 组")
    lines.append(sub)
    for idx, g in enumerate(groups, 1):
        title = g.label or "（未命名）"
        lines.append(f"组 {idx}「{title}」  ——  {g.keyword or '（无主题词）'}")
        if g.legacy is not None:
            lines.append(f"    legacy  {g.legacy.id}  「{g.legacy.content}」")
        if g.live is not None:
            lines.append(f"      ↕ live  {g.live.id}  「{g.live.content}」"
                         f"   相似度 {g.similarity:.3f}")
        else:
            lines.append("      ↕ live  （没有对应的 live 行）")
        lines.append(f"    判定：{RELATION_LABEL.get(g.relation, g.relation)}"
                     f" —— {ACTION_LABEL.get(g.action, g.action)}")
        lines.append(f"    说明：{g.note}")
        if g.detail:
            lines.append(f"          {g.detail}")
        lines.append("")

    counts: dict = {}
    for g in groups:
        counts[g.action] = counts.get(g.action, 0) + 1

    lines.append(sub)
    lines.append("【汇总】")
    lines.append(sub)
    lines.append(f"  将并进 live     ：{counts.get('insert', 0)} 条")
    for g in groups:
        if g.action == "insert" and g.legacy is not None:
            lines.append(f"      + {g.legacy.content}")
    lines.append(f"  将保留 live 那条：{counts.get('keep_live', 0)} 条（语义重复，"
                 f"legacy 那条不并进去）")
    for g in groups:
        if g.action == "keep_live" and g.legacy is not None:
            lines.append(f"      · legacy「{g.legacy.content}」"
                         f" → 丢弃（live 已有同义那条）")
    lines.append(f"  将标记 conflict ：{counts.get('keep_both', 0)} 对"
                 f"（两条都保留 + 打标记，交给 2.4 的 supersede）")
    for g in groups:
        if g.action == "keep_both" and g.legacy is not None and g.live is not None:
            lines.append(f"      ! legacy「{g.legacy.content}」")
            lines.append(f"        ↕ live 「{g.live.content}」")
    lines.append(f"  将丢弃          ：{counts.get('discard', 0)} 条"
                 f"（内容无效的占位符）")
    for g in groups:
        if g.action == "discard" and g.legacy is not None:
            lines.append(f"      × {g.legacy.content}  ← 无效占位符，丢弃")
    if result is not None:
        lines.append("")
        lines.append(sub)
        lines.append("【执行结果】")
        lines.append(sub)
        backup = result.get("backup") or {}
        lines.append(f"  备份目录：{backup.get('dir')}")
        for f in backup.get("files", []):
            lines.append(f"      · {f['dst']}  sha256={f['sha256'][:16]}…  "
                         f"字节一致={f['same_bytes']}  行数={f['rows']}"
                         f"（备份行数 {f['backup_rows']}）"
                         f"  可还原={f['restorable']}")
        for item in result.get("inserted", []):
            lines.append(f"  已插入 live：{item['id']}  「{item['content']}」")
        for item in result.get("skipped", []):
            lines.append(f"  已跳过      ：{item['content']}（{item['why']}）")
        for item in result.get("marked", []):
            lines.append(f"  已打 conflict 标记：{item['group']}  "
                         f"ids={item['ids']}  相似度={item['similarity']}")
        arch = result.get("archived")
        if arch:
            lines.append(f"  已归档 legacy：{arch['from']}")
            lines.append(f"      → {arch['to']}")
            lines.append(f"      字节一致={arch['same_bytes']}  "
                         f"可读={arch['readable']}  行数={arch['rows']}")
    lines.append("")
    if mode == "dry-run":
        lines.append("试运行结束：**什么也没改**。确认无误后加 --apply 才会真正动手。")
    else:
        lines.append("合并结束。legacy 库是**归档**（移动到 backups/），没有删除。")
    return "\n".join(lines)
