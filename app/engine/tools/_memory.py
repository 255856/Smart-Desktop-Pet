"""长期记忆工具：remember_fact / recall_memory / forget_memory。

Stage 2.2：三件套都加 `scope` 参数（默认 project = 老行为不变）：
    - project：本项目记忆库（data/memory[_角色].db）
    - user：用户级记忆库（~/.desktop-pet/user_memory.db，跨角色共享）

Stage 2.3：category（**内容类型**）之外补 **kind（生命周期分类）**；
    描述从「关于主人的」放宽成「**你自己的承诺/立场也要记**」。
Stage 2.4：remember_fact 加 `supersedes`（按 id 改写旧记忆）；
    forget_memory 加 `memory_id`（按 id 精确删，不用先知道旧措辞）。
Stage 2.7：去重命中时回「已经记过了」，不再谎报「已记住」。
"""
from __future__ import annotations

import logging

from ._core import ToolRegistry
from ._decorator import tool

log = logging.getLogger(__name__)

#: scope 取值（与 app/brain/memory.py 的 SCOPE_CHOICES 一致）
SCOPE_ENUM = ["project", "user"]
SCOPE_DESC = ("存储作用域：project=本项目记忆库（默认，老行为不变）；"
              "user=用户级记忆库（~/.desktop-pet/user_memory.db，跨角色共享）")

#: Stage 2.3：kind（生命周期分类）取值 —— 与 app/brain/memory.py 的 KIND_CHOICES 一致
KIND_ENUM = ["activeTopic", "pendingThought", "memberImpression", "fact"]
KIND_DESC = ("生命周期分类：activeTopic=当下/进行中的话题；pendingThought=还没定下来的念头；"
             "memberImpression=对某个人的印象/偏好；fact=确定的事实（默认，不给就按 category 映射）")


def _scope_store(memory, scope: str):
    """按 scope 取对应的记忆库（user scope → 用户级 db；默认 project = 原库）。"""
    sc = (scope or "project").strip().lower()
    if sc == "project":
        return memory
    getter = getattr(memory, "store_for_scope", None)
    if callable(getter):
        try:
            return getter(sc)
        except Exception as e:  # noqa: BLE001
            log.warning("取 %s scope 记忆库失败，回落 project：%s", sc, e)
            return memory
    log.warning("记忆库不支持 scope=%s（没有 store_for_scope），按 project 处理", sc)
    return memory


def _store_count(store):
    """记忆库当前条数（拿不到就 None）。"""
    counter = getattr(store, "count", None)
    if not callable(counter):
        return None
    try:
        return int(counter())
    except Exception:  # noqa: BLE001
        return None


def _is_duplicate(store, before_count) -> bool:
    """这次写入是不是「重复」（内容已存在，没新建行）。

    优先用 store.last_add_was_duplicate（MemoryStore 自己记的）；
    老 store 没这个标记 → 用写前 / 写后条数对比兜底。
    """
    flag = getattr(store, "last_add_was_duplicate", None)
    if flag is not None:
        return bool(flag)
    after = _store_count(store)
    if before_count is not None and after is not None:
        return after == before_count
    return False


def _add_with_scope(store, content, category, importance, scope, kind):
    """调 store.add —— 老 store 不认 scope/kind 时逐级降级（行为不变）。"""
    try:
        return store.add(content, category, importance=importance,
                         scope=scope, kind=kind)
    except TypeError:
        pass
    try:
        return store.add(content, category, importance=importance, scope=scope)
    except TypeError:
        pass
    return store.add(content, category, importance=importance)


def _supersede_memory(store, old_id: str, content: str, category: str,
                      importance, scope: str, kind: str) -> str:
    """按 id 改写旧记忆（Stage 2.4）。

    - store 支持 supersede → 旧条目标过时（status='superseded'），新条 active，
      **两条不并存**；
    - 老 store 没这个能力 → 退化成「新增 + 删旧」，至少不留两条矛盾的事实。
    """
    superseder = getattr(store, "supersede", None)
    if callable(superseder):
        new_item = superseder(old_id, content, category=category,
                               importance=importance, scope=scope, kind=kind)
        if new_item is None:
            return (f"错误：没有 id 为 {old_id} 的记忆，没能改写。"
                    f"请先用 recall_memory 查到旧记忆的 id 再改写。")
        if not getattr(store, "last_save_ok", True):
            return (f"错误：改写没能保存到磁盘（id={new_item.id}），"
                    f"桌宠重启后会丢失。请不要告诉主人「已改写」。")
        return (f"已改写（旧的 id={old_id} 已标记过时，"
                f"新条目 id={new_item.id} "
                f"[{new_item.category}★{new_item.importance:.1f}] {new_item.content}）")
    # 老 store：没有 supersede → 新增 + 删旧（别留两条互相矛盾的记忆）
    item = _add_with_scope(store, content, category, importance, scope, kind)
    remover = getattr(store, "remove", None)
    if callable(remover):
        remover(old_id)
    return f"已改写（旧的 id={old_id} 已删除，新条目 id={item.id} {item.content}）"


