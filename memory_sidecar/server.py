# -*- coding: utf-8 -*-
"""记忆 sidecar 的 HTTP 服务层（标准库 http.server，不引入 FastAPI / uvicorn）。

端点
----
GET  /health    -> {ok, cards, models: {embed, rerank}, degraded, ...}
GET  /list      -> {ok, cards: [...], count, total}（重要度 desc → 新近 desc）
GET  /stats     -> 卡数 / 索引状态 / 降级原因
POST /search    -> {ok, results: [...], path, degraded[, error]}
POST /upsert    -> {ok, upserted}
POST /delete    -> {ok, deleted}
POST /supersede -> {ok, upserted}
POST /rebuild   -> {ok, rebuilt: {vector, bm25, errors}}

约定：/search **永远**返回 200 + 可解析的 JSON。哪怕两条粗排腿全挂，
也是 {ok: false, results: [], error: "..."} —— 调用方不需要为 500 写分支。
"""
import json
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .store import LIST_DEFAULT_LIMIT, MemoryStore

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788
MAX_BODY_BYTES = 8 * 1024 * 1024


def _endpoints():
    return {
        "GET /health": "健康状态（卡数 / 模型是否就绪 / 是否降级）",
        "GET /list": "列卡片：?kind=&limit=（重要度 desc → 新近 desc）",
        "GET /stats": "详细统计（卡数、索引、降级原因）",
        "POST /search": "检索：{query, top_k, mood?, kind?}",
        "POST /upsert": "写入：{cards: [...]}",
        "POST /delete": "删除：{ids: [...]}",
        "POST /supersede": "更新：{oldId, card}",
        "POST /rebuild": "从 JSON 卡片重建两个索引",
    }


def make_handler(store):
    """把 store 绑到处理器上（每个 server 一份，不用模块级全局）。"""

    class MemoryHandler(BaseHTTPRequestHandler):
        server_version = "DesktopPetMemorySidecar/1.0"
        store_ = store

        # 别把每次请求都刷进 stderr（sidecar 的日志要能看）
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
            except Exception:
                pass

        def _path(self):
            return self.path.split("?", 1)[0].rstrip("/") or "/"

        def _query(self):
            """查询参数 → {名字: 第一个值}（/list 用；解析失败就当没传）。"""
            try:
                raw = parse_qs(urlparse(self.path).query)
            except Exception:
                return {}
            return {k: v[0] for k, v in raw.items() if v}

        def _read_json(self):
            length = int(self.headers.get("content-length") or 0)
            if length > MAX_BODY_BYTES:
                raise ValueError("请求体过大：%d 字节（上限 %d）" % (length, MAX_BODY_BYTES))
            if length <= 0:
                return {}
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            if isinstance(data, list):
                return {"cards": data}
            if not isinstance(data, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return data

        # -- GET --------------------------------------------------------
        def do_GET(self):
            path = self._path()
            store = self.store_
            if path in ("/health", "/healthz"):
                try:
                    body = dict(store.health())
                except Exception as e:  # 健康检查自己不能挂
                    body = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
                body["port"] = getattr(self.server, "sidecar_port", None)
                body["uptimeSec"] = round(time.time() - getattr(self.server, "started_at", time.time()), 1)
                self._json(body)
                return
            if path == "/list":
                self._handle_list()
                return
            if path == "/stats":
                try:
                    self._json({"ok": True, "stats": store.stats()})
                except Exception as e:
                    self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)
                return
            if path == "/":
                self._json({"ok": True, "service": "desktop-pet-memory-sidecar", "endpoints": _endpoints()})
                return
            self._json({"ok": False, "error": "not found: %s" % path}, 404)

        # -- POST -------------------------------------------------------
        def do_POST(self):
            path = self._path()
            store = self.store_
            try:
                payload = self._read_json()
            except Exception as e:
                self._json({"ok": False, "error": "请求体解析失败：%s" % e}, 400)
                return
            if path == "/search":
                self._handle_search(payload)
                return
            try:
                if path == "/upsert":
                    n = store.upsert(payload.get("cards") or [])
                    self._json({"ok": True, "upserted": n})
                elif path == "/delete":
                    ids = payload.get("ids") or []
                    if isinstance(ids, str):
                        ids = [i.strip() for i in ids.split(",") if i.strip()]
                    self._json({"ok": True, "deleted": store.delete(ids)})
                elif path == "/supersede":
                    old_id = payload.get("oldId") or payload.get("old_id") or ""
                    n = store.supersede(old_id, payload.get("card") or {})
                    self._json({"ok": True, "upserted": n})
                elif path == "/rebuild":
                    self._json({"ok": True, "rebuilt": store.rebuild_index()})
                else:
                    self._json({"ok": False, "error": "not found: %s" % path}, 404)
            except Exception as e:
                self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)

        # -- /list：列卡片（不检索、不加载模型） ------------------------
        def _handle_list(self):
            store = self.store_
            q = self._query()
            kind = (q.get("kind") or "").strip() or None
            try:
                limit = int(q.get("limit") or LIST_DEFAULT_LIMIT)
            except Exception:
                limit = LIST_DEFAULT_LIMIT  # 传了不是数字的 limit 也不许 500
            # 默认只列 active；?include_superseded=1（或 ?status=all）连历史卡一起列
            flag = (q.get("include_superseded") or q.get("includeSuperseded")
                    or q.get("status") or "").strip().lower()
            include_superseded = flag in ("1", "true", "yes", "on", "all", "any", "superseded")
            try:
                cards = store.list_cards(
                    kind=kind, limit=limit, include_superseded=include_superseded
                )
                total = store.count_cards(kind, include_superseded=include_superseded)
            except Exception as e:
                self._json(
                    {"ok": False, "cards": [], "count": 0, "error": "%s: %s" % (type(e).__name__, e)},
                    500,
                )
                return
            # count = 本次返回条数；total = 符合条件（kind / include_superseded）的总条数
            self._json({
                "ok": True,
                "kind": kind,
                "includeSuperseded": include_superseded,
                "cards": cards,
                "count": len(cards),
                "total": total,
            })

        # -- /search：绝不 500 ------------------------------------------
        def _handle_search(self, payload):
            store = self.store_
            query = payload.get("query", "")
            try:
                top_k = payload.get("top_k", payload.get("topK", 8))
                res = store.search(
                    query,
                    top_k=int(top_k or 8),
                    mood=payload.get("mood") or None,
                    kind=payload.get("kind") or None,
                    kinds=payload.get("kinds") or None,
                    key=payload.get("key") or None,
                )
            except Exception as e:
                self._json(
                    {
                        "ok": False,
                        "results": [],
                        "degraded": True,
                        "error": "检索异常：%s: %s" % (type(e).__name__, e),
                    }
                )
                return
            body = {
                "ok": not res.get("error"),
                "query": query,
                "results": res.get("results") or [],
                "path": res.get("path"),
                "degraded": bool(res.get("degraded")),
            }
            if res.get("error"):
                body["error"] = res["error"]
            self._json(body)

    return MemoryHandler


