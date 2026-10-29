"""长期记忆工具：remember_fact / recall_memory / forget_memory。

后端**只有一个**：记忆 sidecar（独立进程，app/brain/memory_client.py 的 HTTP
客户端）。进程内的老记忆库（SQLite + 向量索引）已经整体删掉了 ——
以前那些「老 store 没有这个能力就退化成别的写法」的分支也一并不存在了。

工具名 / 参数 schema / 对主人说的话都**不变**，变的只是后端。两张映射表是
唯一需要翻译的东西：

* 老 category（内容类型）→ sidecar 的 kind（生命周期分类，决定半衰期）
* 老 kind（生命周期分类）→ sidecar 的 kind
* 老 scope（project / user 两个库）→ 卡片的 key（同一张卡库里的分区）
"""
from __future__ import annotations

import logging

from app.brain.memory_client import (
    CATEGORY_TO_KIND as CATEGORY_TO_SIDECAR_KIND,
    DEFAULT_KIND,
    MemoryClient,
    category_to_kind,
)

from ._core import ToolRegistry
from ._decorator import tool

log = logging.getLogger(__name__)

#: scope 取值（project / user 两块分区，落在卡片的 key 上）
SCOPE_ENUM = ["project", "user"]
SCOPE_DESC = ("存储作用域：project=本项目记忆分区（默认）；"
              "user=用户级记忆分区（跨角色共享的同一批卡片里的 user 区）")

#: kind（生命周期分类）取值
KIND_ENUM = ["activeTopic", "pendingThought", "memberImpression", "fact"]
KIND_DESC = ("生命周期分类：activeTopic=当下/进行中的话题；pendingThought=还没定下来的念头；"
             "memberImpression=对某个人的印象/偏好；fact=确定的事实（默认，不给就按 category 映射）")

#: 老 kind（生命周期分类）→ sidecar kind
KIND_TO_SIDECAR_KIND = {
    "activeTopic": "topic",
    "pendingThought": "thought",
    "memberImpression": "impression",
    "fact": "fact",
}


def _scope_key(scope: str) -> str:
    """老 scope → 卡片的 key（保留「项目区 / 用户区」的分区语义）。"""
    return (scope or "project").strip().lower() or "project"


def sidecar_kind(category: str, kind: str) -> str:
    """老的两维（category / kind）→ sidecar 的一个 kind。显式 kind 优先。"""
    k = (kind or "").strip()
    if k in KIND_TO_SIDECAR_KIND:
        return KIND_TO_SIDECAR_KIND[k]
    return category_to_kind(category)


#: 没注入后端时用的默认客户端（指向本机 sidecar，懒建）
_default_memory_client = None


def default_client() -> MemoryClient:
    """默认的 sidecar 客户端（懒建；指向 http://127.0.0.1:8788）。"""
    global _default_memory_client
    if _default_memory_client is None:
        _default_memory_client = MemoryClient()
    return _default_memory_client


def _as_client(memory) -> MemoryClient:
    """拿到 sidecar 客户端（注入 None → 默认客户端）。"""
    if memory is None:
        return default_client()
    return memory


def _client_error(client, what: str) -> str:
    """客户端失败时的统一说辞（**不是异常**：模型要能如实告诉主人）。"""
    return "错误：记忆服务不可用，%s（%s）。" % (what, client.last_error or "未知原因")


def _client_remember(client, content, category, importance, scope, kind,
                     supersedes) -> str:
    """remember_fact 的实现（sidecar 是唯一后端）。"""
    text = (content or "").strip()
    if not text:
        return "错误：要记的内容是空的，什么都没记。"
    key = _scope_key(scope)
    try:
        imp = max(0.0, min(1.0, float(importance)))
    except (TypeError, ValueError):
        imp = 0.5
    cat = (category or "").strip().lower()
    card = client.make_card(text, key=key, kind=sidecar_kind(category, kind),
                            importance=imp, category=cat)
    old_id = (supersedes or "").strip()
    if old_id:
        if not client.supersede(old_id, card):
            return _client_error(client, "没能改写旧记忆（旧 id=%s）" % old_id) \
                + " 请不要告诉主人「已改写」。"
        return ("已改写（旧的 id=%s 已标记过时，新条目 id=%s [%s★%.1f] %s）"
                % (old_id, card["id"], card["kind"], card["importance"], card["text"]))
    written = client.add(text, key=key, kind=card["kind"], importance=imp,
                         category=cat)
    if not written:
        return _client_error(client, "这条没能存进长期记忆") + \
            " 请不要告诉主人「已记住」。"
    return ("已记住（id=%s [%s★%.1f] %s）"
            % (written["id"], written["kind"], written["importance"], written["text"]))


def _client_recall(client, keyword, category, limit, scope) -> str:
    """recall_memory 的实现。"""
    kw = (keyword or "").strip()
    try:
        top_k = max(1, min(50, int(limit)))
    except (TypeError, ValueError):
        top_k = 10
    kind = ""
    if (category or "").strip():
        kind = CATEGORY_TO_SIDECAR_KIND.get(category.strip().lower(), "")
    if not kw:
        # sidecar 没有「按重要性列出」的接口 —— 只能报个概况，别假装查过了
        stats = client.stats()
        if not stats:
            return _client_error(client, "现在查不到记忆库概况")
        kinds = stats.get("byKind") or {}
        detail = "、".join("%s×%s" % (k, v) for k, v in sorted(kinds.items())) or "空"
        return ("记忆库里现在有 %d 条（%s）。记忆服务只支持按语句检索，"
                "给我一个关键词或一句话我再查。"
                % (int(stats.get("active") or 0), detail))
    rows = client.search(kw, top_k=top_k, kind=kind or None, key=_scope_key(scope))
    if not rows:
        if client.last_error:
            return _client_error(client, "现在查不到记忆")
        return "没有找到相关记忆。"
    lines = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            importance = float(row.get("importance") or 0.5)
        except (TypeError, ValueError):
            importance = 0.5
        lines.append("[%s|%s★%.1f] %s" % (row.get("id") or "", row.get("kind") or DEFAULT_KIND,
                                          importance, row.get("text") or ""))
    if not lines:
        return "没有找到相关记忆。"
    return "找到 %d 条相关记忆（按相关度排序）：\n" % len(lines) + "\n".join(lines)


