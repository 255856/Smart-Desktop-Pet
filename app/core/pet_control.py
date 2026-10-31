# -*- coding: utf-8 -*-
"""桌宠控制服务：控制台（浏览器）改设置、驱动桌宠动作的本地 HTTP 端口。

桌宠主程序（Qt 进程）在这里开一个小 HTTP 服务；控制台进程通过它读写设置、
触发运行时动作（换 Live2D 外观、试听语音…）。

为什么要有这一层：控制台是独立进程，跟桌宠隔着一个进程边界，
Qt 控件和渲染器对象都碰不到 —— 只能主程序自己开端口接请求。

只用标准库（跟 memory_sidecar 一样），不引 FastAPI / uvicorn。

另外这里放着「拍一拍」的核心（PetPokeService）：双击模型是唯一的界面入口
（部位=model），控制台也能指定部位触发 —— 两条路径都走它，
行为才一致（同一个节流、同一套台词、同一份计数）。
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

# ── 「拍一拍」 ────────────────────────────────────────────────────────────
#: 拍一拍的部位：model = **双击模型**（界面唯一入口）；
#: head / body / raise 留给控制台按部位触发
PAT_PARTS = ("model", "head", "body", "raise")
#: 部位 → 中文名（台词 / 记忆卡用）
#: "model" = **双击模型**（owner 定：拍一拍只绑双击，不再按摸头/身体分开）
PAT_PART_LABELS = {"model": "拍拍", "head": "摸头", "body": "身体", "raise": "举起"}
#: 两次拍一拍之间的最小间隔（秒）：连点会烦，给它一个自然的间隔
DEFAULT_PAT_COOLDOWN_S = 1.5
#: 各部位的反应台词（轮换，避免每次都一样）
PAT_LINES = {
    # 双击模型 = 拍一拍（owner 定的唯一入口）
    "model": ("呜哇…主人拍我啦~", "嘿嘿，再拍一下嘛~", "哼，拍得还挺舒服~"),
    "head": ("呜哇…主人摸我头啦~", "嘿嘿，头顶好舒服~", "再摸一下嘛~"),
    "body": ("呀！身上痒痒的啦~", "唔…别戳啦，痒~", "嘿嘿，挠痒痒~"),
    "raise": ("呀…戳我脚丫啦~", "嘿嘿，挠脚心~", "再戳一下嘛~"),
}
#: 各部位的表情（表情名跟渲染器/模型对齐：shy / surprise / happy）
PAT_EMOTIONS = {"model": "happy", "head": "shy", "body": "surprise", "raise": "happy"}


def _endpoints():
    return {
        "GET /health": "桌宠进程是否活着",
        "GET /settings": "读所有设置项（密钥掩码）",
        "GET /schema": "设置项清单（标签 / 范围 / 选项）",
        "POST /settings": "改设置 {changes: {key: value}}",
        "POST /action": "运行时动作 {action: ..., ...}（poke = 拍一拍桌宠）",
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
                        if action == "poke":
                            # 「拍一拍」：桌宠自己注册了处理器才认
                            # （控制台也能拍它；部位 head / body / raise）
                            self._json(trigger_poke(
                                str(params.get("part") or "head")))
                            return
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


# ── 「拍一拍」核心 ───────────────────────────────────────────────────────
class PetPokeService:
    """「拍一拍」的核心：节流 + 计数 + 台词 + 表情。

    不依赖 Qt —— 桌宠窗口（点热区）和控制台（HTTP 触发）都走它，
    两条路径的行为才一致：同一个节流、同一套台词、同一份计数。

    用法：svc = PetPokeService(state=pet_state); result = svc.poke("head")
    —— result["ok"] 为真时，界面照着 text / emotion 演出来。
    """

    def __init__(self, state=None, *, cooldown_s=DEFAULT_PAT_COOLDOWN_S,
                 clock=None) -> None:
        self.state = state
        self.cooldown_s = max(0.0, float(cooldown_s))
        self._clock = clock or time.time
        self._last_ts = 0.0
        self._line_idx: dict = {}

    def poke(self, part: str = "head") -> dict:
        """处理一次「拍一拍」，返回结果字典。

        ok=False 时 reason 说明为什么没生效：throttled（连点太密）/
        unknown_part（部位不认识）。ok=True 时带 text / emotion /
        total / today，交给界面演出来。
        """
        part = str(part or "").strip().lower()
        if part not in PAT_PARTS:
            return self._result(False, part, "unknown_part")
        now = float(self._clock())
        if self.cooldown_s > 0 and (now - self._last_ts) < self.cooldown_s:
            return self._result(False, part, "throttled")
        self._last_ts = now
        record: dict = {}
        state = self.state
        if state is not None and hasattr(state, "record_pat"):
            try:
                record = state.record_pat(part) or {}
            except Exception:  # noqa: BLE001
                log.exception("记录拍一拍失败（忽略）")
                record = {}
        out = self._result(True, part, "")
        out["text"] = self._next_line(part)
        out["emotion"] = PAT_EMOTIONS.get(part, "happy")
        out["animation"] = part
        out["total"] = int(record.get("total") or 0)
        out["today"] = int(record.get("today") or 0)
        return out

    def _result(self, ok: bool, part: str, reason: str) -> dict:
        return {"ok": bool(ok), "part": part, "reason": reason,
                "text": "", "emotion": "", "animation": "",
                "total": 0, "today": 0}

    def _next_line(self, part: str) -> str:
        lines = PAT_LINES.get(part) or ("嘿嘿~",)
        idx = self._line_idx.get(part, 0)
        self._line_idx[part] = idx + 1
        return lines[idx % len(lines)]


#: 「拍一拍」触发回调（桌宠窗口启动时注册自己）—— 控制台也能拍它
_poke_handlers: list = []


def register_poke_handler(fn) -> None:
    """注册「拍一拍」回调（桌宠窗口用；同一个回调不会重复登记）。"""
    if fn is not None and fn not in _poke_handlers:
        _poke_handlers.append(fn)


def unregister_poke_handler(fn) -> None:
    """注销「拍一拍」回调（桌宠窗口关闭时用）。"""
    try:
        _poke_handlers.remove(fn)
    except ValueError:
        pass


def trigger_poke(part: str = "head") -> dict:
    """触发一次「拍一拍」（控制台用）。

    回调由桌宠窗口注册，它只负责发信号 —— HTTP 线程不碰 Qt 控件，
    Qt 会把信号排队到主线程执行。
    """
    if not _poke_handlers:
        return {"ok": False, "handled": False, "part": str(part or ""),
                "error": "桌宠还没注册拍一拍"}
    fn = list(_poke_handlers)[-1]
    try:
        fn(part)
    except Exception as e:  # noqa: BLE001
        log.exception("触发拍一拍失败")
        return {"ok": False, "handled": True, "part": str(part or ""),
                "error": "%s: %s" % (type(e).__name__, e)}
    return {"ok": True, "handled": True, "part": str(part or "")}
