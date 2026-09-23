"""桌面宠物主入口（启动横幅 + Ollama 自动检测 + Dashboard 一键启动）。"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import urllib.request
from pathlib import Path

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


#  启动横幅


from app.core.terminal import Banner as _Banner


#  Ollama 自动检测


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


#  解析参数


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="桌面宠物")
    p.add_argument("--config", default="config.yaml", help="配置文件路径")
    p.add_argument("--character", default=None, help="角色目录名")
    p.add_argument("--reset-state", action="store_true",
                   help="启动时忽略存档，重新开局")
    p.add_argument("--no-banner", action="store_true",
                   help="关闭启动横幅（纯日志输出）")
    p.add_argument("--with-dashboard", action="store_true",
                   help="启动时自动打开 Agent Trace 面板 (http://127.0.0.1:8766)")
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


def _apply_settings_overrides(cfg, cfg_path: Path) -> None:
    """设置面板保存的 renderer / 水印开关优先于 config.yaml。

    规则：cfg.yaml 里**显式写出**时，cfg.yaml 优先（用户改文件就生效）；
    cfg.yaml 没写或被注释，保持 settings.json 的值。
    """
    import yaml as _yaml
    from app.core.settings_store import SettingsStore
    try:
        _yaml_raw = {}
        if Path(cfg_path).is_file():
            with Path(cfg_path).open("r", encoding="utf-8") as _f:
                _yaml_raw = _yaml.safe_load(_f) or {}
        _pet_yaml = (_yaml_raw.get("pet") or {}) if isinstance(_yaml_raw, dict) else {}

        _yaml_has_renderer = (
            "renderer" in _pet_yaml and _pet_yaml["renderer"] in ("sprite", "live2d")
        )
        _sstore = SettingsStore()
        _saved_r = _sstore.get("renderer", None)
        if _yaml_has_renderer:
            if _saved_r and _saved_r != cfg.pet.renderer:
                log.info("以 config.yaml 为准 (%s)，忽略 settings.json 中的 %s",
                         cfg.pet.renderer, _saved_r)
        else:
            if _saved_r in ("sprite", "live2d"):
                cfg.pet.renderer = _saved_r

        _live2d_yaml = (_yaml_raw.get("pet") or {}).get("live2d") or {}
        _yaml_has_hw = "hide_watermark" in _live2d_yaml
        _saved_hw = _sstore.get("hide_watermark", None)
        if _yaml_has_hw:
            if _saved_hw is not None and _saved_hw != cfg.pet.live2d.hide_watermark:
                log.info("Live2D 水印：以 config.yaml 为准 (%s)，忽略 settings.json",
                         cfg.pet.live2d.hide_watermark)
        else:
            if _saved_hw is not None:
                cfg.pet.live2d.hide_watermark = bool(_saved_hw)
    except Exception:
        log.debug("settings overrides 应用失败，沿用 cfg.yaml", exc_info=True)


def _build_tts(cfg, root: Path) -> TTS:
    # 启动时应用设置面板保存的 TTS 配置（settings.json 优先于 config.yaml）
    try:
        from app.core.settings_store import SettingsStore
        _store = SettingsStore()
        _e = _store.get("tts_engine", None)
        if _e:
            cfg.character.tts_engine = str(_e)
        _mv = _store.get("minimax_voice_id", None)
        if _mv is not None:
            cfg.character.minimax_voice_id = str(_mv)
        _gu = _store.get("gptsovits_url", None)
        if _gu:
            cfg.character.gptsovits_url = str(_gu)
        _gr = _store.get("gptsovits_ref_audio", None)
        if _gr:
            cfg.character.gptsovits_ref_audio = str(_gr)
        _gp = _store.get("gptsovits_prompt_text", None)
        if _gp is not None:
            cfg.character.gptsovits_prompt_text = str(_gp)
        _te = _store.get("tts_enabled", None)
        if _te is not None:
            cfg.character.tts_enabled = bool(_te)
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.debug("ignored: %s", e)
    tts_cache = root / "assets" / "tts_cache"
    engine = getattr(cfg.character, "tts_engine", "edge")
    if engine == "gptsovits":
        # 方案 B：本地 GPT-SoVITS（完全免费，需先启动本地 api_v2 服务）
        from app.voice.gptsovits_tts import GPTSoVITSTTS
        tts = GPTSoVITSTTS(
            url=getattr(cfg.character, "gptsovits_url", "http://127.0.0.1:9880"),
            ref_audio=getattr(cfg.character, "gptsovits_ref_audio", ""),
            prompt_text=getattr(cfg.character, "gptsovits_prompt_text", ""),
            cache_dir=tts_cache,
        )
        log.info("TTS 引擎：GPT-SoVITS 本地（%s）", tts.url)
    elif engine == "minimax":
        # 方案 A：MiniMax 声音克隆（样本目录配置后启动时自动克隆，
        # 样本指纹未变则跳过；失败自动回退 edge-tts，不影响启动）
        from app.voice import minimax_tts as _mm
        from app.core.settings_store import SettingsStore as _Store
        api_key = (getattr(cfg.character, "minimax_api_key", "")
                   or getattr(cfg.llm, "api_key", ""))
        voice_id = _mm.normalize_voice_id(
            getattr(cfg.character, "minimax_voice_id", "")
            or cfg.character.tts_voice)
        base_url = getattr(cfg.llm, "base_url", "https://api.minimaxi.com/v1")
        samples_src = getattr(cfg.character, "minimax_samples", "")
        if samples_src and not Path(samples_src).is_absolute():
            samples_src = str(root / samples_src)
        if samples_src and voice_id:
            try:
                if _mm.ensure_voice_cloned(
                        api_key, voice_id, samples_src, base_url,
                        getattr(cfg.character, "minimax_group_id", ""),
                        marker_store=_Store()):
                    log.info("TTS: 声音克隆完成，音色=%s", voice_id)
            except Exception as e:  # noqa: BLE001
                log.warning("TTS: 自动声音克隆失败（%s），本次回退 edge-tts", e)
                engine = "edge"
        if engine == "minimax":
            from app.voice.minimax_tts import MiniMaxTTS
            tts = MiniMaxTTS(
                api_key=api_key,
                voice_id=voice_id,
                base_url=base_url,
                model=getattr(cfg.character, "minimax_model", "speech-01-turbo"),
                group_id=getattr(cfg.character, "minimax_group_id", ""),
                cache_dir=tts_cache,
            )
            log.info("TTS 引擎：MiniMax 声音克隆 voice_id=%s", tts.voice)
        else:
            tts = TTS(voice=cfg.character.tts_voice or "zh-CN-XiaoxiaoNeural",
                      cache_dir=tts_cache)
    else:
        tts = TTS(voice=cfg.character.tts_voice or "zh-CN-XiaoxiaoNeural",
                  cache_dir=tts_cache)
    tts.set_enabled(cfg.character.tts_enabled)
    return tts


#  Dashboard 进程管理


# 本地服务端口：纯静态 Live2D Demo（主入口，8765）与 FastAPI Agent Trace（开发者，8766）
DEMO_PORT = 8765
TRACE_PORT = 8766


def start_demo_subprocess(root: Path, port: int = DEMO_PORT) -> int | None:
    """后台启动纯静态 Live2D Demo（docs/demo/serve.py）。

    返回 PID（新启动）/ -1（端口已有服务）/ None（文件缺失或启动失败）。
    """
    import subprocess
    serve = root / "docs" / "demo" / "serve.py"
    if not serve.is_file():
        log.warning("Demo 服务脚本不存在：%s", serve)
        return None
    if _port_listening(port):
        return -1
    log_file = root / "data" / "demo.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(
            [sys.executable, str(serve), "--port", str(port),
             "--root", str(root)],
            cwd=str(root),
            stdout=open(log_file, "ab"),
            stderr=subprocess.STDOUT,
            creationflags=(subprocess.DETACHED_PROCESS
                           | subprocess.CREATE_NO_WINDOW
                           if os.name == "nt" else 0),
        )
        return proc.pid
    except Exception as e:  # noqa: BLE001
        log.warning("Demo 启动失败：%s", e)
        return None


def start_dashboard_subprocess(root: Path, port: int = TRACE_PORT) -> int | None:
    """后台启动 FastAPI Agent Trace 进程，返回 PID 或 None。"""
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


def _port_listening(port: int) -> bool:
    """端口是否已有服务监听。"""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def start_tts_api_subprocess(root: Path, port: int = 9880) -> int | None:
    """后台启动 GPT-SoVITS TTS 服务（api_v2），返回 PID 或 None。

    整合包目录不存在 → None；端口已有服务（含本函数或手动启动）→ -1。
    服务与桌宠进程解耦：桌宠退出后服务保留，下次启动秒就绪。
    """
    import subprocess
    pkg_dir = root / "GPT-SoVITS-v2pro-20250604-nvidia50"
    runtime_py = pkg_dir / "runtime" / "python.exe"
    if not runtime_py.is_file():
        log.info("GPT-SoVITS 整合包不存在（%s），跳过 TTS 服务启动", pkg_dir)
        return None
    if _port_listening(port):
        return -1
    log_file = root / "data" / "tts_api.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(
            [str(runtime_py), "api_v2.py", "-a", "127.0.0.1", "-p", str(port),
             "-c", "GPT_SoVITS/configs/tts_infer.yaml"],
            cwd=str(pkg_dir),
            stdout=open(log_file, "ab"),
            stderr=subprocess.STDOUT,
            creationflags=(subprocess.DETACHED_PROCESS
                           | subprocess.CREATE_NO_WINDOW
                           if os.name == "nt" else 0),
        )
        return proc.pid
    except Exception as e:  # noqa: BLE001
        log.warning("TTS 服务启动失败：%s", e)
        return None


def wait_dashboard_ready(port: int = DEMO_PORT, timeout: float = 8.0) -> bool:
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


#  App 主类


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
        # gptsovits 引擎需要本地 api_v2 服务：自动拉起（端口已占用则复用）
        if getattr(cfg.character, "tts_engine", "edge") == "gptsovits":
            tts_pid = start_tts_api_subprocess(root, port=9880)
            if tts_pid == -1:
                banner.ok("TTS 服务", "已在运行（端口 9880 复用）")
            elif tts_pid is None:
                banner.info("TTS 服务", "未找到整合包目录，语音合成不可用")
            else:
                self._tts_api_pid = tts_pid
                banner.ok("TTS 服务", f"启动中 pid={tts_pid} · 模型加载约 30 秒")
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
        # live2d 模型目录：支持相对路径（相对项目根解析）
        # cfg.yaml 显式值优先于 settings.json（重启生效项）
        _apply_settings_overrides(cfg, cfg_path)
        _l2d_dir = None
        if getattr(cfg, "pet", None) and getattr(cfg.pet, "live2d", None):
            _l2d_dir = Path(getattr(cfg.pet.live2d, "model_dir", "") or "")
            if _l2d_dir and not _l2d_dir.is_absolute():
                _l2d_dir = root / _l2d_dir
        self.pet = PetWindow(
            sprite_dir, fallback_image=fallback_image,
            scale=cfg.window.scale,
            always_on_top=cfg.window.always_on_top,
            # Live2D 渲染器（v3.1+）：从 cfg.pet 读取
            renderer_type=getattr(cfg.pet, "renderer", "sprite"),
            live2d_model_dir=_l2d_dir,
            live2d_hide_watermark=bool(getattr(cfg.pet.live2d, "hide_watermark", True)),
            # 挂机随机表情（Live2D 专属，池子在模型目录 *.model.yaml）
            live2d_random_exp_cfg={
                "enabled": bool(getattr(cfg.pet.live2d, "random_expression", True)),
                "min_s": int(getattr(cfg.pet.live2d, "random_expression_min_s", 25)),
                "max_s": int(getattr(cfg.pet.live2d, "random_expression_max_s", 70)),
            },
            live2d_max_fps=int(getattr(cfg.pet.live2d, "max_fps", 30)),
            # 触发场景动作配置（按模型一份 JSON：data/live2d_scenes/<模型名>.json）
            live2d_scene_dir=root / "data" / "live2d_scenes",
            # 食物库（右键「喂食」子菜单：图片贴纸 + desc 气泡 + 状态变化）
            foods_path=root / "data" / "foods.json",
        )
        self.pet.move(cfg.window.start_x, cfg.window.start_y)
        self.pet.attach_state(self.state_mgr.state)
        # 统计动画目录（仅 sprite 模式；live2d 不加载帧图，扫描 2550 个 PNG
        # 纯属浪费，之前的「动画帧 · 2550 张」横幅有误导性——那只是数文件数）
        if getattr(cfg.pet, "renderer", "sprite") == "live2d":
            banner.info("动画帧", "sprite 模式未启用，跳过（不占内存）")
        else:
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
            # live2d 不做自主移动（不走动/溜达），sprite 保持原行为
            movement_enabled=(getattr(cfg.pet, "renderer", "sprite") != "live2d"),
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
        # 本地服务进程状态记录
        self._dashboard_pid: int | None = None  # FastAPI Agent Trace（TRACE_PORT）
        self._demo_pid: int | None = None        # 纯静态 Live2D Demo（DEMO_PORT）

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

    def _start_demo(self) -> bool:
        """启动纯静态 Live2D Demo 服务并打开浏览器。"""
        port = DEMO_PORT
        pid = start_demo_subprocess(self.root, port=port)
        if pid is None:
            self.banner.fail("Live2D Demo 启动失败", "查看 data/demo.log")
            return False
        self._demo_pid = pid
        if wait_dashboard_ready(port=port, timeout=6.0):
            url = f"http://127.0.0.1:{port}/"
            self.banner.ok("Live2D Demo", url)
            QTimer.singleShot(300, lambda: open_in_browser(url))
            return True
        self.banner.warn("Live2D Demo 启动超时", "查看 data/demo.log")
        return False

    def open_demo(self) -> bool:
        """给 UI 调用的『打开 Live2D Demo』：已在跑直接打开，否则启动。"""
        port = DEMO_PORT
        if _port_listening(port):
            open_in_browser(f"http://127.0.0.1:{port}/")
            return True
        return self._start_demo()

    def _start_dashboard(self) -> bool:
        """启动 FastAPI Agent Trace 子进程（开发者用，端口 8766）。"""
        port = TRACE_PORT
        log.info("启动 Agent Trace (port=%d)…", port)
        pid = start_dashboard_subprocess(self.root, port=port)
        if pid is None:
            self.banner.fail("Agent Trace 启动失败", "查看 data/dashboard.log")
            return False
        self._dashboard_pid = pid
        if wait_dashboard_ready(port=port, timeout=8.0):
            url = f"http://127.0.0.1:{port}"
            self.banner.ok("Agent Trace", f"{url}  (pid={pid})")
            self.banner.info("浏览器已自动打开",
                            "也可用托盘菜单『Agent Trace（开发）』随时打开")
            QTimer.singleShot(500, lambda: open_in_browser(url))
            return True
        else:
            self.banner.warn("Agent Trace 启动超时", "查看 data/dashboard.log")
            return False

    def open_dashboard(self) -> bool:
        """给 UI 调用的『打开 Agent Trace』：先看是否已启动，没启动就拉一个。"""
        port = TRACE_PORT
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
        # 关本地服务子进程（Live2D Demo / Agent Trace）
        for _pid in (self._demo_pid, self._dashboard_pid):
            if _pid:
                try:
                    import psutil
                    psutil.Process(_pid).terminate()
                except Exception:  # noqa: BLE001
                    pass
        self.ui._quit()

    def _on_about_to_quit(self):
        for _pid in (self._demo_pid, self._dashboard_pid):
            if _pid:
                try:
                    import psutil
                    psutil.Process(_pid).terminate()
                except Exception:  # noqa: BLE001
                    pass
        self.ui._on_about_to_quit()


#  全局异常处理


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
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.debug("ignored: %s", e)


#  入口

# faulthandler 的输出文件句柄（模块级引用，防 GC 关闭文件）
_crash_log_fh = None


def _enable_crash_diagnostics() -> None:
    """记录原生崩溃与子线程异常到 crash.log。

    软轮转：启动时若 crash.log > 5MB，重命名为 crash.log.old，新文件从 0 开始。
    faulthandler 需要稳定 file 句柄，因此不能用 RotatingFileHandler。
    """
    global _crash_log_fh
    import faulthandler
    import threading
    import traceback
    try:
        crash_path = _resolve_root() / "crash.log"
        if crash_path.is_file() and crash_path.stat().st_size > 5 * 1024 * 1024:
            old = crash_path.with_suffix(".log.old")
            if old.exists():
                old.unlink()
            crash_path.rename(old)
        _crash_log_fh = crash_path.open("a", encoding="utf-8")
        _crash_log_fh.write("\n" + "=" * 60 + "\n[session start]\n")
        _crash_log_fh.flush()
        faulthandler.enable(_crash_log_fh)

        def _thread_hook(args):
            msg = "".join(traceback.format_exception(
                args.exc_type, args.exc_value, args.exc_traceback))
            try:
                _crash_log_fh.write(f"\n[THREAD {args.thread.name}] {msg}")
                _crash_log_fh.flush()
            except (OSError, ValueError, KeyError, TypeError) as e:
                log.debug("ignored: %s", e)
            logging.getLogger(__name__).error(
                "子线程异常 [%s]: %s", args.thread.name, msg)
        threading.excepthook = _thread_hook
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.debug("ignored: %s", e)


def main() -> int:
    import sys as _sys
    _sys.excepthook = _crash_handler
    _enable_crash_diagnostics()
    try:
        return _main_inner()
    except Exception:
        _sys.excepthook(*_sys.exc_info())
        return 1


def _main_inner() -> int:
    args = _parse_args()

    # 配置日志级别
    import logging as _logging
    from logging.handlers import RotatingFileHandler
    fmt = _logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # 文件日志（轮转 2MB×3）：比控制台重定向可靠，也方便回看
    try:
        log_dir = _resolve_root() / "logs"
        log_dir.mkdir(exist_ok=True)
        fh = RotatingFileHandler(log_dir / "pet.log", maxBytes=2_000_000,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        fh.setLevel(_logging.INFO)
        _logging.getLogger().addHandler(fh)
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.debug("ignored: %s", e)
    # 降噪：第三方库的常规噪音不进控制台/文件（保留 WARNING+）
    for noisy in ("urllib3", "httpx", "httpcore", "asyncio", "websockets"):
        _logging.getLogger(noisy).setLevel(_logging.WARNING)
    cfg_path = (_resolve_root() / args.config) if not Path(args.config).is_absolute() else Path(args.config)
    log_level = "INFO"
    if cfg_path.is_file():
        try:
            import yaml
            with cfg_path.open("r", encoding="utf-8") as f:
                d = yaml.safe_load(f) or {}
            log_level = (d.get("app", {}) or {}).get("log_level", "INFO")
            _logging.getLogger().setLevel(getattr(_logging, log_level.upper(), _logging.INFO))
        except (OSError, ValueError, KeyError, TypeError) as e:
            log.debug("ignored: %s", e)

    # 启动横幅
    banner = _Banner(enabled=not args.no_banner)
    # 先读 config 拿角色名（横幅要用）
    _ensure_config(_resolve_root())
    char_name = "鲸鱼娘"
    try:
        cfg = load_config(cfg_path)
        char_name = cfg.character.name or char_name
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.debug("ignored: %s", e)
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
            memory_store=core.brain.memory,
        )
        cw.reply_ready.connect(core._on_chat_reply_ready)
        cw.show()
        core.ui._chat_window = cw

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
