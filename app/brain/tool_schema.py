"""手写 JSON Schema → pydantic 模型（标准后端的工具契约对齐，Stage 0.1）。

背景
----
桌宠每个工具的参数是**手写 JSON Schema**（app/engine/tools/* 里
@tool(parameters=...) 给的那份 dict）。轻量后端把它原样发给模型，所以
「属性名 / 属性描述 / enum / min-max」都是作者写什么就发什么。

标准后端（LangChain）以前是 StructuredTool.from_function(func=tool.fn, ...)，
**从不传 args_schema** —— LangChain 于是从 fn 的 Python 签名反推 args_schema，
手写的那份直接被丢掉。实测后果（61 个内置工具）：

  - 84 个属性里 75 条属性描述全丢；
  - 11 处 min/max 边界 + 2 处 enum 全丢；
  - 4 个工具的属性集直接变了（run_script / generate_image / generate_video /
    generate_audio 多了 kwargs、少了手写属性）；
  - run_script 的 args 撞上 pydantic BaseTool.args，被改名成 v__args：
    invoke({"script_path": ..., "args": ...}) 报 unexpected keyword，
    invoke({"script_path": ..., "v__args": ...}) 报 expected list ——
    两种调用方式全废。

修法
----
把手写 JSON Schema 转成 pydantic 模型，当作 args_schema 传给 StructuredTool：
标准后端暴露的 schema 与手写 schema 一致，属性名也不再被 pydantic 改名
（我们自己建的模型里 args 就是 args）。

刻意不做的事
------------
- 不改任何工具的 parameters：手写 schema 是单源真相，标准后端向它看齐；
- 不用 Literal / Field(ge=..., le=...) 把 enum / 边界变成**运行时**强校验：
  手写 schema 里的 enum / min-max 是给模型看的提示（轻量后端也不校验），
  真正执行时各工具自己兜底（例如 run_script 自己 clamp timeout_s）。
  约束只出现在暴露给模型的 schema 里（走 Annotated metadata，见 _SchemaExtra）。
"""
from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Annotated, Any, Callable, Optional

from pydantic import BaseModel, Field, create_model

if TYPE_CHECKING:  # pragma: no cover - 仅供类型标注
    from app.engine.tools import Tool

log = logging.getLogger(__name__)

# JSON Schema 的 type → Python 注解类型
_JSON_TYPE_TO_PY: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
    "null": type(None),
}

# 手写 schema 里要原样带出去的键：
# enum / 边界 / 嵌套 items 等，暴露给模型的 schema 一个都不能少
_EXTRA_SCHEMA_KEYS = (
    "enum", "const",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength", "minItems", "maxItems",
    "pattern", "format", "examples",
    "items", "properties", "additionalProperties",
)

_MISSING = object()


class _SchemaExtra:
    """把 JSON Schema 的额外键（enum / minimum / maximum / items...）挂到字段上。

    为什么不直接用 Field(json_schema_extra=...)：LangChain 生成
    tool_call_schema（真正发给模型的那份 schema）时会**重建**每个字段，
    只搬 description / default / metadata —— json_schema_extra 被丢掉，
    metadata 不会。所以约束放进 metadata，模型那边也看得见。

    同时它**不做运行时校验**：手写 schema 里的 enum / min-max 是提示，
    轻量后端也不校验，真正执行时各工具自己兜底。
    """

    def __init__(self, extra: dict) -> None:
        self.extra = dict(extra)

    def __get_pydantic_json_schema__(self, schema: dict, handler) -> dict:
        schema = handler(schema)
        schema.update(self.extra)
        return schema


def _py_type_for(prop: dict) -> Any:
    """JSON Schema 属性 → Python 注解类型（数组按 items 递归）。"""
    jtype = prop.get("type")
    if isinstance(jtype, list):        # ["string", "null"] 这种联合写法
        jtype = next((t for t in jtype if t != "null"), None)
    base = _JSON_TYPE_TO_PY.get(jtype or "", Any)
    if base is list:
        items = prop.get("items")
        if isinstance(items, dict):
            item_type = _py_type_for(items)
            if item_type is not Any:
                return list[item_type]   # type: ignore[valid-type]
        return list
    return base


