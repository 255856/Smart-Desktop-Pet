"""ActivityWatch 客户端封装（桌宠主进程内 HTTP 客户端）。

跟 app/mcp/activitywatch_server.py 是姐妹模块:
  - aw server 通过 stdio JSON-RPC 给 LLM 用 (有 tool schema)
  - 这个文件被 ProactiveBrain 直接 import, 用于在主线程拉活动 context
    (不用经过 MCP 协议 — 一次 HTTP GET 比 spawn 子进程 + JSON-RPC 简单)

为什么不像 mcp 那样起子进程:
  ProactiveBrain._fire 在主线程 25-45 分钟触发一次, 直接走 HTTP 最简单。
  LLM 想查活动数据仍然走 aw_* 工具 (经 MCP), 两条路并行不冲突。

失败语义: 所有公开函数都返 None / 空字符串 / False, 不抛异常。
ProactiveBrain 看到 None 就当「AW 没开 / 没数据」处理, 不会让主动发言挂掉。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger(__name__)


def _aw_url(path: str, server_url: Optional[str] = None) -> str:
    base = (server_url or os.environ.get("AW_SERVER_URL")
            or "http://127.0.0.1:5600").rstrip("/")
    return f"{base}{path}"


def _hostname(server_hostname: Optional[str] = None) -> str:
    if server_hostname:
        return server_hostname
    return os.environ.get("AW_HOSTNAME") or socket.gethostname()


def _http_get_json(url: str, timeout: float = 4.0):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "desktop-pet-aw-client/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else None
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError,
            TimeoutError, ConnectionError, OSError) as e:
        log.debug("aw GET %s failed: %s", url, e)
        return None


def is_available(server_url: Optional[str] = None) -> bool:
    """aw-server 是否可达 (heartbeat /api/0/info)。"""
    info = _http_get_json(_aw_url("/api/0/info", server_url), timeout=2.0)
    return isinstance(info, dict)


def _find_bucket(buckets, prefix: str, hostname: str):
    """aw 0.14+ bucket 命名是 `aw-watcher-<type>_<hostname>` (下划线).
    早期版本用 -, 兼容两种。
    """
    host = hostname.lower()
    for b in buckets:
        bid = b.get("id", "")
        if bid.startswith((f"{prefix}-{host}", f"{prefix}_{host}")):
            return bid
    # fallback: 任意同 prefix bucket (单设备/老 hostname 时也能用)
    for b in buckets:
        bid = b.get("id", "")
        if bid.startswith((prefix, prefix)):
            return bid
    return None


def _query_events(bucket_id: str, minutes: int, limit: int, server_url=None):
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=max(1, int(minutes)))
    url = (
        f"{_aw_url(f'/api/0/buckets/{bucket_id}/events', server_url)}"
        f"?start={start.isoformat().replace('+00:00', 'Z')}"
        f"&end={end.isoformat().replace('+00:00', 'Z')}"
        f"&limit={max(1, int(limit))}"
    )
    data = _http_get_json(url, timeout=6.0)
    return data if isinstance(data, list) else []


def _format_window_event_zh(ev: dict) -> str:
    """aw-watcher-window 事件 → 中文人话。"""
    data = ev.get("data", {}) or {}
    app = data.get("app") or data.get("appname") or "未知应用"
    title = data.get("title") or data.get("window_title") or ""
    dur = ev.get("duration", 0)
    if isinstance(dur, (int, float)):
        secs = int(dur)
        time_str = f"{secs // 60}m{secs % 60}s" if secs >= 60 else f"{secs}s"
    else:
        time_str = "?"
    return f"{app}: {title[:80]} (持续 {time_str})" if title else f"{app} (持续 {time_str})"


def get_recent_activities_summary(
    minutes: int = 30,
    limit: int = 15,
    *,
    server_url: Optional[str] = None,
    hostname: Optional[str] = None,
) -> Optional[str]:
    """拉最近 minutes 分钟的窗口活动, 转成 LLM-friendly 中文摘要。

    返回 None 表示 aw-server 不可达 / 没装 watcher — ProactiveBrain 当
    「数据不可用」处理, context 字段空着就行, 不影响主动搭话。

    返回非空字符串时表示有数据, ProactiveBrain 把它塞进 context 的
    recent_activities 字段。
    """
    if not is_available(server_url):
        return None
    buckets = _http_get_json(_aw_url("/api/0/buckets/", server_url))
    if not isinstance(buckets, list):
        return None
    bid = _find_bucket(buckets, "aw-watcher-window", _hostname(hostname))
    if not bid:
        return None
    events = _query_events(bid, minutes=minutes, limit=limit, server_url=server_url)
    if not events:
        return f"最近 {minutes} 分钟无窗口活动记录 (aw-watcher-window 未采集到数据)。"
    lines = [f"过去 {minutes} 分钟的窗口活动 (最新 → 最早, 最多 {limit} 条):"]
    for ev in events:
        lines.append("- " + _format_window_event_zh(ev))
    return "\n".join(lines)


def get_current_window_one_liner(
    *,
    server_url: Optional[str] = None,
    hostname: Optional[str] = None,
) -> Optional[str]:
    """拉当前焦点窗口一句话描述, 给 LLM 一眼看到主人当下在干嘛。

    返 None → 数据不可用; 返 str → 直接拼到 ProactiveBrain context 里。
    """
    if not is_available(server_url):
        return None
    buckets = _http_get_json(_aw_url("/api/0/buckets/", server_url))
    if not isinstance(buckets, list):
        return None
    bid = _find_bucket(buckets, "aw-watcher-window", _hostname(hostname))
    if not bid:
        return None
    events = _query_events(bid, minutes=2, limit=1, server_url=server_url)
    if not events:
        return None
    return "当前窗口: " + _format_window_event_zh(events[0])


__all__ = [
    "is_available",
    "get_recent_activities_summary",
    "get_current_window_one_liner",
]
