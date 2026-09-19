"""桌面宠物主入口（启动横幅 + Ollama 自动检测 + Dashboard 一键启动）。

启动顺序：
    1. 打印彩色启动横幅（分阶段展示进度）
    2. 读 config（无则用默认）
    3. QApplication
    4. StateManager（存档 + 状态 + 提醒）
    5. TTS
    6. PetWindow（透明/置顶/拖动/边沿隐藏）
    7. MotionController（自走 + smartmove）
    8. BrainController（记忆 + 工具 + 主动行为）
    9. UIController（设置面板 + 托盘 + 聊天 + 信号连接）
    10. 主 tick（每秒推进状态）
    11. 启 main loop
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import urllib.request
from pathlib import Path

# PR-fix-WebEngine-OpenGL: 在 import Qt 任何东西之前先 setAttribute
# 否则 PyQtWebEngine 在 headless 下会警告 "Please set Qt::AA_ShareOpenGLContexts"
try:
    from PyQt5.QtCore import Qt as _Qt
    from PyQt5.QtCore import QCoreApplication as _QCA
    _QCA.setAttribute(_Qt.AA_ShareOpenGLContexts, True)
except Exception:  # noqa: BLE001
    pass

try:
    import faster_whisper  # noqa: F401
except Exception:  # noqa: BLE001
    pass


from .core.qt_compat import QApplication, QIcon, QTimer
from .core.config import load_config
from .voice.characters import apply_character, list_characters
from .ui.pet_window import PetWindow
from .animation.motion import MotionController
from .voice.voice import TTS
from .engine.state_manager import StateManager
from .brain.brain_controller import BrainController
from .ui.ui_controller import UIController

log = logging.getLogger(__name__)


# ============================================================================
#  启动横幅
# ============================================================================


class _Banner:
    """彩色启动横幅：分阶段展示「正在做什么」「结果如何」。"""

    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BLUE = "\033[94m"
    GRAY = "\033[90m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    def __init__(self, enabled: bool = True):
        self.enabled = enabled and sys.stdout.isatty()
        # Windows Terminal / 现代 PowerShell 支持 ANSI
        if os.name == "nt":
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x4
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            except Exception:
                self.enabled = False

    def _c(self, color: str, text: str) -> str:
        return f"{color}{text}{self.RESET}" if self.enabled else text

    def title(self, char_name: str) -> None:
        bar = "═" * 60
        print()
        print(self._c(self.CYAN + self.BOLD, f"╔{bar}╗"))
        print(self._c(self.CYAN + self.BOLD, f"║{'🐳 桌面宠物 · ' + char_name:^60}║"))
        print(self._c(self.CYAN + self.BOLD, f"╚{bar}╝"))
        print(self._c(self.GRAY, "  v3.0 · Multi-Agent Desktop Companion\n"))

    def section(self, title: str) -> None:
        print(self._c(self.BLUE + self.BOLD, f"▶ {title}"))

    def ok(self, label: str, detail: str = "") -> None:
        icon = self._c(self.GREEN, "✓")
        print(f"  {icon} {label}" + (self._c(self.GRAY, f"  · {detail}") if detail else ""))

    def info(self, label: str, detail: str = "") -> None:
        icon = self._c(self.BLUE, "·")
        print(f"  {icon} {label}" + (self._c(self.GRAY, f"  · {detail}") if detail else ""))

    def warn(self, label: str, detail: str = "") -> None:
        icon = self._c(self.YELLOW, "!")
        print(f"  {icon} {label}" + (self._c(self.GRAY, f"  · {detail}") if detail else ""))

    def fail(self, label: str, detail: str = "") -> None:
        icon = self._c(self.RED, "✗")
        print(f"  {icon} {label}" + (self._c(self.GRAY, f"  · {detail}") if detail else ""))

    def done(self, msg: str = "") -> None:
        if msg:
            print(self._c(self.GREEN + self.BOLD, f"\n✓ {msg}\n"))
        else:
            print()


# ============================================================================
#  Ollama 自动检测
# ============================================================================


def detect_ollama(timeout: float = 1.5) -> dict | None:
    """探测本机 Ollama 是否运行。

    Returns:
        {"base_url": "http://127.0.0.1:11434/v1", "models": [...]}
        或 None（未运行 / 未安装）
    """
    base = "http://127.0.0.1:11434"
    try:
        req = urllib.request.Request(f"{base}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            import json
            data = json.loads(resp.read().decode("utf-8"))
            models = [m.get("name", "") for m in data.get("models", [])]
            return {"base_url": f"{base}/v1", "models": models}
    except Exception:
        return None


# ============================================================================
#  解析参数
# ============================================================================


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="桌面宠物")
    p.add_argument("--config", default="config.yaml", help="配置文件路径")
    p.add_argument("--character", default=None, help="角色目录名")
    p.add_argument("--reset-state", action="store_true",
                   help="启动时忽略存档，重新开局")
    p.add_argument("--no-banner", action="store_true",
                   help="关闭启动横幅（纯日志输出）")
    p.add_argument("--with-dashboard", action="store_true",
                   help="启动时自动打开 Web Dashboard (http://127.0.0.1:8765)")
    return p.parse_args()


def _resolve_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _get_meipass() -> Path | None:
    if getattr(sys, "_MEIPASS", None):
        return Path(sys._MEIPASS)
    return None


def _find_resource_dir(root: Path, meipass: Path | None) -> Path | None:
    if meipass is not None:
        p1 = meipass / "assets"
        if p1.is_dir() and (p1 / "sprites").is_dir():
            return p1
    p2 = root / "assets"
    if p2.is_dir() and (p2 / "sprites").is_dir():
        return p2
    return None


def _get_assets_root() -> Path | None:
    return _find_resource_dir(_resolve_root(), _get_meipass())


def _ensure_config(root: Path) -> None:
    import shutil
    cfg_path = root / "config.yaml"
    if cfg_path.is_file():
        return
    src = root / "config.example.yaml"
    if src.is_file():
        shutil.copy2(src, cfg_path)
        log.info("首次运行：已复制 config.yaml → %s", cfg_path)
        return
    meipass = _get_meipass()
    if meipass is not None:
        src2 = meipass / "config.example.yaml"
        if src2.is_file():
            shutil.copy2(src2, cfg_path)
            log.info("首次运行：已复制 config.yaml（来自打包资源） → %s", cfg_path)
            return
    log.warning("config.example.yaml 不存在，将使用默认配置")


def _build_tts(cfg, root: Path) -> TTS:
    tts_cache = root / "assets" / "tts_cache"
    tts = TTS(voice=cfg.character.tts_voice or "zh-CN-XiaoxiaoNeural",
              cache_dir=tts_cache)
    tts.set_enabled(cfg.character.tts_enabled)
    return tts


# ============================================================================
#  Dashboard 进程管理
# ============================================================================


def start_dashboard_subprocess(root: Path, port: int = 8765) -> int | None:
    """后台启动 Dashboard 进程，返回 PID 或 None。"""
    import subprocess
    log_file = root / "data" / "dashboard.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.web.dashboard",
             "--port", str(port),
             "--trace-db", str(root / "data" / "traces.db"),
             "--memory-file", str(root / "data" / "memory.json")],
            cwd=str(root),
            stdout=open(log_file, "ab"),
            stderr=subprocess.STDOUT,
            creationflags=(subprocess.DETACHED_PROCESS
                           | subprocess.CREATE_NO_WINDOW
                           if os.name == "nt" else 0),
        )
        return proc.pid
    except Exception as e:  # noqa: BLE001
        log.warning("Dashboard 启动失败：%s", e)
        return None


def wait_dashboard_ready(port: int = 8765, timeout: float = 8.0) -> bool:
    """等待 Dashboard 在 :port 监听起来。"""
    import socket
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def open_in_browser(url: str) -> None:
    """打开默认浏览器到 url（Windows/macOS/Linux）。"""
    import webbrowser
    try:
        webbrowser.open(url)
    except Exception as e:  # noqa: BLE001
        log.warning("打开浏览器失败：%s", e)


# ============================================================================
#  App 主类
# ============================================================================


class App:
    """聚合所有资源 + 组装各控制器。"""

    def __init__(self, args: argparse.Namespace, banner: _Banner) -> None:
        root = _resolve_root()
        self.root = root
        self.banner = banner

        banner.section("① 加载配置")
        _ensure_config(root)

        cfg_path = Path(args.config)
        if not cfg_path.is_absolute():
            cfg_path = root / cfg_path
        cfg = load_config(cfg_path)
        if args.character:
            names = list_characters(root)
            if args.character in names:
                cfg = apply_character(cfg, args.character, root)
                banner.info(f"使用角色：{args.character}")
            else:
                banner.warn(f"--character={args.character} 不在 characters/ 下",
                            f"可用：{names}")
        self.cfg = cfg

        # LLM 配置健康度
        if cfg.has_api_key():
            banner.ok(f"LLM：{cfg.llm.model}",
                      cfg.llm.base_url[:60] + ("…" if len(cfg.llm.base_url) > 60 else ""))
        else:
            banner.warn("LLM API Key 未配置",
                        "打开设置面板填 key，或用 Ollama 本地模型")

        # 检查 Ollama
        ollama = detect_ollama()
        if ollama and ollama["models"]:
            banner.ok("检测到 Ollama", f"{len(ollama['models'])} 个模型已安装")
            # 列出常用模型（带简短说明）
            for m in ollama["models"][:5]:
                note = ""
                if "embed" in m.lower():
                    note = "  ← 可用于本地向量记忆"
                elif "r1" in m.lower():
                    note = "  ← 推理模型（带思考过程）"
                banner.info(f"  · {m}{note}")
        else:
            banner.info("未检测到 Ollama", "https://ollama.com 安装后可走本地")

        banner.section("② 加载存档 + 状态")
        self.state_mgr = StateManager(root, cfg, args)

        banner.section("③ 初始化 TTS")
        self.tts = _build_tts(cfg, root)
        if cfg.character.tts_enabled:
            banner.ok("TTS", f"voice={cfg.character.tts_voice}")
        else:
            banner.info("TTS 已关闭")

        banner.section("④ 桌宠窗口")
        self._asset_root = _get_assets_root()
        sprite_subdir = cfg.sprite.directory
        if self._asset_root is not None:
            sub = sprite_subdir
            if sub.startswith("assets/"):
                sub = sub[len("assets/"):]
            sprite_dir = self._asset_root / sub
        else:
            sprite_dir = root / sprite_subdir
        fallback_rel = cfg.sprite.fallback
        if self._asset_root is not None:
            fb_sub = fallback_rel
            if fb_sub.startswith("assets/"):
                fb_sub = fb_sub[len("assets/"):]
            fallback_image = self._asset_root / fb_sub
        else:
            fallback_image = root / fallback_rel
        self.pet = PetWindow(
            sprite_dir, fallback_image=fallback_image,
            scale=cfg.window.scale,
            always_on_top=cfg.window.always_on_top,
            # Live2D 渲染器（v3.1+）：从 cfg.pet 读取
            renderer_type=getattr(cfg.pet, "renderer", "sprite"),
            live2d_model_dir=Path(getattr(cfg.pet.live2d, "model_dir", "")) if getattr(cfg, "pet", None) and getattr(cfg.pet, "live2d", None) else None,
            live2d_hide_watermark=bool(getattr(cfg.pet.live2d, "hide_watermark", True)),
        )
        self.pet.move(cfg.window.start_x, cfg.window.start_y)
        self.pet.attach_state(self.state_mgr.state)
        # 统计动画目录
        sprite_count = sum(
            1 for p in sprite_dir.rglob("*.png")
            if not p.name.startswith("body_front"))
        if sprite_count > 0:
            banner.ok(f"动画帧", f"{sprite_count} 张")
        else:
            banner.info("未找到动画帧",
                        "用静态 fallback 显示（不影响聊天/工具）")

        banner.section("⑤ 运动控制器")
        self.motion = MotionController(
            get_window=lambda: self.pet,
            screen_geometry_getter=self._compute_screen_geometry,
            animator=self.pet.animator,
            is_user_interacting=lambda: bool(
                self.pet._user_inside or self.pet._dragging),
        )

        banner.section("⑥ 智能中枢")
        self.brain = BrainController(
            root, cfg, self.state_mgr.state, self.state_mgr.reminders,
        )
        self.brain.set_sleeping_checker(
            lambda: self.pet.animator.is_sleeping())
        tool_count = len(self.brain.tool_registry.names()) if self.brain.tool_registry else 0
        mem_count = self.brain.memory.count()
        backend_label = "LangChain 1.0+" if cfg.brain.backend == "standard" else "手写 ReAct"
        banner.ok("BrainController",
                  f"{tool_count} 个工具 · {mem_count} 条记忆 · 后端={backend_label}")
        if self.brain.proactive is not None:
            banner.ok("主动行为", "已启用（空闲时会主动关心）")

        banner.section("⑦ UI 控制器（托盘 + 设置 + 聊天）")
        self.ui = UIController(
            root, cfg, self.state_mgr, self.pet, self.tts,
            self.brain, self.motion,
        )
        # Dashboard 状态记录
        self._dashboard_pid: int | None = None

        banner.section("⑧ 启动")
        self.state_mgr.start()
        QTimer.singleShot(50, self.motion.start)

        self._state_tick = QTimer()
        self._state_tick.setInterval(1000)
        self._state_tick.timeout.connect(self._state_tick_once)
        self._state_tick.start()

        self.state_mgr.mode_changed.connect(self._on_mode_changed)
        self.brain.connect_tool_animation(self.pet)

        # 启动 Dashboard（如果指定）
        if args.with_dashboard:
            self._start_dashboard()

        banner.done(f"{cfg.character.name} 已就绪 · 双击托盘图标唤起")

    def _compute_screen_geometry(self):
        return MotionController.merged_screen_geometry()

    def _state_tick_once(self) -> None:
        self.state_mgr.state_tick_once(
            is_sleeping=self.pet.animator.is_sleeping())

    def _on_mode_changed(self, old, new) -> None:
        try:
            self.pet.animator.set_idle()
        except Exception as e:  # noqa: BLE001
            log.warning("mode 切换回调异常：%s", e)

    def _start_dashboard(self) -> bool:
        """启动 Dashboard 子进程。"""
        port = 8765
        log.info("启动 Dashboard (port=%d)…", port)
        pid = start_dashboard_subprocess(self.root, port=port)
        if pid is None:
            self.banner.fail("Dashboard 启动失败", "查看 data/dashboard.log")
            return False
        self._dashboard_pid = pid
        if wait_dashboard_ready(port=port, timeout=8.0):
            url = f"http://127.0.0.1:{port}"
            self.banner.ok("Dashboard", f"{url}  (pid={pid})")
            self.banner.info("浏览器已自动打开",
                            "也可用托盘菜单『📊 调试面板』随时打开")
            # 稍等再开浏览器，避免阻塞
            QTimer.singleShot(500, lambda: open_in_browser(url))
            return True
        else:
            self.banner.warn("Dashboard 启动超时", "查看 data/dashboard.log")
            return False

    def open_dashboard(self) -> bool:
        """给 UI 调用的『打开 Dashboard』：先看是否已启动，没启动就拉一个。"""
        port = 8765
        import socket
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                # 已启动
                open_in_browser(f"http://127.0.0.1:{port}")
                return True
        except OSError:
            pass
        # 没启动：拉一个
        return self._start_dashboard()

    # ---- 兼容旧接口的属性 ----
    @property
    def state(self):
        return self.state_mgr.state

    @property
    def reminders(self):
        return self.state_mgr.reminders

    @property
    def tool_registry(self):
        return self.brain.tool_registry

    @property
    def memory(self):
        return self.brain.memory

    @property
    def proactive(self):
        return self.brain.proactive

    @property
    def settings_window(self):
        return self.ui.settings_window

    @property
    def tray(self):
        return self.ui.tray

    def _chat_context(self) -> str:
        return self.brain.chat_context()

    def _on_chat_reply_ready(self, text, emotion, tts_enabled):
        self.ui._on_chat_reply_ready(text, emotion, tts_enabled)

    def _show_chat_window(self):
        self.ui._show_chat_window()

    def _show_settings(self):
        self.ui._show_settings()

    def _on_quick_chat_sent(self, text):
        self.ui._on_quick_chat_sent(text)

    def _quit(self):
        # 关 Dashboard 子进程
        if self._dashboard_pid:
            try:
                import psutil
                p = psutil.Process(self._dashboard_pid)
                p.terminate()
            except Exception:  # noqa: BLE001
                pass
        self.ui._quit()

    def _on_about_to_quit(self):
        if self._dashboard_pid:
            try:
                import psutil
                p = psutil.Process(self._dashboard_pid)
                p.terminate()
            except Exception:  # noqa: BLE001
                pass
        self.ui._on_about_to_quit()


# ============================================================================
#  全局异常处理
# ============================================================================


def _crash_handler(exc_type, exc, tb) -> None:
    """全局异常处理器：崩溃时打印错误到控制台 + 写入 crash.log。"""
    import traceback
    import sys as _sys
    msg = "".join(traceback.format_exception(exc_type, exc, tb))
    _sys.stderr.write(msg)
    try:
        root = _resolve_root()
        crash_path = root / "crash.log"
        with crash_path.open("a", encoding="utf-8") as f:
            f.write("\n" + "=" * 60 + "\n")
            f.write(msg)
        print(f"  [CRASH] 崩溃日志已写入: {crash_path}", file=_sys.stderr)
    except Exception:
        pass


# ============================================================================
#  入口
# ============================================================================


def main() -> int:
    import sys as _sys
    _sys.excepthook = _crash_handler
    try:
        return _main_inner()
    except Exception:
        _sys.excepthook(*_sys.exc_info())
        return 1


def _main_inner() -> int:
    args = _parse_args()

    # 配置日志级别
    import logging as _logging
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cfg_path = (_resolve_root() / args.config) if not Path(args.config).is_absolute() else Path(args.config)
    log_level = "INFO"
    if cfg_path.is_file():
        try:
            import yaml
            with cfg_path.open("r", encoding="utf-8") as f:
                d = yaml.safe_load(f) or {}
            log_level = (d.get("app", {}) or {}).get("log_level", "INFO")
            _logging.getLogger().setLevel(getattr(_logging, log_level.upper(), _logging.INFO))
        except Exception:
            pass

    # 启动横幅
    banner = _Banner(enabled=not args.no_banner)
    # 先读 config 拿角色名（横幅要用）
    _ensure_config(_resolve_root())
    char_name = "鲸鱼娘"
    try:
        cfg = load_config(cfg_path)
        char_name = cfg.character.name or char_name
    except Exception:
        pass
    banner.title(char_name)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("desktop-pet")
    _icon_path = _resolve_root() / "assets" / "icon.ico"
    if _icon_path.is_file():
        app.setWindowIcon(QIcon(str(_icon_path)))
    else:
        _png_path = _resolve_root() / "assets" / "icon.png"
        if _png_path.is_file():
            app.setWindowIcon(QIcon(str(_png_path)))

    core = App(args, banner)
    # 把 App 实例挂到 QApplication，方便 UIController 调 Dashboard
    app._desktop_pet_app = core
    core.pet.show()

    if core.cfg.app.open_chat_on_start:
        from .ui.chat_window import ChatWindow
        from .brain.trace import TraceRecorder
        from .brain.langchain_agent import LangChainAgentConfig
        trace = TraceRecorder(core.root / "data" / "traces.db")
        cw = ChatWindow(
            core.cfg.llm, core.cfg.character, core.cfg.sprite.directory,
            asr_enabled=core.cfg.asr.enabled,
            asr_model=core.cfg.asr.model_size,
            asr_language=core.cfg.asr.language,
            registry=core.tool_registry,
            context_provider=core._chat_context,
            trace_recorder=trace,
            backend=core.cfg.brain.backend,
            langchain_cfg=LangChainAgentConfig(
                enable_checkpointer=core.cfg.brain.langchain.enable_checkpointer,
                checkpoint_db=str(core.root / core.cfg.brain.langchain.checkpoint_db),
                max_iterations=core.cfg.brain.langchain.max_iterations,
                return_intermediate_steps=core.cfg.brain.langchain.return_intermediate_steps,
            ),
        )
        cw.reply_ready.connect(core._on_chat_reply_ready)
        cw.show()
        core.ui._chat_window = cw

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
