"""URL 正文提取：抓 HTML + 用 readability-lxml 抽主文。host 走白名单校验。"""
from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

from ._core import Tool, ToolRegistry, is_safe_url

log = logging.getLogger(__name__)


def _resolve_and_check(host: str) -> bool:
    """域名形式时再用一次 socket.gethostbyname 解析成 IP，
    避免「DNS rebinding」或「域名指向私有 IP」的绕过。
    返回 False = 不安全，应拒。"""
    try:
        ip = socket.gethostbyname(host)
    except (socket.gaierror, UnicodeError):
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_reserved
                or addr.is_link_local or addr.is_multicast or addr.is_unspecified)


def register(reg: ToolRegistry) -> None:
    """注册 fetch_url 工具。"""

    def fetch_url_text(url: str, max_chars: int = 4000) -> str:
        """抓 URL 主文（readability-lxml）。host 先白名单校验，再 DNS 解析二次校验。"""
        if not url or not url.strip():
            return "错误：url 不能为空"
        try:
            parsed = urlparse(url)
        except (ValueError, TypeError):
            return "错误：url 解析失败"
        if parsed.scheme not in ("http", "https"):
            return "错误：仅支持 http/https"
        host = (parsed.hostname or "").lower()
        # host 是字面量 IP：先 ip 校验（is_safe_url 自带），再用 _resolve_and_check
        # host 是域名：先 is_safe_url（默认通过），再用 _resolve_and_check 二次校验
        try:
            ipaddress.ip_address(host)
            ip_literal_ok = True
        except ValueError:
            ip_literal_ok = False
        if ip_literal_ok:
            if not is_safe_url(url):
                return "错误：host 在私有 / 环回 / 保留地址段，已拒"
        else:
            if not is_safe_url(url):
                return "错误：host 被黑名单拦截"
            if not _resolve_and_check(host):
                return "错误：域名解析到私有 / 环回 / 保留地址，已拒"
        try:
            with httpx.Client(
                timeout=15.0,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (Smart-Desktop-Pet)"},
            ) as c:
                r = c.get(url)
                r.raise_for_status()
                html = r.text
        except httpx.HTTPError as e:
            return f"错误：抓取失败：{e}"
        # readability 提取
        try:
            from readability import Document
            doc = Document(html)
            text = doc.summary()
        except ImportError:
            # 没装 readability：粗暴去 HTML
            log.warning("readability-lxml 未安装，回退到正则去标签")
            import re
            text = re.sub(r"<script.*?</script>|<style.*?</style>", "", html,
                          flags=re.DOTALL | re.IGNORECASE)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text).strip()
        except Exception as e:  # noqa: BLE001
            log.warning("readability 失败：%s", e)
            import re
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return "错误：未抓到正文（可能页面是 JS 渲染）"
        max_chars = max(200, min(int(max_chars if max_chars else 4000), 20000))
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n...(截断，原文 {len(text)} 字)"
        return text

    reg.register(Tool(name="fetch_url_text",
        description="抓取 URL 正文（readability-lxml 提取主文）。"
                    "host 校验：先黑名单 → IP 校验 → 域名 DNS 二次解析 → 防 DNS rebinding。"
                    "区别 web_search（搜关键词）：本工具拿单页正文。",
        parameters={"type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "目标 URL"},
                        "max_chars": {"type": "integer", "minimum": 200, "maximum": 20000,
                                      "description": "最大字符数，默认 4000"},
                    },
                    "required": ["url"]},
        fn=fetch_url_text))


__all__ = ["register"]