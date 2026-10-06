"""聊天窗口：把桌宠和大模型对话接到一起。

本模块是包 `chat_window/`，主类 ChatWindow 在此。其余辅助代码拆分到子模块：
    chat_window.message            - 聊天消息 dataclass (Message)
    chat_window.utils              - 常量、HTML 转义、工具元数据
    chat_window.worker             - _AgentWorker / _StreamWorker (QThread)
    chat_window.command_completer  - _CommandCompleter（/命令弹窗）
    chat_window.chrome             - _TitleBarDrag / _InputFocusWatcher

向后兼容：`from app.ui.chat_window import ChatWindow, Message, _AgentWorker, ...`
全部继续工作（re-export）。
"""
from __future__ import annotations
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


def _safe_qapp():
    """返回当前 QApplication 实例（无 GUI 环境返回 None）。

    用于「阻塞等 worker 完成 + 处理 Qt 事件」的场景：
    不阻塞会导致 Qt 信号无法投递，UI 死锁；processEvents 又必须有 QApplication。
    """
    try:
        from PyQt5.QtWidgets import QApplication
        return QApplication.instance()
    except Exception:  # noqa: BLE001
        return None

from app.core.qt_compat import (
    QApplication, QFont, QFrame, QHBoxLayout, QKeyEvent,
    QKeySequence, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QObject, QPixmap, QPlainTextEdit, QPoint, QPushButton,
    QSplitter, Qt, QTextBrowser, QTextCursor, QTextDocument, QToolButton,
    QVBoxLayout, QWidget, Signal, QThread, QTimer, QSize, QColor, QEvent, QRect,
    QGraphicsDropShadowEffect, QPainter, QPainterPath, QLinearGradient,
    QUrl, QBuffer, QByteArray,
    event_global_pos, event_local_pos,
)

# Re-export for type hints
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from PyQt5.QtGui import QLabel as _QLabel  # type: ignore

from app.voice.character import Emotion, guess_emotion, parse_reply
from app.core.config import CharacterConfig, LLMConfig
from app.core.api_keys import is_placeholder_key
from app.brain.llm_client import ChatMessage, LLMClient, LLMError
from app.brain.agent import AgentLoop, DANGEROUS_TOOLS
from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
from app.engine.tools import ToolRegistry
from app.voice.asr import SpeechRecognizer, asr_available
from app.engine.chat_store import ChatStore

# 危险工具的中文标签（弹窗里给主人看）+ 等待上限 — 拆到 chat_window.utils
from .utils import _DANGEROUS_LABELS, _CONFIRM_TIMEOUT_S  # noqa: F401
from app.brain.trace import TraceRecorder
from app.ui import ui_style
from app.ui.memory_panel import MemoryDialog

log = logging.getLogger(__name__)


# 工具 UI 元数据 + 工具结果状态 + 工具元组解包 — 拆到 chat_window.utils
from .utils import (  # noqa: F401
    _TOOL_UI_META, _CIRCLED_NUMS, _TOOL_FAIL_MARKERS,
    _unpack_tool, _tool_label, _tool_status,
)


# Message — 拆到 chat_window.message
from .message import Message  # noqa: F401


# _AgentWorker / _StreamWorker — 拆到 chat_window.worker
from .worker import _AgentWorker, _StreamWorker  # noqa: F401


#  / 命令补全（QPlainTextEdit 没有 setCompleter，自己写一个轻量版）


# _html_escape / _format_time_short — 拆到 chat_window.utils
from .utils import _html_escape, _format_time_short  # noqa: F401
# _CommandCompleter — 拆到 chat_window.command_completer
from .command_completer import _CommandCompleter  # noqa: F401


# _TitleBarDrag — 拆到 chat_window.chrome
from .chrome import _TitleBarDrag  # noqa: F401


