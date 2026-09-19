"""Live2DRenderer：用 QWebEngineView + Cubism Web SDK 渲染 Live2D 模型。

实现 PetRenderer 接口的 Live2D 版本。所有方法（set_idle / set_emotion /
play_animation 等）都通过 QWebEngineView 调到 JS 桥层。

模型专属知识（表情分类、触发映射、睡眠参数、水印形式、动作文件等）不在本文件
硬编码，而是来自每个模型目录下的 ``*.model.yaml``（见 live2d_model_profile.py）；
没有 YAML 的模型按文件名启发式自动解析。

依赖（可选）：
    - PyQtWebEngine 5.15+
    - app/animation/cubism-sdk/live2dcubismcore.min.js  （官方 Cubism 4 Core）
    - app/animation/cubism-sdk/pixi.min.js               （pixi.js v7）
    - app/animation/cubism-sdk/cubism4.min.js            （pixi-live2d-display，Cubism4）

资源加载：
    Chromium 禁止 file:// 页面跨目录 XHR 加载模型（moc3 / json / 纹理），
    这里在 127.0.0.1 随机端口起一个本地 HTTP 服务，同时提供 bridge.html、
    SDK 脚本和模型目录（/model/*），页面与模型同源，彻底绕开 CORS。

    很多模型（冰糖/超频猫猫）的 model3.json 没有注册 Motions/Expressions
    （全靠 VTube Studio 热键驱动）。本服务在回传 model3.json 时动态注入
    这两段（原模型文件一字不动），让官方 ExpressionManager / MotionManager
    直接接管表情与待机动作。

如果 PyQtWebEngine 未安装，本类直接 ImportError，PetWindow 会 fallback 到
SpriteRenderer。
"""
from __future__ import annotations

import json
import logging
import mimetypes
import random
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from app.animation.live2d_model_profile import (
    ActionSpec,
    Live2DModelProfile,
    load_model_profile,
)
from app.animation.pet_renderer import PetRenderer
from app.core.qt_compat import QObject, QTimer, QUrl

# PR-fix-WebEngine-OpenGL: 在 import QWebEngineView 之前设置 AA_ShareOpenGLContexts，
# 否则 PyQtWebEngine 会在模块加载时初始化 OpenGL plugin 并报错
from PyQt5.QtCore import Qt, QCoreApplication, pyqtSlot as Slot
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)

log = logging.getLogger(__name__)

# 资源目录（bridge.html 与本文件同目录；cubism-sdk 在其下）
_HERE = Path(__file__).resolve().parent
_BRIDGE_HTML = _HERE / "live2d_bridge.html"
_SDK_DIR = _HERE / "cubism-sdk"

# 补全 Python 可能缺失的 MIME 映射（影响 QWebEngine 对脚本的解析）
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("application/json", ".json")
mimetypes.add_type("application/octet-stream", ".moc3")
mimetypes.add_type("application/octet-stream", ".vbridger")

# 可选 Qt 依赖：失败就抛 ImportError，让 PetWindow 知道要走 sprite fallback
try:
    from PyQt5.QtWebEngineWidgets import QWebEngineView, QWebEnginePage
    try:
        from PyQt5.QtWebChannel import QWebChannel  # PyQt5 5.15.4+
    except ImportError:
        from PyQt5.QtWebEngineChannel import QWebChannel  # 部分打包版本
    _WEB_ENGINE_AVAILABLE = True
except ImportError as _e:  # pragma: no cover
    log.warning("PyQtWebEngine 未安装：Live2D 不可用，PetWindow 将用 sprite fallback")
    _WEB_ENGINE_AVAILABLE = False
    _WEB_ENGINE_IMPORT_ERROR = _e

# 玩一下/双击等旧接口动作名 → 中文标签（内联动作没有条目名可显示时用）
_LEGACY_ACTION_LABELS = {
    'jump': '起跳', 'stretch': '伸懒腰', 'spin': '转圈圈', 'swim': '游泳',
    'eat': '吃饭', 'file': '吃文件', 'think': '思考', 'tongue': '吐舌头',
    'cheek': '鼓腮帮',
}
# 玩一下菜单默认展示的动作（模型 profile 未声明 quick_play 时）
_DEFAULT_QUICK_PLAY = ['jump', 'eat', 'spin', 'stretch']

# 随机表情一次展示时长（ms）
_RANDOM_HOLD_MS = 4000


class _BridgeProxy(QObject):
    """Python 对象暴露给 JS（QWebChannel）。

    JS 端通过 channel.objects.petBridge 访问。
    """

    def __init__(self, renderer: "Live2DRenderer"):
        super().__init__()
        self._renderer = renderer

    @Slot(str)
    def onReady(self, expressions_json: str) -> None:
        self._renderer._on_model_ready(expressions_json)

    @Slot(str)
    def onError(self, error: str) -> None:
        self._renderer._on_model_error(error)


