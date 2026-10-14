"""`@tool` 装饰器 —— 把普通函数包装成 `Tool`(对齐 LangChain 1.0+ `@tool` 风格)。

用法:
    from app.engine.tools._decorator import tool

    @tool
    def get_current_time() -> str:
        \"\"\"获取当前日期和时间。\"\"\"
        return json.dumps({"now": now}, ensure_ascii=False)

也可以显式给 description 覆盖 docstring:
    @tool(description="扫雷游戏难度", parameters={"type": "object", ...})
    def play_minesweeper(difficulty: int) -> str:
        ...

支持类型:
- str / int / float / bool
- Optional[T] (= T | None)
- list[T] / dict[str, T]
- Path → string
- 不支持的类型:抛出 TypeError(让用户显式给 parameters=)

设计取舍:
- 不解析 docstring 风格(Google/NumPy/reST)—— 用 docstring 第一段作为 description,想
  精确就显式传 description=。这是「对外说什么」和「开发注释」的常见分离。
- 装饰后函数仍可被 ToolRegistry.register 接收(向后兼容老的 `Tool(...)` 注册)。
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union, get_args, get_origin

log = logging.getLogger(__name__)


# 类型映射：Python 类型 → JSON Schema 类型
# 简化：只覆盖 OpenAI tool schema 实际用得到的子集;不覆盖的用 string + warning 兜底
def _py_type_to_json_type(tp: Any) -> tuple[str, dict]:
    """把 Python 类型转成 (json_type, extra) 元组。extra 是 format/enum 等附加信息。"""
    origin = get_origin(tp)
    # Optional[T] = Union[T, None] —— OpenAI tool schema 没有 null 类型,用任意 T
    if origin is Union:
        non_none = [a for a in get_args(tp) if a is not type(None)]
        if len(non_none) == 1:
            return _py_type_to_json_type(non_none[0])
        if len(non_none) > 1:
            # 真正 Union:退化成 string + warning
            log.warning("不支持 Union 类型 %r,降级为 string", tp)
            return "string", {}
    if origin is list:
        (item_tp,) = get_args(tp) or (str,)
        item_type, _ = _py_type_to_json_type(item_tp)
        return "array", {"items": {"type": item_type}}
    if origin is dict:
        return "object", {"additionalProperties": True}
    if tp is str or tp is Path:
        return "string", {}
    if tp is int:
        return "integer", {}
    if tp is float:
        return "number", {}
    if tp is bool:
        return "boolean", {}
    if tp is type(None):
        return "null", {}
    # 不识别 —— 兜底 string
    log.warning("无法推断类型 %r,降级为 string", tp)
    return "string", {}


def _function_to_json_schema(fn: Callable) -> dict:
    """从函数的 type hints + 默认值生成 OpenAI tool 用的 JSON Schema。"""
    sig = inspect.signature(fn)
    hints = {}
    for name, p in sig.parameters.items():
        if p.annotation is inspect.Parameter.empty:
            hints[name] = (str, "")   # 无注解 → 兜底 string
        else:
            hints[name] = (p.annotation, p.default)
    properties: dict[str, dict] = {}
    required: list[str] = []
    for name, (ann, default) in hints.items():
        json_type, extra = _py_type_to_json_type(ann)
        prop: dict[str, Any] = {"type": json_type}
        prop.update(extra)
        # 默认值 → enum 限制（OpenAI tool schema 用 enum 表达"只接受这几个值"）
        if default is not inspect.Parameter.empty and default is not None:
            # 只支持基本类型的默认值（str/int/bool）;Path/list/dict 不进 enum
            if json_type in ("string", "integer", "number", "boolean"):
                if isinstance(default, (str, int, float, bool)):
                    prop["default"] = default
        properties[name] = prop
        if default is inspect.Parameter.empty:
            required.append(name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _docstring_to_description(fn: Callable, fallback: str = "") -> str:
    """取 docstring 第一段非空行作为 description。"""
    doc = inspect.getdoc(fn)
    if not doc:
        return fallback
    for line in doc.strip().splitlines():
        s = line.strip()
        if s:
            return s
    return fallback


@dataclass
class _DecoratedTool:
    """`@tool` 包装后的对象,属性与 `Tool` 一致,ToolRegistry.register 直接用。"""
    name: str
    description: str
    parameters: dict
    fn: Callable[..., str]
    # 仅装饰器内部用:被装饰的原函数(供 `_internal_tool_to_langchain` 复用)
    raw_fn: Callable = field(repr=False)


def tool(
    fn: Optional[Callable] = None,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    parameters: Optional[dict] = None,
) -> Any:
    """`@tool` 装饰器:无参(只写 `@tool`)或带参(`@tool(name=..., description=...)`)。

    行为:
    - name: 默认用 `fn.__name__`(snake_case,与 OpenAI tool convention 一致)
    - description: 优先显式传入,否则取 docstring 第一段,否则空字符串
    - parameters: 显式传入则跳过自动推断(给 JSON schema 手写控制权);
                  否则从 type hints + defaults 推
    - 返回值: 仍然 callable(直接 `tool_fn(**args)` 也能调),同时暴露
              .name / .description / .parameters / .fn 四个属性,
              方便 ToolRegistry.register 统一处理
    """
    def _wrap(target_fn: Callable) -> _DecoratedTool:
        tool_name = name or target_fn.__name__
        tool_desc = description if description is not None else _docstring_to_description(target_fn)
        if parameters is not None:
            tool_params = parameters
        else:
            try:
                tool_params = _function_to_json_schema(target_fn)
            except Exception as e:  # noqa: BLE001
                raise TypeError(
                    f"@tool 自动推断 {target_fn.__name__} 的 JSON schema 失败：{e}。"
                    "请显式传 parameters={{\"type\": \"object\", ...}}"
                ) from e
        wrapper = _DecoratedTool(
            name=tool_name,
            description=tool_desc,
            parameters=tool_params,
            fn=target_fn,
            raw_fn=target_fn,
        )
        # 让 wrapper 本身也可被调用(返回字符串结果)
        def _callable(*args, **kwargs):
            return target_fn(*args, **kwargs)
        # 把属性挂上,让 isinstance / 访问 .name 都 work
        _callable.name = tool_name
        _callable.description = tool_desc
        _callable.parameters = tool_params
        _callable.fn = target_fn
        _callable.raw_fn = target_fn
        _callable.__wrapped__ = target_fn
        return _callable
    if fn is not None:
        # `@tool` 无参用法,直接装饰
        return _wrap(fn)
    # `@tool(...)` 带参用法,返回 decorator
    return _wrap


__all__ = ["tool", "_DecoratedTool", "_function_to_json_schema"]