def register(reg: ToolRegistry, *, memory) -> None:
    """注册 memory 三件套。"""

    @tool(
        description=(
            "把重要信息存入长期记忆（偏好/事实/事件/技能/人物/共同经历等），以便以后引用。"
            "**不只记主人的事**：你自己的承诺、立场、想变成的样子也要记"
            "（只记别人的事 = 把自己的成长丢在会过期的短期记忆里）。"
            "category: preference/fact/event/skill/person/shared/other（内容类型）。"
            "kind: activeTopic/pendingThought/memberImpression/fact（生命周期分类）。"
            "importance 0-1：偏好/重要事实用 0.8+，琐事 0.3（改写旧记忆时不给就沿用旧条目的重要性）。"
            "scope: project（本项目库，默认）/ user（用户级库，跨角色共享）。"
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
        store = _scope_store(memory, scope)
        scope_norm = (scope or "project").strip().lower()
        kind_norm = (kind or "").strip()
        if kind_norm and kind_norm not in KIND_ENUM:
            kind_norm = ""
        old_id = (supersedes or "").strip()

        if old_id:
            # Stage 2.4：改写旧记忆（旧的标过时，两条不并存）
            # category / importance 用默认值 = 「没指定」→ 沿用旧条目的
            cat = "" if category == "other" else category
            imp = None if abs(float(importance) - 0.5) < 1e-9 else float(importance)
            return _supersede_memory(store, old_id, content, cat, imp,
                                     scope_norm, kind_norm)

        before_count = _store_count(store)
        item = _add_with_scope(store, content, category, importance,
                                scope_norm, kind_norm)
        # 落盘失败必须说出来。原实现只看 add() 有没有抛异常，而 MemoryStore._save()
        # 把写盘异常吞成 log.warning —— 磁盘只读/写满时桌宠照样对主人说
        # 「已记住」，主人重启一开桌宠就发现这条记忆根本没存上。
        if not getattr(store, "last_save_ok", True):
            return (f"错误：记忆已写入内存但**没能保存到磁盘**（"
                    f"[{item.category}★{item.importance:.1f}] {item.content}），"
                    f"桌宠重启后会丢失。请不要告诉主人「已记住」。")
        if _is_duplicate(store, before_count):
            # Stage 2.7：去重命中 → 如实说「已经记过了」，别谎报「已记住」
            # （原来这里返回的 id 从来没存进库，主人重启就没了）
            return (f"已经记过了（id={item.id} "
                    f"[{item.category}★{item.importance:.1f}] {item.content}）")
        return (f"已记住（id={item.id} "
                f"[{item.category}★{item.importance:.1f}] {item.content}）")

    @tool(
        description=(
            "按关键词（或语义）检索长期记忆。keyword 为空时按重要性返回。"
            "可指定 category 过滤。可指定 scope 决定查哪个库。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "关键词，为空则按重要性返回"},
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
        """按关键词（或留空按重要性）检索长期记忆。支持语义检索。"""
        store = _scope_store(memory, scope)
        try:
            found = store.search(keyword=keyword, category=category,
                                 limit=max(1, min(50, int(limit))))
        except (TypeError, ValueError):
            found = store.search(keyword=keyword, category=category, limit=10)
        if not found:
            return "没有找到相关记忆。"
        lines = [f"[{i.id}|{i.category}★{i.importance:.1f}] {i.content}"
                 for i in found]
        return f"找到 {len(found)} 条相关记忆（按相关度排序）：\n" + "\n".join(lines)

    @tool(
        description=(
            "删除记忆。可按 keyword（子串匹配，命中几条删几条）"
            "或 memory_id（精确删一条）—— 知道 id 就用 memory_id，"
            "不用先知道旧措辞。可指定 scope 决定删哪个库。"
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
        store = _scope_store(memory, scope)
        mid = (memory_id or "").strip()
        if mid:
            # Stage 2.4：按 id 精确删（模型不用先知道旧措辞）
            forgetter = getattr(store, "forget_by_id", None)
            if callable(forgetter):
                ok = forgetter(mid)
            else:
                getter = getattr(store, "get", None)
                remover = getattr(store, "remove", None)
                item = getter(mid) if callable(getter) else None
                ok = bool(item) and bool(remover and remover(mid))
            if not ok:
                return f"错误：没有 id 为 {mid} 的记忆，什么都没删掉。"
            return f"已删除 1 条记忆（id={mid}）。"
        kw = (keyword or "").strip()
        if not kw:
            return "错误：keyword 和 memory_id 至少给我一个。"
        removed = store.forget_by_keyword(kw)
        # 删了 0 条原来返回「已删除 0 条…」，不含任何失败词 → 被判 ok →
        # 模型会接着对主人说「已经帮你删掉了」，其实什么都没删。
        if not removed:
            return f"错误：没有匹配「{kw}」的记忆，什么都没删掉。"
        return f"已删除 {removed} 条匹配「{kw}」的记忆。"

    reg.register(remember_fact)
    reg.register(recall_memory)
    reg.register(forget_memory)


__all__ = ["register"]
