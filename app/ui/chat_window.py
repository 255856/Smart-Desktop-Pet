"""聊天窗口：把桌宠和大模型对话接到一起。"""
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
from app.brain.llm_client import ChatMessage, LLMClient, LLMError
from app.brain.agent import AgentLoop
from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
from app.engine.tools import ToolRegistry
from app.voice.asr import SpeechRecognizer, asr_available
from app.engine.chat_store import ChatStore
from app.brain.trace import TraceRecorder
from app.ui import ui_style
from app.ui.memory_panel import MemoryDialog

log = logging.getLogger(__name__)


# 工具名 -> (中文名, 动作目标参数键，按优先级)
_TOOL_UI_META = {
    "web_search": ("联网搜索", ["query", "q", "keyword"]),
    "open_website": ("打开网页", ["url", "website", "query"]),
    "open_app": ("启动应用", ["app_name", "name", "app"]),
    "list_installed_apps": ("查找应用", ["query", "keyword"]),
    "calculate": ("计算", ["expression", "query"]),
    "convert_units": ("单位换算", ["query", "expression"]),
    "add_reminder": ("设置提醒", ["content", "text", "title", "message"]),
    "list_reminders": ("查看提醒", []),
    "delete_reminder": ("删除提醒", ["content", "title", "index"]),
    "remember_fact": ("记住信息", ["content", "fact", "key"]),
    "recall_memory": ("回忆信息", ["query", "key", "topic"]),
    "forget_memory": ("忘记信息", ["key", "query"]),
    "take_screenshot": ("屏幕截图", []),
    "system_info": ("系统信息", []),
    "get_current_time": ("查询时间", ["timezone"]),
    "date_info": ("查询日期", ["query"]),
    "get_pet_status": ("桌宠状态", []),
    "feed_self": ("投喂", ["food"]),
    "play_animation": ("播放动画", ["animation", "name"]),
    "change_pet_emotion": ("切换表情", ["emotion"]),
    "say_to_user": ("发送消息", ["text", "content"]),
    "clipboard_copy": ("复制到剪贴板", ["text", "content"]),
    "send_notification": ("发送通知", ["title", "message"]),
    "list_desktop_files": ("查看桌面文件", []),
    "read_text_file": ("读取文件", ["path", "file"]),
    "open_task_manager": ("打开任务管理器", []),
    "open_control_panel": ("打开控制面板", []),
    "open_windows_settings": ("打开系统设置", []),
    "open_file_explorer": ("打开文件资源管理器", []),
    "open_terminal": ("打开终端", []),
    "open_notepad": ("打开记事本", []),
    "open_calculator": ("打开计算器", []),
}
_CIRCLED_NUMS = ["①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩"]
_TOOL_FAIL_MARKERS = ("失败", "错误", "无法", "未安装", "没有找到", "找不到")


def _unpack_tool(t):
    """tools 元组兼容 2 元 (name, summary) 与 3 元 (name, args, result)。"""
    if len(t) >= 3:
        return t[0], t[1] or "", t[2] or ""
    return t[0], "", (t[1] if len(t) > 1 else "") or ""


def _tool_label(name: str, args: str):
    """返回 (中文名, 动作目标短文本)。"""
    import json as _json
    cn, keys = _TOOL_UI_META.get(name, (name, []))
    target = ""
    a = {}
    try:
        a = _json.loads(args) if args else {}
    except Exception:  # noqa: BLE001
        a = {}
    if isinstance(a, dict):
        for k in keys:
            v = a.get(k)
            if v:
                target = str(v)
                break
        if not target:
            # 兜底：取第一个短参数
            for v in a.values():
                if isinstance(v, (str, int, float)) and 0 < len(str(v)) <= 30:
                    target = str(v)
                    break
    if target:
        if name == "open_website" and "://" in target:
            target = target.split("://", 1)[1]
        if target.startswith("www."):
            target = target[4:]
        if len(target) > 20:
            target = target[:20] + "…"
    return cn, target


def _tool_status(result: str) -> str:
    r = result or ""
    if "取消" in r:
        return "cancel"
    if any(k in r for k in _TOOL_FAIL_MARKERS):
        return "fail"
    return "ok"


