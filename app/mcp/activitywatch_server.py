"""ActivityWatch MCP server。

通过 aw-server (https://activitywatch.net/) 的 HTTP API 暴露用户活动数据
(窗口标题 / 浏览器标签 / AFK 状态) 给桌宠 LLM, 让 LLM 能回答"我刚才在
干嘛"以及为 ProactiveBrain 提供主动搭话的 context。

设计原则:
  - 不依赖 aw-client Python SDK, 仅用 stdlib (urllib) → 不引入新依赖,
    pip install 即可使用
  - aw-server 未运行时优雅降级 (返"未运行"消息, 不抛致命错)
  - timeout 短, 不阻塞主程序太久

工具:
  aw_get_current_window() → 当前活动窗口 (app + title)
  aw_get_recent_activities(minutes=30, limit=20) → 最近窗口事件摘要
  aw_get_browser_tabs(minutes=30, limit=10) → 最近浏览器标签页
  aw_status() → aw-server 是否在跑 (健康检查)

环境变量 (可选):
  AW_SERVER_URL  默认 http://127.0.0.1:5600
  AW_HOSTNAME    默认 socket.gethostname()
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)


def _force_utf8_stdio() -> None:
    """强制 stdin/stdout UTF-8 编码 (跟 filesystem_server 一致, 修 Windows
    中文报错问题)。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


_force_utf8_stdio()


def _aw_url(path: str) -> str:
    base = os.environ.get("AW_SERVER_URL", "http://127.0.0.1:5600").rstrip("/")
    return f"{base}{path}"


def _hostname() -> str:
    return os.environ.get("AW_HOSTNAME") or socket.gethostname()