class MemorySidecarServer:
    """把 MemoryStore 包成一个 HTTP 服务。

    用法::

        srv = MemorySidecarServer(store, port=8788)
        srv.serve_forever()            # 阻塞；Ctrl+C 优雅退出

    测试里用 start() / stop()（后台线程，不阻塞）。
    """

    def __init__(self, store, host=DEFAULT_HOST, port=DEFAULT_PORT, warmup=True):
        self.store = store if store is not None else MemoryStore()
        self.host = host or DEFAULT_HOST
        self.requested_port = int(port or 0)
        self.warmup = bool(warmup)
        self.port = None
        self._httpd = None
        self._thread = None
        self._warmup_thread = None
        self._stop_lock = threading.Lock()

    # -- 组装 ------------------------------------------------------------
    def _make_httpd(self):
        httpd = ThreadingHTTPServer((self.host, self.requested_port), make_handler(self.store))
        httpd.daemon_threads = True  # 关服务时不陪长连接耗着
        httpd.sidecar_port = int(httpd.server_address[1])
        httpd.started_at = time.time()
        return httpd

    def bind(self):
        if self._httpd is None:
            self._httpd = self._make_httpd()
            self.port = int(self._httpd.sidecar_port)
        return self._httpd

    def url(self):
        return "http://%s:%s" % (self.host, self.port)

    # -- 启动 / 停止 -----------------------------------------------------
    def start(self):
        """后台线程起服务（测试用）。返回实际端口（port=0 时由系统分配）。"""
        httpd = self.bind()
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="memory-sidecar-http", daemon=True
        )
        self._thread.start()
        self._start_warmup()
        return self.port

    def serve_forever(self):
        """前台阻塞运行（命令行 serve 用）。"""
        httpd = self.bind()
        self._install_signal_handlers()
        print(
            "[memory_sidecar] 监听 %s  数据目录=%s  卡片=%d  模型预热=%s"
            % (
                self.url(),
                self.store.data_dir,
                len(self.store._active_cards()),
                "开" if self.warmup else "关",
            ),
            flush=True,
        )
        self._start_warmup()
        try:
            httpd.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            print("[memory_sidecar] 收到 Ctrl+C，正在退出", flush=True)
        finally:
            self.stop()

    def _install_signal_handlers(self):
        def handler(signum, _frame):
            print("[memory_sidecar] 收到信号 %s，正在退出" % signum, flush=True)
            # shutdown() 会等 serve_forever 返回，不能在 serve_forever 所在线程里直接调
            threading.Thread(target=self.stop, daemon=True).start()

        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, handler)
            except Exception:
                pass

    def stop(self):
        """优雅关闭：停止 accept 循环 + 关 socket（可重复调用）。"""
        with self._stop_lock:
            httpd, self._httpd = self._httpd, None
            thread, self._thread = self._thread, None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)

    # -- 预热 ------------------------------------------------------------
    def _start_warmup(self):
        if not self.warmup or self._warmup_thread is not None:
            return
        self._warmup_thread = threading.Thread(
            target=self._warmup_worker, name="memory-sidecar-warmup", daemon=True
        )
        self._warmup_thread.start()

    def _warmup_worker(self):
        t0 = time.time()
        try:
            report = self.store.warmup()
        except Exception as e:
            report = {"error": "%s: %s" % (type(e).__name__, e)}
        print(
            "[memory_sidecar] 预热完成 耗时=%dms %s"
            % (int((time.time() - t0) * 1000), json.dumps(report, ensure_ascii=False)),
            flush=True,
        )


def serve(store=None, host=DEFAULT_HOST, port=DEFAULT_PORT, data_dir=None, warmup=True, **store_kwargs):
    """一步起服务（前台阻塞）。返回退出码。"""
    if store is None:
        store = MemoryStore(data_dir=data_dir, **store_kwargs)
        store.load()
    srv = MemorySidecarServer(store, host=host, port=port, warmup=warmup)
    srv.serve_forever()
    return 0


if __name__ == "__main__":  # pragma: no cover - 便于 python memory_sidecar/server.py 直接跑
    sys.exit(serve())
