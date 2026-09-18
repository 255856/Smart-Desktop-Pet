"""聊天窗口：把桌宠和大模型对话接到一起。

功能：
    - 多轮消息历史（带情绪标签）
    - 流式输出（每个 token 增量追加到气泡）
    - 打字时桌宠显示「思考中」表情
    - 收到完整回复后解析末尾 [emotion] 标签切回对应表情
    - 可选 TTS 朗读
    - 「清除上下文」「暂停/继续生成」按钮

性能要点（PR1 修复）：
    - 流式渲染从「clear + 重画整段历史」改为「cursor patch」——每个 chunk O(1)。
    - 「停止」按钮真接 cancel_event，传给 chat_stream —— httpx 流能真断开。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from app.core.qt_compat import (
    QFont, QFrame, QHBoxLayout, QKeyEvent,
    QKeySequence, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QObject, QPixmap, QPlainTextEdit, QPoint, QPushButton,
    QSplitter, Qt, QTextBrowser, QTextCursor, QToolButton, QVBoxLayout,
    QWidget, Signal, QThread, QSize,
)

# Re-export for type hints
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from PyQt5.QtGui import QLabel as _QLabel  # type: ignore

from app.voice.character import Emotion, guess_emotion, parse_reply
from app.core.config import CharacterConfig, LLMConfig
from app.brain.llm_client import ChatMessage, LLMClient, LLMError
from app.brain.agent import AgentLoop
from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
from app.engine.tools import ToolRegistry
from app.voice.asr import SpeechRecognizer, asr_available
from app.engine.chat_store import ChatStore
from app.brain.trace import TraceRecorder
from app.ui import ui_style

log = logging.getLogger(__name__)


@dataclass
class Message:
    role: str
    content: str
    emotion: Optional[Emotion] = None
    ts: float = field(default_factory=time.time)
    tools: list[tuple[str, str]] = field(default_factory=list)   # (工具名, 结果摘要)


class _AgentWorker(QThread):
    """Agent 循环 worker：流式正文 + 工具调用（Function Calling）。

    agent.run 产出 ("text"|"tool"|"done", ...) 事件，这里转成 Qt 信号：
        chunk     —— 正文增量
        tool_used —— 一次工具执行完成 (name, args, result)
        done      —— 完整文本（所有正文段拼接）
        failed    —— 出错
    """

    chunk = Signal(str)
    tool_used = Signal(str, str, str)
    done = Signal(str)
    failed = Signal(str)

    def __init__(self, agent: AgentLoop, messages: list[dict]):
        super().__init__()
        self.agent = agent
        self.messages = messages
        self.cancel_event = threading.Event()

    def request_stop(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        """在 QThread 里跑 agent 循环。

        LangChainAgent 自带持久 loop（run_sync）；其他 agent 仍走 asyncio.run()。
        """
        try:
            full: list[str] = []
            # LangChainAgent 提供 run_sync（在持久 loop 里跑，跨调用不切换）
            if hasattr(self.agent, "run_sync"):
                events = self.agent.run_sync(
                    self.messages, cancel_check=self.cancel_event.is_set)
                for kind, *payload in events:
                    if kind == "text":
                        full.append(payload[0])
                        self.chunk.emit(payload[0])
                    elif kind == "tool":
                        # payload = (name, args, result)
                        self.tool_used.emit(*payload)
                    # "done" 不需要额外处理
            else:
                async def drive() -> None:
                    async for ev in self.agent.run(
                            self.messages, cancel_check=self.cancel_event.is_set):
                        kind = ev[0]
                        if kind == "text":
                            full.append(ev[1])
                            self.chunk.emit(ev[1])
                        elif kind == "tool":
                            self.tool_used.emit(ev[1], ev[2], ev[3])
                asyncio.run(drive())
            self.done.emit("".join(full))
        except LLMError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"调用出错：{e!r}")


class _StreamWorker(QThread):
    """把 async 流式调用包装到 QThread 里跑。

    PR1 修复：
        - 新增 cancel_event（threading.Event），「停止」按钮只需 set()，无需走 Qt 中断
          协议 —— httpx stream 端每行会检查 cancel_check()，触发后立即断开连接。
        - run() 内现在 asyncio.set_event_loop(loop) 先于 run_until_complete，
          避免「loop 还没在主线程注册」导致的潜在 ResourceWarning。
    """
    chunk = Signal(str)
    done = Signal(str)             # 完整文本
    failed = Signal(str)

    def __init__(self, client: LLMClient, messages: list[ChatMessage]):
        super().__init__()
        self.client = client
        self.messages = messages
        # threading.Event 跨线程访问安全；UI 线程调 .set() 即可让 stream 端退出
        self.cancel_event = threading.Event()

    def request_stop(self) -> None:
        """「停止」按钮调用这里即可。stream 端会在下一个 chunk 前断开。"""
        self.cancel_event.set()

    def run(self) -> None:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            full: list[str] = []

            async def drive() -> None:
                async for tok in self.client.chat_stream(
                    self.messages,
                    cancel_check=self.cancel_event.is_set,
                ):
                    full.append(tok)
                    self.chunk.emit(tok)
                    if self.cancel_event.is_set():
                        # 已收到部分 token；断开 stream 跳出循环
                        break

            loop.run_until_complete(drive())
            loop.run_until_complete(asyncio.sleep(0))   # 给 aclose 一个机会落地
            loop.close()
            self.done.emit("".join(full))
        except LLMError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"调用出错：{e!r}")


# ============================================================================
#  / 命令补全（QPlainTextEdit 没有 setCompleter，自己写一个轻量版）
# ============================================================================


class _CommandCompleter(QObject):
    """监听 QPlainTextEdit 的按键事件，当输入行以 / 开头时弹候选列表。

    实现：
        - 监听 keyPress（Up/Down/Tab/Enter/Esc/字符键）
        - 用一个 QListWidget 当 popup
        - 匹配 prefix 的命令显示在 popup 里
        - Enter / Tab 接受第一个候选；Esc 关闭；鼠标点击也能选
    """

    def __init__(self, edit: QPlainTextEdit, candidates: list[str]):
        super().__init__(edit)
        self.edit = edit
        self.candidates = candidates
        self.popup = QListWidget()
        self.popup.setWindowFlags(Qt.WindowType.ToolTip)
        self.popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.popup.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.popup.setStyleSheet("""
            QListWidget {
                background: #ffffff;
                border: 1px solid #a78bfa;
                border-radius: 8px;
                padding: 4px;
                color: #2c2c38;
                font-size: 10pt;
            }
            QListWidget::item { padding: 4px 12px; border-radius: 4px; }
            QListWidget::item:selected { background: #ede9fe; color: #6d28d9; }
        """)
        self.popup.itemClicked.connect(self._accept)
        # 拦截事件
        edit.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is not self.edit:
            return False
        if event.type() == event.Type.KeyPress:
            key = event.key()
            if self.popup.isVisible():
                if key in (Qt.Key.Key_Down,):
                    self._move(1)
                    return True
                if key in (Qt.Key.Key_Up,):
                    self._move(-1)
                    return True
                if key in (Qt.Key.Key_Tab, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                    self._accept_current()
                    return True
                if key == Qt.Key.Key_Escape:
                    self.popup.hide()
                    return True
            # 文本变化时重新计算 popup
            if key == Qt.Key.Key_Slash or self._current_word().startswith("/"):
                self._refresh()
            elif key in (Qt.Key.Key_Space, Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
                self._refresh()
        return False

    def _current_word(self) -> str:
        """取光标所在行的第一个 token。"""
        cursor = self.edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfLine,
                            QTextCursor.MoveMode.KeepAnchor)
        line = cursor.selectedText()
        # 取第一个 token（空格前）
        for i, ch in enumerate(line):
            if ch in (" ", "\n", "\t"):
                return line[:i]
        return line

    def _refresh(self) -> None:
        word = self._current_word()
        if not word.startswith("/"):
            self.popup.hide()
            return
        prefix = word.lower()
        matches = [c for c in self.candidates if c.lower().startswith(prefix)]
        if not matches:
            self.popup.hide()
            return
        self.popup.clear()
        self.popup.addItems(matches)
        self.popup.setCurrentRow(0)
        # 定位在输入框下方
        rect = self.edit.cursorRect()
        global_pos = self.edit.mapToGlobal(rect.bottomLeft())
        self.popup.move(global_pos + QPoint(0, 4))
        popup_w = max(180, self.popup.sizeHintForColumn(0) + 24)
        self.popup.setFixedWidth(popup_w)
        self.popup.setFixedHeight(min(180, self.popup.sizeHintForRow(0) * len(matches) + 16))
        self.popup.show()

    def _move(self, delta: int) -> None:
        n = self.popup.count()
        if n == 0:
            return
        cur = (self.popup.currentRow() + delta) % n
        self.popup.setCurrentRow(cur)

    def _accept_current(self) -> None:
        item = self.popup.currentItem()
        if item is not None:
            self._apply_text(item.text())
        self.popup.hide()

    def _accept(self, item) -> None:
        self._apply_text(item.text())
        self.popup.hide()

    def _apply_text(self, full: str) -> None:
        """把当前行的第一个 token 替换成 full（保留后续内容）。"""
        cursor = self.edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.StartOfLine)
        cursor.movePosition(QTextCursor.MoveOperation.EndOfLine,
                            QTextCursor.MoveMode.KeepAnchor)
        line = cursor.selectedText()
        # 找到第一个空格位置
        rest = ""
        for i, ch in enumerate(line):
            if ch in (" ", "\n", "\t"):
                rest = line[i:]
                break
        cursor.insertText(full + rest)
        self.edit.setTextCursor(cursor)


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
                 langchain_cfg: Optional[LangChainAgentConfig] = None):
        super().__init__(parent)
        self.llm_cfg = llm_cfg
        self.char_cfg = char_cfg
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
        else:
            log.warning("ASR 不可用（未安装 sounddevice / faster-whisper，或配置关闭），语音输入按钮将隐藏")

        self.setWindowTitle(f"和 {char_cfg.name} 聊天")
        # 设置窗口图标
        _ico = Path(__file__).resolve().parent.parent / "assets" / "icon.ico"
        if _ico.is_file():
            from app.core.qt_compat import QIcon
            self.setWindowIcon(QIcon(str(_ico)))
        self.resize(720, 540)
        self.setMinimumSize(560, 400)
        self.setStyleSheet(ui_style.CHAT_QSS)

        self._avatar_path = self._pick_avatar()
        self.avatar_label: Optional[QLabel] = None
        self._build_ui()
        self._append_system_welcome()
        self._load_history()  # 从 JSON 加载上次的聊天记录

    def _pick_avatar(self) -> Optional[Path]:
        """挑一张头像（优先 idle_calm/0，否则任意 jpg）。"""
        for d in ['idle_calm', 'idle_blink', 'idle_bounce']:
            p = self.sprite_dir / d
            if p.is_dir():
                files = sorted(p.glob('*.jpg'))
                if files:
                    return files[0]
        return None

    # ---------------- UI ----------------
    def _build_ui(self) -> None:
        """新版 UI：能力展示 + 聊天 + 多行输入 + 命令。

        结构（从上到下）：
            ┌──────────────────────────────────────────────┐
            │ 头像  鲸鱼娘                                  │
            │       模型 · 工具数 · 记忆数   [🧹清空] [📊调试] │   ← 标题
            ├──────────────────────────────────────────────┤
            │ ┌──────────────────────────────────────────┐ │   ← 能力展示面板
            │ │ [聊天陪伴] [定时提醒] [记住事实]          │ │
            │ │ [打开应用] [查时间/单位/农历] [看桌面]      │ │
            │ │ [多智能体协作] [可视化调试面板]            │ │
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
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # ============ 顶部：标题 + 状态 ============
        header = QFrame()
        header.setObjectName("chat_header")
        header.setStyleSheet("""
            QFrame#chat_header {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #f8f7ff, stop:1 #fdf2f8);
                border: 1px solid #e9e3ff;
                border-radius: 12px;
            }
        """)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(14, 10, 14, 10)

        # 头像
        self.avatar_label = QLabel()
        if self._avatar_path:
            pix = QPixmap(str(self._avatar_path))
            if not pix.isNull():
                pix = pix.scaled(48, 48, Qt.AspectRatioMode.KeepAspectRatio,
                                 Qt.TransformationMode.SmoothTransformation)
                self.avatar_label.setPixmap(pix)
        hl.addWidget(self.avatar_label)

        # 标题 + 副标题（状态）
        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        title = QLabel(f"和 {self.char_cfg.name} 的对话")
        title_font = QFont()
        title_font.setPointSize(13)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setStyleSheet("color: #2c2c38;")
        title_box.addWidget(title)

        self.subtitle_label = QLabel(self._status_text())
        self.subtitle_label.setStyleSheet("color: #8a8a9c; font-size: 9pt;")
        title_box.addWidget(self.subtitle_label)
        hl.addLayout(title_box, 1)

        # 右侧按钮：清空 + 调试面板（不用 emoji，按钮只放文字 + tooltip）
        btn_style = """
            QPushButton {
                background: transparent;
                border: 1px solid #e0e0ee;
                border-radius: 8px;
                padding: 6px 12px;
                font-size: 10pt;
                color: #5b4f9c;
            }
            QPushButton:hover { background: #f0eaff; border-color: #a78bfa; }
        """
        self.btn_clear = QPushButton("清空")
        self.btn_clear.setToolTip("清空上下文（不影响长期记忆）")
        self.btn_clear.setStyleSheet(btn_style)
        self.btn_clear.clicked.connect(self._on_clear)
        hl.addWidget(self.btn_clear)

        self.btn_dashboard = QPushButton("调试面板")
        self.btn_dashboard.setToolTip("打开可视化调试面板 http://127.0.0.1:8765")
        self.btn_dashboard.setStyleSheet(btn_style)
        self.btn_dashboard.clicked.connect(self._open_dashboard)
        hl.addWidget(self.btn_dashboard)

        root.addWidget(header)

        # ============ 中间：聊天气泡区（占满剩余空间）============
        self.chat_view = QTextBrowser()
        self.chat_view.setOpenExternalLinks(True)
        self.chat_view.setStyleSheet("""
            QTextBrowser {
                background: #fbfbfd;
                border: 1px solid #e9e9f0;
                border-radius: 12px;
                padding: 8px 6px;
            }
        """)
        chat_font = QFont()
        chat_font.setFamily("Microsoft YaHei, PingFang SC, Segoe UI, sans-serif")
        chat_font.setPointSize(10)
        self.chat_view.setFont(chat_font)
        self.chat_view.document().setDefaultStyleSheet(ui_style.CHAT_BUBBLE_CSS)
        root.addWidget(self.chat_view, 1)

        # ============ 输入区（多行 + 工具栏）============
        input_container = QFrame()
        input_container.setStyleSheet("""
            QFrame {
                background: #ffffff;
                border: 2px solid #e7e3f5;
                border-radius: 14px;
            }
            QFrame:focus-within { border-color: #a78bfa; }
        """)
        il = QVBoxLayout(input_container)
        il.setContentsMargins(10, 8, 10, 6)
        il.setSpacing(4)

        self.input_edit = QPlainTextEdit()
        self.input_edit.setPlaceholderText("输入消息，回车发送（Shift+回车 换行）")
        self.input_edit.setMaximumHeight(110)
        self.input_edit.setStyleSheet("""
            QPlainTextEdit {
                background: transparent;
                border: none;
                font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
                font-size: 10pt;
                color: #2c2c38;
            }
        """)
        # 自定义按键事件：Enter 发送，Shift+Enter 换行
        self.input_edit.keyPressEvent = self._input_key_press
        # / 命令补全
        self._cmd_completer = _CommandCompleter(self.input_edit, [
            "/状态", "/喂 ", "/记 ", "/时间", "/说 ", "/帮助", "/记忆",
            "/提醒", "/清除", "/调试", "/打开 ",
        ])
        il.addWidget(self.input_edit)

        # 输入区底部：状态 + 按钮
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(4)

        # 麦克风（文字 + 图标）
        self.mic_btn = QPushButton("语音")
        self.mic_btn.setToolTip("按住说话，松开自动识别并发送" if self.asr
                                else "未安装 faster-whisper / sounddevice")
        self.mic_btn.setFixedHeight(28)
        self.mic_btn.setEnabled(self.asr is not None)
        self.mic_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                border: 1px solid #e0e0ee;
                border-radius: 8px;
                padding: 4px 10px;
                font-size: 9pt;
                color: #5b4f9c;
            }
            QPushButton:hover { background: #f0eaff; border-color: #a78bfa; }
            QPushButton:disabled { color: #ccc; border-color: #f0f0f0; }
        """)
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
        self.stop_btn.setFixedHeight(30)
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.setEnabled(False)
        bottom_row.addWidget(self.stop_btn)

        # 发送按钮（不再绑 Enter 快捷键，否则会和 input_edit.keyPressEvent 双发）
        self.send_btn = QPushButton("发送")
        self.send_btn.setObjectName("send_btn")
        self.send_btn.setFixedHeight(30)
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.clicked.connect(self._on_send)
        bottom_row.addWidget(self.send_btn)

        il.addLayout(bottom_row)
        root.addWidget(input_container)

        # 旧属性兼容
        self.history_list = None

    def _status_text(self) -> str:
        """副标题：模型 + 工具数 + 记忆数（无 emoji）。"""
        model = self.llm_cfg.model or "(未配置)"
        tool_n = len(self.registry.names()) if self.registry else 0
        mem_n = 0
        if self.context_provider:
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
        """Enter 发送，Shift+Enter 换行（重写自 QPlainTextEdit.keyPressEvent）。"""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                # Shift+Enter：插入换行
                cursor = self.input_edit.textCursor()
                cursor.insertText("\n")
            else:
                # Enter：发送
                self._on_send()
            return
        # 其他键：默认行为
        from app.core.qt_compat import QPlainTextEdit as _QPT
        _QPT.keyPressEvent(self.input_edit, event)

    def _open_dashboard(self) -> None:
        """托盘 / 标题栏共享：打开调试面板。"""
        app = self._find_app()
        if app is not None and hasattr(app, "open_dashboard"):
            app.open_dashboard()
        else:
            from app.main import start_dashboard_subprocess, wait_dashboard_ready, open_in_browser
            from pathlib import Path
            pid = start_dashboard_subprocess(Path.cwd(), port=8765)
            if pid and wait_dashboard_ready(port=8765, timeout=8.0):
                open_in_browser("http://127.0.0.1:8765")
                self._append_system_msg("📊 调试面板已打开 (http://127.0.0.1:8765)")
            else:
                self._append_system_msg("⚠️ 调试面板启动失败，查看 data/dashboard.log")

    def _find_app(self):
        qa = QApplication.instance()
        return getattr(qa, "_desktop_pet_app", None) if qa else None

    def _append_system_msg(self, text: str) -> None:
        """在聊天区追加一条系统消息（灰色提示）。"""
        self._render_message(Message(role="system", content=text))

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

    # ---------------- 历史/输入 ----------------
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
                "💡 命令：/喂 /状态 /时间 /记 /说 /记忆 /提醒 /清除 /调试 /打开")
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
                    "• /调试      — 打开 Dashboard\n"
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

        # /调试：打开 Dashboard
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

    # ---------------- 语音输入（ASR） ----------------
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

    # ---------------- 流式生成 ----------------
    def _kickoff_llm(self) -> None:
        if not self.llm_cfg.api_key or self.llm_cfg.api_key == "PUT-YOUR-API-KEY-HERE":
            self._render_message(Message(
                role="assistant",
                content="还没填 API Key，请先打开 config.yaml，把 llm.api_key 改成你的 key 再重启。",
                emotion=Emotion.CONFUSED,
            ))
            return

        # 构造请求 messages（用 history）
        msgs = [ChatMessage(role=m.role, content=m.content) for m in self.history
                if m.role in ("user", "assistant")]

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
        self._streaming_anchor_pos = self._insert_placeholder(self._current_bot_msg)

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
                # 标准后端关闭在 _on_done / _on_failed 里做
                self._lc_agent = lc_agent
            else:
                self._worker = _AgentWorker(
                    AgentLoop(client, self.registry),
                    [{"role": m.role, "content": m.content} for m in msgs])
                self._worker.chunk.connect(self._on_chunk)
                self._worker.tool_used.connect(self._on_tool)
        else:
            self._worker = _StreamWorker(client, msgs)
            self._worker.chunk.connect(self._on_chunk)
        self._worker.done.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_tool(self, name: str, args: str, result: str) -> None:
        """一次工具调用完成：在当前气泡里追加一行工具记录。"""
        if self._current_bot_msg is None:
            return
        summary = result.replace("\n", " ")[:80]
        self._current_bot_msg.tools.append((name, summary))
        # Trace
        if self.trace is not None and self._trace_run_id:
            try:
                import json as _json
                self.trace.record(self._trace_run_id, "tool", {
                    "name": name,
                    "args": _json.loads(args) if isinstance(args, str) else args,
                    "result": summary,
                })
            except Exception:  # noqa: BLE001
                pass
        self._refresh_streaming_message()

    def _on_chunk(self, tok: str) -> None:
        if self._current_bot_msg is None:
            return
        self._current_bot_msg.content += tok
        self._refresh_streaming_message()
        # Trace：累计的完整文本（每 chunk 一次）
        if self.trace is not None and self._trace_run_id and tok:
            self.trace.record(self._trace_run_id, "text", {
                "delta": tok,
                "accumulated": self._current_bot_msg.content[:500],
            })
        # 同步到桌宠头顶气泡（只同步纯文本，跳过空内容）
        text = self._current_bot_msg.content.strip()
        if text:
            self.streaming_chunk.emit(text)

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
        if parsed.emotion == Emotion.HAPPY and parsed.text == self._current_bot_msg.content:
            # 模型没标情绪，按关键词兜底
            parsed.emotion = guess_emotion(parsed.text)
        self._current_bot_msg.content = parsed.text
        self._current_bot_msg.emotion = parsed.emotion
        self.history.append(self._current_bot_msg)
        self._trim_history()
        # 持久化 bot 消息
        tools_list = [list(t) for t in self._current_bot_msg.tools] if self._current_bot_msg.tools else []
        self.chat_store.add(
            "assistant", parsed.text,
            emotion=parsed.emotion.value if parsed.emotion else "",
            tools=tools_list,
        )
        self._refresh_streaming_message(finished=True, emotion=parsed.emotion.value)
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
        # 通知桌宠：思考结束
        self.thinking_stopped.emit()
        # 通知外部（pet 窗口）切表情 + 触发 TTS
        self.reply_ready.emit(parsed.text, parsed.emotion, self.char_cfg.tts_enabled)
        # 通知桌宠气泡：流式输出结束
        self.streaming_done.emit()
        # 清除当前消息状态，避免重复
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

    # ---------------- 渲染 ----------------
    def _msg_html(self, msg: Message, *, streaming_meta: Optional[str] = None) -> str:
        """按已完成的 Message 渲染成 HTML 字符串（控制台风格单行聊天）。

        设计：
            - 系统消息：居中灰条
            - 用户消息：左对齐，单行（默认色）
            - 桌宠消息：左对齐，单行（浅蓝色 #7eb6ff）
            - 工具调用：在 bot 消息下方一行一行显示（小灰字）
            - 不显示角色名 / 时间戳
        """
        # ---- 系统消息：居中灰色条 ----
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

        # 工具调用（bot 消息专用，灰色小字，紧凑）
        tools_html = "".join(
            f'<div style="color:#888;font-size:9pt;margin:2px 0;">↳ {name} → {summary}</div>'
            for name, summary in msg.tools
        )

        # 消息体：每个 <p> 独立 block，margin-bottom:10px 视觉分段
        # 关键：Qt QTextBrowser 用 QTextDocument 解析，cursor.insertHtml() 会把多个 <p> 合并成一个块。
        # 必须在 HTML 末尾追加 "\n\n"（纯文本换行）才能真正分段——QTextDocument 把 \n 当段落分隔。
        # 用户：默认色；桌宠：浅蓝色
        color = "#2c2c38" if is_user else "#4a90e2"
        msg_p = (
            f'<p style="margin:0 0 10px 0;color:{color};">'
            f'{safe}</p>'
            f'\n\n'   # 强制 QTextDocument 新段落（关键！）
        )

        return f'{msg_p}{tools_html}'

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

        # 无序列表（每行以 - 开头）
        text = re.sub(r'^- (.+)$', r'<li>\1</li>', text, flags=re.MULTILINE)
        text = re.sub(r'(<li>.*</li>)', r'<ul>\1</ul>', text, flags=re.DOTALL)

        # 有序列表（数字. 开头）
        text = re.sub(r'^(\d+)\. (.+)$', r'<li>\1. \2</li>', text, flags=re.MULTILINE)
        text = re.sub(r'(<li>\d+\..*</li>)', r'<ol>\1</ol>', text, flags=re.DOTALL)

        # 换行
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
        cursor.insertBlock()  # PR-fix-line-breaks: 让下一条消息从新 <p> 开始
        sb = self.chat_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _refresh_streaming_message(self, finished: bool = False, emotion: str = "") -> None:
        """流式刷新「当前 bot 占位 div」——从 anchor 到末尾删除旧内容后插入新内容。"""
        meta = emotion if finished else "typing…"
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

    # ---------------- 信号 ----------------
    reply_ready = Signal(str, object, bool)   # text, Emotion, tts_enabled
    streaming_chunk = Signal(str)              # 流式输出增量（已累积的完整文本）
    streaming_done = Signal()                  # 流式输出结束
    thinking_started = Signal()                # 模型开始思考（触发表情）
    thinking_stopped = Signal()                # 模型思考结束（回到 idle）