def _fn_defaults(fn: Optional[Callable]) -> dict[str, Any]:
    """fn 签名里的默认值（按参数名）。

    用途：手写 schema 里「非必填且没写 default」的属性，运行时总得有个值。
    取 fn 自己的默认值 = 跟轻量后端（只传模型给的参数、其余走 fn 默认）一致。
    """
    if fn is None:
        return {}
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return {}
    out: dict[str, Any] = {}
    for pname, p in sig.parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if p.default is not inspect.Parameter.empty:
            out[pname] = p.default
    return out


def _default_matches(default: Any, py_type: Any) -> bool:
    """默认值跟声明类型对不对得上（pydantic 默认不校验默认值，但类型错乱更难查）。"""
    if default is None:
        return True
    if py_type is str:
        return isinstance(default, str)
    if py_type is int:
        return isinstance(default, int) and not isinstance(default, bool)
    if py_type is float:
        return isinstance(default, (int, float)) and not isinstance(default, bool)
    if py_type is bool:
        return isinstance(default, bool)
    if py_type is list:
        return isinstance(default, list)
    if py_type is dict:
        return isinstance(default, dict)
    return True


def json_schema_to_model(
    name: str,
    schema: Optional[dict] = None,
    fn: Optional[Callable] = None,
) -> type[BaseModel]:
    """把手写 JSON Schema 转成 pydantic 模型（可直接当 StructuredTool.args_schema）。

    映射规则：
      - 属性名：原样（pydantic 不改名，run_script 的 args 还是 args）
      - 类型：string/integer/number/boolean/array/object → Python 类型
      - 描述：Field(description=...)
      - enum / min / max 等：Annotated metadata（_SchemaExtra）原样透传
      - 必填：手写 schema 的 required 列表说了算（fn 签名里有默认值也不算必填）
      - 非必填属性的默认值：手写 schema 的 default → fn 签名的默认值 → None
    """
    schema = schema or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    fn_def = _fn_defaults(fn)

    fields: dict[str, Any] = {}
    for pname, prop in props.items():
        prop = prop if isinstance(prop, dict) else {}
        py_type = _py_type_for(prop)
        kwargs: dict[str, Any] = {}
        if prop.get("description"):
            kwargs["description"] = prop["description"]
        extra = {k: prop[k] for k in _EXTRA_SCHEMA_KEYS if k in prop}
        if extra:
            # 走 Annotated metadata（不是 Field(json_schema_extra=...)）：
            # LangChain 重建 tool_call_schema 时只搬 metadata，enum / min-max
            # 才不会在「发给模型的那份 schema」里丢掉。
            py_type = Annotated[py_type, _SchemaExtra(extra)]
        if pname in required:
            # 手写 schema 说必填 → pydantic 也必填（哪怕 fn 签名里给了默认值）
            fields[pname] = (py_type, Field(**kwargs))
            continue
        default = prop.get("default", _MISSING)
        if default is _MISSING:
            default = fn_def.get(pname, None)
        if not _default_matches(default, py_type):
            default = None
        fields[pname] = (py_type, Field(default, **kwargs))

    try:
        return create_model(name or "ToolArgs", **fields)
    except Exception as e:  # noqa: BLE001
        # 极端情况（属性名不是合法标识符等）不能让整个 agent 建不起来：
        # 退化成「所有属性都宽松接受」的模型，工具仍可调用。
        log.warning("工具 %s 的 schema 转 pydantic 失败，退化为宽松模型: %s", name, e)
        loose = {p: (Any, None) for p in props}
        return create_model(f"{name or 'Tool'}Loose", **loose)


def tool_to_args_schema(tool: "Tool") -> type[BaseModel]:
    """Tool(手写 parameters) → StructuredTool.args_schema 用的 pydantic 模型。"""
    return json_schema_to_model(
        getattr(tool, "name", "") or "ToolArgs",
        getattr(tool, "parameters", None),
        fn=getattr(tool, "fn", None),
    )


__all__ = ["json_schema_to_model", "tool_to_args_schema"]