def _client_forget(client, keyword, memory_id, scope) -> str:
    """forget_memory 的实现。"""
    mid = (memory_id or "").strip()
    if mid:
        if client.delete([mid]):
            return "已删除 1 条记忆（id=%s）。" % mid
        if client.last_error:
            return _client_error(client, "什么都没删掉")
        return "错误：没有 id 为 %s 的记忆，什么都没删掉。" % mid
    kw = (keyword or "").strip()
    if not kw:
        return "错误：keyword 和 memory_id 至少给我一个。"
    rows = client.search(kw, top_k=50, key=_scope_key(scope))
    if not rows and client.last_error:
        return _client_error(client, "什么都没删掉")
    # 老语义是「子串命中，命中几条删几条」：检索只用来**找候选**，删前再按子串筛一遍
    ids = [str(r.get("id")) for r in rows
           if isinstance(r, dict) and r.get("id") and kw in str(r.get("text") or "")]
    removed = client.delete(ids) if ids else 0
    if not removed:
        return "错误：没有匹配「%s」的记忆，什么都没删掉。" % kw
    return "已删除 %d 条匹配「%s」的记忆。" % (removed, kw)


def register(reg: ToolRegistry, *, memory=None) -> None:
    """注册 memory 三件套（memory = sidecar 客户端；不给就用默认客户端）。"""

    @tool(
        description=(
            "把重要信息存入长期记忆（偏好/事实/事件/技能/人物/共同经历等），以便以后引用。"
            "**不只记主人的事**：你自己的承诺、立场、想变成的样子也要记"
            "（只记别人的事 = 把自己的成长丢在会过期的短期记忆里）。"
            "category: preference/fact/event/skill/person/shared/other（内容类型）。"
            "kind: activeTopic/pendingThought/memberImpression/fact（生命周期分类）。"
            "importance 0-1：偏好/重要事实用 0.8+，琐事 0.3。"
            "scope: project（本项目区，默认）/ user（用户级区，跨角色共享）。"
            "supersedes: 旧记忆 id —— 事实变了就填旧 id 改写它，别新记一条造成互相矛盾。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要记住的内容，一句话"},
                "category": {"type": "string", "enum": list(
                    ("preference", "fact", "event", "skill", "person", "shared", "other")),
                    "description": "内容类型"},
                "kind": {"type": "string", "enum": list(KIND_ENUM),
                         "description": KIND_DESC},
                "importance": {"type": "number", "description": "重要性 0-1，默认 0.5"},
                "scope": {"type": "string", "enum": SCOPE_ENUM,
                          "description": SCOPE_DESC},
                "supersedes": {"type": "string", "description":
                               "要改写的旧记忆 id（recall_memory 的结果里带 id）。"
                               "填了就是「改写」：旧条目标记为过时，新条成为 active。"},
            },
            "required": ["content"],
        },
    )
    def remember_fact(content: str, category: str = "other",
                      importance: float = 0.5, scope: str = "project",
                      kind: str = "", supersedes: str = "") -> str:
        """记一条事实。importance 0-1：偏好/重要事实用 0.8+，琐事 0.3。"""
        return _client_remember(_as_client(memory), content, category, importance,
                                scope, kind, supersedes)

    @tool(
        description=(
            "按关键词（或语义）检索长期记忆。keyword 为空时只回记忆库概况（记忆服务只支持按语句检索）。"
            "可指定 category 过滤。可指定 scope 决定查哪个区。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "关键词，为空则只回记忆库概况"},
                "category": {"type": "string", "enum": list(
                    ("preference", "fact", "event", "skill", "person", "shared", "other")),
                    "description": "按类别过滤"},
                "limit": {"type": "integer", "description": "返回条数（默认 10）"},
                "scope": {"type": "string", "enum": SCOPE_ENUM,
                          "description": SCOPE_DESC},
            },
        },
    )
    def recall_memory(keyword: str = "", category: str = "",
                      limit: int = 10, scope: str = "project") -> str:
        """按关键词（或语义）检索长期记忆。支持语义检索。"""
        return _client_recall(_as_client(memory), keyword, category, limit, scope)

    @tool(
        description=(
            "删除记忆。可按 keyword（子串匹配，命中几条删几条）"
            "或 memory_id（精确删一条）—— 知道 id 就用 memory_id，"
            "不用先知道旧措辞。可指定 scope 决定删哪个区。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "关键词（子串匹配）"},
                "memory_id": {"type": "string",
                              "description": "记忆 id（精确删除，优先于 keyword）"},
                "scope": {"type": "string", "enum": SCOPE_ENUM,
                          "description": SCOPE_DESC},
            },
        },
    )
    def forget_memory(keyword: str = "", memory_id: str = "",
                      scope: str = "project") -> str:
        return _client_forget(_as_client(memory), keyword, memory_id, scope)

    reg.register(remember_fact)
    reg.register(recall_memory)
    reg.register(forget_memory)


__all__ = ["register", "sidecar_kind", "default_client",
           "CATEGORY_TO_SIDECAR_KIND", "KIND_TO_SIDECAR_KIND", "SCOPE_ENUM", "KIND_ENUM"]
