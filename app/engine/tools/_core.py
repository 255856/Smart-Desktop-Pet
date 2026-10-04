"""Tool / ToolRegistry 核心类型。"""
from __future__ import annotations

import ipaddress
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def is_under_any(path: Path, roots: list[Path]) -> bool:
    """判断 path 是否位于 roots 任意一棵子树的**内部**（按路径语义，非字符串前缀）。

    禁止用 ``str(p).startswith(str(root))``：那是纯字符串比较，
    ``C:\\Users\\alice-backup\\x.txt`` 会被 ``C:\\Users\\alice`` 前缀命中，
    导致沙箱被同前缀兄弟目录绕过。这里统一用 ``relative_to``。

    Args:
        path: 待判定路径（会先 ``resolve()`` 消解 ``..`` 与符号链接）。
        roots: 允许的根目录列表。
    """
    try:
        p_resolved = Path(path).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    for root in roots:
        try:
            r_resolved = Path(root).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        try:
            p_resolved.relative_to(r_resolved)
            return True
        except ValueError:
            continue
    return False


_BLOCKLISTED_HOSTNAMES = frozenset({
    "localhost", "localhost.localdomain",
    "ip6-localhost", "ip6-loopback",
    "0.0.0.0", "::", "::1",
})


def is_safe_url(url: str, allowed_hosts: Optional[set[str]] = None) -> bool:
    """URL 安全检查：仅 http/https；host 不在环回 / 私有 / 保留地址里。

    Args:
        url: 待检查的 URL。
        allowed_hosts: 若非空，仅这些 host 通过；
                       host 不在白名单 → 一律拒绝（无论公私）。
    """
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    # 白名单模式：host 不在白名单 → 拒
    if allowed_hosts is not None:
        if host in allowed_hosts:
            return True
        return False
    # 无白名单：走 IP 校验
    if host in _BLOCKLISTED_HOSTNAMES:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # host 是域名（无法静态判定 IP），调用方负责用 socket.gethostbyname 再校验一次
        return True
    if (ip.is_private or ip.is_loopback or ip.is_reserved
            or ip.is_link_local or ip.is_multicast or ip.is_unspecified):
        return False
    return True


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict            # JSON Schema
    fn: Callable[..., str]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def to_openai(self) -> list[dict]:
        """转成 OpenAI tools 字段格式。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def _signature_hint(self, tool: Tool) -> str:
        """从 JSON Schema 生成「本工具接受哪些参数」，用于参数错误时告诉模型怎么改。"""
        props = (tool.parameters or {}).get("properties", {}) or {}
        required = (tool.parameters or {}).get("required", []) or []
        if not props:
            return ""
        parts = []
        for key in props:
            parts.append(f"{key}（必填）" if key in required else f"{key}（可选）")
        return "、".join(parts)

    def execute(self, name: str, arguments: str | dict) -> str:
        """执行一个工具调用，永远返回字符串（异常也转成错误文本给模型）。"""
        tool = self._tools.get(name)
        if tool is None:
            return f"错误：未知工具 {name}"
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        except json.JSONDecodeError as e:
            return f"错误：参数不是合法 JSON（{e}）"
        if not isinstance(args, dict):
            return f"错误：参数必须是 JSON 对象，收到 {type(args).__name__}"
        try:
            return str(tool.fn(**args))
        except TypeError as e:
            # 不把 TypeError 原文回给模型：里面带 register.<locals> 这类内部信息，
            # 对模型毫无意义。改成给出「本工具接受哪些参数」，模型才知道怎么改。
            log.debug("工具 %s 参数不匹配: %s", name, e)
            hint = self._signature_hint(tool)
            msg = f"错误：{name} 的参数不匹配。"
            if hint:
                msg += f"它接受的参数：{hint}。"
            msg += "请按上述参数名重新调用。"
            return msg
        except Exception as e:  # noqa: BLE001
            log.exception("工具 %s 执行失败", name)
            return f"错误：{e}"


__all__ = ["Tool", "ToolRegistry", "is_under_any"]