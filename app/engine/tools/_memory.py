"""长期记忆工具：remember_fact / recall_memory / forget_memory。"""
from __future__ import annotations

from ._core import Tool, ToolRegistry


def register(reg: ToolRegistry, *, memory) -> None:
    """注册 memory 三件套。"""

    def remember_fact(content: str, category: str = "other",
                      importance: float = 0.5) -> str:
        """记一条事实。importance 0-1：用户偏好/重要事实用 0.8+，琐事 0.3。"""
        item = memory.add(content, category, importance=importance)
        return f"已记住（[{item.category}★{item.importance:.1f}] {item.content}）"

    def recall_memory(keyword: str = "", category: str = "",
                      limit: int = 10) -> str:
        """按关键词（或留空按重要性）检索长期记忆。支持语义检索。"""
        found = memory.search(keyword=keyword, category=category,
                              limit=max(1, min(50, limit)))
        if not found:
            return "没有找到相关记忆。"
        lines = [f"[{i.id}|{i.category}★{i.importance:.1f}] {i.content}"
                 for i in found]
        return f"找到 {len(found)} 条相关记忆（按相关度排序）：\n" + "\n".join(lines)

    def forget_memory(keyword: str) -> str:
        removed = memory.forget_by_keyword(keyword)
        return f"已删除 {removed} 条匹配「{keyword}」的记忆。"

    reg.register(Tool(
        name="remember_fact",
        description=(
            "把关于主人的重要信息存入长期记忆（偏好/事实/事件等），"
            "以便以后引用。category: preference/fact/event/skill/person/other。"
            "importance 0-1：偏好/重要事实用 0.8+，琐事 0.3。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要记住的内容，一句话"},
                "category": {"type": "string", "enum": list(
                    ("preference", "fact", "event", "skill", "person", "other")),
                    "description": "类别"},
                "importance": {"type": "number", "description": "重要性 0-1，默认 0.5"},
            },
            "required": ["content"],
        },
        fn=remember_fact,
    ))
    reg.register(Tool(
        name="recall_memory",
        description=(
            "按关键词（或语义）检索长期记忆。keyword 为空时按重要性返回。"
            "可指定 category 过滤。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "关键词，为空则按重要性返回"},
                "category": {"type": "string", "enum": list(
                    ("preference", "fact", "event", "skill", "person", "other")),
                    "description": "按类别过滤"},
                "limit": {"type": "integer", "description": "返回条数（默认 10）"},
            },
        },
        fn=recall_memory,
    ))
    reg.register(Tool(
        name="forget_memory",
        description="删除匹配关键词的记忆。",
        parameters={
            "type": "object",
            "properties": {"keyword": {"type": "string"}},
            "required": ["keyword"],
        },
        fn=forget_memory,
    ))


__all__ = ["register"]