# _InputFocusWatcher — 拆到 chat_window.chrome
from .chrome import _InputFocusWatcher  # noqa: F401
class ChatWindow(QWidget):
    """独立聊天窗。"""

    def __init__(self, llm_cfg: LLMConfig, char_cfg: CharacterConfig,
                 sprite_dir: str | Path, parent: Optional[QWidget] = None,
                 asr_enabled: bool = True, asr_model: str = "base",
                 asr_language: str = "zh",
                 registry: Optional[ToolRegistry] = None,
                 context_provider: Optional[Callable[[], str]] = None,
                 trace_recorder: Optional[TraceRecorder] = None,
                 backend: str = "lightweight",
                 langchain_cfg: Optional[LangChainAgentConfig] = None,
                 tts: Optional[object] = None,
                 memory_store: Optional[object] = None):
        super().__init__(parent)
        self.llm_cfg = llm_cfg
        self.char_cfg = char_cfg
        # TTS 引擎引用（用于在 _on_done 时同步 prepare 音频，让聊天窗回复与声音同步）
        self.tts = tts
        # 长期记忆存储（MemoryStore，可选）：供标题栏「记忆」管理面板增删
        self.memory_store = memory_store
        self._memory_dlg: Optional[MemoryDialog] = None
        self.sprite_dir = Path(sprite_dir)
        self.asr_enabled = asr_enabled
        self.asr_model = asr_model
        self.asr_language = asr_language
        # 工具注册表：非 None 时走 Agent 循环（能调工具），否则纯聊天
        self.registry = registry
        # 每次发消息前调用，返回追加到 system prompt 的动态上下文
        # （当前时间 / 桌宠状态 / 长期记忆）
        self.context_provider = context_provider
        # Agent Trace recorder（None = 不记录）
        self.trace = trace_recorder
        # Agent 后端选择："lightweight"（手写 ReAct） 或 "standard"（LangChain 1.0+）
        self.backend = backend
        self.langchain_cfg = langchain_cfg or LangChainAgentConfig()
        self._trace_run_id: Optional[str] = None
        self.history: list[Message] = []
        self._MAX_HISTORY = 50  # 最多保留 50 轮对话（超出则丢弃最早的）
        self._worker: Optional[_StreamWorker] = None
        self._generating = False
        self._mic_busy = False
        self.chat_store = ChatStore()  # 聊天历史持久化

        # 语音识别（ASR）：按住说话 → 松开识别 → 自动发送
        self.asr: Optional[SpeechRecognizer] = None
        if asr_available() and getattr(self, "asr_enabled", True):
            self.asr = SpeechRecognizer(model_size=self.asr_model or "base",
                                        language=self.asr_language or "zh")
            self.asr.text_ready.connect(self._on_speech_text)
            self.asr.error.connect(self._on_asr_error)
            self.asr.recording_started.connect(self._on_recording_start)
            self.asr.recording_finished.connect(self._on_recording_finish)
            # ASR 模型加载完（首次较慢），共享给 wake_word 避免重复加载
            self.asr.model_loaded.connect(self._share_asr_model_to_wake_word)
            # 预热：延迟 1s 在主线程调度 _get_model()，让首次说话瞬时返回
            self._warmup_asr_model()
        else:
            log.warning("ASR 不可用（未安装 sounddevice / faster-whisper，或配置关闭），语音输入按钮将隐藏")

        # 语音唤醒（WakeWord）：后台始终监听麦克风
        # 默认未启动；用户设置面板里勾选后由 ui_controller 调 start()
        self.wake_word = None
        if asr_available():
            try:
                from app.voice.wake_word import WakeWordRecognizer, wake_available
                if wake_available():
                    self.wake_word = WakeWordRecognizer(
                        model_size=self.asr_model or "tiny",
                        language=self.asr_language or "zh",
                    )
                    self.wake_word.command_ready.connect(self._on_speech_text)
                    log.info("WakeWordRecognizer 已实例化（未启动，由 ui_controller 决定）")
            except Exception:  # noqa: BLE001
                log.exception("WakeWordRecognizer 初始化失败")
                self.wake_word = None

        self.setWindowTitle(f"和 {char_cfg.name} 聊天")
        # 设置窗口图标
        _ico = Path(__file__).resolve().parent.parent.parent / "assets" / "icon.ico"
        if _ico.is_file():
            from app.core.qt_compat import QIcon
            self.setWindowIcon(QIcon(str(_ico)))
        self.resize(720, 540)
        self.setMinimumSize(560, 400)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setObjectName("chat_root")
        self.setStyleSheet(ui_style.CHAT_QSS)

        self._avatar_path = self._pick_avatar()
        self.avatar_label: Optional[QLabel] = None
        # 富文本圆形头像（QTextBrowser 对 document 资源在 clear() 后会失效，
        # 改用自包含的 data URI：<img src="data:image/png;base64,...">）
        self._avatar_data_bot: str = ""
        self._avatar_data_user: str = ""
        self._build_ui()
        self._register_avatar_resources()
        self._append_system_welcome()
        self._load_history()  # 从 JSON 加载上次的聊天记录

        # 流式等待中的「三点跳动」动画：常驻定时器，仅在存在占位气泡时重绘
        self._current_bot_msg = None
        self._streaming_anchor_pos = None
        # 流式期间累积的「未送 TTS 的尾部文本」（没遇到句末标点的那一段）
        self._tts_tail = ""
        # 上次已送 TTS 的累积位置（字符偏移），用于增量切分避免重复入队
        self._tts_sent_tail = 0
        # 已「commit」（prepare 完成 + chat_view 渲染）的句子（用于 _on_done 时不再重复 prepare）
        self._committed_text = ""
        # 句子 prepare worker（每句一个，完成后 emit sentence_ready）
        self._sentence_workers = []   # QThread 列表，保留引用防 GC
        self._typing_frame = 0
        self._typing_timer = QTimer(self)
        self._typing_timer.setInterval(380)
        self._typing_timer.timeout.connect(self._on_typing_tick)
        self._typing_timer.start()

    def _pick_avatar(self) -> Optional[Path]:
        """挑一张头像（优先日常/开心动作的第一帧，兼容旧 idle_calm 命名）。"""
        candidates = [
            'Default', 'Idle_tail', 'Idle_shake', 'Emotion_happy',
            'Idle_stars', 'Idle_yawning',
            'idle_calm', 'idle_blink', 'idle_bounce',
        ]
        for d in candidates:
            p = self.sprite_dir / d
            if p.is_dir():
                # 素材可能是 PNG（rmbg-*）或旧 JPG
                files = sorted(list(p.glob('*.png')) + list(p.glob('*.jpg')))
                if files:
                    return files[0]
        return None

    def _make_avatar_pixmap(self, role: str, size: int = 40) -> QPixmap:
        """生成圆形头像 QPixmap（外圈 2px 白边）。

        bot 角色：统一使用项目图标 assets/icon.ico（居中裁剪成圆）；
                  图标缺失时回退角色首帧，再无图则用首字 + 粉紫渐变。
        user 角色：首字「我」+ 蓝色渐变。
        """
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)

        if role == "bot":
            # 聊天窗桌宠头像统一使用 assets/icon.ico；缺失时回退角色首帧
            icon = Path(__file__).resolve().parent.parent.parent / "assets" / "icon.ico"
            src = QPixmap(str(icon)) if icon.is_file() else QPixmap()
            if src.isNull() and self._avatar_path:
                src = QPixmap(str(self._avatar_path))
            if not src.isNull():
                # 先画白色底圆（即 2px 白边）
                p.setBrush(QColor(255, 255, 255))
                p.drawEllipse(0, 0, size, size)
                # 内圆裁剪后画头像图（居中裁剪填满圆）
                inner = QPainterPath()
                m = 2
                inner.addEllipse(m, m, size - 2 * m, size - 2 * m)
                p.setClipPath(inner)
                scaled = src.scaled(size - 2 * m, size - 2 * m,
                                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                    Qt.TransformationMode.SmoothTransformation)
                x = (size - scaled.width()) // 2
                y = (size - scaled.height()) // 2
                p.drawPixmap(x, y, scaled)
            else:
                grad = QLinearGradient(0, 0, size, size)
                grad.setColorAt(0.0, QColor("#f9a8d4"))
                grad.setColorAt(1.0, QColor("#ec6aa9"))
                p.setBrush(grad)
                p.drawEllipse(0, 0, size, size)
                self._draw_avatar_char(p, (self.char_cfg.name or "宠")[:1], size)
        else:
            grad = QLinearGradient(0, 0, size, size)
            grad.setColorAt(0.0, QColor("#60a5fa"))
            grad.setColorAt(1.0, QColor("#3b6cf0"))
            p.setBrush(grad)
            p.drawEllipse(0, 0, size, size)
            self._draw_avatar_char(p, "我", size)
        p.end()
        return pm

    @staticmethod
    def _draw_avatar_char(p: QPainter, ch: str, size: int) -> None:
        """在圆形头像中央画白色加粗字符。"""
        from app.core.qt_compat import QFont
        f = QFont("Microsoft YaHei UI")
        f.setPixelSize(max(12, int(size * 0.5)))
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRect(0, 0, size, size),
                   Qt.AlignmentFlag.AlignCenter, ch)

    def _pixmap_to_data_uri(self, pm: QPixmap) -> str:
        """QPixmap → data:image/png;base64,...（自包含，clear() 后也不会失效）。"""
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QBuffer.OpenModeFlag.ReadWrite)
        pm.save(buf, "PNG")
        buf.close()
        import base64
        return "data:image/png;base64," + base64.b64encode(bytes(ba)).decode("ascii")

    def _register_avatar_resources(self) -> None:
        """生成并缓存两枚圆形头像的 data URI（bot/user，40px）。"""
        self._avatar_data_bot = self._pixmap_to_data_uri(self._make_avatar_pixmap("bot", 40))
        self._avatar_data_user = self._pixmap_to_data_uri(self._make_avatar_pixmap("user", 40))

    def set_avatar(self, path: Path) -> None:
        """外部更新角色头像（切换角色时用）。"""
        self._avatar_path = path
        # 标题栏头像
        self.avatar_label.setPixmap(self._make_avatar_pixmap("bot", 44))
        self.avatar_label.setFixedSize(44, 44)
        # 富文本头像缓存（只影响之后的新消息）
        self._register_avatar_resources()

    def _build_ui(self) -> None:
        """新版 UI：能力展示 + 聊天 + 多行输入 + 命令。

        结构（从上到下）：
            ┌──────────────────────────────────────────────┐
            │ 头像  鲸鱼娘                                  │
            │       模型 · 工具数 · 记忆数   [清空] [Demo]  │   ← 标题
            ├──────────────────────────────────────────────┤
            │ ┌──────────────────────────────────────────┐ │   ← 能力展示面板
            │ │ [聊天陪伴] [定时提醒] [记住事实]          │ │
            │ │ [打开应用] [查时间/单位/农历] [看桌面]      │ │
            │ │ [多智能体协作] [Live2D Demo]              │ │
            │ └──────────────────────────────────────────┘ │
            ├──────────────────────────────────────────────┤
            │                                              │
            │          [聊天气泡区]                          │
            │                                              │
            ├──────────────────────────────────────────────┤
            │ [设提醒] [记一条] [开应用] [看桌面] [问问题]    │   ← 一键示例
            ├──────────────────────────────────────────────┤
            │ [多行输入框                                  ]│
            │                                               │
            ├──────────────────────────────────────────────┤
            │ [🎤]  0 / 2000                  [停止][发送]   │
            └──────────────────────────────────────────────┘
        """
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 18)
        root.setSpacing(0)

        card = QFrame(self)
        card.setObjectName("window_card")
        card.setStyleSheet(ui_style.WINDOW_CARD_QSS)
        _shadow = QGraphicsDropShadowEffect(card)
        _shadow.setBlurRadius(48)
        _shadow.setOffset(0, 10)
        _shadow.setColor(QColor(90, 78, 200, 55))
        card.setGraphicsEffect(_shadow)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(8)
        root.addWidget(card, 1)

        header = QFrame(card)
        header.setObjectName("titlebar")
        header.setStyleSheet(ui_style.TITLEBAR_QSS)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(14, 8, 10, 8)
        hl.setSpacing(10)

        # 头像（圆形裁剪 + 白色描边，复用富文本头像的统一绘制）
        self.avatar_label = QLabel()
        self.avatar_label.setPixmap(self._make_avatar_pixmap("bot", 44))
        self.avatar_label.setFixedSize(44, 44)
        hl.addWidget(self.avatar_label)

        # 标题区（标题 + 副标题；按住可拖动窗口）
        drag_zone = QWidget(header)
        dz = QVBoxLayout(drag_zone)
        dz.setContentsMargins(0, 0, 0, 0)
        dz.setSpacing(1)
        title = QLabel(f"和 {self.char_cfg.name} 的对话")
        title.setObjectName("titlebar_title")
        dz.addWidget(title)
        self.subtitle_label = QLabel(self._status_text())
        self.subtitle_label.setObjectName("titlebar_subtitle")
        dz.addWidget(self.subtitle_label)
        hl.addWidget(drag_zone, 1)

        # 右侧操作按钮（清空 / 调试 / 最小化 / 关闭）
        self.btn_clear = QPushButton("清空")
        self.btn_clear.setObjectName("ghost_btn")
        self.btn_clear.setToolTip("清空上下文（不影响长期记忆）")
        self.btn_clear.clicked.connect(self._on_clear)
        hl.addWidget(self.btn_clear)

        self.btn_demo = QPushButton("Demo")
        self.btn_demo.setObjectName("ghost_btn")
        self.btn_demo.setToolTip("打开本地 Live2D 演示页 http://127.0.0.1:8765")
        self.btn_demo.clicked.connect(self._open_demo)
        hl.addWidget(self.btn_demo)

        self.btn_memory = QPushButton("记忆")
        self.btn_memory.setObjectName("ghost_btn")
        self.btn_memory.setToolTip("管理长期记忆（查看 / 新增 / 删除）")
        self.btn_memory.clicked.connect(self._open_memory)
        hl.addWidget(self.btn_memory)

        self.btn_min = QToolButton()
        self.btn_min.setObjectName("win_btn")
        self.btn_min.setText("─")
        self.btn_min.setToolTip("最小化")
        self.btn_min.clicked.connect(self.showMinimized)
        hl.addWidget(self.btn_min)

        self.btn_close = QToolButton()
        self.btn_close.setObjectName("win_btn_close")
        self.btn_close.setText("✕")
        self.btn_close.setToolTip("关闭")
        self.btn_close.clicked.connect(self.close)
        hl.addWidget(self.btn_close)

        cl.addWidget(header)

        # 标题栏拖动（头像 / 标题 / 副标题区域）
        self._titlebar = header
        self._drag_filter = _TitleBarDrag(self)
        drag_zone.installEventFilter(self._drag_filter)
        self.avatar_label.installEventFilter(self._drag_filter)
        title.installEventFilter(self._drag_filter)

        self.chat_view = QTextBrowser()
        self.chat_view.setObjectName("chat_view")
        self.chat_view.setOpenExternalLinks(True)
        chat_font = QFont()
        chat_font.setFamily("Microsoft YaHei, PingFang SC, Segoe UI, sans-serif")
        chat_font.setPointSize(10)
        self.chat_view.setFont(chat_font)
        self.chat_view.document().setDefaultStyleSheet(ui_style.CHAT_BUBBLE_CSS)
        cl.addWidget(self.chat_view, 1)

        input_container = QFrame()
        input_container.setObjectName("input_container")
        il = QVBoxLayout(input_container)
        il.setContentsMargins(12, 8, 12, 8)
        il.setSpacing(4)

        self.input_edit = QPlainTextEdit()
        self.input_edit.setObjectName("chat_input")
        self.input_edit.setPlaceholderText(
            "输入消息，回车发送（Shift+回车 或 Ctrl+回车 换行）")
        self.input_edit.setMaximumHeight(110)
        # 自定义按键事件：Enter 发送，Shift+Enter 换行
        self.input_edit.keyPressEvent = self._input_key_press
        # / 命令补全
        self._cmd_completer = _CommandCompleter(self.input_edit, [
            "/状态", "/喂 ", "/记 ", "/时间", "/说 ", "/帮助", "/记忆",
            "/提醒", "/清除", "/demo", "/调试", "/打开 ",
        ])
        il.addWidget(self.input_edit)

        # 输入区底部：状态 + 按钮
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(4)

        # 麦克风（文字 + 图标）
        self.mic_btn = QPushButton("语音")
        self.mic_btn.setObjectName("ghost_btn")
        self.mic_btn.setToolTip("按住说话，松开自动识别并发送" if self.asr
                                else "未安装 faster-whisper / sounddevice")
        self.mic_btn.setMinimumHeight(28)
        self.mic_btn.setEnabled(self.asr is not None)
        if self.asr is not None:
            self.mic_btn.pressed.connect(self._on_mic_pressed)
            self.mic_btn.released.connect(self._on_mic_released)
        bottom_row.addWidget(self.mic_btn)

        # 字符计数
        self.input_status = QLabel("0 / 2000")
        self.input_status.setStyleSheet("color: #aaa; font-size: 8pt;")
        self.input_edit.textChanged.connect(
            lambda: self.input_status.setText(
                f"{len(self.input_edit.toPlainText())} / 2000"))
        bottom_row.addWidget(self.input_status)
        bottom_row.addStretch()

        # 停止按钮
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("stop_btn")
        self.stop_btn.setMinimumHeight(32)
        self.stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.setEnabled(False)
        bottom_row.addWidget(self.stop_btn)

        # 发送按钮（不再绑 Enter 快捷键，否则会和 input_edit.keyPressEvent 双发）
        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("send_btn")
        self.send_btn.setMinimumHeight(32)
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.clicked.connect(self._on_send)
        bottom_row.addWidget(self.send_btn)

        il.addLayout(bottom_row)
        cl.addWidget(input_container)

        # 输入框焦点高亮输入容器边框
        self._focus_watcher = _InputFocusWatcher(input_container)
        self.input_edit.installEventFilter(self._focus_watcher)

        # 旧属性兼容
        self.history_list = None

    def _status_text(self) -> str:
        """副标题：模型 + 工具数 + 记忆数（无 emoji）。"""
        model = self.llm_cfg.model or "(未配置)"
        tool_n = len(self.registry.names()) if self.registry else 0
        mem_n = 0
        if self.memory_store is not None:
            try:
                mem_n = int(self.memory_store.count())
            except Exception:  # noqa: BLE001
                mem_n = 0
        if not mem_n and self.context_provider:
            try:
                ctx = self.context_provider()
                mem_n = sum(1 for line in ctx.split("\n")
                            if line.strip().startswith("- ["))
            except Exception:  # noqa: BLE001
                pass
        parts = [f"模型 {model}"]
        if tool_n:
            parts.append(f"{tool_n} 工具")
        if mem_n:
            parts.append(f"{mem_n} 记忆")
        return "  ·  ".join(parts)

    def _input_key_press(self, event: QKeyEvent) -> None:
        """Enter 发送，Shift+Enter / Ctrl+Enter 换行（重写自 QPlainTextEdit.keyPressEvent）。

        为什么也支持 Ctrl+Enter：
            - 大量 IDE / 聊天工具（Slack、Discord、VS Code）把 Ctrl+Enter 作为换行
            - 用户从这些工具迁移过来会下意识按 Ctrl+Enter，单独只支持 Shift 会让他们
              错误地连发多条消息
            - Shift 兼容老用户习惯，Ctrl 兼容现代用户习惯，两者并存零成本
        """
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            mods = event.modifiers()
            newline_mod = (
                Qt.KeyboardModifier.ShiftModifier
                | Qt.KeyboardModifier.ControlModifier
            )
            if mods & newline_mod:
                # Shift+Enter 或 Ctrl+Enter：插入换行
                cursor = self.input_edit.textCursor()
                cursor.insertText("\n")
            else:
                # Enter：发送
                self._on_send()
            return
        # 其他键：默认行为
        from app.core.qt_compat import QPlainTextEdit as _QPT
        _QPT.keyPressEvent(self.input_edit, event)

    def _open_memory(self) -> None:
        """标题栏「记忆」：打开长期记忆管理面板（非模态，可边聊边开）。"""
        if self.memory_store is None:
            self._append_system_msg("⚠️ 当前未接入记忆存储，无法管理长期记忆")
            return
        if self._memory_dlg is None:
            self._memory_dlg = MemoryDialog(self.memory_store, self)
            # 增删后实时刷新副标题「N 记忆」
            self._memory_dlg.changed.connect(
                lambda: self.subtitle_label.setText(self._status_text()))
        self._memory_dlg.refresh()
        self._memory_dlg.show()
        self._memory_dlg.raise_()
        self._memory_dlg.activateWindow()

    def _open_demo(self) -> None:
        """打开纯静态 Live2D Demo（本地 8765）。"""
        app = self._find_app()
        if app is not None and hasattr(app, "open_demo"):
            app.open_demo()
            return
        from app.main import (start_demo_subprocess, wait_dashboard_ready,
                              open_in_browser, DEMO_PORT)
        from pathlib import Path
        pid = start_demo_subprocess(Path.cwd(), port=DEMO_PORT)
        if pid is not None and wait_dashboard_ready(port=DEMO_PORT, timeout=6.0):
            open_in_browser(f"http://127.0.0.1:{DEMO_PORT}/")
            self._append_system_msg("Live2D Demo 已打开 (http://127.0.0.1:8765/)")
        else:
            self._append_system_msg("Live2D Demo 启动失败，查看 data/demo.log")

    def _open_dashboard(self) -> None:
        """打开 FastAPI Agent Trace（开发者，8766）。"""
        app = self._find_app()
        if app is not None and hasattr(app, "open_dashboard"):
            app.open_dashboard()
            return
        from app.main import (start_dashboard_subprocess, wait_dashboard_ready,
                              open_in_browser, TRACE_PORT)
        from pathlib import Path
        pid = start_dashboard_subprocess(Path.cwd(), port=TRACE_PORT)
        if pid is not None and wait_dashboard_ready(port=TRACE_PORT, timeout=8.0):
            open_in_browser(f"http://127.0.0.1:{TRACE_PORT}")
            self._append_system_msg("Agent Trace 已打开 (http://127.0.0.1:8766)")
        else:
            self._append_system_msg("Agent Trace 启动失败，查看 data/dashboard.log")

    def _find_app(self):
        qa = QApplication.instance()
        return getattr(qa, "_desktop_pet_app", None) if qa else None

    def _append_system_msg(self, text: str) -> None:
        """在聊天区追加一条系统消息（灰色提示）。"""
        self._render_message(Message(role="system", content=text))

    # 【幻觉检测】匹配「已 XX」类断言性话术
    _HALLUCINATION_PATTERNS = [
        (r"已打开\s*[\"「]?([^\s」。,\.]+?)[\"」]?", "open"),
        (r"已经打开\s*[\"「]?([^\s」。,\.]+?)[\"」]?", "open"),
        (r"已启动\s*[\"「]?([^\s」。,\.]+?)[\"」]?", "start"),
        (r"已经启动\s*[\"「]?([^\s」。,\.]+?)[\"」]?", "start"),
        (r"已发送\s*(?:给|了)?\s*[\"「]?([^\s」。,\.]+?)[\"」]?", "send"),
        (r"已设置\s*[\"「]?([^\s」。,\.]+?)[\"」]?", "set"),
        (r"已复制\s*(?:到)?\s*剪贴板", "copy"),
        (r"已截图", "screenshot"),
        (r"已提醒", "remind"),
        (r"已记住", "remember"),
    ]

    def _detect_hallucination(self, response_text: str, tools_used: list) -> None:
        """检测模型幻觉：模型说「已打开 X」但本轮没有调用任何工具。

        匹配到话术 + 本轮未调工具 → 在聊天区追加一条系统消息
        （灰色，明显区分于正常消息），让主人一眼看出没有真的执行。

        为什么不直接重试 / 再次调用 LLM：
        - 重试可能再次幻觉
        - 直接调用又需要解析用户意图（"打开 XX"→open_app 还是 open_website？）
        - 让主人知道 + 提示重试，最稳

        Args:
            response_text: 模型最终回复的纯文本
            tools_used: 本轮实际调用的工具 [(name, summary), ...]
        """
        if tools_used:
            return    # 真有工具调用 → 不是幻觉

        import re
        for pattern, action in self._HALLUCINATION_PATTERNS:
            m = re.search(pattern, response_text)
            if m:
                target = m.group(1) if m.groups() else ""
                # 排除「已问好」之类误伤（target 必须是「应用/网址」才告警）
                if not target or len(target) > 40:
                    continue
                self._append_system_msg(
                    f"⚠️ 刚才「{target}」没真的{'打开' if action == 'open' else '启动' if action == 'start' else '操作'}："
                    f"模型回了文字但没调用工具（可能 LLM 幻觉）。"
                    f"请换种说法重试（如「帮我打开 {target}」）。"
                )
                log.warning(
                    "Hallucination detected: model claims '%s' did %s, "
                    "but no tool was called in this turn",
                    target, action,
                )
                # 只告警一次（避免重复刷屏）
                return

    def _append_system_welcome(self) -> None:
        self._render_message(Message(
            role="assistant",
            content=f"喵~ 主人你好呀！我是 {self.char_cfg.name}，有什么我能帮你的吗？",
            emotion=Emotion.HAPPY,
        ))

    def _load_history(self) -> None:
        """从 ChatStore 加载上次的聊天记录到界面。"""
        stored = self.chat_store.all()
        if not stored:
            return
        log.info("加载 %d 条历史聊天记录", len(stored))
        for sm in stored:
            # 跳过系统欢迎消息（已在 _append_system_welcome 中添加）
            if sm.role == "assistant" and "主人你好呀" in sm.content:
                continue
            # 解析情绪
            emotion = None
            if sm.emotion:
                try:
                    emotion = Emotion(sm.emotion)
                except ValueError:
                    pass
            msg = Message(role=sm.role, content=sm.content, emotion=emotion)
            self._render_message(msg)
            self.history.append(msg)

    def _on_clear(self) -> None:
        if self._generating:
            QMessageBox.information(self, "提示", "生成中，请先停止再清空")
            return
        self.history.clear()
        self.chat_view.clear()
        self.chat_store.clear()  # 持久化清空
        self._append_system_welcome()

    def _on_send(self) -> None:
        if self._generating:
            return
        text = self.input_edit.toPlainText().strip()
        if not text:
            return
        # 限长：>2000 字符警告并截断
        if len(text) > 2000:
            self._append_system_msg(
                f"⚠️ 消息太长（{len(text)} 字符），已截断到 2000")
            text = text[:2000]

        # 快捷命令
        if text.startswith("/"):
            self._handle_command(text)
            return

        self.input_edit.clear()
        self._quick_send(text)

    def _handle_command(self, text: str) -> None:
        """处理 /命令（快捷命令系统）。"""
        self.input_edit.clear()
        cmd = text.split()[0].lower()
        args = text[len(cmd):].strip()

        # 帮助
        if cmd in ("/帮助", "/help", "/?"):
            self._append_system_msg(
                "命令：/喂 /状态 /时间 /记 /说 /记忆 /提醒 /清除 /demo /调试 /打开")
            self._render_message(Message(
                role="assistant",
                content=(
                    "常用命令：\n"
                    "• /喂 食物名  — 喂食（如 /喂 拿铁）\n"
                    "• /状态      — 查看桌宠数值\n"
                    "• /时间      — 看当前时间\n"
                    "• /记 内容   — 记住（/记 主人不吃香菜）\n"
                    "• /说 文字   — 桌宠冒气泡\n"
                    "• /记忆      — 看长期记忆\n"
                    "• /提醒      — 看/管提醒\n"
                    "• /清除      — 清空聊天上下文（不影响长期记忆）\n"
                    "• /demo      — 打开 Live2D Demo 演示页\n"
                    "• /调试      — 打开 Agent Trace 调试面板（开发）\n"
                    "• /打开 名称 — 打开应用（如 /打开 记事本）"
                ),
            ))
            return

        # /记忆
        if cmd == "/记忆":
            items = self.context_provider().split("\n") if self.context_provider else []
            mem = [l for l in items if l.strip().startswith("- [")]
            self._render_message(Message(
                role="assistant",
                content="🧠 长期记忆（最新）：\n" + ("\n".join(mem[:10]) if mem
                                   else "还没有记住任何事 ~"),
            ))
            return

        # /提醒：调 list_reminders（如果有 registry）
        if cmd == "/提醒":
            if self.registry is None:
                self._render_message(Message(
                    role="assistant", content="⚠️ 当前没有工具注册表"))
            else:
                tool = self.registry.get("list_reminders")
                if tool is None:
                    self._render_message(Message(
                        role="assistant", content="⚠️ list_reminders 不可用"))
                else:
                    result = tool.fn()
                    self._render_message(Message(
                        role="assistant", content=f"⏰ {result}"))
            return

        # /demo：打开 Live2D Demo；/调试：打开 Agent Trace（开发者）
        if cmd == "/demo":
            self._open_demo()
            return
        if cmd == "/调试":
            self._open_dashboard()
            return

        # /打开
        if cmd == "/打开" and args:
            if self.registry:
                tool = self.registry.get("open_app")
                if tool:
                    import json as _json
                    r = tool.fn(app_name=args)
                    self._render_message(Message(role="assistant", content=r))
                    return
            self._render_message(Message(
                role="assistant", content="⚠️ 工具不可用"))
            return

        # 其他命令 → 直接当文本发给 LLM（带命令提示）
        hint = f"（主人用了快捷命令 {text}）"
        self._quick_send(hint)

    def _quick_send(self, text: str) -> None:
        """从快捷输入框发送消息（直接传入文本，不走 input_edit）。"""
        if self._generating:
            return
        text = text.strip()
        if not text:
            return
        msg = Message(role="user", content=text)
        self.history.append(msg)
        self._trim_history()
        self.chat_store.add("user", text)  # 持久化用户消息
        self._render_message(msg)
        self._kickoff_llm()

    def _trim_history(self) -> None:
        """聊天历史超过 MAX_HISTORY 时丢弃最早的记录（保留最近 MAX_HISTORY 条）。"""
        if len(self.history) > self._MAX_HISTORY:
            overflow = len(self.history) - self._MAX_HISTORY
            self.history = self.history[-self._MAX_HISTORY:]
            log.debug("聊天历史裁剪：丢弃最早 %d 条", overflow)

    def _on_stop(self) -> None:
        """停止当前正在进行的流式生成。

        PR1 修复：以前调 requestInterruption() 但 QThread.run 不响应、等同没用。
        现在 worker 持有 cancel_event（threading.Event），UI 线程 set() 后，stream
        端在下一个 chunk 之前会断开 HTTP 连接，正常向上抛「done(partial)」。
        """
        if self._worker is not None:
            self._worker.request_stop()
            self.stop_btn.setEnabled(False)

    def _on_mic_pressed(self) -> None:
        """按住麦克风按钮：开始录音。"""
        if self._mic_busy or self.asr is None:
            return
        self._mic_busy = True
        self.mic_btn.setText("🎤 录音中…")
        self.mic_btn.setStyleSheet("background-color: #ffcccc;")
        self.asr.start_recording()

    def _on_mic_released(self) -> None:
        """松开麦克风按钮：停止录音并开始识别。"""
        if self.asr is None:
            return
        self.mic_btn.setText("🔍 识别中…")
        self.mic_btn.setEnabled(False)
        self.asr.stop_and_recognize()

    def _on_recording_start(self) -> None:
        """录音已开始（UI 反馈）。"""
        # 已在 _on_mic_pressed 处理

    def _on_recording_finish(self) -> None:
        """录音已停止（等待识别结果）。"""
        # 已在 _on_mic_released 处理

    def _share_asr_model_to_wake_word(self, model) -> None:
        """ASR 模型加载完（首次较慢），共享给 wake_word 避免重复加载。"""
        ww = getattr(self, "wake_word", None)
        if ww is None:
            return
        try:
            ww.set_shared_model(model)
            log.info("WakeWord 共享 ASR 模型")
        except Exception:  # noqa: BLE001
            log.exception("WakeWord set_shared_model failed")

    def _warmup_asr_model(self) -> None:
        """预热 ASR 模型（ctranslate2 首次初始化 3~5s）。

        用 QTimer.singleShot 延迟 1s 调度到主线程，避免 chat_window 构造时立刻抢 CPU。
        主线程通过 _do_warmup 跑 self.asr._get_model()，让首次说话瞬时返回。
        失败时仅 debug log（首次说话仍可触发加载）。
        """
        if self.asr is None:
            return
        try:
            from app.core.qt_compat import QTimer

            def _do_warmup():
                model = None
                # 重试一次：首次加载容易受 OpenMP/MKL 初始化影响
                for attempt in (1, 2):
                    try:
                        model = self.asr._get_model()    # noqa: SLF001
                        break
                    except Exception as e:  # noqa: BLE001
                        log.debug("ASR 模型预热第 %d 次失败：%s", attempt, e)
                if model is not None:
                    log.info("ASR 模型预热完成（首次加载已缓存）")
                    self._share_asr_model_to_wake_word(model)
                else:
                    log.debug("ASR 模型预热 2 次都失败（首次说话时会再尝试）")

            QTimer.singleShot(1000, _do_warmup)
        except Exception as e:  # noqa: BLE001
            log.debug("ASR 模型预热启动失败：%s", e)

    def _confirm_dangerous_tool(self, name: str, args_json: str) -> Optional[bool]:
        """危险工具执行前的 UI 确认弹窗。

        返回：
          True  = 主人点了「是」，执行
          False = 主人点了「否」，拒绝
          None  = 弹窗超时没等到人（不是拒绝！），同样不执行

        三态是必要的：原实现超时也返回 False，AgentLoop 会把它当成
        「用户取消了此操作」讲给主人听，但主人从头到尾没看见过那个弹窗。

        线程模型（PyQt5 modal dialog 必须主线程）：
          1. AgentLoop 通过 asyncio.to_thread 在 Python ThreadPoolExecutor 子线程调到这里
          2. 子线程用 threading.Event 等结果（不能用 QEventLoop.exec()——
             QEventLoop 必须由拥有 QThread 的线程启动，子线程用 QEventLoop().exec()
             会卡死或 abort，已实测确认）
          3. QTimer.singleShot 把弹窗任务投递到主线程 event queue
          4. 主线程 _show_confirm_box show+raise_+activateWindow 后进 exec()
             （置前是必须的：主人常在用语音、没开聊天窗，弹窗不置前就永远超时）
          5. 用户点 Yes/No → 弹窗返回 → on_done 写 holder + Event.set()
          6. 子线程 Event.wait() 返回 → 拿到结果
          7. 走满 _CONFIRM_TIMEOUT_S → 返回 None，并让主线程关掉残留弹窗
        """
        # 嵌套防御（防弹窗套弹窗）
        if getattr(self, "_confirm_in_progress", False):
            log.warning("嵌套危险工具确认，自动通过（避免套弹窗）")
            return True
        if name not in DANGEROUS_TOOLS:
            # 非危险工具不需要确认
            return True

        # 解析参数（子线程做，没 Qt 依赖）
        try:
            import json as _json
            args_obj = _json.loads(args_json) if isinstance(args_json, str) else args_json
        except Exception:  # noqa: BLE001
            args_obj = {"raw": args_json}
        args_pretty = "\n".join(f"  {k}: {v}" for k, v in (args_obj or {}).items())

        self._confirm_in_progress = True
        # 投递到主线程弹窗；用 threading.Event 跨线程等结果
        # （不能用 QEventLoop.exec：QEventLoop 必须由拥有 QThread 的线程启动，
        # 子线程用 QEventLoop().exec() 会卡死或 abort，PyQt5 强制要求）
        result_event = threading.Event()

        def _on_result(ok: bool):
            self._confirm_in_progress = False
            result_event.set()    # 唤醒子线程 wait()

        QTimer.singleShot(
            0,
            lambda: self._show_confirm_box(name, args_pretty, _on_result))
        # 子线程阻塞等主线程弹完弹窗（60s 超时防卡死）
        if not result_event.wait(timeout=_CONFIRM_TIMEOUT_S):
            # 超时：把还留在屏幕上的弹窗关掉。原实现直接 return，
            # 而主线程仍卡在 box.exec() 里 → 屏幕上留一个僵尸模态框，
            # 下一个危险工具还能再叠一个（2026-10-04 日志实况）。
            log.warning("危险工具确认超时（%ds），拒绝执行并关闭弹窗",
                        int(_CONFIRM_TIMEOUT_S))
            self._confirm_in_progress = False
            # close 必须在主线程执行
            QTimer.singleShot(0, self._close_confirm_box)
            return None      # None = 超时，区别于 False（用户明确拒绝）
        return holder.get("ok", False)

    def _close_confirm_box(self) -> None:
        """主线程槽：关掉可能还开着的确认弹窗（超时兜底）。"""
        box = getattr(self, "_confirm_box", None)
        if box is None:
            return
        self._confirm_box = None
        try:
            box.reject()          # 等价于点「否」，让 exec() 正常返回
        except RuntimeError:     # C++ 对象已被销毁
            pass
        except Exception:  # noqa: BLE001
            log.debug("关闭确认弹窗失败", exc_info=True)

    def _show_confirm_box(self, name: str, args_pretty: str, on_done) -> None:
        """主线程槽：弹 QMessageBox → 调 on_done（Event.set() 唤醒子线程）。"""
        try:
            label = _DANGEROUS_LABELS.get(name, name)
            text = (f"桌宠想要调用危险工具：\n\n"
                    f"工具：{label}\n"
                    f"参数：\n{args_pretty or '  (无)'}\n\n"
                    f"是否允许？")
            box = QMessageBox(self)
            # 桌宠场景主人往往没在盯聊天窗，弹窗必须自己跳到前台，
            # 否则它会藏在后面，60s 必然超时（同文件 _open_memory 里已有
            # raise_/activateWindow 的正确范式）。
            box.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            box.setWindowModality(Qt.WindowModality.ApplicationModal)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("危险工具确认")
            box.setText(text)
            box.setStandardButtons(
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            box.setDefaultButton(QMessageBox.StandardButton.No)
            self._confirm_box = box      # 供超时路径 _close_confirm_box 用
            try:
                box.show()
                box.raise_()
                box.activateWindow()
                result = box.exec()
            finally:
                self._confirm_box = None
            on_done(result == QMessageBox.StandardButton.Yes)
        except Exception:  # noqa: BLE001
            log.exception("confirm dialog failed")
            on_done(False)

    def _on_speech_text(self, text: str) -> None:
        """语音识别完成：填入输入框并自动发送。"""
        self._mic_busy = False
        self.mic_btn.setText("📢")
        self.mic_btn.setStyleSheet("")
        self.mic_btn.setEnabled(True)

        if not text.strip():
            self.chat_view.append("⚠️ 未识别到语音")
            return

        # 填入输入框并自动发送
        self.input_edit.setText(text)
        self._on_send()

    def _on_asr_error(self, err: str) -> None:
        """语音识别出错：恢复按钮状态。"""
        self._mic_busy = False
        self.mic_btn.setText("📢")
        self.mic_btn.setStyleSheet("")
        self.mic_btn.setEnabled(True)
        self.chat_view.append(f"⚠️ {err}")

    def _kickoff_llm(self) -> None:
        if is_placeholder_key(self.llm_cfg.api_key):
            self._render_message(Message(
                role="assistant",
                content="还没填 API Key，请先打开 config.yaml，把 llm.api_key 改成你的 key 再重启。",
                emotion=Emotion.CONFUSED,
            ))
            return

        # 构造请求 messages（用 history，但**清洗幻觉痕迹**——见 _build_clean_messages_for_llm）
        msgs = self._build_clean_messages_for_llm()

        # 动态 system 上下文：时间 / 桌宠状态 / 长期记忆
        system_prompt = self.char_cfg.persona
        if self.context_provider is not None:
            try:
                extra = self.context_provider()
                if extra:
                    system_prompt = system_prompt + "\n\n" + extra
            except Exception as e:  # noqa: BLE001
                log.warning("context_provider 失败：%s", e)
        client = LLMClient(self.llm_cfg, system_prompt)

        # 占位气泡：插入一个占位 div 并记下 cursor 作为 streaming patch anchor
        self._current_bot_msg = Message(role="assistant", content="")
        self._streaming_raw = ""   # 流式原始累积（最终规范前），供逐帧清洗显示
        self._streaming_anchor_pos = self._insert_placeholder(self._current_bot_msg)
        # 重试类提示每轮只弹一次。模型偶发忽略 tool_choice 时会连着重试好几轮
        # （实测带历史时可达 5 轮 / 12 秒），每轮都追加一张卡片会把聊天区刷满
        # 「自动重试」噪声，主人看着干着急却什么也没多得到。
        self._retry_notice_shown = False

        # Trace 记录：开始一条 run（每条用户消息 = 一条 run）
        if self.trace is not None:
            user_msg = msgs[-1].content if msgs else ""
            self._trace_run_id = self.trace.begin_run(
                goal=user_msg[:200],
                mode="react" if (self.registry and self.registry.names()) else "single",
                meta={"model": self.llm_cfg.model,
                      "base_url": self.llm_cfg.base_url[:60]},
            )

        self._generating = True
        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.thinking_started.emit()

        if self.registry is not None and self.registry.names():
            # 根据 backend 选择 Agent：手写轻量级 或 LangChain 标准实现
            if self.backend == "standard":
                lc_agent = LangChainAgent(
                    llm_cfg=self.llm_cfg,
                    registry=self.registry,
                    persona=system_prompt,
                    cfg=self.langchain_cfg,
                )
                self._worker = _AgentWorker(
                    lc_agent,
                    [{"role": m.role, "content": m.content} for m in msgs])
                self._worker.chunk.connect(self._on_chunk)
                self._worker.tool_used.connect(self._on_tool)
                self._worker.meta.connect(self._on_meta)
                # 标准后端关闭在 _on_done / _on_failed 里做
                self._lc_agent = lc_agent
            else:
                # 轻量 backend：直接用 AgentLoop（真正的 ReAct 循环）。
                # 这是与 LangChain create_agent 语义对齐的手写实现：
                #   模型自主决定 → Thought: 调什么工具 → Action: 调工具 →
                #   Observation: 工具结果作为 ToolMessage 回传 →
                #   模型继续推理 → 直到模型给出 final answer（不再调工具）。
                # 关键特性：
                #   1. force_tool_use=True（首轮）+ 服务端降级（user-prompt 强制）
                #   2. 第一轮没调工具 → 注入强提示 + force_retry 重试
                #   3. 连续 N 轮纯调工具 → 强制进入 final 阶段让模型总结
                # 这样更智能：
                #   模型可以自己决定调几次工具、什么时候给 final answer。
                self._worker = _AgentWorker(
                    AgentLoop(client, self.registry, confirm_tool=self._confirm_dangerous_tool),
                    [{"role": m.role, "content": m.content} for m in msgs])
                self._worker.chunk.connect(self._on_chunk)
                self._worker.tool_used.connect(self._on_tool)
                self._worker.meta.connect(self._on_meta)
        else:
            self._worker = _StreamWorker(client, msgs)
            self._worker.chunk.connect(self._on_chunk)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _build_clean_messages_for_llm(self) -> list:
        """构造发给 LLM 的 messages 列表。

        关键：检测并改写「历史 assistant 消息里只说『已打开 XX』但**没有工具调用**」
        的情况——这些是 LLM 幻觉的痕迹，会污染后续的对话。

        清洗策略：
            - 在 self.history 上找 assistant 消息：有幻觉模式 + tools 为空
            - 在该消息**之前**插入一条 user 提示（以 user role 注入；
              OpenAI 没有 role=system 之外的「system 提示」，用 user 模拟）：
              「上文『已 XX』是 LLM 幻觉，实际并未打开。主人现在又问了 XX，
                请**真正调用** open_app / open_website 工具！」

        注意：必须从 self.history 取（保留 tools 字段），不能从 ChatMessage list 取
        —— ChatMessage 只有 role/content，没有 tools。
        """
        import re
        # 先在 self.history 上找幻觉索引（保留 tools）
        history = [m for m in self.history if m.role in ("user", "assistant")]

        hallucination_indices: list[int] = []
        for i, m in enumerate(history):
            if m.role != "assistant":
                continue
            tools_used = getattr(m, "tools", None) or []
            if tools_used:
                continue
            for pattern, _ in self._HALLUCINATION_PATTERNS:
                if re.search(pattern, m.content):
                    hallucination_indices.append(i)
                    break

        if not hallucination_indices:
            # 路径 1：没幻觉痕迹 → 直接转 ChatMessage list
            return [ChatMessage(role=m.role, content=m.content) for m in history]

        # 路径 2：有幻觉痕迹 → 在幻觉点之前插入 user 提示
        cleaned: list = []
        for i, m in enumerate(history):
            if i in hallucination_indices:
                # 找对应 user 消息（往前找最近一个 user）
                prev_user = ""
                for j in range(i - 1, -1, -1):
                    if history[j].role == "user":
                        prev_user = history[j].content
                        break
                # 插入「user 角色」的系统提示（OpenAI 兼容协议：tool 提示用 user 注入）
                cleaned.append(ChatMessage(
                    role="user",
                    content=(
                        f"[系统提醒] 上面那条回复是 LLM 幻觉——模型写了"
                        f"「{m.content[:60]}...」但**没有真的调用工具**，"
                        f"主人看不到任何效果。如果主人现在又问同类问题，"
                        f"请**真正调用** open_app / open_website 工具再回话，"
                        f"别再假装做了。"
                    ),
                ))
            cleaned.append(ChatMessage(role=m.role, content=m.content))
        return cleaned

    def _on_tool(self, name: str, args: str, result: str) -> None:
        """一次工具调用完成：在当前气泡里追加一行工具记录。"""
        if self._current_bot_msg is None:
            return
        # run_script：把 [OK] path\n--- stdout ---\n…\n--- stderr ---\n… 的结构化结果
        # 拆成命令块（meta）和简化结果，渲染端识别后展示终端风格块
        if name == "run_script" and "--- stdout ---" in result:
            self._parse_run_script_into_blocks(name, args, result)
        else:
            self._current_bot_msg.tools.append((name, args or "", result or ""))
        # Trace
        if self.trace is not None and self._trace_run_id:
            try:
                import json as _json
                self.trace.record(self._trace_run_id, "tool", {
                    "name": name,
                    "args": _json.loads(args) if isinstance(args, str) else args,
                    "result": result.replace("\n", " ")[:80],
                })
            except Exception:  # noqa: BLE001
                pass
        self._refresh_streaming_message()

    def _parse_run_script_into_blocks(self, name: str, args: str, result: str) -> None:
        """run_script 结果拆成：命令块（execs）+ 简化结果（tools）。"""
        import re as _re
        cmd = ""
        output = result
        ec = 0
        m = _re.search(r"\[([^\]]+)\]\s*(.+?)(?=\n---\s*stdout|$)", result, _re.DOTALL)
        if m:
            status = m.group(1).strip()
            try:
                if status.startswith("exit="):
                    ec = int(status.split("=")[1])
                else:
                    ec = 0
            except ValueError:
                ec = 0
        # 命令：args 里 path + args
        try:
            import json as _json
            a = _json.loads(args) if isinstance(args, str) else (args or {})
        except Exception:
            a = {}
        if isinstance(a, dict):
            cmd = a.get("script_path", "") or ""
            if a.get("args"):
                cmd = f"{cmd} {a['args']}".strip()
        # stdout：取 "--- stdout ---\n" 之后到 "--- stderr ---\n" 之前
        sm = _re.search(r"---\s*stdout\s*---\n(.+?)(?=\n---\s*stderr|$)", result, _re.DOTALL)
        if sm:
            output = sm.group(1).rstrip()
        # 存到 execs 块（命令块 UI 渲染）
        self._current_bot_msg.execs.append({
            "cmd": cmd or "(run_script)",
            "output": output or "(无输出)",
            "exit_code": ec,
        })
        # 同时保留简化结果（不进 TTS）
        simple = f"[{'OK' if ec == 0 else f'exit={ec}'}] "
        self._current_bot_msg.tools.append((name, args or "", simple))

    def _on_meta(self, kind: str, payload: dict) -> None:
        """内部事件（plan / reflection / meta / thought / exec）。

        Args:
            kind: "plan" | "reflection" | "meta" | "thought" | "exec"
            payload: 事件载荷
        """
        if self.trace is not None and self._trace_run_id:
            try:
                self.trace.record(self._trace_run_id, kind, payload or {})
            except Exception:  # noqa: BLE001
                pass

        # lightweight 后端的 meta 事件嵌套了"event" 字段（reasoning_delta / force_retry /
        # force_final / verify_retry）；展开映射到 UI 块类型
        ev_name = None
        if isinstance(payload, dict):
            ev_name = payload.get("event")

        # 思考块累积（推理模型的 planning / 反思 / reasoning_delta 内容）
        # lightweight 后端 emit ('meta', {'event':'reasoning_delta', 'content':...})
        # 标准后端 _AgentWorker emit ('meta', {'kind':'thought', 'text':...})
        # 都归到 thoughts 列表
        if self._current_bot_msg is not None:
            thought_text = None
            if kind == "thought":
                thought_text = (payload or {}).get("text", "")
            elif kind in ("plan", "reflection", "reasoning"):
                payload_dict = payload if isinstance(payload, dict) else {}
                if not payload_dict:
                    payload_dict = {"text": str(payload)} if payload else {}
                thought_text = payload_dict.get("text") or payload_dict.get("content", "")
            elif kind == "meta":
                ev_name = (payload or {}).get("event")
                if ev_name == "reasoning_delta":
                    thought_text = (payload or {}).get("content", "")
                elif (payload or {}).get("kind") == "thought":
                    thought_text = (payload or {}).get("text", "")
            if thought_text and thought_text not in self._current_bot_msg.thoughts:
                self._current_bot_msg.thoughts.append(thought_text)
                self._refresh_streaming_message()
                return

        # 命令块累积（run_script 之类的外部执行结果）
        if self._current_bot_msg is not None and kind == "exec":
            entry = {
                "cmd": (payload or {}).get("cmd", ""),
                "output": (payload or {}).get("output", ""),
                "exit_code": int((payload or {}).get("exit_code", 0)),
            }
            self._current_bot_msg.execs.append(entry)
            self._refresh_streaming_message()
            return

        # force_retry 给一个小提示（避免主人误以为模型已经做完）
        # 每轮**只提示一次**：连续重试是模型内部自愈过程，逐轮刷屏只会让人以为
        # 桌宠坏了。完整重试过程照常进 Trace（_on_meta 开头已经 record）。
        if kind == "meta" and isinstance(payload, dict):
            ev = payload.get("event")
            if ev == "force_retry":
                if not getattr(self, "_retry_notice_shown", False):
                    self._retry_notice_shown = True
                    self._append_system_msg(
                        "⚙️ 正在调用工具，主人稍等一下~"
                    )
                    log.info("ChatWindow: force_retry 已提示用户（后续重试不再重复提示）")
                else:
                    log.info("ChatWindow: force_retry（第 %s 轮，静默重试）",
                             payload.get("reason", ""))
            elif ev == "verify_retry":
                # 工具结果与模型答复矛盾（如工具失败却报喜），已要求重答
                if not getattr(self, "_retry_notice_shown", False):
                    self._retry_notice_shown = True
                    self._append_system_msg(
                        "🔎 核对了一下执行结果，好像不太对，让我重新确认一下~"
                    )
                    log.info("ChatWindow: verify_retry 事件已提示用户")

    def _on_chunk(self, tok: str) -> None:
        if self._current_bot_msg is None:
            return
        from app.brain.llm_client import sanitize_text
        # 原始累积（worker 来的 token 已做流式安全清洗）；最终文本以 _on_done(full) 为准
        self._streaming_raw = (self._streaming_raw or "") + tok
        # 流式渲染统一走最终规范：聊天窗也不闪现 CoT / 规则复读 / 英文思考。
        # 占位气泡的 content 直接存清洗后的显示文本（CoT 阶段保持空 → 三点动画）。
        sanitized = sanitize_text(self._streaming_raw).strip()
        # 累积 content 但**不**实时刷 chat_view ——等 TTS prepare 完成才显示
        # （实现「文字与语音同步」：声音准备好后文字才一起出现）
        self._current_bot_msg.content = sanitized
        # chat_view 保持「准备语音…」占位（_refresh_streaming_message 故意不调）
        # Trace：累计的原始文本（每 chunk 一次），便于调试时看到模型原始输出
        if self.trace is not None and self._trace_run_id and tok:
            self.trace.record(self._trace_run_id, "text", {
                "delta": tok,
                "accumulated": self._streaming_raw[:500],
            })
        # 同步桌宠头顶气泡（与聊天窗同一份清洗后文本，口型同步也用它）
        if sanitized:
            self.streaming_chunk.emit(sanitized)
            # **逐句 prepare**：每收完一句话立即启动后台 QThread 准备 TTS
            self._tts_drain_sentences(sanitized)

    # 中文/英文/数字标点都算句末边界。GPT-SoVITS 一次合成一句短句的体感最自然
    _TTS_SENT_END = re.compile(r"[。！？!?\n;；]+")

    def _tts_drain_sentences(self, accumulated: str) -> None:
        """逐句准备 TTS：累积内容里每出现句末标点，启动后台 QThread 调 tts.prepare(句子)。

        prepare 完成后 emit sentence_ready(text) → 主线程才在 chat_view 渲染该句 +
        调 tts.speak(text) 让 worker 立即播放（缓存命中）。

        关键设计：流式期间 chat_view 不显示句子（保持「准备语音…」占位），声音准
        备好后**一起**出现——实现「文字与语音同步」。

        为什么用 _tts_sent_tail 字符偏移而不依赖 startswith：累积是 sanitize_text 输
        出，可能跳变（修整段落 / 剥离 think 标签），前缀匹配会失效。
        """
        if not self.char_cfg.tts_enabled or self.tts is None:
            return
        sent_tail = getattr(self, "_tts_sent_tail", 0)
        if sent_tail > len(accumulated):
            sent_tail = 0
        scan = accumulated[sent_tail:]
        last_end = 0
        for m in self._TTS_SENT_END.finditer(scan):
            sentence = scan[last_end:m.end()].strip()
            last_end = m.end()
            if sentence:
                self._launch_sentence_prepare(sentence)
        self._tts_sent_tail = sent_tail + last_end
        # 保留无句末标点的尾部；_on_done 时 _flush_tts_tail_to_prepare 会
        # 为它启动 prepare（否则尾部丢失 → 只能靠 reply_ready 整段补播 → 重复播报）
        self._tts_tail = scan[last_end:]

    def _launch_sentence_prepare(self, sentence: str) -> None:
        """为单句启动后台 QThread 调 tts.prepare(sentence)，完成后 emit sentence_ready。"""
        from app.core.qt_compat import QThread, Signal

        class _PrepareWorker(QThread):
            done = Signal(str)

            def __init__(self, tts_obj, txt):
                super().__init__()
                self.tts_obj = tts_obj
                self.txt = txt

            def run(self):
                try:
                    self.tts_obj.prepare(self.txt)
                except Exception:  # noqa: BLE001
                    log.exception("TTS.prepare 异常: %r", self.txt[:60])
                self.done.emit(self.txt)

        w = _PrepareWorker(self.tts, sentence)
        w.done.connect(self._on_sentence_ready)
        # 保留引用防 GC，等线程结束自动清理
        self._sentence_workers.append(w)
        w.finished.connect(lambda ww=w: self._sentence_workers.remove(ww))
        w.start()

    def _on_sentence_ready(self, sentence: str) -> None:
        """单句 TTS prepare 完成：把该句加入 chat_view + 立即 speak 触发播放。"""
        # 拼接到已 commit 的显示文本（_current_bot_msg.content 已流式累积）
        # 当前 _current_bot_msg.content = sanitized（每 chunk 整体覆盖）。
        # 这里只调 speak 让 worker 立即播放（缓存命中），chat_view 显示由 _on_done 一次性完成。
        # 流式期间保持占位动画（_refresh_streaming_message 不调），等 _on_done 才正式显示。
        self._committed_text = (self._committed_text or "") + sentence
        try:
            self.tts.speak(sentence)
        except Exception:  # noqa: BLE001
            log.exception("TTS.speak 入队失败: %r", sentence[:60])

    def _flush_tts_tail_to_prepare(self) -> None:
        """_on_done 时把最后一段无句末标点的尾部启动 prepare（之前只是送 speak）。

        注意：每 chunk 的 _tts_drain_sentences 已启动句子的 prepare worker。
        如果 _tts_tail 还有内容（最后一段无句末标点），这里启动它的 prepare。
        """
        tail = (self._tts_tail or "").strip()
        if tail and self.char_cfg.tts_enabled and self.tts is not None:
            self._launch_sentence_prepare(tail)
        # 重置（避免下次会话污染）
        self._tts_tail = ""
        self._tts_sent_tail = 0

    def _wait_all_sentence_workers(self, timeout_s: float = 120.0) -> None:
        """阻塞等所有 sentence prepare worker 完成（或完成 + 失败）。

        阻塞主线程一段时间换取「文字等语音」体验：等所有句子 prepare 完成后
        _on_done 才把回复正式写入 history / chat_view / chat_view 渲染。
        超时则放弃等待（避免 TTS 服务挂掉时桌宠卡死）。
        """
        import time
        deadline = time.monotonic() + timeout_s
        while self._sentence_workers and time.monotonic() < deadline:
            # 处理 Qt 事件循环，避免 _on_chunk 等 callback 卡死
            QApplication = _safe_qapp()
            if QApplication is not None:
                QApplication.processEvents()
            time.sleep(0.05)
        if self._sentence_workers:
            log.warning("TTS prepare 超时（%d 个 worker 未完成），放弃等待",
                        len(self._sentence_workers))

    def _on_done(self, full: str) -> None:
        # LangChain 标准后端：释放 SqliteSaver 连接
        if self.backend == "standard" and getattr(self, "_lc_agent", None) is not None:
            try:
                self._lc_agent.close()
            except Exception:  # noqa: BLE001
                log.exception("close lc agent")
            self._lc_agent = None
        # 解析 [emotion] 标签
        parsed = parse_reply(full)
        # 最终清洗（每个 chunk 内 sanitize 过，但跨 chunk 的长括号段要等全文）
        from app.brain.llm_client import sanitize_text
        parsed.text = sanitize_text(parsed.text)
        # 仅在模型真的没标情绪（tag_found=False）时才按关键词兜底
        if not parsed.tag_found:
            parsed.emotion = guess_emotion(parsed.text)
        # 兜底：整段被 sanitize 丢空（纯英文 CoT / 模型暴走）时，避免空白气泡
        if not parsed.text.strip():
            if self._current_bot_msg.tools:
                # 工具执行了但模型没给文字 → 一句完成确认
                parsed.text = "好的，已经帮主人搞定啦~"
                if not parsed.tag_found:
                    parsed.emotion = Emotion.HAPPY
            else:
                # 模型没给出有效回答（思考过程被清洗掉）→ 角色化地请主人重说
                import random as _random
                parsed.text = _random.choice([
                    "唔……刚刚走神了一下，主人再说一遍好不好？",
                    "咦？刚刚没反应过来呢，主人能再说一次吗？",
                ])
                if not parsed.tag_found:
                    parsed.emotion = Emotion.SHY

        # 流式期间 _on_chunk 已逐句启动 prepare worker（文字与语音同步）。
        # 这里收尾：
        # 1. 把最后一段无句末标点的尾部也启动 prepare（如果有）
        # 2. **等所有 prepare worker 完成**（主线程短时阻塞；GPT-SoVITS 通常 3-5 秒）
        self._current_bot_msg.content = parsed.text
        self._current_bot_msg.emotion = parsed.emotion
        self._flush_tts_tail_to_prepare()  # 启动尾部 prepare
        self._wait_all_sentence_workers(timeout_s=120.0)  # 阻塞等全部 prepare 完

        self.history.append(self._current_bot_msg)
        self._trim_history()
        tools_list = [list(t) for t in self._current_bot_msg.tools] if self._current_bot_msg.tools else []
        self.chat_store.add(
            "assistant", parsed.text,
            emotion=parsed.emotion.value if parsed.emotion else "",
            tools=tools_list,
        )
        self._refresh_streaming_message(finished=True,
                                         emotion=parsed.emotion.value if parsed.emotion else "")

        self._detect_hallucination(parsed.text, self._current_bot_msg.tools)

        # Trace：完成 run
        if self.trace is not None and self._trace_run_id:
            try:
                self.trace.record(self._trace_run_id, "finish", {
                    "emotion": parsed.emotion.value if parsed.emotion else "",
                    "tool_count": len(self._current_bot_msg.tools),
                })
                self.trace.end_run(self._trace_run_id, parsed.text, status="success")
            except Exception:  # noqa: BLE001
                pass
            self._trace_run_id = None
        self._generating = False
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        # 通知桌宠：思考结束 + 流式结束（桌宠气泡停止）
        self.thinking_stopped.emit()
        self.streaming_done.emit()
        # 通知外部（pet 窗口）切表情。TTS 由本类逐句链路负责（流式期间逐句
        # prepare+speak + _flush_tts_tail_to_prepare 补尾部），这里必须传 False：
        # 若传 True，ui_controller 会把整段文本再 speak 一遍 → 重复播报。
        self.reply_ready.emit(parsed.text, parsed.emotion, False)
        # 清理状态
        self._current_bot_msg = None
        self._streaming_anchor_pos = None

    def _on_failed(self, err: str) -> None:
        # LangChain 标准后端：释放 SqliteSaver 连接
        if self.backend == "standard" and getattr(self, "_lc_agent", None) is not None:
            try:
                self._lc_agent.close()
            except Exception:  # noqa: BLE001
                log.exception("close lc agent on fail")
            self._lc_agent = None
        self._current_bot_msg.content = f"[错误] {err}"
        self._current_bot_msg.emotion = Emotion.SAD
        self._refresh_streaming_message(finished=True, emotion="sad")
        # Trace：失败
        if self.trace is not None and self._trace_run_id:
            try:
                self.trace.record(self._trace_run_id, "error", {"message": err})
                self.trace.end_run(self._trace_run_id,
                                   f"[错误] {err}", status="failed")
            except Exception:  # noqa: BLE001
                pass
            self._trace_run_id = None
        self._generating = False
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.thinking_stopped.emit()
        self.reply_ready.emit(self._current_bot_msg.content, Emotion.SAD, False)
        self.streaming_done.emit()

    # 气泡内联样式（Qt 富文本对多 class 选择器 / td border-radius 支持不稳定，
    # 直接内联最可靠）
    _BUBBLE_STYLE_USER = (
        "background:#95ec69;border:1px solid #7ed463;padding:9px 13px;"
        "color:#1f2937;font-size:10pt;line-height:1.55;")
    _BUBBLE_STYLE_BOT = (
        "background:#ffffff;border:1px solid #e9e6f7;padding:9px 13px;"
        "color:#2c2c38;font-size:10pt;line-height:1.55;")

    def _tool_steps_html(self, tools: list) -> str:
        """把工具调用渲染成步骤卡片（编号 + 中文名 + 动作目标 + 成败状态）。

        工具先于最终回答执行，故卡片置于气泡正文上方；工具结果不进 TTS。
        """
        rows = []
        for i, t in enumerate(tools):
            name, args, result = _unpack_tool(t)
            cn, target = _tool_label(name, args)
            status = _tool_status(result)
            num = _CIRCLED_NUMS[i] if i < len(_CIRCLED_NUMS) else str(i + 1)
            if status == "cancel":
                mark = '<span style="color:#9ca3af;">⊘ 取消</span>'
            elif status == "fail":
                mark = '<span style="color:#dc2626;">✕ 失败</span>'
            else:
                mark = '<span style="color:#16a34a;">✓</span>'
            target_html = (
                f'&nbsp;<span style="color:#a1a1aa;">·</span>&nbsp;<b>{_html_escape(target)}</b>'
                if target else ""
            )
            rows.append(
                '<table width="100%" cellspacing="0" cellpadding="0" style="margin:1px 0;">'
                '<tr>'
                '<td style="width:14px;color:#8b5cf6;font-size:9pt;padding:1px 4px 1px 0;'
                'vertical-align:middle;white-space:nowrap;">' + num + '</td>'
                '<td style="font-size:9pt;color:#3f3f46;vertical-align:middle;">'
                + _html_escape(cn) + target_html + '</td>'
                '<td align="right" style="font-size:8pt;vertical-align:middle;'
                'white-space:nowrap;padding-left:8px;">' + mark + '</td>'
                '</tr></table>'
            )
        head = (
            '<div style="color:#8b5cf6;font-size:8pt;font-weight:bold;'
            'margin-bottom:2px;letter-spacing:0.5px;">已执行操作 · '
            + str(len(tools)) + '</div>'
        )
        return '<div class="tools">' + head + "".join(rows) + '</div>'

    def _thoughts_html(self, thoughts: list) -> str:
        """思考块：折叠展示（默认收起，主人点开看推理过程）。"""
        if not thoughts:
            return ""
        joined = "\n".join(f"> {t.strip()}" for t in thoughts if t.strip())
        # 折叠（<details>）：最多 6 行可折叠，避免铺满屏幕
        return (
            '<details style="margin:2px 0;background:#fafafa;border:1px solid #e5e7eb;'
            'border-radius:4px;padding:2px 8px;font-size:9pt;color:#6b7280;">'
            '<summary style="cursor:pointer;font-weight:500;color:#7c3aed;">'
            f'思考过程（{len(thoughts)} 步）</summary>'
            '<pre style="margin:4px 0 2px 0;white-space:pre-wrap;font-family:'
            'Consolas, Menlo, monospace;font-size:9pt;color:#374151;">'
            + _html_escape(joined) + '</pre>'
            '</details>'
        )

    def _execs_html(self, execs: list) -> str:
        """命令执行块：终端风格（深色背景 + monospace 字体 + exit_code 标记）。"""
        if not execs:
            return ""
        rows = []
        for i, e in enumerate(execs):
            cmd = _html_escape(e.get("cmd", ""))
            output = _html_escape(e.get("output", ""))
            ec = int(e.get("exit_code", 0))
            ec_color = "#16a34a" if ec == 0 else "#dc2626"
            ec_icon = "✓" if ec == 0 else "✕"
            rows.append(
                '<div style="margin:3px 0;background:#0f172a;border-radius:6px;'
                'padding:6px 10px;font-family:Consolas,Menlo,monospace;font-size:9pt;'
                'color:#e2e8f0;">'
                f'<div style="color:#94a3b8;">$ {cmd}</div>'
                f'<pre style="margin:3px 0 0 0;white-space:pre-wrap;color:#cbd5e1;'
                f'overflow-x:auto;">{output}</pre>'
                f'<div style="margin-top:3px;font-size:8pt;color:{ec_color};">'
                f'{ec_icon} exit_code={ec}</div>'
                '</div>'
            )
        head = (
            '<div style="color:#0891b2;font-size:8pt;font-weight:bold;'
            'margin:3px 0 2px;letter-spacing:0.5px;">执行命令 · '
            + str(len(execs)) + '</div>'
        )
        return '<div class="execs">' + head + "".join(rows) + '</div>'

    def _file_edits_html(self, tools: list) -> str:
        """文件编辑块：从工具调用结果里抽 path 变更。
        只展示 write_file / edit_file / shell 类工具触发的"文件改动"。
        """
        if not tools:
            return ""
        edits = []
        for t in tools:
            # 容错：tests/legacy 可能传 (name, args) 二元组；生产是三元
            if len(t) == 3:
                name, args, _result = t
            else:
                name, args = t[:2]
            try:
                import json as _json
                a = _json.loads(args) if isinstance(args, str) else (args or {})
            except Exception:
                continue
            if not isinstance(a, dict):
                continue
            path = a.get("path") or a.get("file_path") or a.get("filepath") or a.get("file")
            content = a.get("content")
            if not path or not isinstance(path, str):
                continue
            if name not in ("write_file", "edit_file", "replace_in_file"):
                continue
            edits.append({
                "path": path,
                "size": len(content) if isinstance(content, str) else 0,
                "preview": (content[:120] + "…") if isinstance(content, str) and len(content) > 120
                           else (content if isinstance(content, str) else ""),
            })
        if not edits:
            return ""
        rows = []
        for e in edits:
            rows.append(
                '<div style="margin:2px 0;background:#fef3c7;border:1px solid #fbbf24;'
                'border-radius:4px;padding:4px 8px;font-size:9pt;color:#78350f;">'
                '<span style="color:#d97706;">📝</span> '
                f'<b>{_html_escape(e["path"])}</b>'
                f'&nbsp;<span style="color:#92400e;">· {e["size"]} 字节</span>'
                '</div>'
            )
        head = (
            '<div style="color:#d97706;font-size:8pt;font-weight:bold;'
            'margin:3px 0 2px;letter-spacing:0.5px;">文件编辑 · '
            + str(len(edits)) + '</div>'
        )
        return '<div class="file_edits">' + head + "".join(rows) + '</div>'

    def _msg_html(self, msg: Message, *, streaming_meta: Optional[str] = None) -> str:
        """按已完成的 Message 渲染成 HTML 字符串（气泡式聊天）。

        布局（Qt 富文本最稳的 table 方案）：
            外层 table 宽 100%，两列：
              - 头像列固定 44px（<img> 圆形头像，由 QPainter 预生成）
              - 内容列：meta（名字/时间）+ 内层 shrink-to-fit table（整块气泡背景）
            用户消息：内容列右对齐、头像列在右；桌宠消息反之。
        """
        if msg.role == "system":
            raw = (msg.content or "").replace("&", "&amp;").replace(
                "<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")
            return (
                '<div style="text-align:center;margin:10px 0;">'
                f'<span class="system-msg">{raw}</span></div>'
            )

        is_user = msg.role == "user"

        # 清理内容
        raw = msg.content or ""
        raw = self._clean_content(raw)

        # HTML 转义 + 换行转 <br/>
        # 用户消息不做 markdown 处理（避免 weird 行为），bot 消息做简单 markdown
        if is_user:
            safe = (raw.replace("&", "&amp;").replace("<", "&lt;").replace(
                ">", "&gt;").replace("\n", "<br/>"))
        else:
            safe = self._render_markdown(raw)

        # 流式输出中：bot 气泡末尾追加「三点跳动」等待动画
        # - streaming_meta 为 truthy 字符串 + msg.emotion 未设 → 走三点跳动
        # - streaming_meta 是「准备语音…」/「typing…」等显式文本 + msg.emotion 已设
        #   → 显示该文本（提示用户当前状态）
        if not is_user and streaming_meta and not (msg.emotion and msg.role == "assistant"):
            safe = safe + " " + self._typing_dots_html()
        elif not is_user and streaming_meta and (msg.emotion and msg.role == "assistant"):
            # 已设 emotion 但还在流式（如「准备语音…」状态）→ 显示提示文本
            meta_safe = (streaming_meta
                         .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            safe = safe + (
                f' <span style="color:#8b5cf6;font-size:10px;'
                f'margin-left:6px;">· {meta_safe}</span>'
            )

        # AI Coding 风格多块渲染：思考（折叠）→ 命令（终端）→ 文件编辑（黄色）→ 工具步骤 → 正文
        thoughts_html = self._thoughts_html(getattr(msg, "thoughts", []) or [])
        execs_html = self._execs_html(getattr(msg, "execs", []) or [])
        file_edits_html = self._file_edits_html(msg.tools)
        tools_html = self._tool_steps_html(msg.tools) if msg.tools else ""
        prepend_html = (
            (thoughts_html + execs_html + file_edits_html + tools_html) if not is_user else ""
        )
        if prepend_html:
            safe = prepend_html + safe

        time_str = _format_time_short(msg.ts)

        if is_user:
            # 右对齐：内容列 + 头像列
            meta_html = f'<span class="meta right">{time_str}</span><br/>'
            bubble = (
                '<table cellspacing="0" cellpadding="0" border="0">'
                '<tr><td class="bubble user" '
                f'style="{self._BUBBLE_STYLE_USER}">{safe}</td></tr></table>'
            )
            avatar = (
                '<span class="avatar-col user">'
                f'<img src="{self._avatar_data_user}" width="40" height="40" alt="我"/></span>'
            )
            return (
                '<table width="100%" cellspacing="0" cellpadding="0" border="0" '
                'style="margin:10px 0;">'
                '<tr>'
                '<td valign="top" align="right" style="padding:0 6px 0 48px;">'
                f'{meta_html}{bubble}</td>'
                '<td width="44" valign="top" align="center">'
                f'{avatar}</td>'
                '</tr></table>'
            )

        # 桌宠：头像列 + 内容列
        meta_html = (
            '<span class="meta left">'
            f'<span class="name">{_html_escape(self.char_cfg.name or "桌宠")}</span>'
            f'{time_str}</span><br/>'
        )
        bubble = (
            '<table cellspacing="0" cellpadding="0" border="0">'
            '<tr><td class="bubble bot" '
            f'style="{self._BUBBLE_STYLE_BOT}">{safe}</td></tr></table>'
        )
        avatar = (
            '<span class="avatar-col bot">'
            f'<img src="{self._avatar_data_bot}" width="40" height="40" alt=""/></span>'
        )
        return (
            '<table width="100%" cellspacing="0" cellpadding="0" border="0" '
            'style="margin:10px 0;">'
            '<tr>'
            '<td width="44" valign="top" align="center">'
            f'{avatar}</td>'
            '<td valign="top" align="left" style="padding:0 48px 0 6px;">'
            f'{meta_html}{bubble}</td>'
            '</tr></table>'
        )

    def _clean_content(self, text: str) -> str:
        """清理消息内容：去掉首尾空白，字面量 \\n 转实际换行。"""
        # 字面量 \n（反斜杠+n）→ 实际换行
        text = text.replace("\\n", "\n")
        # 去掉首尾空白和多余空行
        text = text.strip()
        return text

    def _render_markdown(self, text: str) -> str:
        """把 Markdown 文本渲染为 HTML（简单的实现，不依赖外部库）。"""
        import re

        # 先转义 HTML 特殊字符（防止 XSS）
        text = text.replace("&", "&amp;")
        text = text.replace("<", "&lt;")
        text = text.replace(">", "&gt;")

        # 代码块（用 class，由 CHAT_BUBBLE_CSS 接管样式）
        text = re.sub(r'```(\w+)?\n(.*?)```',
                      r'<pre><code>\2</code></pre>',
                      text, flags=re.DOTALL)

        # 行内代码
        text = re.sub(r'`([^`]+)`',
                      r'<code>\1</code>',
                      text)

        # 加粗
        text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)

        # 斜体
        text = re.sub(r'\*([^*]+)\*', r'<em>\1</em>', text)

        # 无序列表（每行以 - 开头）— 整段连续 - 行包成一个 <ul>，避免 <br/> 串进列表
        def _wrap_ul(m: "re.Match[str]") -> str:
            items = re.findall(r'^- (.+)$', m.group(0), flags=re.MULTILINE)
            return '<ul>' + ''.join(f'<li>{it}</li>' for it in items) + '</ul>'
        text = re.sub(r'(?:^- .+\n?)+', _wrap_ul, text, flags=re.MULTILINE)

        # 有序列表（数字. 开头）— 同上
        def _wrap_ol(m: "re.Match[str]") -> str:
            items = re.findall(r'^\d+\. (.+)$', m.group(0), flags=re.MULTILINE)
            return '<ol>' + ''.join(f'<li>{it}</li>' for it in items) + '</ol>'
        text = re.sub(r'(?:^\d+\. .+\n?)+', _wrap_ol, text, flags=re.MULTILINE)

        # 剩余段落里的换行 → <br/>
        text = text.replace("\n", "<br/>")

        return text

    def _insert_placeholder(self, msg: Message) -> int:
        """在 chat_view 文档末尾插入占位 div，返回该 div 起始的字符位置（作为 patch anchor）。

        PR-fix-line-breaks: cursor.insertHtml() 不会自动创建新 <p>，必须 insertBlock()
        强制开始新段落，否则消息会全部挤在同一个 <p> 里。
        """
        html = self._msg_html(msg, streaming_meta="typing…")
        cursor = self.chat_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        # 记录 anchor（插入前的位置），后续 refresh 从 anchor 到末尾删除旧内容
        anchor = cursor.position()
        cursor.insertHtml(html)
        cursor.insertBlock()  # 强制新段落：下一条消息会从新 <p> 开始
        sb = self.chat_view.verticalScrollBar()
        sb.setValue(sb.maximum())
        return anchor

    def _render_message(self, msg: Message, streaming: bool = False) -> None:
        """渲染一条消息到 chat_view 末尾，强制独立段落。"""
        html = self._msg_html(msg)
        cursor = self.chat_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertHtml(html)
        cursor.insertBlock()
        sb = self.chat_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _refresh_streaming_message(self, finished: bool = False, emotion: str = "") -> None:
        """流式刷新「当前 bot 占位 div」——从 anchor 到末尾删除旧内容后插入新内容。

        Args:
            finished: True = 把流式占位切换为最终完整消息（含 emotion）
                      False = 仍是流式占位（继续打字）或「准备语音…」占位
            emotion: 仅 finished=True 时用作 bubble 顶部 meta
        """
        if finished:
            meta = emotion
        else:
            # 流式打字机期间一律显示三点跳动（typing 动画）
            meta = "typing…"
        html = self._msg_html(self._current_bot_msg, streaming_meta=meta)

        cursor = self.chat_view.textCursor()
        anchor = getattr(self, "_streaming_anchor_pos", None)
        if anchor is None:
            self._render_message(self._current_bot_msg)
            return
        cursor.setPosition(anchor)
        cursor.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        cursor.removeSelectedText()
        cursor.insertHtml(html)

        sb = self.chat_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _typing_dots_html(self) -> str:
        """回复等待中的「三点跳动」动画：三个圆点依次点亮、循环流动。

        Qt 富文本不支持 CSS @keyframes，所以动画由 QTimer 定时推进
        _typing_frame（0/1/2）并整体重绘占位气泡实现。
        """
        frame = getattr(self, "_typing_frame", 0) % 3
        dots = []
        for i in range(3):
            color = "#8b5cf6" if i == frame else "#d3cdf0"
            dots.append(f'<span style="color:{color};">●</span>')
        return (
            '<span style="font-size:11px;letter-spacing:1px;">'
            + '&nbsp;&nbsp;'.join(dots) + '</span>'
        )

    def _on_typing_tick(self) -> None:
        """定时推进等待动画：仅当存在未完成的流式占位气泡时重绘。"""
        if getattr(self, "_current_bot_msg", None) is None:
            return
        if getattr(self, "_streaming_anchor_pos", None) is None:
            return
        self._typing_frame = (getattr(self, "_typing_frame", 0) + 1) % 3
        self._refresh_streaming_message()

    reply_ready = Signal(str, object, bool)   # text, Emotion, tts_enabled
    streaming_chunk = Signal(str)              # 流式输出增量（已累积的完整文本）
    streaming_done = Signal()                  # 流式输出结束
    thinking_started = Signal()                # 模型开始思考（触发表情）
    thinking_stopped = Signal()                # 模型思考结束（回到 idle）