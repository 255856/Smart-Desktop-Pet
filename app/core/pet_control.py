# -*- coding: utf-8 -*-
"""桌宠控制服务：控制台（浏览器）改设置、驱动桌宠动作的本地 HTTP 端口。

桌宠主程序（Qt 进程）在这里开一个小 HTTP 服务；控制台进程通过它读写设置、
触发运行时动作（换 Live2D 外观、试听语音…）。

为什么要有这一层：控制台是独立进程，跟桌宠隔着一个进程边界，
Qt 控件和渲染器对象都碰不到 —— 只能主程序自己开端口接请求。

只用标准库（跟 memory_sidecar 一样），不引 FastAPI / uvicorn。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8790
MAX_BODY_BYTES = 4 * 1024 * 1024


def _endpoints():
    return {
        "GET /health": "桌宠进程是否活着",
        "GET /settings": "读所有设置项（密钥掩码）",
        "GET /schema": "设置项清单（标签 / 范围 / 选项）",
        "POST /settings": "改设置 {changes: {key: value}}",
        "POST /action": "运行时动作 {action: ..., ...}",
        "GET /appearance": "Live2D 外观分类（菜单组 + 当前激活项）",
        "GET /avatars": "可用头像列表",
    }


class PetControlServer:
    """桌宠控制服务。

    handlers 由 UIController 注入：
      * ``get_settings``  → dict
      * ``get_schema``    → list
      * ``update_settings``(changes) → dict
      * ``get_appearance`` → dict
      * ``list_avatars``  → list
      * ``do_action``(action, params) → dict
    """

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, **handlers):
        self.host = host or DEFAULT_HOST
        self.requested_port = int(port or 0)
        self.handlers = dict(handlers)
        self._httpd = None
        self._thread = None
        self.port = None
        self._started_at = time.time()

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> Optional[int]:
        """后台线程起服务。返回实际端口（失败返回 None）。"""
        if self._httpd is not None:
            return self.port
        try:
            self._httpd = ThreadingHTTPServer((self.host, self.requested_port),
                                             self._make_handler())
        except OSError as e:
            log.warning("桌宠控制端口起不来（%s:%s）：%s", self.host,
                        self.requested_port, e)
            return None
        self._httpd.daemon_threads = True
        self.port = int(self._httpd.server_address[1])
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="pet-control-http", daemon=True)
        self._thread.start()
        log.info("桌宠控制服务： http://%s:%s", self.host, self.port)
        return self.port

    def stop(self) -> None:
        httpd, self._httpd = self._httpd, None
        thread, self._thread = self._thread, None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except Exception:  # noqa: BLE001
                pass
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3)

    def url(self) -> str:
        return "http://%s:%s" % (self.host, self.port or 0)

    # -- 请求处理 ---------------------------------------------------------
    def _make_handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "DesktopPetControl/1.0"

            def log_message(self, fmt, *args):
                pass

            def _json(self, obj, code=200):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("content-type", "application/json; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:  # noqa: BLE001
                    pass

            def _read_json(self):
                length = int(self.headers.get("content-length") or 0)
                if length > MAX_BODY_BYTES:
                    raise ValueError("请求体过大")
                if length <= 0:
                    return {}
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                return data if isinstance(data, dict) else {}

            def do_GET(self):
                path = urlparse(self.path).path.rstrip("/") or "/"
                try:
                    if path in ("/health", "/healthz"):
                        self._json({"ok": True, "uptimeSec": round(
                            time.time() - outer._started_at, 1), "port": outer.port})
                    elif path == "/settings":
                        self._json({"ok": True, "values": outer._call("get_settings")})
                    elif path == "/schema":
                        self._json({"ok": True, "schema": outer._call("get_schema")})
                    elif path == "/appearance":
                        self._json({"ok": True, "appearance": outer._call("get_appearance")})
                    elif path == "/avatars":
                        self._json({"ok": True, "avatars": outer._call("list_avatars")})
                    elif path == "/":
                        self._json({"ok": True, "service": "desktop-pet-control",
                                    "endpoints": _endpoints()})
                    else:
                        self._json({"ok": False, "error": "not found: %s" % path}, 404)
                except Exception as e:  # noqa: BLE001
                    self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)

            def do_POST(self):
                path = urlparse(self.path).path.rstrip("/") or "/"
                try:
                    payload = self._read_json()
                except Exception as e:  # noqa: BLE001
                    self._json({"ok": False, "error": "请求体解析失败：%s" % e}, 400)
                    return
                try:
                    if path == "/settings":
                        changes = payload.get("changes")
                        if not isinstance(changes, dict):
                            changes = {k: v for k, v in payload.items() if k != "ok"}
                        result = outer._call("update_settings", changes)
                        self._json(result if isinstance(result, dict)
                                   else {"ok": bool(result)})
                    elif path == "/action":
                        action = str(payload.get("action") or "")
                        params = payload.get("params")
                        if not isinstance(params, dict):
                            params = {k: v for k, v in payload.items()
                                      if k not in ("action", "ok")}
                        result = outer._call("do_action", action, params)
                        self._json(result if isinstance(result, dict)
                                   else {"ok": bool(result)})
                    else:
                        self._json({"ok": False, "error": "not found: %s" % path}, 404)
                except Exception as e:  # noqa: BLE001
                    self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)

        return Handler

    def _call(self, name, *args):
        fn = self.handlers.get(name)
        if fn is None:
            return None
        return fn(*args)