class _ConsolePage(QWebEnginePage):
    """把 JS console 消息转发到 Python logging。"""

    def javaScriptConsoleMessage(self, level, message, lineNumber, sourceId):
        src = Path(str(sourceId)).name
        # JS 的常规 INFO（模型加载进度/Cubism 启动横幅）是纯噪音 → DEBUG；
        # WARN/ERROR 保留（真问题时才有日志可看）
        if level == QWebEnginePage.InfoMessageLevel:
            log.debug("[Live2D JS INFO] %s (%s:%s)", message, src, lineNumber)
        elif level == QWebEnginePage.WarningMessageLevel:
            log.warning("[Live2D JS WARN] %s (%s:%s)", message, src, lineNumber)
        else:
            log.error("[Live2D JS ERROR] %s (%s:%s)", message, src, lineNumber)


def _motion_file(model_dir: Path, motions_cfg: dict, key: str) -> Optional[str]:
    """profile 里声明的动作文件是否存在（存在返回相对路径）。"""
    rel = str(motions_cfg.get(key) or "")
    if rel and (model_dir / rel).is_file():
        return rel
    return None


def _build_settings_payload(model_dir: Path, settings_name: str,
                            profile: Live2DModelProfile) -> bytes:
    """读取 model3.json 并动态注入 Motions / Expressions（原文件不动）。

    很多官方热键包模型（冰糖/超频猫猫）model3.json 极简：表情全靠 VTube
    Studio 热键表驱动、动作文件躺在目录里没注册。注入后官方
    ExpressionManager / MotionManager 直接接管（待机动作自动循环）。
    """
    try:
        settings = json.loads(
            (model_dir / settings_name).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        log.warning("Live2D: 读取 model3.json 失败，跳过注入: %s", e)
        return b""

    fr = settings.setdefault("FileReferences", {})

    # ---- Expressions 注入 ----
    expr_defs = profile.expression_defs()
    if expr_defs and not fr.get("Expressions"):
        fr["Expressions"] = expr_defs
        log.info("Live2D: 注入 %d 个表情注册", len(expr_defs))

    # ---- Motions 注入 ----
    if not fr.get("Motions"):
        motions: dict[str, list[dict]] = {}
        idle_file = _motion_file(model_dir, profile.motions_cfg, "idle_file")
        if profile.idle_motion_group and idle_file:
            motions[profile.idle_motion_group] = [{"File": idle_file}]
        sleep_file = _motion_file(model_dir, profile.motions_cfg, "sleep_file")
        if profile.sleep_motion_group and sleep_file:
            motions[profile.sleep_motion_group] = [{"File": sleep_file}]
        if motions:
            fr["Motions"] = motions
            log.info("Live2D: 注入动作组 %s",
                     {k: v[0]["File"] for k, v in motions.items()})

    return json.dumps(settings, ensure_ascii=False).encode("utf-8")


def _make_handler(model_dir: Path, settings_name: str, settings_payload: bytes):
    """生成一个本地 HTTP handler：同时提供 bridge.html / SDK / 模型（同源）。"""
    model_dir = model_dir.resolve()
    sdk_dir = _SDK_DIR.resolve()
    bridge = _BRIDGE_HTML.resolve()
    allowed_roots = [str(model_dir), str(sdk_dir), str(bridge.parent)]

    def _safe_resolve(base: Path, rel: str) -> Optional[Path]:
        """安全拼接路径，防 ../../ 穿越。"""
        try:
            target = (base / rel).resolve()
        except Exception:  # noqa: BLE001
            return None
        if not any(str(target).startswith(root) for root in allowed_roots):
            return None
        return target

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send_file(self, fpath: Path):
            if not fpath.is_file():
                self.send_error(404, f"Not found: {fpath.name}")
                return
            try:
                data = fpath.read_bytes()
            except OSError:
                self.send_error(500)
                return
            ctype = mimetypes.guess_type(str(fpath))[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def _send_bytes(self, data: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        def _route(self, path: str):
            if path in ("/", "/bridge.html"):
                return bridge
            if path.startswith("/cubism-sdk/"):
                rel = path[len("/cubism-sdk/"):]
                return _safe_resolve(sdk_dir, rel)
            if path.startswith("/model/"):
                rel = path[len("/model/"):]
                if rel == settings_name:
                    return "__payload__"      # 注入后的 model3.json
                return _safe_resolve(model_dir, rel)
            return None

        def do_GET(self):  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            path = urllib.parse.unquote(parsed.path)

            if path == "/index.json":
                payload = json.dumps({
                    "model_dir": str(model_dir),
                    "settings_json": settings_name,
                }).encode("utf-8")
                self._send_bytes(payload, "application/json; charset=utf-8")
                return

            fpath = self._route(path)
            if fpath == "__payload__":
                if settings_payload:
                    self._send_bytes(settings_payload, "application/json; charset=utf-8")
                else:
                    self.send_error(404, "settings payload unavailable")
                return
            if fpath is None:
                self.send_error(404, f"Unknown path: {path}")
                return
            self._send_file(fpath)

        def do_OPTIONS(self):  # noqa: N802
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.end_headers()

        def log_message(self, fmt, *args):  # noqa: A002
            log.debug("[Live2D HTTP] %s", fmt % args)

    return _Handler


class Live2DRenderer(PetRenderer):
    """PetRenderer 的 Live2D 实现（QWebEngineView + Cubism Web SDK）。"""

    def __init__(self, model_dir: str | Path, widget_size: tuple[int, int] = (800, 800),
                 scale: float = 0.5, hide_watermark: bool = True,
                 random_exp_cfg: Optional[dict] = None,
                 max_fps: int = 30):
        if not _WEB_ENGINE_AVAILABLE:
            raise ImportError(
                "Live2DRenderer 需要 PyQtWebEngine。"
                "请 `pip install PyQtWebEngine` 后重试。"
            )

        self.model_dir = Path(model_dir)
        self.widget_size = widget_size
        self.scale = scale
        self.hide_watermark = hide_watermark
        # 渲染帧率上限（性能）：桌宠待机 30fps 足够顺滑
        self.max_fps = max(5, int(max_fps))
        self._ready = False
        self._sleeping = False
        self._thinking = False
        self._expressions: list[str] = []
        self._error: Optional[str] = None
        self._page = None
        self._httpd = None
        self._current_emotion: str = "natural"
        # 手动叠加的 toggle 条目（配件/手势/特殊），记录激活项便于再点取消
        self._active_toggles: dict[str, list[str]] = {}   # item 名 -> 参数列表
        # 思考期间叠加的手势条目（set_idle 恢复时收起）
        self._overlay_items: list[str] = []

        # 找到 model3.json（不硬编码文件名）
        model3 = sorted(self.model_dir.glob("*.model3.json"))
        if not model3:
            raise RuntimeError(f"model_dir 没有 *.model3.json: {self.model_dir}")
        self._settings_name = model3[0].name

        # 模型专属映射配置（每模型一份 *.model.yaml，无 YAML 时自动启发式解析）
        self.profile = load_model_profile(self.model_dir, random_exp_cfg)
        self._settings_payload = _build_settings_payload(
            self.model_dir, self._settings_name, self.profile)

        # 本地同源 HTTP 服务：bridge.html / SDK / 模型都从这里走
        handler = _make_handler(self.model_dir, self._settings_name,
                                self._settings_payload)
        # 静默连接重置：WebView 取消资源加载时 socket 会被强行断开，
        # socketserver 默认把 ConnectionResetError 整段 traceback 打到
        # stderr（纯噪音，静默启动时甚至可能触发写已关闭管道）
        class _QuietHTTPServer(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                import sys
                exc = sys.exc_info()[1]
                if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                                    BrokenPipeError, TimeoutError)):
                    log.debug("[Live2D HTTP] 连接中断 %s: %s", client_address, exc)
                    return
                log.warning("[Live2D HTTP] 请求处理异常 %s: %r",
                            client_address, exc)

        self._httpd = _QuietHTTPServer(("127.0.0.1", 0), handler)
        self._httpd.daemon_threads = True
        self._http_port = self._httpd.server_address[1]
        self._http_base = f"http://127.0.0.1:{self._http_port}"
        self._http_thread = threading.Thread(
            target=self._httpd.serve_forever, name="live2d-http", daemon=True)
        self._http_thread.start()
        log.info("Live2D HTTP server: %s serving %s", self._http_base, self.model_dir)

        # 创建 WebView
        self.view = QWebEngineView()
        self.view.setFixedSize(*widget_size)
        self.view.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # PR-fix-drag-menu: 让 QWebEngineView 不吞鼠标事件。
        # 1) WA_TransparentForMouseEvents=False 显式声明（虽然默认值就是 False，但明确写出来更稳）
        # 2) NoContextMenu 策略：不让 WebView 弹自己的右键菜单，让 PetWindow 的 _show_context_menu 处理
        self.view.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)

        # 自定义 Page：透明背景 + console 转发
        self._page = _ConsolePage(self.view)
        self.view.setPage(self._page)
        from PyQt5.QtGui import QColor
        self._page.setBackgroundColor(QColor(0, 0, 0, 0))

        # 渲染进程崩溃诊断
        try:
            self.view.renderProcessTerminated.connect(
                lambda status, code: log.error(
                    "Live2D WebEngine 渲染进程终止 status=%s code=%s", status, code))
        except Exception:  # noqa: BLE001
            pass

        # QWebChannel 桥
        self._channel = QWebChannel()
        self._proxy = _BridgeProxy(self)
        self._channel.registerObject("petBridge", self._proxy)
        self._page.setWebChannel(self._channel)

        # 从同源 HTTP 加载 bridge（不再用 file://）
        self._load_attempted = False
        self._page.loadFinished.connect(self._on_page_loaded)
        self.view.setUrl(QUrl(f"{self._http_base}/bridge.html"))

        # 挂机随机表情定时器（模型 ready 后才真正开始触发）
        self._random_enabled = bool(self.profile.random_enabled)
        self._random_busy = False
        self._random_timer = QTimer()
        self._random_timer.setSingleShot(True)
        self._random_timer.timeout.connect(self._on_random_timeout)

    # ----- 生命周期 -----
    def _on_page_loaded(self, ok: bool) -> None:
        """WebView 页面加载完成 → 触发 JS loadModel。"""
        if not ok or self._load_attempted:
            return
        self._load_attempted = True
        QTimer.singleShot(400, self._js_load_model)

    def _js_load_model(self) -> None:
        fit = 0.9  # 模型在画布中的占比（上下留边，避免头顶/脚底贴到窗口边缘）
        # drawable 指纹拦截只对声明了 watermark.mode=drawable 的模型（冰糖）启用；
        # 其它模型（如超频猫猫的水印=Key1 参数开关）走参数层，避免误伤部件。
        hide_wm = "true" if (self.hide_watermark and
                             self.profile.watermark_mode == "drawable") else "false"
        js = (
            f"window.live2d.loadModel("
            f"{json.dumps(self._http_base)}, "
            f"{json.dumps(self._settings_name)}, {fit}, {hide_wm}, "
            f"{int(self.max_fps)});"
        )
        self._page.runJavaScript(js)
        QTimer.singleShot(10000, self._check_ready_timeout)

    def pause(self) -> None:
        """暂停渲染循环（桌宠隐藏时零 GPU/CPU 开销）。"""
        self._js("window.live2d.pause();")

    def resume(self) -> None:
        """恢复渲染循环（桌宠重新显示）。"""
        self._js("window.live2d.resume();")

    def _check_ready_timeout(self) -> None:
        if not self._ready and not self._error:
            log.error("Live2D 模型 10 秒内未 ready（资源加载失败，见上方 [Live2D JS] 日志）")

    def _on_model_ready(self, expressions_json: str) -> None:
        self._ready = True
        try:
            self._expressions = json.loads(expressions_json)
        except Exception:  # noqa: BLE001
            self._expressions = []
        # 同步一次画布尺寸（防止页面加载时 view 尚未布局导致 fit 到错误尺寸）
        w, h = self.widget_size
        self._js(f"window.live2d.resize({w}, {h});")
        log.info("Live2D 模型加载成功: %d 个表情", len(self._expressions))

        # 水印处理（按 profile 模式）：
        # * drawable：loadModel(hideWm=true) 已在桥端按指纹拦截图层（冰糖）
        # * part：把水印部件透明度置 0（超频猫猫 Part14 星雾语企划水印）
        # * param：保证水印开关参数处于安全值
        if self.hide_watermark:
            if self.profile.watermark_mode == "part" and self.profile.watermark_part_ids:
                self._js("window.live2d.hideParts("
                         f"{json.dumps(self.profile.watermark_part_ids)}, 0);")
            elif self.profile.watermark_mode == "param" \
                    and self.profile.watermark_param:
                self._apply_params([], {self.profile.watermark_param:
                                        self.profile.watermark_safe_value})
        # 隐藏装饰部件（如超频猫猫 Part10 粉色翅膀/星星背景框）
        if self.profile.hide_parts:
            self._js("window.live2d.hideParts("
                     f"{json.dumps(self.profile.hide_parts)}, 0);")

        # 启动挂机随机表情心跳
        if self._random_enabled:
            self._arm_random_timer()

    def _on_model_error(self, error: str) -> None:
        self._ready = False
        self._error = error
        log.error("Live2D 模型加载失败: %s", error)

    def _js(self, code: str) -> None:
        """通过 WebView 执行 JS。

        统一加 ``window.live2d`` 守卫：PetWindow 构造时会立即调 set_idle，
        此刻页面/桥脚本可能还没加载完（window.live2d 为 undefined），
        不加守卫会在 console 抛 TypeError。
        """
        if self._page is not None:
            self._page.runJavaScript(f"if(window.live2d){{{code}}}")

    def is_ready(self) -> bool:
        return self._ready

    # ---------- 参数 / 条目播放底层 ----------
    def _apply_params(self, reset_ids: list[str], param_map: dict[str, float]) -> None:
        """直接写一组 core 参数（先复位 reset_ids，再设置 param_map），持久、不被表情切换撤销。"""
        js = (
            f"window.live2d.applyParams({json.dumps(reset_ids)}, "
            f"{json.dumps(param_map)});"
        )
        self._js(js)

    def _play_item_expr(self, item, hold_ms: Optional[int] = None,
                        fade_in: float = 0.3, fade_out: float = 0.4) -> None:
        """把一个分类条目作为表情播放（ExpressionManager，切表情自动替换）。"""
        params = [{"Id": pid, "Value": val, "Blend": "Add"}
                  for pid, val in item.params.items()]
        if not params:
            return
        self._play_custom_expr(f"__item_{item.name}", params, hold_ms=hold_ms,
                               fade_in=fade_in, fade_out=fade_out)

    def _play_custom_expr(self, slot: str, params: list[dict],
                          hold_ms: Optional[int] = None,
                          fade_in: float = 0.12, fade_out: float = 0.3) -> None:
        """播放自定义表情（模型支持但没注册成 exp3 文件的参数）。

        slot 是唯一槽位名（同名只注册一次并复用）；params 为
        [{Id, Value, Blend}]。hold_ms 不为 None 时，到时自动恢复当前情绪；
        为 None 时持续显示，由 set_idle / _restore_emotion 恢复。
        """
        js = (
            f"window.live2d.playCustomExpression("
            f"{json.dumps(slot)}, {json.dumps(params)}, {fade_in}, {fade_out});"
        )
        self._js(js)
        if hold_ms is not None:
            QTimer.singleShot(int(hold_ms), self._restore_emotion_if_awake)

    def _restore_emotion_if_awake(self) -> None:
        """一次性表情结束后恢复当前情绪（睡觉中不打断）。"""
        if not self._sleeping:
            self._restore_emotion()

    def _restore_emotion(self) -> None:
        """恢复当前选定的情绪（一次性动作 / 思考 / 触摸反应结束后调用）。"""
        if self._current_emotion and self._current_emotion != "natural":
            cat = self.profile.emotion_category()
            item = cat.item(self._current_emotion) if cat else None
            if item is not None:
                self._play_item_expr(item)
                return
        self._js("window.live2d.resetExpressionState();")

    def _resolve_emotion(self, name: str) -> Optional[str]:
        """把通用情绪名 / 模型表情名解析为模型表情名；自然 / 无法解析返回 None。"""
        if name in (None, "", "natural", "default", "none"):
            return None
        cat = self.profile.emotion_category()
        if cat and cat.item(name) is not None:
            return name
        target = self.profile.emotion_aliases.get(str(name).lower())
        if target and self.profile.find_item(target) is not None:
            return target
        return None

    # ---------- 挂机随机表情 ----------
    def _arm_random_timer(self) -> None:
        lo = max(1, self.profile.random_min_s)
        delay = random.randint(lo, max(lo, self.profile.random_max_s)) * 1000
        self._random_timer.start(delay)

    def _on_random_timeout(self) -> None:
        if self._random_enabled and self._ready:
            if not self._sleeping and not self._thinking and not self._random_busy:
                pool = self.profile.random_pool_items()
                if pool:
                    name = random.choice(pool)
                    # 避免和上一次相同（不重样才显得"在变"）
                    if name == getattr(self, "_last_random", None) and len(pool) > 1:
                        name = random.choice([p for p in pool if p != name])
                    self._last_random = name
                    item = self.profile.find_item(name)
                    if item is not None:
                        cat = self.profile.category(item.category)
                        log.debug("Live2D 随机播放: %s", name)
                        if cat is not None and cat.emotion:
                            # 表情：展示几秒后恢复当前情绪
                            self._random_busy = True
                            self._play_item_expr(item, hold_ms=_RANDOM_HOLD_MS)
                            QTimer.singleShot(
                                _RANDOM_HOLD_MS + 500,
                                lambda: setattr(self, "_random_busy", False))
                        else:
                            # 手势/动作：持久生效（保持到下次随机/手动切换）
                            self.activate_menu_item(item.category, item.name,
                                                    toggle_off=False)
                            # 概率叠加一个随机表情（姿势+表情同时变化，更生动）
                            faces = [n for n in pool
                                     if (it := self.profile.find_item(n)) is not None
                                     and (c := self.profile.category(it.category))
                                     is not None and c.emotion]
                            if faces and random.random() < 0.5:
                                face = self.profile.find_item(random.choice(faces))
                                if face is not None:
                                    self._play_item_expr(
                                        face, hold_ms=_RANDOM_HOLD_MS + 1500)
            self._arm_random_timer()

    def note_activity(self) -> None:
        """外部交互（触摸/拖拽/聊天）后重置随机表情计时（有互动就不挂机）。"""
        if self._random_enabled and self._random_timer.isActive():
            self._arm_random_timer()

    def set_random_expressions(self, enabled: bool) -> None:
        """开关挂机随机表情（右键菜单 / 设置页）。"""
        self._random_enabled = bool(enabled)
        if enabled:
            self._arm_random_timer()
        else:
            self._random_timer.stop()

    def set_random_interval(self, min_s: int, max_s: int) -> None:
        """调整挂机随机的触发间隔（秒），立即重新计时。"""
        self.profile.random_min_s = max(5, int(min_s))
        self.profile.random_max_s = max(self.profile.random_min_s, int(max_s))
        self.note_activity()

    def set_max_fps(self, fps: int) -> None:
        """运行时调整渲染帧率上限。"""
        self.max_fps = max(5, int(fps))
        self._js(f"if(window.live2d){{window.live2d.setMaxFps({self.max_fps});}}")

    def is_random_expressions_enabled(self) -> bool:
        return self._random_enabled

    # ---------- 表情包贴纸（桌宠右上角随机弹出） ----------
    def get_sticker_config(self) -> Optional[dict]:
        """模型表情包贴纸配置；无配置或目录不存在返回 None。

        返回 {"files": [绝对路径], "min_s", "max_s", "duration_s", "size"}。
        """
        if not self.profile.stickers_dir:
            return None
        d = Path(self.profile.stickers_dir)
        if not d.is_absolute():
            d = self.model_dir / self.profile.stickers_dir
        if not d.is_dir():
            log.info("Live2D 表情包目录不存在: %s", d)
            return None
        files = sorted(
            p for p in d.iterdir()
            if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"))
        if not files:
            return None
        return {
            "files": [str(p) for p in files],
            "min_s": self.profile.stickers_min_s,
            "max_s": self.profile.stickers_max_s,
            "duration_s": self.profile.stickers_duration_s,
            "size": self.profile.stickers_size,
        }

    # ---------- PetRenderer 接口实现 ----------
    def set_idle(self) -> None:
        """待机：头身姿态回正、临时姿态参数复位，并恢复当前情绪（发型保持）。

        走路/拖拽/一次性动作会留下角度偏移，回待机时平滑回正；
        表情类临时效果（吃饭/思考等）通过 ExpressionManager 切回当前情绪恢复；
        用户主动选择的情绪与发型不会被覆盖。
        """
        for p in self.profile.pose_angle_params:
            self._js(f"window.live2d.setParam({json.dumps(p)}, 0.0, 400);")
        # 收起思考等叠加的手势条目
        if self._overlay_items:
            overlay, self._overlay_items = self._overlay_items, []
            for name in overlay:
                item = self.profile.find_item(name)
                if item is not None:
                    self._apply_params(list(item.params.keys()), {})
        if not self._sleeping:
            self._restore_emotion()

    def _idle_if_awake(self) -> None:
        """一次性动作结束后回待机（睡觉中不打断）。"""
        if not self._sleeping:
            self.set_idle()

    def is_sleeping(self) -> bool:
        """是否在睡觉（主程序 state.tick 据此跳过衰减、自主运动据此暂停）。"""
        return self._sleeping

    def set_sleep(self) -> None:
        self._sleeping = True
        # 回正头身（角度参数仍可用 setParam 平滑）
        for p in self.profile.pose_angle_params:
            self._js(f"window.live2d.setParam({json.dumps(p)}, 0.0, 400);")
        # 模型自带睡眠开关参数（如超频猫猫 Param216/218）
        if self.profile.sleep_params:
            self._apply_params([], self.profile.sleep_params)
        # 内联睡眠表情（冰糖闭眼合嘴）
        if self.profile.sleep_expr_params:
            self._play_custom_expr("__pet_sleep", self.profile.sleep_expr_params,
                                   hold_ms=None, fade_in=0.4, fade_out=0.3)
        # 睡眠动作（Zzz）：先停掉待机动作，避免动作每帧覆写睡眠参数
        if self.profile.sleep_motion_group:
            self._js("window.live2d.stopMotions();")
            self._js(f"window.live2d.playMotion("
                     f"{json.dumps(self.profile.sleep_motion_group)});")

    def set_wake(self) -> None:
        self._sleeping = False
        if self.profile.wake_params:
            self._apply_params([], self.profile.wake_params)
        # 待机动作通常由 MotionManager 自动恢复，这里显式补一刀更稳
        if self.profile.idle_motion_group:
            self._js(f"window.live2d.playMotion("
                     f"{json.dumps(self.profile.idle_motion_group)});")
        # set_idle 会把 ExpressionManager 切回当前情绪（自然睁眼），角度回正
        self.set_idle()

    def set_thinking(self) -> None:
        """AI 思考中：持续思考表情 + 可选叠加手势（回复后 set_idle 恢复）。"""
        self._thinking = True
        if self.profile.thinking_expr:
            item = self.profile.find_item(self.profile.thinking_expr)
            if item is not None:
                self._play_item_expr(item)
        elif self.profile.thinking_params:
            self._play_custom_expr("__pet_think", self.profile.thinking_params,
                                   hold_ms=None)
        for name in self.profile.thinking_items:
            item = self.profile.find_item(name)
            if item is not None and name not in self._overlay_items:
                self._overlay_items.append(name)
                self._apply_params([], item.params)

    def play_reaction(self, where: str) -> None:
        """触摸反应：短暂表情/微笑 + 歪头，随后回到当前情绪。"""
        self.note_activity()
        tilt = 0.3 if where == 'head' else 0.15
        if self.profile.touch_expr:
            item = self.profile.find_item(self.profile.touch_expr)
            if item is not None:
                self._play_item_expr(item, hold_ms=self.profile.touch_hold_ms)
        elif self.profile.touch_params:
            self._play_custom_expr("__pet_touch", self.profile.touch_params,
                                   hold_ms=self.profile.touch_hold_ms)
        self._js(f"window.live2d.setParam('ParamAngleX', {tilt}, 150);")
        QTimer.singleShot(self.profile.touch_hold_ms, self._restore_emotion_if_awake)

    def play_animation(self, anim_name: str) -> None:
        """一次性动作：按 profile.actions 映射（引用条目 / 内联表情 / 姿态插值）。"""
        self.note_activity()
        spec = self.profile.actions.get(anim_name.lower())
        if spec is None:
            log.warning("Live2D: 未知动作 '%s'（模型 %s 未配置）",
                        anim_name, self.profile.name)
            return
        self._run_action_spec(spec, slot=f"__pet_{anim_name.lower()}")

    def _run_action_spec(self, spec: ActionSpec, slot: str) -> None:
        dur = int(spec.duration_ms or 800)
        if spec.kind == "item":
            item = self.profile.find_item(spec.item)
            if item is None:
                log.warning("Live2D: 动作引用的条目不存在: %s", spec.item)
                return
            # 持久生效：动作/表情保持到下一次切换（聊天情绪/触发规则自动换、
            # 或菜单手动换），而不是播一下就还原成呆立。待机动作在其下继续循环。
            self.activate_menu_item(item.category, item.name, toggle_off=False)
            return
        if spec.kind == "expr":
            self._play_custom_expr(slot, spec.params, hold_ms=dur + 250)
            return
        # 姿态类：setParam 平滑插值，结束回待机（角度回正）
        if spec.param:
            self._js(
                f"window.live2d.setParam({json.dumps(spec.param)}, "
                f"{spec.value}, {dur});"
            )
        QTimer.singleShot(dur + 300, self._idle_if_awake)

    def play_eat(self) -> None:
        self.play_animation('eat')

    def play_file(self) -> None:
        self.play_animation('file')

    def play_spin(self) -> None:
        self.play_animation('spin')

    def play_stretch(self) -> None:
        self.play_animation('stretch')

    def play_jump(self) -> None:
        self.play_animation('jump')

    def play_swim(self) -> None:
        self.play_animation('swim')

    def start_drag(self) -> None:
        self.note_activity()
        self._js("window.live2d.setParam('ParamAngleX', 0.5, 200);")
        self._js("window.live2d.setParam('ParamAngleY', -0.3, 200);")

    def end_drag(self) -> None:
        self.set_idle()

    def set_walk(self, direction: str) -> None:
        if direction == 'left':
            self._js("window.live2d.setParam('ParamAngleY', 1.0, 300);")
        elif direction == 'right':
            self._js("window.live2d.setParam('ParamAngleY', -1.0, 300);")

    def set_crawl(self, direction: str) -> None:
        """模型无爬行 motion，退化为走路（转头朝向移动方向）。"""
        self.set_walk(direction)

    def set_edge_hide(self, direction: str) -> None:
        """模型无边缘躲藏 motion，退化为待机。"""
        self.set_idle()

    def set_parameter(self, name: str, value: float, duration_ms: int = 0) -> None:
        self._js(
            f"window.live2d.setParam({json.dumps(name)}, {value}, {duration_ms});"
        )

    def look_at(self, dx: float, dy: float) -> None:
        """头部/眼睛看向某个方向（dx/dy ∈ [-1,1]，相对画面中心的归一化偏移）。

        持续调用（如鼠标跟踪定时器）会驱动物理链（头发/手臂摆动），
        是 Live2D 角色"生动"的主要输入。
        """
        dx = max(-1.0, min(1.0, float(dx)))
        dy = max(-1.0, min(1.0, float(dy)))
        self._js(f"window.live2d.focus({dx:.3f}, {dy:.3f});")

    def set_expression(self, name: str) -> None:
        """兼容旧接口：已知表情走条目外观，未知名字交给 ExpressionManager。"""
        key = self._resolve_emotion(name)
        if key:
            self.set_emotion(key)
        else:
            self._js(f"window.live2d.setExpression({json.dumps(name)});")

    def get_widget(self):
        return self.view

    def set_size(self, w: int, h: int) -> None:
        """调整渲染区尺寸（设置面板改角色大小时），模型按新画布重新 fit。"""
        self.widget_size = (int(w), int(h))
        if self.view is not None:
            self.view.setFixedSize(*self.widget_size)
        # 页面未 ready 时只记尺寸，_on_model_ready 会按 widget_size resize
        self._js(f"window.live2d.resize({int(w)}, {int(h)});")

    def shutdown(self) -> None:
        self._random_timer.stop()
        self._js("window.live2d.shutdown();")
        if getattr(self, "_httpd", None) is not None:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
                self._http_thread.join(timeout=2)
            except Exception:  # noqa: BLE001
                log.exception("关闭 Live2D HTTP server 失败")
            self._httpd = None
        if self.view:
            self.view.stop()
            self.view.deleteLater()
            self.view = None
        self._page = None

    def supports_expression_listing(self) -> bool:
        return True

    def list_expressions(self) -> list[str]:
        return list(self._expressions)

    # ---------- 情绪表情 ----------
    def get_emotion_options(self) -> list[tuple[str, str]]:
        """情绪菜单：自然表情 + 模型情绪组条目。"""
        options = self.profile.emotion_options()
        if len(options) <= 1:
            return super().get_emotion_options()
        return options

    def set_emotion(self, name: str) -> None:
        """切换情绪（走 ExpressionManager，与发型独立共存）；'natural' 恢复自然。"""
        self.note_activity()
        if name in (None, "", "natural", "default", "none"):
            self.reset_emotion()
            return
        key = self._resolve_emotion(name)
        item = self.profile.find_item(key) if key else None
        if item is not None:
            self._current_emotion = item.name
            self._play_item_expr(item)
        else:
            # 兜底：模型注册了但 profile 没覆盖的名字，直接交给 ExpressionManager
            self._current_emotion = "natural"
            self._js(f"window.live2d.setExpression({json.dumps(name)});")

    def reset_emotion(self) -> None:
        """恢复自然表情（ExpressionManager 回到空表情），发型保持不变。"""
        self._current_emotion = "natural"
        self._js("window.live2d.resetExpressionState();")

    # ---------- 分类外观（右键菜单 / 设置页驱动） ----------
    def get_menu_groups(self) -> list[dict]:
        """Live2D 分类子菜单数据：五大类条目（菜单据此动态生成）。

        返回 [{"id","label","mode","items":[(item_id,label)]}, ...]；
        sprite 渲染器返回空列表（PetRenderer 默认实现），菜单不渲染这部分。
        """
        groups: list[dict] = []
        for cat in self.profile.categories:
            items: list[tuple[str, str]] = []
            if cat.hairstyle:
                items.append(("__default__", "默认发型"))
            items.extend((it.name, it.label) for it in cat.items)
            if items:
                kind = "emotion" if cat.emotion else ("hairstyle" if cat.hairstyle
                                                      else "category")
                groups.append({"id": cat.id, "label": cat.label, "kind": kind,
                               "mode": cat.mode, "items": items})
        return groups

    def activate_menu_item(self, group_id: str, item_id: str,
                           toggle_off: bool = True) -> None:
        """菜单/设置页点击一个分类条目。

        toggle_off=False 时若条目已激活则保持激活（动作播放路径用：
        「保持到下次切换」而不是再点一下取消）。
        """
        cat = self.profile.category(group_id)
        if cat is None:
            return
        self.note_activity()
        if item_id == "__default__":
            # 组复位：情绪组回自然表情（走 ExpressionManager），其余整组参数归零（发型默认）
            if cat.emotion:
                self.reset_emotion()
            else:
                self._apply_params(cat.params(), {})
            return
        item = cat.item(item_id)
        if item is None:
            return
        if cat.emotion:
            self.set_emotion(item_id)      # 情绪条目：持久并参与恢复
            return
        if cat.mode == "exclusive":
            self._apply_params(cat.params(), item.params)
            return
        # toggle 组：再点一次取消（toggle_off=False 时保持）
        if item_id in self._active_toggles and toggle_off:
            self._apply_params(self._active_toggles.pop(item_id), {})
        else:
            if not toggle_off:
                # 动作播放路径：同分类只保留最新一个手势（避免叠加出奇怪姿势）
                for name in [n for n in self._active_toggles
                             if n != item_id and cat.item(n) is not None]:
                    self._apply_params(self._active_toggles.pop(name), {})
            self._active_toggles[item_id] = list(item.params.keys())
            self._apply_params([], item.params)

    def reset_all_appearance(self) -> None:
        """复位全部外观：所有分类参数归零 + 表情回自然（相当于 VTS 的 F6）。"""
        self._active_toggles.clear()
        self._overlay_items.clear()
        self._current_emotion = "natural"
        self._apply_params(self.profile.all_item_params(), {})
        if self.hide_watermark:
            if self.profile.watermark_mode == "part" and self.profile.watermark_part_ids:
                self._js("window.live2d.hideParts("
                         f"{json.dumps(self.profile.watermark_part_ids)}, 0);")
            elif self.profile.watermark_mode == "param" \
                    and self.profile.watermark_param:
                self._apply_params([], {self.profile.watermark_param:
                                        self.profile.watermark_safe_value})
        self._js("window.live2d.resetExpressionState();")

    def get_active_items(self) -> set:
        """当前激活的分类条目名集合（供设置面板 chip 初始化选中态）。

        包含 toggle 组（配件/手势/特殊）叠加项 + 当前情绪；发型为互斥组、
        不做持久选中追踪（与右键菜单一致）。
        """
        active = set(self._active_toggles.keys())
        if self._current_emotion and self._current_emotion != "natural":
            active.add(self._current_emotion)
        return active

    # ---------- 玩一下（一次性动作菜单） ----------
    def get_play_options(self) -> list[tuple[str, str]]:
        """玩一下菜单数据：[(动作名, 标签)]（profile 已配置的动作）。"""
        names = list(_DEFAULT_QUICK_PLAY)
        names.extend(k for k in self.profile.actions if k not in _DEFAULT_QUICK_PLAY)
        out: list[tuple[str, str]] = []
        for name in names:
            spec = self.profile.actions.get(name)
            if spec is None:
                continue
            if spec.kind == "item":
                item = self.profile.find_item(spec.item)
                label = item.label if item else spec.item
            else:
                label = _LEGACY_ACTION_LABELS.get(name, name)
            out.append((name, label))
        return out

    # ---------- 兼容旧接口（发型） ----------
    def supports_hairstyles(self) -> bool:
        """模型是否自带可切换发型（菜单据此决定是否显示“发型”子菜单）。"""
        return self.profile.hairstyle_category() is not None

    def get_hairstyle_options(self) -> list[tuple[str, str]]:
        """发型菜单：默认发型 + 模型自带发型预设。"""
        cat = self.profile.hairstyle_category()
        if cat is None:
            return []
        out: list[tuple[str, str]] = [("default", "默认发型")]
        out.extend((it.name, it.label) for it in cat.items)
        return out

    def set_hairstyle(self, name: str) -> None:
        """切换发型预设（整组覆写参数）；'default' 全部恢复默认。"""
        cat = self.profile.hairstyle_category()
        if cat is None:
            return
        if name in (None, "", "default", "natural", "__default__"):
            self._apply_params(cat.params(), {})
            return
        item = cat.item(name)
        if item is None:
            return
        self._apply_params(cat.params(), item.params)


__all__ = ["Live2DRenderer", "_WEB_ENGINE_AVAILABLE"]