def _http_get_json(url: str, timeout: float = 4.0) -> Any:
    """GET 一个 URL, 返 JSON。失败 / 超时返 None, 不抛异常 (mcp 层不让挂)。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "desktop-pet-aw/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else None
    except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError,
            TimeoutError, ConnectionError, OSError) as e:
        log.debug("aw GET %s failed: %s", url, e)
        return None


def _aw_status() -> dict:
    """查 aw-server 是否在跑 (heartbeat 端点). 失败返 {ok: False, ...}."""
    info = _http_get_json(_aw_url("/api/0/info"), timeout=2.0)
    if not isinstance(info, dict):
        return {
            "ok": False,
            "hint": (
                "aw-server 未运行或不可达 (默认 http://127.0.0.1:5600)。\n"
                "  - 在另一终端启 `aw-server` (Rust 后台, 占用 5600 端口);\n"
                "  - 或在设置面板改 `AW 服务器地址` + 重启桌宠;"
            ),
        }
    return {
        "ok": True,
        "version": info.get("version"),
        "hostname": info.get("hostname"),
        "device_id": info.get("device_id"),
    }


def _list_buckets() -> list[dict]:
    """列 aw-server 已注册的所有 bucket。返 [] 表示 aw-server 没跑。"""
    data = _http_get_json(_aw_url("/api/0/buckets/"))
    return data if isinstance(data, list) else []


def _find_window_bucket(buckets: list[dict]) -> Optional[str]:
    """找当前 host 的窗口 bucket。aw-watcher-window-{hostname}."""
    host = _hostname().lower()
    for b in buckets:
        bid = b.get("id", "")
        if bid.startswith(f"aw-watcher-window-{host}"):
            return bid
    # fallback: 任意 window bucket (单设备场景)
    for b in buckets:
        bid = b.get("id", "")
        if bid.startswith("aw-watcher-window-"):
            return bid
    return None


def _find_web_bucket(buckets: list[dict]) -> Optional[str]:
    host = _hostname().lower()
    for b in buckets:
        bid = b.get("id", "")
        if bid.startswith(f"aw-watcher-web-{host}"):
            return bid
    for b in buckets:
        bid = b.get("id", "")
        if bid.startswith("aw-watcher-web-"):
            return bid
    return None


def _query_events(bucket_id: str, minutes: int, limit: int) -> list[dict]:
    """取一个 bucket 最近 minutes 分钟内的事件, 按时间倒序。"""
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=max(1, int(minutes)))
    # aw-server 期望 ISO 8601 UTC
    url = (
        f"{_aw_url(f'/api/0/buckets/{bucket_id}/events')}"
        f"?start={start.isoformat().replace('+00:00', 'Z')}"
        f"&end={end.isoformat().replace('+00:00', 'Z')}"
        f"&limit={max(1, int(limit))}"
    )
    data = _http_get_json(url, timeout=6.0)
    return data if isinstance(data, list) else []


def _format_event_zh(ev: dict) -> str:
    """把单条 window 事件 (aw-watcher-window schema) 转成中文描述."""
    data = ev.get("data", {}) or {}
    app = data.get("app") or data.get("appname") or "未知应用"
    title = data.get("title") or data.get("window_title") or ""
    dur = ev.get("duration", 0)
    if isinstance(dur, (int, float)):
        secs = int(dur)
        if secs >= 60:
            time_str = f"{secs // 60}m{secs % 60}s"
        else:
            time_str = f"{secs}s"
    else:
        time_str = "?"
    if title:
        return f"{app}: {title[:80]} (持续 {time_str})"
    return f"{app} (持续 {time_str})"


def _format_web_event_zh(ev: dict) -> str:
    """把单条 web 事件 (aw-watcher-web schema) 转成中文描述."""
    data = ev.get("data", {}) or {}
    url = data.get("url") or ""
    title = data.get("title") or ""
    dur = ev.get("duration", 0)
    if isinstance(dur, (int, float)):
        secs = int(dur)
        time_str = f"{secs // 60}m{secs % 60}s" if secs >= 60 else f"{secs}s"
    else:
        time_str = "?"
    if url and title:
        return f"[{title[:60]}] {url[:80]} ({time_str})"
    if url:
        return f"{url[:100]} ({time_str})"
    return f"(空 url, {time_str})"


TOOLS = [
    {
        "name": "aw_status",
        "description": (
            "查 ActivityWatch (aw-server) 是否在运行。让 LLM 在 AW 相关问题 "
            "前先确认后端可达 — 主人也许忘了起 aw-server。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "aw_get_current_window",
        "description": (
            "返回用户**当前**活动窗口的应用名 + 窗口标题。"
            "若主人在浏览器里, 也能体现 tab title。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "aw_get_recent_activities",
        "description": (
            "返回用户**最近 N 分钟**内的窗口活动摘要 (应用 + 标题 + 时长),"
            "按时间倒序。适合主人问「我刚才在干嘛」「过去一小时做了什么」。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "minutes": {
                    "type": "integer",
                    "default": 30,
                    "description": "时间范围 (分钟), 默认 30",
                },
                "limit": {
                    "type": "integer",
                    "default": 20,
                    "description": "最多返回几条, 默认 20",
                },
            },
            "required": [],
        },
    },
    {
        "name": "aw_get_browser_tabs",
        "description": (
            "返回用户**最近 N 分钟**内访问过的浏览器标签页 (标题 + URL + 时长)。"
            "适合主人问「我最近看啥网页」。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "minutes": {
                    "type": "integer",
                    "default": 30,
                    "description": "时间范围 (分钟), 默认 30",
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "description": "最多返回几条, 默认 10",
                },
            },
            "required": [],
        },
    },
]


def _ok_text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _err_text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


def handle_call(name: str, arguments: dict) -> dict:
    if name == "aw_status":
        status = _aw_status()
        if status["ok"]:
            return _ok_text(
                f"aw-server 正常: version={status.get('version')}, "
                f"hostname={status.get('hostname')}, device_id={status.get('device_id')}"
            )
        return _err_text(
            status.get("hint")
            or "aw-server 不可达。"
        )

    if name == "aw_get_current_window":
        # v25: 错误分清楚 (1) aw-server 没跑 (2) 装了 server 但没装 watcher
        # (3) watcher 装了但没记录事件。LLM 看到这些能告诉主人怎么修
        status = _aw_status()
        if not status["ok"]:
            return _err_text(status.get("hint") or "aw-server 不可达。")
        buckets = _list_buckets()
        bid = _find_window_bucket(buckets)
        if not bid:
            return _err_text(
                f"aw-server 跑着, 但没找到窗口 bucket (hostname={_hostname()})。"
                "可能是 aw-watcher-window 没启, 或 Process Explorer/WMI 没权限。"
                "Windows 上 aw-qt 托盘右键 → 'Modules' 能看 watcher 状态。"
            )
        events = _query_events(bid, minutes=2, limit=1)
        if not events:
            return _err_text(
                f"aw-watcher-window 装着但最近 2 分钟一条事件都没记录。"
                "常见原因: watcher 进程抽风 (Win32 GetForegroundWindow "
                "在某些 Windows 会话里返回 NULL), 需要从 aw-qt 托盘重启 watcher。"
                f"bucket last_updated={datetime.now(timezone.utc).isoformat()[:19]}Z "
                "(检查 aw-qt 是否仍提示 'watchers running')。"
            )
        latest = events[0]
        text = f"当前窗口: {_format_event_zh(latest)}"
        # 顺便给最近 5 分钟的活动数量
        recent = _query_events(bid, minutes=5, limit=10)
        if len(recent) > 1:
            text += f"\n(过去 5 分钟切换了 {len(recent)} 个窗口)"
        return _ok_text(text)

    if name == "aw_get_recent_activities":
        minutes = int(arguments.get("minutes") or 30)
        limit = int(arguments.get("limit") or 20)
        # v25: 区分空数据的三种原因 — 后端没跑 / watcher 没装 / watcher 没记录
        status = _aw_status()
        if not status["ok"]:
            return _err_text(status.get("hint") or "aw-server 不可达。")
        buckets = _list_buckets()
        bid = _find_window_bucket(buckets)
        if not bid:
            return _err_text(
                f"aw-server 跑着但没找到窗口 bucket (hostname={_hostname()})。"
                "请确认 aw-watcher-window 在 AW 托盘里启用。"
            )
        events = _query_events(bid, minutes=minutes, limit=limit)
        if not events:
            return _ok_text(
                f"过去 {minutes} 分钟没有窗口活动记录(aw-watcher-window 可能挂着,"
                f"从 aw-qt 托盘重启 watcher 试试)。"
            )
        lines = [f"过去 {minutes} 分钟的窗口活动 (最新 → 最早, 最多 {limit} 条):"]
        for ev in events:
            lines.append("- " + _format_event_zh(ev))
        return _ok_text("\n".join(lines))

    if name == "aw_get_browser_tabs":
        minutes = int(arguments.get("minutes") or 30)
        limit = int(arguments.get("limit") or 10)
        buckets = _list_buckets()
        if not buckets:
            return _err_text("aw-server 未运行或返回空。")
        bid = _find_web_bucket(buckets)
        if not bid:
            return _err_text(
                f"未找到浏览器 bucket (hostname={_hostname()})。"
                "可能是 aw-watcher-web (浏览器扩展) 没装/没启。"
            )
        events = _query_events(bid, minutes=minutes, limit=limit)
        if not events:
            return _ok_text(f"过去 {minutes} 分钟没有浏览器标签记录。")
        lines = [f"过去 {minutes} 分钟访问过的标签 (最新 → 最早, 最多 {limit} 条):"]
        for ev in events:
            lines.append("- " + _format_web_event_zh(ev))
        return _ok_text("\n".join(lines))

    return _err_text(f"未知工具: {name}")


def run_server() -> None:
    log.info("MCP activitywatch_server 启动 (aw=%s, host=%s)",
             os.environ.get("AW_SERVER_URL", "http://127.0.0.1:5600"),
             _hostname())
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        req_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params", {}) or {}

        if method == "initialize":
            send(req_id, {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name": "activitywatch_server",
                    "version": "1.0.0",
                },
            })
            continue
        if method == "notifications/initialized":
            continue
        if method == "tools/list":
            send(req_id, {"tools": TOOLS})
            continue
        if method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments", {}) or {}
            result = handle_call(name, args)
            send(req_id, result)
            continue
        if req_id is not None:
            send(req_id, None, error={"code": -32601,
                                        "message": f"未知方法：{method}"})


def send(req_id, result=None, error=None) -> None:
    msg = {"jsonrpc": "2.0", "id": req_id}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--aw-url", default=None,
                   help="aw-server URL (默认 http://127.0.0.1:5600, 也可用 AW_SERVER_URL 环境变量)")
    p.add_argument("--hostname", default=None,
                   help="本机 hostname (默认 socket.gethostname())")
    args = p.parse_args()
    if args.aw_url:
        os.environ["AW_SERVER_URL"] = args.aw_url
    if args.hostname:
        os.environ["AW_HOSTNAME"] = args.hostname
    run_server()


__all__ = ["TOOLS", "handle_call", "run_server", "main"]


if __name__ == "__main__":
    main()