@dataclass
class Message:
    role: str
    content: str
    emotion: Optional[Emotion] = None
    ts: float = field(default_factory=time.time)
    tools: list[tuple[str, str, str]] = field(default_factory=list)  # (工具名, args_json, 结果)


class _AgentWorker(QThread):
    """Agent 循环 worker：流式正文 + 工具调用（Function Calling）。

    agent.run 产出 ("text"|"tool"|"done"|"meta"|"reflection"|"plan", ...) 事件，
    这里转成 Qt 信号：
        chunk     —— 正文增量
        tool_used —— 一次工具执行完成 (name, args, result)
        meta      —— 内部事件（force_retry / replan 等），仅 trace + 状态显示
        done      —— 完整文本（所有正文段拼接）
        failed    —— 出错
    """

    chunk = Signal(str)
    tool_used = Signal(str, str, str)
    meta = Signal(str, dict)       # (kind, payload) —— 内部事件，给 Trace 用
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

        修复：把 `done` 事件的 payload 也作为 final 文本的兜底（防御性）。
        历史上某些 AgentLoop 变体（如 AgentLoopV2 的 Planner+Executor 模式）
        不一定 yield text 事件就 yield done，导致 `chat reply ready: ''`。

        处理推理模型：text 事件里**不再**含 reasoning_content / reasoning 字段
        （由 LLMClient._do_stream_request 区分）。reasoning 通过 meta 事件
        {"event": "reasoning", "content": "..."} 转发给 Trace，UI 不显示。
        """
        try:
            full: list[str] = []
            done_fallback: str = ""   # 防御：done 事件 payload 作为 last-resort
            reasoning_parts: list[str] = []   # 收集推理模型的思考内容（Trace 用）
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
                    elif kind == "meta":
                        # payload = (dict)
                        self.meta.emit("meta", payload[0] if payload else {})
                    elif kind in ("plan", "reflection"):
                        # 透传到 Trace
                        self.meta.emit(kind, payload[0] if payload else {})
                    elif kind == "done":
                        # 防御：如果之前没收到任何 text，把 done payload 当兜底
                        if payload and payload[0]:
                            done_fallback = str(payload[0])
            else:
                async def drive(done_box: list[str],
                                reasoning_box: list[str]) -> None:
                    """驱动 agent.run 异步迭代。

                    done_box: 用于回传 done 兜底文本（防御性）
                    reasoning_box: 用于回传推理模型思考内容（Trace 用，不给 UI）
                    """
                    async for ev in self.agent.run(
                            self.messages, cancel_check=self.cancel_event.is_set):
                        kind = ev[0]
                        if kind == "text":
                            full.append(ev[1])
                            self.chunk.emit(ev[1])
                        elif kind == "tool":
                            self.tool_used.emit(ev[1], ev[2], ev[3])
                        elif kind == "meta":
                            # ev = ("meta", dict)
                            meta_payload = ev[1] if len(ev) > 1 else {}
                            # 特殊：reasoning meta 累积到 reasoning_box（一次性 trace）
                            if isinstance(meta_payload, dict) and \
                                    meta_payload.get("event") == "reasoning_delta":
                                reasoning_box.append(meta_payload.get("content", ""))
                                # 不 emit 给 UI（meta 仍然 emit 给 Trace 记录）
                            self.meta.emit("meta", meta_payload)
                        elif kind in ("plan", "reflection"):
                            self.meta.emit(kind, ev[1] if len(ev) > 1 else {})
                        elif kind == "done":
                            # 防御：记录 done payload 作为兜底文本
                            if len(ev) > 1 and ev[1]:
                                done_box.append(str(ev[1]))

                done_box: list[str] = []
                reasoning_box: list[str] = []
                asyncio.run(drive(done_box, reasoning_box))
                if done_box:
                    done_fallback = done_box[-1]
                # 一次性把推理内容透传给 Trace（不发给 UI）
                if reasoning_box:
                    self.meta.emit("meta", {
                        "event": "reasoning",
                        "content": "".join(reasoning_box),
                    })
            # 拼接最终文本：如果累积的 full 为空且 done 有 payload，用 done 的
            final_text = "".join(full) or done_fallback
            self.done.emit(final_text)
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


#  / 命令补全（QPlainTextEdit 没有 setCompleter，自己写一个轻量版）


def _html_escape(text: str) -> str:
    """HTML 字符转义（防 XSS + 让浏览器不解析）。"""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))


def _format_time_short(ts: float) -> str:
    """把时间戳格式化成 HH:MM（用于气泡上方 meta）。"""
    import datetime
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")
    except Exception:
        return ""


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


class _TitleBarDrag(QObject):
    """无边框窗口标题栏拖动：按住标题区 / 头像即可拖动整个窗口。"""

    def __init__(self, win: "ChatWindow"):
        super().__init__(win)
        self._win = win
        self._offset = QPoint()
        self._dragging = False

    def eventFilter(self, obj, ev) -> bool:
        t = ev.type()
        if t == QEvent.Type.MouseButtonPress and \
                ev.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._offset = (event_global_pos(ev)
                            - self._win.frameGeometry().topLeft())
            return True
        if t == QEvent.Type.MouseMove and self._dragging and \
                (ev.buttons() & Qt.MouseButton.LeftButton):
            self._win.move(event_global_pos(ev) - self._offset)
            return True
        if t == QEvent.Type.MouseButtonRelease:
            self._dragging = False
            return True
        return False


class _InputFocusWatcher(QObject):
    """输入框获得 / 失去焦点时高亮输入容器边框（Qt QSS 不支持 :focus-within）。"""

    def __init__(self, container: QFrame):
        super().__init__(container)
        self._container = container

    def eventFilter(self, obj, ev) -> bool:
        if ev.type() == QEvent.Type.FocusIn:
            self._container.setStyleSheet(
                "QFrame { background: #ffffff; border: 2px solid #a99bfa; "
                "border-radius: 16px; }")
        elif ev.type() == QEvent.Type.FocusOut:
            self._container.setStyleSheet("")
        return False


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
        else:
            log.warning("ASR 不可用（未安装 sounddevice / faster-whisper，或配置关闭），语音输入按钮将隐藏")

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
        if not self.llm_cfg.api_key or self.llm_cfg.api_key == "PUT-YOUR-API-KEY-HERE":
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
                    AgentLoop(client, self.registry),
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
        summary = result.replace("\n", " ")[:80]
        # 保留完整 args / result，渲染时解析中文名、动作目标与成败状态
        self._current_bot_msg.tools.append((name, args or "", result or ""))
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

    def _on_meta(self, kind: str, payload: dict) -> None:
        """内部事件（plan / reflection / meta）。当前主要做两件事：
            1. Trace 记录（Dashboard 可回放决策过程）
            2. force_retry 时给主人一个轻提示（不打断流式输出）

        Args:
            kind: "plan" | "reflection" | "meta"
            payload: 事件载荷
        """
        if self.trace is not None and self._trace_run_id:
            try:
                self.trace.record(self._trace_run_id, kind, payload or {})
            except Exception:  # noqa: BLE001
                pass
        # force_retry 给一个小提示（避免主人误以为模型已经做完）
        if kind == "meta" and isinstance(payload, dict):
            ev = payload.get("event")
            if ev == "force_retry":
                # 在 chat 末尾追加一条灰色提示（小气泡，不抢戏）
                self._append_system_msg(
                    "⚙️ 模型刚才回了文字但没调用工具——自动重试一次，主人稍等~"
                )
                log.info("ChatWindow: force_retry meta 事件已提示用户")
            elif ev == "verify_retry":
                # 工具结果与模型答复矛盾（如工具失败却报喜），已要求重答
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
        self._tts_tail = ""

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
        # 通知外部（pet 窗口）切表情（TTS 已通过 _tts_drain_sentences / _flush_tts_tail 启动播放）
        self.reply_ready.emit(parsed.text, parsed.emotion, bool(self.char_cfg.tts_enabled))
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

        # 工具调用（bot 消息专用）：Claude Code 风格步骤卡片，置于最终结果上方
        tools_html = self._tool_steps_html(msg.tools) if msg.tools else ""
        if tools_html:
            safe = tools_html + safe

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