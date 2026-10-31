# -*- coding: utf-8 -*-
"""黑话 / 口头禅学习回路（qq-bridge slang-learner.js 的桌宠版）。

同款契约（主人要求「同款」）
--------------------------
* 三种状态：candidate（候选）/ confirmed（已确认）/ rejected（已否决）；
* 一条词包含：content / meaning / usage / example / risk / sources / status /
  evidence / 时间戳（外加 id / source / count）；
* **容量上限 + 排序**：confirmed > candidate > rejected，再按出现次数，最后按最近更新；
* **隔离（quarantine）**：JSON 文件坏了就把原文另存成 .corrupt-<时间戳>，
  **继续往下走**（不当致命错误）；
* 两个工具：query_slang（查已收录词）/ submit_slang（提一个新词当候选）；
* **只有 confirmed 能用**：注入提示词的只有 confirmed 词条。

跟 qq-bridge 的差别
-------------------
* 存储落 data/slang.json（**不是**记忆 sidecar —— 这是**词表**，不是记忆卡）；
* 候选**只进候选**：模型没有「自己确认」的权力，确认由主人做（改 JSON 里的
  status），所以 submit_slang 永远不会产出 confirmed；
* 提示词块是**常量**（只在词库文件变了才变），不随每轮变化 —— 前缀缓存不会被打掉。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ._core import ToolRegistry
from ._decorator import tool

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# 常量（照 qq-bridge 的 slang-learner.js）
# --------------------------------------------------------------------------

#: 三种状态
STATUS_CANDIDATE = "candidate"
STATUS_CONFIRMED = "confirmed"
STATUS_REJECTED = "rejected"
SLANG_STATUSES = (STATUS_CANDIDATE, STATUS_CONFIRMED, STATUS_REJECTED)

#: 词库容量上限。为什么必须有：词库是**唯一**没有上限的存储 —— 三个写入点
#: （工具、主人手工编辑、复学）都只 push 不裁剪，字段长度也没上限。它只会涨。
SLANG_MAX_ENTRIES = 2000
#: 各字段长度上限（防止文件膨胀）
SLANG_FIELD_MAX = 300
SLANG_CONTENT_MAX = 50
EVIDENCE_TEXT_MAX = 80
EVIDENCE_MAX = 20
SOURCES_MAX = 10

#: 提示词里最多带多少条已确认词（qq-bridge 是 8）
SLANG_PROMPT_MAX = 8

#: 注入提示词的规则（**常量** —— 不随每轮变化）
SLANG_USAGE_RULE = "已收录的词按语境自然用；不确定别硬塞；未收录的不用。"

#: 提示词块的标题（跟 AI 味黑名单一个位置：system prompt 里的稳定段）
SLANG_PROMPT_HEADER = "【已收录黑话】"


# --------------------------------------------------------------------------
# 路径
# --------------------------------------------------------------------------

_lock = threading.RLock()
_library_path: Optional[Path] = None


def _app_root() -> Path:
    """项目根目录（源码运行时 = <root>/app/engine/tools/_slang.py 的上上级）。"""
    try:
        here = Path(__file__).resolve()
        if len(here.parents) >= 4:
            return here.parents[3]
    except Exception:  # noqa: BLE001
        pass
    return Path.cwd()


def library_path() -> Path:
    """黑话库文件路径（默认 data/slang.json）。"""
    if _library_path is not None:
        return Path(_library_path)
    return _app_root() / "data" / "slang.json"


def set_library_path(path) -> None:
    """改黑话库路径（测试 / 本机换位置用；传 None 恢复默认）。"""
    global _library_path
    _library_path = Path(path) if path else None
    invalidate_prompt_cache()


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

#: 控制字符（存进 JSON 没意义，还会让提示词变形）
_CTRL_RE = re.compile(r"[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F]")

#: 提示词里要转义的字符（防止词条内容把提示词结构撑开 / 注入指令）
_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}


def now_iso() -> str:
    """当前时间（ISO 8601，UTC）。"""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def create_id() -> str:
    """词条 id（时间戳 + 随机后缀）。"""
    return "%x-%s" % (int(time.time() * 1000), os.urandom(3).hex())


def _clip(value: Any, max_len: int) -> str:
    """去控制字符 + 去首尾空白 + 截断。"""
    text = _CTRL_RE.sub("", str(value if value is not None else "")).strip()
    return text[:max_len]


def _escape_prompt_text(value: Any) -> str:
    """转义后放进提示词（防 XML/HTML 标签与 prompt injection 污染）。"""
    text = _CTRL_RE.sub("", str(value if value is not None else ""))
    return "".join(_ESCAPES.get(ch, ch) for ch in text)


def _count_of(entry: Any) -> int:
    """词条的 count（脏数据一律当 0）。"""
    try:
        return max(0, int((entry or {}).get("count") or 0))
    except (TypeError, ValueError):
        return 0


def normalize_slang_entry(raw: Any) -> dict:
    """把任意 dict 规整成一条**合法**词条（字段齐、长度封顶、状态合法）。"""
    entry = raw if isinstance(raw, dict) else {}
    status = entry.get("status")
    if status not in SLANG_STATUSES:
        status = STATUS_CANDIDATE
    raw_sources = entry.get("sources")
    sources = ([_clip(s, SLANG_FIELD_MAX) for s in raw_sources]
               if isinstance(raw_sources, list) else [])
    sources = [s for s in sources if s][-SOURCES_MAX:]
    raw_evidence = entry.get("evidence")
    evidence = list(raw_evidence)[-EVIDENCE_MAX:] if isinstance(raw_evidence, list) else []
    cleaned_evidence = []
    for item in evidence:
        if isinstance(item, dict):
            cleaned_evidence.append(
                dict(item, text=_clip(item.get("text"), EVIDENCE_TEXT_MAX)))
        elif item is not None:
            cleaned_evidence.append({"text": _clip(item, EVIDENCE_TEXT_MAX)})
    return {
        "id": str(entry.get("id") or create_id()),
        "content": _clip(entry.get("content"), SLANG_CONTENT_MAX),
        "meaning": _clip(entry.get("meaning"), SLANG_FIELD_MAX),
        "usage": _clip(entry.get("usage"), SLANG_FIELD_MAX),
        "example": _clip(entry.get("example"), SLANG_FIELD_MAX),
        "risk": _clip(entry.get("risk"), SLANG_FIELD_MAX),
        "sources": sources,
        "status": status,
        "source": "manual" if entry.get("source") == "manual" else "ai",
        "count": _count_of(entry),
        "evidence": cleaned_evidence,
        "createdAt": str(entry.get("createdAt") or now_iso()),
        "updatedAt": str(entry.get("updatedAt") or now_iso()),
    }


def _status_rank(entry: Any) -> int:
    """状态排序权重：confirmed > candidate > rejected。"""
    status = (entry or {}).get("status")
    if status == STATUS_CONFIRMED:
        return 2
    if status == STATUS_CANDIDATE:
        return 1
    return 0


def cap_slang_entries(entries: Any,
                       max_entries: int = SLANG_MAX_ENTRIES) -> tuple:
    """按上限裁剪词库：confirmed > candidate > rejected，再按出现次数，最后按最近更新。

    返回 (保留的词条, 丢弃的条数)。
    """
    items = list(entries) if isinstance(entries, list) else []
    try:
        limit = max(1, int(max_entries))
    except (TypeError, ValueError):
        limit = SLANG_MAX_ENTRIES
    if len(items) <= limit:
        return items, 0
    ranked = sorted(
        items,
        key=lambda e: (_status_rank(e), _count_of(e),
                        str((e or {}).get("updatedAt") or "")),
        reverse=True,
    )
    kept = ranked[:limit]
    return kept, len(items) - len(kept)


# --------------------------------------------------------------------------
# 读写（含 quarantine）
# --------------------------------------------------------------------------

def quarantine_file(path, text: str) -> Optional[Path]:
    """把坏掉的原文另存成 <file>.corrupt-<时间戳>（存不了就当没存）。"""
    target = Path(str(path) + ".corrupt-%d" % int(time.time() * 1000))
    try:
        target.write_text(text, encoding="utf-8")
        return target
    except OSError as e:  # noqa: BLE001
        log.warning("黑话库隔离文件写入失败（%s）：%s", target, e)
        return None


def load_slang(path=None, *, on_corrupt=None) -> list:
    """读黑话库。

    三种情形必须分开：
      · **文件不存在**（首次运行）→ 空库；
      · **读失败**（权限 / 被占用）→ 抛错（绝不能当成空库，否则下一次保存
        会把原库覆盖掉 —— 学到的词条不可复现）；
      · **内容坏了**（JSON 解析失败 / 顶层不是数组）→ 把原文另存成
        .corrupt-<时间戳>，**继续往下走**（返回空库），不当致命错误。
    """
    p = Path(path) if path else library_path()
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as e:
        raise OSError("黑话库读取失败（%s）：%s"
                      % (getattr(e, "errno", "unknown"), e))
    if text.startswith("\ufeff"):
        text = text[1:]
    if not text.strip():
        return []
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError) as e:
        target = quarantine_file(p, text)
        message = ("黑话库 JSON 解析失败，原文已另存为 %s：%s"
                   % (target or "（隔离文件写入失败）", e))
        log.warning(message)
        if callable(on_corrupt):
            on_corrupt(message)
        return []
    if not isinstance(parsed, list):
        target = quarantine_file(p, text)
        message = ("黑话库顶层不是数组，原文已另存为 %s"
                   % (target or "（隔离文件写入失败）"))
        log.warning(message)
        if callable(on_corrupt):
            on_corrupt(message)
        return []
    out = []
    for item in parsed:
        entry = normalize_slang_entry(item)
        if entry["content"]:
            out.append(entry)
    return out


def save_slang(entries: Any, path=None, *,
               max_entries: int = SLANG_MAX_ENTRIES) -> tuple:
    """写词库（原子写：先写临时文件再改名，中途崩了也不会写坏半截）。"""
    p = Path(path) if path else library_path()
    normalized = [normalize_slang_entry(e) for e in (entries or [])
                  if isinstance(e, dict)]
    normalized = [e for e in normalized if e["content"]]
    capped, dropped = cap_slang_entries(normalized, max_entries)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise OSError("黑话库目录创建失败：%s" % e)
    tmp = p.with_name("%s.%d.%s.tmp" % (p.name, os.getpid(), os.urandom(4).hex()))
    try:
        tmp.write_text(json.dumps(capped, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(str(tmp), str(p))
    except OSError:
        try:
            tmp.unlink()
        except OSError:  # noqa: BLE001
            pass
        raise
    invalidate_prompt_cache()
    return capped, dropped


def create_slang_entry(*, content: str, meaning: str = "", usage: str = "",
                       example: str = "", risk: str = "", sources=None,
                       status: str = STATUS_CANDIDATE, source: str = "ai",
                       evidence=None, count: int = 1) -> dict:
    """新建一条词条（默认 candidate）。"""
    return normalize_slang_entry({
        "content": content,
        "meaning": meaning,
        "usage": usage,
        "example": example,
        "risk": risk,
        "sources": list(sources or []),
        "status": status,
        "source": source,
        "count": count,
        "evidence": list(evidence or []),
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
    })


def merge_evidence(current: Any, incoming: Any) -> list:
    """合并证据（按内容去重，最多留 EVIDENCE_MAX 条）。"""
    seen = set()
    merged = []
    for item in list(current or []) + list(incoming or []):
        if not isinstance(item, dict):
            continue
        try:
            key = json.dumps(item, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            key = repr(item)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged[-EVIDENCE_MAX:]


def upsert_slang_entry(entries: list, content: str,
                       patch: Optional[dict] = None) -> tuple:
    """按 content 写入/更新一条词条。

    返回 (词条列表, 命中的词条, 是否新建)。已存在的词条：count +1、证据合并、
    updatedAt 刷新。
    """
    patch = dict(patch or {})
    key = str(content or "").strip()
    if not key:
        return entries, None, False
    try:
        increment = max(0, int(patch.pop("countIncrement", 1)))
    except (TypeError, ValueError):
        increment = 1
    for i, existing in enumerate(entries):
        if (existing or {}).get("content") != key:
            continue
        merged = dict(existing)
        merged.update(patch)
        merged["content"] = key
        merged["count"] = _count_of(existing) + increment
        merged["evidence"] = merge_evidence(existing.get("evidence"),
                                            patch.get("evidence"))
        merged["updatedAt"] = now_iso()
        entries[i] = normalize_slang_entry(merged)
        return entries, entries[i], False
    entry = normalize_slang_entry({
        **create_slang_entry(content=key, count=increment),
        **patch,
        "content": key,
        "count": increment,
        "evidence": list(patch.get("evidence") or []),
    })
    entries.append(entry)
    return entries, entry, True


# --------------------------------------------------------------------------
# 提示词块（只有 confirmed 能进）
# --------------------------------------------------------------------------

def confirmed_entries(entries: Any) -> list:
    """已确认（confirmed）的词条 —— **只有这些能进提示词**。"""
    out = []
    for e in (entries or []):
        if not isinstance(e, dict):
            continue
        if e.get("status") != STATUS_CONFIRMED:
            continue
        if not (e.get("content") or "").strip():
            continue
        out.append(e)
    return out


def build_slang_context(entries: Any,
                        max_entries: int = SLANG_PROMPT_MAX) -> str:
    """已确认词条 → 提示词里的紧凑正文（没有已确认词时返回空串）。"""
    confirmed = [e for e in confirmed_entries(entries)
                 if (e.get("meaning") or "").strip()]
    confirmed.sort(key=lambda e: (_count_of(e), str(e.get("updatedAt") or "")),
                   reverse=True)
    try:
        limit = max(1, min(30, int(max_entries)))
    except (TypeError, ValueError):
        limit = SLANG_PROMPT_MAX
    lines = []
    for e in confirmed[:limit]:
        line = "- %s：%s" % (_escape_prompt_text(e.get("content")),
                             _escape_prompt_text(e.get("meaning")))
        if (e.get("usage") or "").strip():
            line += "（用法：%s）" % _escape_prompt_text(e.get("usage"))
        if (e.get("example") or "").strip():
            line += "（例：%s）" % _escape_prompt_text(e.get("example"))
        lines.append(line)
    return "\n".join(lines)


#: 提示词块缓存：路径 -> (文件戳, 文本)
_prompt_cache: dict = {}


def invalidate_prompt_cache(path=None) -> None:
    """清掉提示词块缓存（词库写盘后调）。"""
    if path is None:
        _prompt_cache.clear()
        return
    _prompt_cache.pop(str(Path(path)), None)


def confirmed_prompt_block(path=None) -> str:
    """已确认词条 → 提示词块（**常量字符串**）。

    内容只随词库文件变化（很少），不随每轮变化 ⇒ 放 system prompt 里
    不会打掉前缀缓存。词库坏了会自动隔离并降级成空块（绝不崩聊天）。
    """
    p = Path(path) if path else library_path()
    key = str(p)
    try:
        st = p.stat()
        stamp = (st.st_mtime_ns, st.st_size)
    except OSError:
        stamp = None
    cached = _prompt_cache.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        entries = load_slang(p)
    except OSError as e:
        log.warning("读黑话库失败，提示词里不带黑话表：%s", e)
        entries = []
    body = build_slang_context(entries)
    text = ("%s%s\n%s" % (SLANG_PROMPT_HEADER, SLANG_USAGE_RULE, body)
            if body else "")
    _prompt_cache[key] = (stamp, text)
    return text


# --------------------------------------------------------------------------
# 工具实现
# --------------------------------------------------------------------------

def _format_entry(entry: dict) -> str:
    """一条词条 → 一行人话。"""
    head = "- %s：%s" % (entry.get("content") or "", entry.get("meaning") or "")
    tail = []
    if (entry.get("usage") or "").strip():
        tail.append("用法：%s" % entry["usage"])
    if (entry.get("example") or "").strip():
        tail.append("例：%s" % entry["example"])
    if (entry.get("risk") or "").strip():
        tail.append("注意：%s" % entry["risk"])
    tail.append("出现 %d 次" % _count_of(entry))
    return head + "（" + "，".join(tail) + "）"


def _query_slang(keyword: str = "", limit: int = 10,
                 status: str = STATUS_CONFIRMED) -> str:
    """query_slang 的实现。"""
    try:
        entries = load_slang()
    except OSError as e:
        return "错误：黑话库读不出来（%s），没查成。" % e
    want = (status or STATUS_CONFIRMED).strip().lower()
    if want not in SLANG_STATUSES and want != "all":
        return "错误：status 只能是 confirmed / candidate / rejected / all。"
    if want == "all":
        pool = [e for e in entries if isinstance(e, dict)]
    else:
        pool = [e for e in entries
                if isinstance(e, dict) and e.get("status") == want]
    kw = (keyword or "").strip()
    if kw:
        pool = [e for e in pool
                if kw in (e.get("content") or "")
                or kw in (e.get("meaning") or "")
                or kw in (e.get("usage") or "")
                or kw in (e.get("example") or "")]
    try:
        top = max(1, min(50, int(limit)))
    except (TypeError, ValueError):
        top = 10
    pool.sort(key=lambda e: (_count_of(e), str(e.get("updatedAt") or "")),
              reverse=True)
    label = "已收录的" if want == "confirmed" else ""
    if not pool:
        if kw:
            return "没查到符合条件的%s词条。" % label
        return "词库里还没有%s词条。" % label
    shown = pool[:top]
    if want == "confirmed":
        head = "已收录 %d 条黑话：" % len(shown)
    else:
        head = "查到 %d 条词条（%s）：" % (len(shown), want)
    lines = [head]
    for e in shown:
        lines.append(_format_entry(e))
    if len(pool) > len(shown):
        lines.append("（还有 %d 条没显示，可以用 keyword 精确查）"
                     % (len(pool) - len(shown)))
    return "\n".join(lines)


def _submit_slang(content: str, meaning: str = "", usage: str = "",
                  example: str = "", risk: str = "", evidence: str = "") -> str:
    """submit_slang 的实现（只产出 candidate）。"""
    word = (content or "").strip()
    if not word:
        return "错误：content 是空的，没提交成。"
    with _lock:
        try:
            entries = load_slang()
        except OSError as e:
            return "错误：黑话库读不出来（%s），没提交成。" % e
        for e in entries:
            if (e or {}).get("content") == word:
                if e.get("status") == STATUS_CONFIRMED:
                    return ("「%s」已经是已收录（confirmed）的词了，不用再提交。"
                            % word)
                if e.get("status") == STATUS_REJECTED:
                    return ("「%s」之前被否决过（rejected），不再提交。" % word)
        ev = []
        if (evidence or "").strip():
            ev = [{"text": _clip(evidence, EVIDENCE_TEXT_MAX)}]
        entries, entry, created = upsert_slang_entry(
            entries, word,
            {"meaning": meaning, "usage": usage, "example": example,
             "risk": risk, "status": STATUS_CANDIDATE, "source": "ai",
             "evidence": ev},
        )
        try:
            save_slang(entries)
        except OSError as e:
            return "错误：黑话库写不进去（%s），没提交成。" % e
    if created:
        return ("已提交候选「%s」（%d 次）。候选要主人确认才算收录，"
                "确认前不要直接用这个词。" % (word, _count_of(entry)))
    return ("「%s」已在候选里（第 %d 次），等主人确认。" % (word, _count_of(entry)))


# --------------------------------------------------------------------------
# 注册
# --------------------------------------------------------------------------

def register(reg: ToolRegistry) -> None:
    """注册 query_slang / submit_slang 两个内置工具。"""

    @tool(
        description=(
            "查已收录的黑话/口头禅表（主人确认过的词）。"
            "**什么时候用**：主人用了某个词你不太确定意思、或者你想知道某个梗"
            "在主人那边到底什么意思 —— 先查这里，别自己瞎解释。"
            "keyword 留空 = 列出全部已收录词；给了 keyword = 按词条/含义模糊匹配。"
            "默认只返回 confirmed（已确认，可以直接用）的词条。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "keyword": {"type": "string",
                            "description": "要查的词或短语（留空 = 列出全部已收录词）"},
                "limit": {"type": "integer",
                          "description": "最多返回几条（默认 10，上限 50）"},
                "status": {"type": "string",
                           "enum": ["confirmed", "candidate", "rejected", "all"],
                           "description": "要查哪种状态的词条（默认 confirmed = 已收录）"},
            },
        },
    )
    def query_slang(keyword: str = "", limit: int = 10,
                    status: str = STATUS_CONFIRMED) -> str:
        """查已收录的黑话表（默认只返回 confirmed）。"""
        return _query_slang(keyword=keyword, limit=limit, status=status)

    @tool(
        description=(
            "提交一个新词当**候选**：主人反复在用、而你不知道含义的黑话/口头禅/网络用语。"
            "**该提交**：这个词在对话里反复出现（至少两次），而且你确实不懂或没把握。\n"
            "**不该提交**：打错的字、只出现一次的临时说法、人名、@、表情包、纯标点、"
            "含义本来就清楚的普通词 —— 那些不是黑话，提交了是噪音。\n"
            "提交后状态是 candidate（候选），要主人确认才算收录；"
            "**确认前不要直接用这个词**，也不许替主人把它说成 confirmed。"
            "meaning 你不确定就留空（空着更容易被确认），别硬编。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string",
                            "description": "词条本身（2~8 个字，别超过 50 个）"},
                "meaning": {"type": "string",
                            "description": "你判断的含义（不确定就留空）"},
                "usage": {"type": "string",
                          "description": "使用场景/语气（可选）"},
                "example": {"type": "string",
                            "description": "一句自然短例（可选）"},
                "risk": {"type": "string",
                         "description": "有没有敏感/慎用风险（可选，没有就留空）"},
                "evidence": {"type": "string",
                             "description": "主人的原话/语境（可选，作为证据留存）"},
            },
            "required": ["content"],
        },
    )
    def submit_slang(content: str, meaning: str = "", usage: str = "",
                     example: str = "", risk: str = "", evidence: str = "") -> str:
        """提交一个新词当候选（不会变成已收录）。"""
        return _submit_slang(content=content, meaning=meaning, usage=usage,
                             example=example, risk=risk, evidence=evidence)

    reg.register(query_slang)
    reg.register(submit_slang)


__all__ = [
    "register",
    "STATUS_CANDIDATE", "STATUS_CONFIRMED", "STATUS_REJECTED", "SLANG_STATUSES",
    "SLANG_MAX_ENTRIES", "SLANG_PROMPT_MAX", "SLANG_USAGE_RULE",
    "SLANG_PROMPT_HEADER",
    "library_path", "set_library_path",
    "now_iso", "create_id", "normalize_slang_entry", "cap_slang_entries",
    "load_slang", "save_slang", "create_slang_entry", "upsert_slang_entry",
    "merge_evidence", "quarantine_file",
    "confirmed_entries", "build_slang_context", "confirmed_prompt_block",
    "invalidate_prompt_cache",
]
