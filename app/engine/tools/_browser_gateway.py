# -*- coding: utf-8 -*-
"""browser —— 浏览器自动化的**网关工具**（playwright 的 25 个工具不进原生工具表）。

为什么要有网关：
  playwright 那 25 个工具的 schema ≈ 9.5k token（占全部工具 schema 的 41%），
  摆进 tools 字段每轮都要带（首轮 +9.5k，工具集一变缓存就失效）。
  所以把它们**从原生工具表里摘掉**，只留一个 browser 网关：模型想用浏览器时
  调 browser(action="browser_navigate", args={...})，网关按名字分发。

安全：
  * action 必须在 playwright 的工具名白名单里（不能拼任意名字）；
  * browser_run_code_unsafe（页面里跑任意 JS）**直接拒绝**；
  * 分发走的是注册表里的真工具 → 原来的白名单/风险/超时都还在；
  * 浏览器这组默认锁 Lv9（companion_config.yaml 的 mcp_levels.playwright）。
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from ._core import ToolRegistry
from ._decorator import tool

log = logging.getLogger(__name__)

#: 浏览器网关对应的 MCP server（工具名前缀）
SERVER = "playwright"

#: **不给**模型用的工具（在页面里执行任意 JS / 未审计的能力）
DENIED = {"browser_run_code_unsafe"}


def _action_names() -> list[str]:
    """当前注册表里这个 server 的工具名（去掉 mcp_<server>_ 前缀）。"""
    reg = _registry_ref[0]
    if reg is None:
        return []
    prefix = "mcp_%s_" % SERVER
    out = []
    for name in getattr(reg, "_tools", {}):
        if name.startswith(prefix):
            out.append(name[len(prefix):])
    return sorted(out)


#: 放注册表的引用（避免注册时序问题：MCP 工具是**之后**才进来的）
_registry_ref: list = [None]


def register(reg: ToolRegistry) -> None:
    """注册 browser 网关工具。"""
    _registry_ref[0] = reg

    @tool(
        description="开浏览器（用系统 Edge）。action 是要做的动作名，例如 "
                    "browser_navigate（打开网址，args 里给 url）/ "
                    "browser_click（点，args 里给 element 或 ref）/ "
                    "browser_type（在输入框打字，args 里给 text 和 element）/ "
                    "browser_snapshot（看当前页面有什么）/ "
                    "browser_take_screenshot（截图）/ browser_evaluate（执行 JS）/ "
                    "browser_press_key（按键）/ browser_go_back（后退）/ "
                    "browser_wait_for（等某东西出现）。action 写成 "
                    "browser_navigate 这种**不带 mcp 前缀**的名字。",
    )
    def browser(action: str, args: Optional[dict] = None) -> str:
        """按名字分发到浏览器工具。"""
        reg = _registry_ref[0]
        if reg is None:
            return "错误：浏览器网关还没接上（MCP server 没起来）。"
        act = str(action or "").strip()
        if act.startswith("mcp_%s_" % SERVER):
            act = act[len("mcp_%s_" % SERVER):]
        if act in DENIED:
            return ("错误：%s 不给用（可以在页面里执行任意 JS，风险太高）。" % act)
        full = "mcp_%s_%s" % (SERVER, act)
        tool_obj = getattr(reg, "_tools", {}).get(full)
        if tool_obj is None:
            avail = _action_names()
            return ("错误：没有 %s 这个动作。可用：%s"
                    % (act, "、".join(avail) or "（浏览器 server 没起来）"))
        payload = args if isinstance(args, dict) else {}
        try:
            return tool_obj.fn(**payload)
        except TypeError as e:
            return ("错误：参数不对（%s）。这个动作要的参数：%s"
                    % (e, json.dumps(getattr(tool_obj, "parameters", {}),
                                       ensure_ascii=False)[:400]))
        except Exception as e:  # noqa: BLE001
            return "错误：浏览器动作失败：%s" % (str(e) or type(e).__name__)

    reg.register(browser)
