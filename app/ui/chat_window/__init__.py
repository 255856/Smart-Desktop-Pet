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

import logging
import re
import threading
import time
from pathlib import Path
from typing import Callable, Optional

# 2026-10-14 美化：用户/桌宠头像持久化目录(.gitignore 的 data/ 兜底,不会入库)
AVATAR_DIR = Path("data") / "avatars"
AVATAR_DIR.mkdir(parents=True, exist_ok=True)


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


def _convert_loaded_tools(stored_tools):
    """把 ChatStore 读回的 tools（JSON 序列化的 list-of-lists）转为
    Message dataclass 期望的 list[tuple[str, str, str]] 形态。

    SQLite 里存的是 tools_json = JSON，每个工具是 [name, args, result] 三元 list。
    _on_done 里写入是 tools_list = [list(t) for t in self._current_bot_msg.tools]，
    反序列化后应当还原成 tuple 给 Message(... tools=...)，否则 _msg_html 渲染时
    isintance 检查失败，"已执行操作" 工具卡片丢失（Bug 2：重进聊天窗没工具信息）。

    兼容旧版 2 元组 (name, summary)；丢掉一切损坏项（避免单条坏数据搞挂整次加载）。
    """
    out = []
    for t in (stored_tools or []):
        if isinstance(t, (list, tuple)) and len(t) >= 3:
            out.append((str(t[0]), str(t[1]), str(t[2])))
        elif isinstance(t, (list, tuple)) and len(t) == 2:
            # 旧版 2 元组 (name, summary)
            out.append((str(t[0]), "", str(t[1])))
        # 其他形态：跳过（防御性，不抛）
    return out


from app.core.qt_compat import (
    QApplication, QFileDialog, QFont, QFrame, QHBoxLayout, QKeyEvent,
    QLabel, QMenu, QMessageBox, QPixmap, QPlainTextEdit, QPushButton,
    Qt, QTextBrowser, QTextCursor, QToolButton,
    QVBoxLayout, QWidget, Signal, QTimer, QColor, QRect,
    QGraphicsDropShadowEffect, QPainter, QPainterPath, QLinearGradient,
    QBuffer, QByteArray, pyqtSlot,
    QDragEnterEvent, QDragMoveEvent, QDropEvent, QMimeData, QUrl,
    QScrollArea,
)

from app.voice.character import Emotion, guess_emotion, parse_reply
from app.core.config import CharacterConfig, LLMConfig
from app.core.api_keys import is_placeholder_key
from app.brain.llm_client import ChatMessage, LLMClient
from app.brain.agent import AgentLoop, DANGEROUS_TOOLS
from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
from app.engine.tools import ToolRegistry
from app.voice.asr import SpeechRecognizer, asr_available
from app.engine.chat_store import ChatStore

# 危险工具的中文标签（弹窗里给主人看）+ 等待上限 — 拆到 chat_window.utils
from .utils import _DANGEROUS_LABELS, _CONFIRM_TIMEOUT_S  # noqa: F401
from app.brain.trace import TraceRecorder
from app.ui import ui_style
from app.ui.elided_label import ElidedLabel
from app.ui.memory_panel import MemoryDialog

log = logging.getLogger(__name__)


class _NoCmdNavQTextBrowser(QTextBrowser):
    """chat_view 专用 QTextBrowser —— 覆盖 doSetSource 让内部 cmd: 链接不导航。

    2026-10-15 v4: PyQt5 5.x 的 setOpenLinks(False) 在 openLinks=False 时
    不只是禁止默认导航, 还会**禁用 click** (mousePressEvent 不再走链接路径,
    anchorClicked 信号也不发, 用户实测「点击没反应」)。

    正确做法是 subclass QTextBrowser 覆盖 doSetSource —— 默认实现尝试把
    url 解析成本地文件路径, 找不到时用空 QTextDocument 替换 view 造成白屏。
    我们的 override 让 cmd: scheme 走 no-op (不让 QTextBrowser 替换 view),
    http/https/mailto 走父类默认 (setOpenExternalLinks(False) 之后不会
    触发 OS 弹窗, anchorClicked 信号在 click 时仍正常 emit, 由
    _on_chat_anchor_clicked 路由到对应 handler)。
    """
    def doSetSource(self, name):
        try:
            url = name.toUrl() if hasattr(name, "toUrl") else name
            scheme = url.scheme() if hasattr(url, "scheme") else ""
        except Exception:  # noqa: BLE001
            scheme = ""
        if scheme in ("http", "https", "mailto"):
            # 外部链接: 让 setOpenExternalLinks(False) + anchorClicked
            # 处理, 父类 doSetSource 实际不会调到这里 (被前面的 flag 拦住)
            return
        # cmd: / 其它 scheme: 不导航, 不替换 view, 让 chat 保持原样
        return





# 工具 UI 元数据 + 工具结果状态 + 工具元组解包 — 拆到 chat_window.utils
from .utils import (  # noqa: F401
    _TOOL_UI_META, _CIRCLED_NUMS, _TOOL_FAIL_MARKERS,
    _unpack_tool, _tool_label, _tool_status, _unpack_status_mark,
)


# Message — 拆到 chat_window.message
from .message import Message  # noqa: F401


# _AgentWorker / _StreamWorker / _PlanWorker — 拆到 chat_window.worker
from .worker import _AgentWorker, _StreamWorker, _PlanWorker  # noqa: F401


#  / 命令补全（QPlainTextEdit 没有 setCompleter，自己写一个轻量版）


# _html_escape / _format_time_short — 拆到 chat_window.utils
from .utils import _html_escape, _format_time_short  # noqa: F401
# _CommandCompleter — 拆到 chat_window.command_completer
from .command_completer import _CommandCompleter  # noqa: F401


# _TitleBarDrag — 拆到 chat_window.chrome
from .chrome import _TitleBarDrag  # noqa: F401


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
                 memory_store: Optional[object] = None,
                 chat_cfg=None,
                 cfg_path: str = "config.yaml",
                 # 接线(a):Phase 2.1-3.4 引擎层上下文 —— 全部透传给 LangChainAgent
                 cwd: Optional[str] = None,
                 session_id: Optional[str] = None,
                 session_store: Optional[object] = None,
                 skills_dir: Optional[str] = None,
                 auto_load_skills: Optional[list] = None,
                 extra_docs_paths: Optional[list] = None):
        super().__init__(parent)
        self.llm_cfg = llm_cfg
        self.char_cfg = char_cfg
        # 把 llm.project_dirs(用户配置)+ cwd(自动注入) 加进沙箱白名单。
        # grep_files / glob_files / search_files / edit_file / multi_edit 等工具都受这条
        # 限制影响。
        # 关键:cwd 是项目根(如 E:\study\desktop-pet),即使用户没在 config.yaml 里
        # 写 project_dirs,自动把 cwd 加进白名单,这样 chat 里说「在 E:\study\xxx
        # 项目下搜 YY」也能跑通,而不是被一句「不在沙箱内」挡掉。
        try:
            from app.engine.tools import _filesearch
            extra = list(getattr(self.llm_cfg, "project_dirs", []) or [])
            if cwd:
                # 把 cwd 自身 + cwd 父目录链也都放行,允许 cwd 上级有项目文档
                try:
                    from pathlib import Path as _P
                    extra.append(str(_P(cwd).resolve()))
                except Exception:  # noqa: BLE001
                    extra.append(cwd)
            _filesearch.set_extra_safe_roots(extra)
        except Exception as e:  # noqa: BLE001
            log.warning("set_extra_safe_roots 失败: %s", e)
        self._chat_timeout_s: int | None = (
            chat_cfg.request_timeout_s if chat_cfg is not None else None
        )
        # 在 AI 气泡上方展示「已执行操作 · N」卡片（开关见 ChatConfig.show_tool_steps），
        # 主人如果嫌啰嗦可以在 config.yaml / 设置面板里关掉。
        self._show_tool_steps: bool = (
            chat_cfg.show_tool_steps if chat_cfg is not None else True
        )
        # 工具步骤默认折叠 —— Qt QTextBrowser 不支持 <details>,用 anchorClicked
        # 切回 setHtml 模拟。新一轮 chat 起始 reset 成 True。
        # 2026-10-15 v6: 改成按消息分别记折叠状态（dict，键 = id(msg)）——
        # 之前 self._tools_collapsed 是全局 bool, 点 A 消息的 toggle 会影响所有消息
        # 的工具卡片 (用户实测「点击一个后所有的都会展开」)。
        self._tools_collapsed_by_msg: dict = {}
        self._tools_collapsed_default: bool = True  # 新消息默认折叠
        # 2026-10-14 美化：用户改头像后,要把 chat 段写回 config.yaml
        self._cfg_path: str = cfg_path
        # TTS 引擎引用（用于在 _on_done 时同步 prepare 音频，让聊天窗回复与声音同步）
        self.tts = tts
        # 长期记忆存储（MemoryStore，可选）：供标题栏「记忆」管理面板增删
        self.memory_store = memory_store
        self._memory_dlg: Optional[MemoryDialog] = None
        # 接线(a):Phase 2/3 引擎层上下文 —— 全存下来,LangChainAgent 构造时透传
        # cwd:项目根(用作 AGENTS.md 向上查找的起点 + 项目级指令上下文)
        # session_id/session_store:对齐 Claude Code/ZCode 的会话列表 / 续接
        # skills_dir/auto_load_skills:加载 skills/*.md 行为说明
        # extra_docs_paths:主程序注入的额外 AGENTS.md / CLAUDE.md 路径
        self._cwd = cwd
        self._session_id = session_id
        self._session_store = session_store
        self._skills_dir = skills_dir
        self._auto_load_skills = list(auto_load_skills or [])
        self._extra_docs_paths: list = list(extra_docs_paths or [])
        # v9: 当前会话激活的 skill 名 (单独 /skills/<name> 切换, 整段对话期间
        # 拼到 system_prompt 末尾)。v12 起主线改 per-message (msg.skill),
        # 这字段保留作为兜底 + 老测试兼容。
        self._active_skill: Optional[str] = None
        # v12: 从 cfg_path 读 multimodal 段, 注入到 _multimodal.setup_config,
        # 之后 generate_image / video / audio tool 直接读 cfg。v11 的
        # multimodal_router / _active_api 段已删 (单一组 config 不再需要路由)。
        # cfg 读失败 (yaml 没装 / 路径不对 / 没 multimodal 段) 都视作空 dict,
        # 后续 _check_or_raise 会告诉主人怎么填。
        try:
            import yaml as _yaml
            with open(cfg_path, encoding="utf-8") as _f:
                _full_cfg = _yaml.safe_load(_f) or {}
            _mm_cfg = (_full_cfg.get("multimodal") or {}) if isinstance(_full_cfg, dict) else {}
        except Exception:  # noqa: BLE001
            _mm_cfg = {}
        try:
            from app.engine.tools import _multimodal as _mm
            _mm.setup_config(_mm_cfg)
        except Exception:  # noqa: BLE001
            log.debug("multimodal.setup_config 注入失败 (非致命)", exc_info=True)
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
        self._MAX_HISTORY = 50  # 最多保留 50 条对话（超出则丢弃最早的）
        # 送给 LLM 的上下文字符预算。只按条数裁剪会让长对话越滚越贵：
        # 单条输入上限 2000 字符，50 条最坏 10 万字符，每轮全量重发。
        # 12000 字符 ≈ 4~6K token，够覆盖最近十来个来回，又不至于拖垮延迟。
        self._CTX_CHAR_BUDGET = 12000
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
            # 预热：延迟 1s 在主线程调度 _get_model()，让首次说话瞬时返回
            self._warmup_asr_model()
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
        # 2026-10-15 v8: 主人实测要求「拖动图片直接渲染」—— 整个窗口接收 drag&drop
        # 进入时拦截, 把图片保存到 assets/uploads 后渲染进 chat_view。
        self.setAcceptDrops(True)

        # 2026-10-15:修复「Bug 3 窗口重叠 / 错位显示」——
        # 默认让 chat 出现在「桌宠精灵的右侧」(主人大概率会在那里点),如果没有
        # 桌宠位置就居中。同时用 QSettings 记忆上次关闭时的位置,下次打开就
        # 恢复到那里(不会每次跳到屏幕正中)。超出当前屏幕也裁回。
        self._restore_or_position_window(parent)

        # 移动事件 → 实时记到 QSettings,下次打开恢复
        self._save_pos_on_close = True

        self._avatar_path = self._pick_avatar()
        self.avatar_label: Optional[QLabel] = None
        # 富文本圆形头像（QTextBrowser 对 document 资源在 clear() 后会失效，
        # 改用自包含的 data URI：<img src="data:image/png;base64,...">）
        self._avatar_data_bot: str = ""
        # 2026-10-15 v2: 用户头像已移除(用户实测指定"把更改头像中的我的头像功能去除,
        # 只保留桌宠头像"), 不再需要 _avatar_data_user / _user_avatar_path。
        # 桌宠头像保留 (chat_cfg.bot_avatar_path)。
        self._bot_avatar_path: str = (
            chat_cfg.bot_avatar_path if chat_cfg is not None else ""
        ) or ""
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
        # 2026-10-15:句子朗读串行化 — 每句分配 _seq 序号,准备完成不直接 speak,
        # 而是入队到 _pending_speak;_drain_speak_queue 按编号连续性 speak,
        # 保证朗读顺序跟句子到达顺序一致(避免并发 prepare 颠倒)。
        self._tts_seq_next = 0
        self._tts_seq_drain = 0
        self._pending_speak: dict[int, str] = {}
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

        2026-10-15 v2:用户头像已移除,_make_avatar_pixmap('user', ...) 现在
        直接返回空 QPixmap(不再生成「我」字蓝渐变)。chat 内部不再 render
        用户头像,所以这个方法对 user 角色来说没用了, 但保留入口以防
        外部代码意外调用, 给出空 pixmap 比生成假图更安全。
        """
        if role != "bot":
            # user 角色已禁用,返回空 pixmap
            return QPixmap()
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)

        # 1) 优先用用户自定义头像
        custom = self._load_custom_avatar_pixmap("bot")
        if custom is not None and not custom.isNull():
            self._draw_avatar_from_pixmap(p, custom, size)
            p.end()
            return pm

        # bot 默认: assets/icon.ico → 角色首帧 → 首字渐变
        icon = Path(__file__).resolve().parent.parent.parent / "assets" / "icon.ico"
        src = QPixmap(str(icon)) if icon.is_file() else QPixmap()
        if src.isNull() and self._avatar_path:
            src = QPixmap(str(self._avatar_path))
        if not src.isNull():
            self._draw_avatar_from_pixmap(p, src, size)
        else:
            grad = QLinearGradient(0, 0, size, size)
            grad.setColorAt(0.0, QColor("#f9a8d4"))
            grad.setColorAt(1.0, QColor("#ec6aa9"))
            p.setBrush(grad)
            p.drawEllipse(0, 0, size, size)
            self._draw_avatar_char(p, (self.char_cfg.name or "宠")[:1], size)
        p.end()
        return pm
        pm = QPixmap(size, size)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)

        # 1) 优先用用户自定义头像
        custom = self._load_custom_avatar_pixmap(role)
        if custom is not None and not custom.isNull():
            self._draw_avatar_from_pixmap(p, custom, size)
            p.end()
            return pm

        if role == "bot":
            # 聊天窗桌宠头像统一使用 assets/icon.ico；缺失时回退角色首帧
            icon = Path(__file__).resolve().parent.parent.parent / "assets" / "icon.ico"
            src = QPixmap(str(icon)) if icon.is_file() else QPixmap()
            if src.isNull() and self._avatar_path:
                src = QPixmap(str(self._avatar_path))
            if not src.isNull():
                self._draw_avatar_from_pixmap(p, src, size)
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

    def _load_custom_avatar_pixmap(self, role: str) -> Optional[QPixmap]:
        """读用户自定义头像(返回 None = 没设/读不到,继续走默认)。

        2026-10-15 v2: 只支持 bot (桌宠) 角色;user 角色已移除。
        """
        if role != "bot":
            return None
        path = self._bot_avatar_path
        if not path:
            return None
        p = Path(path)
        if not p.is_file():
            return None
        try:
            return QPixmap(str(p))
        except Exception:  # noqa: BLE001
            return None

    def _draw_avatar_from_pixmap(self, p: QPainter, src: QPixmap, size: int) -> None:
        """把任意 QPixmap 居中裁剪成圆形头像（外圈 2px 白边）。"""
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
        """生成并缓存桌宠圆形头像的 data URI（40px）。

        2026-10-15 v2: 只生成 bot 头像;user 头像已移除。
        """
        self._avatar_data_bot = self._pixmap_to_data_uri(self._make_avatar_pixmap("bot", 40))

    def set_avatar(self, path: Path) -> None:
        """外部更新角色头像（切换角色时用）。"""
        self._avatar_path = path
        # 标题栏头像
        self.avatar_label.setPixmap(self._make_avatar_pixmap("bot", 44))
        self.avatar_label.setFixedSize(44, 44)
        # 富文本头像缓存（只影响之后的新消息）
        self._register_avatar_resources()

    # 2026-10-14 美化：用户和桌宠头像自定义入口
    def pick_bot_avatar(self, parent=None) -> bool:
        """弹文件选择对话框让用户给桌宠选头像;选完复制到 data/avatars/bot.png。

        2026-10-15 v2: 用户头像已移除,只保留桌宠头像。
        """
        from PyQt5.QtWidgets import QFileDialog
        src, _ = QFileDialog.getOpenFileName(
            parent or self,
            "选择桌宠的头像",
            "",
            "Images (*.png *.jpg *.jpeg *.bmp *.gif)",
        )
        if not src:
            return False
        return self._set_custom_avatar("bot", src)

    def _set_custom_avatar(self, role: str, src_path: str) -> bool:
        """复制 src_path 到 data/avatars/<role>.png,更新 self._<role>_avatar_path,刷新 UI。

        2026-10-15 v2: 只支持 bot 角色;user 角色已移除。

        选空文件 / 复制失败 / 加载 QPixmap 失败 → 返回 False,不污染当前状态。
        """
        import shutil
        if role != "bot":
            return False
        src = Path(src_path)
        if not src.is_file():
            return False
        # 试加载,坏的图直接拒
        test_pm = QPixmap(str(src))
        if test_pm.isNull():
            return False
        # 复制到 data/avatars/bot.png(覆盖)
        dst = AVATAR_DIR / f"{role}.png"
        try:
            shutil.copy2(src, dst)
        except OSError:
            return False
        self._bot_avatar_path = str(dst)
        # 标题栏头像(桌宠那个)
        if self.avatar_label is not None:
            self.avatar_label.setPixmap(self._make_avatar_pixmap("bot", 44))
            self.avatar_label.setFixedSize(44, 44)
        # 富文本头像缓存(影响之后的新消息)
        self._register_avatar_resources()
        # 2026-10-14 持久化：把 chat 段写回 config.yaml,下次启动自动恢复
        self._persist_chat_overrides()
        return True

    def _persist_chat_overrides(self) -> None:
        """把当前 chat 段(头像路径等)merge 写回 config.yaml。"""
        try:
            from app.core.config import Config, save_chat_overrides
            from app.core.config import ChatConfig as _CC
            chat_cfg = _CC(
                request_timeout_s=self._chat_timeout_s or 20,
                show_tool_steps=self._show_tool_steps,
                bot_avatar_path=self._bot_avatar_path,
            )
            # 构造一个最小 Config 实例(只 chat 段会被 save_chat_overrides 用到)
            cfg = Config(chat=chat_cfg)
            save_chat_overrides(self._cfg_path, cfg)
        except Exception:  # noqa: BLE001
            # 写盘失败不污染 UI,主流程已经改好了
            pass

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
            │ [语音]  0 / 2000                  [停止][发送]   │
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
        hl.setContentsMargins(12, 8, 8, 8)
        hl.setSpacing(4)

        # 头像（圆形裁剪 + 白色描边，复用富文本头像的统一绘制）
        self.avatar_label = QLabel()
        self.avatar_label.setPixmap(self._make_avatar_pixmap("bot", 40))
        self.avatar_label.setFixedSize(40, 40)
        hl.addWidget(self.avatar_label)

        # 标题区（标题 + 副标题；按住可拖动窗口）
        drag_zone = QWidget(header)
        dz = QVBoxLayout(drag_zone)
        dz.setContentsMargins(0, 0, 0, 0)
        dz.setSpacing(1)
        title = ElidedLabel(f"和 {self.char_cfg.name} 的对话")
        title.setObjectName("titlebar_title")
        dz.addWidget(title)
        self.subtitle_label = ElidedLabel(self._status_text())
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

        # 2026-10-14 美化：自定义头像入口(菜单：换我 / 换桌宠)
        self.btn_avatar = QPushButton("头像")
        self.btn_avatar.setObjectName("ghost_btn")
        self.btn_avatar.setToolTip("自定义聊天头像（换我 / 换桌宠）")
        self.btn_avatar.clicked.connect(self._on_avatar_menu)
        hl.addWidget(self.btn_avatar)

        self.btn_min = QToolButton()
        self.btn_min.setObjectName("win_btn")
        self.btn_min.setText("─")
        self.btn_min.setToolTip("最小化")
        self.btn_min.clicked.connect(self.showMinimized)
        hl.addWidget(self.btn_min)

        # 2026-10-15 v2: 加最大化/还原按钮 —— 跟常见窗口右上角控件一致
        # (用户实测"右上角要有全屏和窗口功能")
        self.btn_max = QToolButton()
        self.btn_max.setObjectName("win_btn")
        self.btn_max.setText("▢")
        self.btn_max.setToolTip("最大化")
        self.btn_max.clicked.connect(self._toggle_maximized)
        hl.addWidget(self.btn_max)

        # 2026-10-15 v3: 用户实测 "只需要最小化最大化和关闭, 不需要多余的全屏"。
        # 全屏按钮 + F11 快捷键已移除。
        self.btn_close = QToolButton()
        self.btn_close.setObjectName("win_btn_close")
        self.btn_close.setText("X")
        self.btn_close.setToolTip("关闭")
        self.btn_close.clicked.connect(self.close)
        hl.addWidget(self.btn_close)

        # 2026-10-15 美化：标题栏文字按钮锁定最小宽度 —— 空间紧张时让标题区
        # (ElidedLabel) 省略，而不是把按钮文字裁掉（曾出现 Demo 被压成 em）。
        for _tb in (self.btn_clear, self.btn_demo, self.btn_memory,
                    self.btn_avatar):
            _tb.setMinimumWidth(_tb.sizeHint().width())

        cl.addWidget(header)

        # 标题栏拖动（头像 / 标题 / 副标题区域）
        self._titlebar = header
        self._drag_filter = _TitleBarDrag(self)
        drag_zone.installEventFilter(self._drag_filter)
        self.avatar_label.installEventFilter(self._drag_filter)
        title.installEventFilter(self._drag_filter)

        self.chat_view = _NoCmdNavQTextBrowser()
        self.chat_view.setObjectName("chat_view")
        # 2026-10-15 修复: 不要 setOpenExternalLinks(True) —— 它让 Qt 试图用
        # OS shell 打开 scheme,Windows 找不到 cmd: handler 会弹「获取打开此
        # cmd 链接的应用」系统对话框(用户实测)。内部命令用 anchorClicked
        # signal 拦截,外部链接用 openExternalLinks event filter 自己处理。
        self.chat_view.setOpenExternalLinks(False)
        # 2026-10-15 v4: 不要 setOpenLinks(False) —— PyQt5 5.x 在 openLinks=False
        # 时会**禁用 click** (用户实测「点击没反应」)。改用 _NoCmdNavQTextBrowser
        # 覆盖 doSetSource: cmd: scheme 走 no-op, 不替换 view; http/https
        # 走 setOpenExternalLinks(False) 那一路。
        # click 时 anchorClicked 信号照常 emit, _on_chat_anchor_clicked 路由
        # cmd:toggle_tools / http/https/mailto。
        self.chat_view.anchorClicked.connect(self._on_chat_anchor_clicked)
        chat_font = QFont()
        chat_font.setFamily("Microsoft YaHei, PingFang SC, Segoe UI, sans-serif")
        chat_font.setPointSize(10)
        self.chat_view.setFont(chat_font)
        # 2026-10-15: 换成 MiniMax Code 风格的样式表 (CHAT_MD_CSS,线性流 + 完整 markdown)
        # 旧 CHAT_BUBBLE_CSS 保留在 ui_style.py 里作历史兼容,以防有外部模块直接 import。
        self.chat_view.document().setDefaultStyleSheet(ui_style.CHAT_MD_CSS)
        cl.addWidget(self.chat_view, 1)

        input_container = QFrame()
        input_container.setObjectName("input_container")
        il = QVBoxLayout(input_container)
        il.setContentsMargins(12, 8, 12, 8)
        il.setSpacing(4)

        # 2026-10-15 v10: 主人在聊天开头 / 按发送之前, 拖入 / 上传的图片先
        # 堆到 "输入框上方" 的缩略图条 (iMessage / 微信 / QQ 同款姿势) ——
        # 不再 v8/v9 那样"拖入立刻渲染进 chat", 因为主人实测:
        # "拖进去应该显示在输入框上方, 缩小版, 而不是直接进入对话框"。
        # pending_strip 是一个 QScrollArea (横向), 内部用 QHBoxLayout 装
        # 缩略图 + 文件名 + 关闭按钮。主人按发送时, 这些 attachment 跟
        # 当前 input_edit 文本一起作为 user message 渲染进 chat + 持久化。
        self._pending_attachments: list[Path] = []   # 待发送文件 (复制到 uploads 后)
        self._pending_thumb_widgets: list[QFrame] = []  # 缩略图条上每个 QLabel 容器
        self.pending_strip = QScrollArea()
        self.pending_strip.setObjectName("pending_strip")
        self.pending_strip.setWidgetResizable(True)
        self.pending_strip.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.pending_strip.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.pending_strip.setFixedHeight(82)
        self.pending_strip.setVisible(False)  # 没附件时隐藏
        self._pending_strip_inner = QWidget()
        self._pending_strip_layout = QHBoxLayout(self._pending_strip_inner)
        self._pending_strip_layout.setContentsMargins(4, 4, 4, 4)
        self._pending_strip_layout.setSpacing(6)
        self._pending_strip_layout.addStretch()
        self.pending_strip.setWidget(self._pending_strip_inner)
        il.addWidget(self.pending_strip)

        self.input_edit = QPlainTextEdit()
        self.input_edit.setObjectName("chat_input")
        self.input_edit.setPlaceholderText(
            "输入消息，回车发送（Shift+回车 或 Ctrl+回车 换行）")
        self.input_edit.setMaximumHeight(110)
        # 自定义按键事件：Enter 发送，Shift+Enter 换行
        self.input_edit.keyPressEvent = self._input_key_press
        # / 命令补全 (2026-10-15 v6: 改为英文 + 加 /plan / /skills)
        # v10: 动态拼 /skills/<name> 候选项 —— 让主人输入 "/skills/" 时 popup
        # 列出当前 skills_dir 下所有 skill 名字。
        skill_names: list[str] = []
        try:
            from app.brain.skills import list_skills
            skill_names = list_skills(getattr(self, "_skills_dir", None) or "skills")
        except Exception:
            pass
        candidates = [
            "/help", "/status", "/feed ", "/time", "/say ", "/remember ",
            "/memory", "/reminders", "/clear", "/demo", "/debug", "/open ",
            "/plan ", "/skills",
        ] + [f"/skills/{n} " for n in skill_names]
        self._cmd_completer = _CommandCompleter(self.input_edit, candidates)
        il.addWidget(self.input_edit)

        # 输入区底部：状态 + 按钮
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(4)

        # 2026-10-15 v8: 主人实测要求 — 「拖入图片直接显示，底部 + 上传，
        #   多模态生成（图 / 视频）也加在底部」。
        # 这一排三个辅助按钮：上传 +  / 多模态 ▼  / 语音。
        # 上传 +：打开 QFileDialog 选图片/文件，走和拖拽相同的渲染路径。
        self.upload_btn = QToolButton()
        self.upload_btn.setText("+")
        self.upload_btn.setObjectName("ghost_btn")
        self.upload_btn.setToolTip("上传图片或文件（也支持把图片拖到聊天窗）")
        self.upload_btn.setMinimumHeight(28)
        self.upload_btn.setMinimumWidth(36)
        self.upload_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.upload_btn.clicked.connect(self._on_upload_clicked)
        bottom_row.addWidget(self.upload_btn)

        # 多模态下拉：图 / 视频 / 语音（音频用现有 TTS，跳过）
        self.media_btn = QToolButton()
        self.media_btn.setText("多模态")
        self.media_btn.setObjectName("ghost_btn")
        self.media_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.media_btn.setMinimumHeight(28)
        self.media_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.media_btn.setToolTip("让 agent 生成图 / 视频（需 model provider 支持）")
        media_menu = QMenu(self.media_btn)
        self._media_menu = media_menu
        act_img = media_menu.addAction("生成图片")
        act_img.triggered.connect(lambda: self._on_media_action("image"))
        act_vid = media_menu.addAction("生成视频")
        act_vid.triggered.connect(lambda: self._on_media_action("video"))
        act_aud = media_menu.addAction("生成音效（stub）")
        act_aud.triggered.connect(lambda: self._on_media_action("audio"))
        media_menu.addSeparator()
        act_help = media_menu.addAction("配置多模态 provider（占位）")
        act_help.triggered.connect(lambda: self._on_media_action("config"))
        self.media_btn.setMenu(media_menu)
        bottom_row.addWidget(self.media_btn)

        # 麦克风（文字 + 图标）—— 用 QToolButton 与 +/多模态 按钮完全统一
        self.mic_btn = QToolButton()
        self.mic_btn.setText("语音")
        self.mic_btn.setObjectName("ghost_btn")
        self.mic_btn.setToolTip("按住说话，松开自动识别并发送" if self.asr
                                else "未安装 faster-whisper / sounddevice")
        self.mic_btn.setMinimumHeight(28)
        self.mic_btn.setCursor(Qt.CursorShape.PointingHandCursor)
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

        # 停止按钮：仅在生成中显示，平时隐藏（避免灰色按钮常驻占位）
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("stop_btn")
        self.stop_btn.setMinimumHeight(32)
        self.stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_btn.clicked.connect(self._on_stop)
        self.stop_btn.setVisible(False)
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

        # 2026-10-15 v8: 主人实测「每次点击输入框会有一个紫色线圈不需要」，
        # 不再用 _InputFocusWatcher 高亮边框，输入容器永远保持中性灰。
        # 上一版的 _InputFocusWatcher 已在 chat_window/chrome.py 删除。

        # 无边框窗口没有系统边框，补上「拖拽四边/四角调整大小」
        # （后装于标题拖拽，边缘命中优先，不与顶部拖动冲突）
        from app.ui.frameless import FramelessResizer
        self._resizer = FramelessResizer(self).attach()

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
            self._append_system_msg("当前未接入记忆存储，无法管理长期记忆")
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

    def _on_avatar_menu(self) -> None:
        """标题栏「头像」按钮：2026-10-15 v2 用户头像已移除,直接弹桌宠头像选择。

        旧版曾提供「换我的头像 / 换桌宠头像」两个选项, 现在只留桌宠。
        """
        ok = self.pick_bot_avatar(self)
        if ok:
            self._append_system_msg("头像已更新（保存在 data/avatars/）")

    def _toggle_maximized(self) -> None:
        """最大化 / 还原 切换 (2026-10-15 加)。

        全屏状态下要先退出全屏, 否则 maximize 会被忽略。"""
        if self.isFullScreen():
            self.showNormal()
        if self.isMaximized():
            self.showNormal()
            self.btn_max.setText("▢")
            self.btn_max.setToolTip("最大化")
        else:
            self.showMaximized()
            self.btn_max.setText("[ ]")
            self.btn_max.setToolTip("还原")

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
                    f"刚才「{target}」没真的{'打开' if action == 'open' else '启动' if action == 'start' else '操作'}："
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
            # 把 ChatStore 反序列化的 list-of-lists 还原成 tuple，让 Message 的
            # tools 字段在重进聊天窗时也被填上（Bug 2：旧版 _load_history 没传
            # sm.tools，导致"已执行操作"卡片丢失）。
            tools_loaded = _convert_loaded_tools(sm.tools)
            msg = Message(role=sm.role, content=sm.content,
                          emotion=emotion, tools=tools_loaded)
            self._render_message(msg)
            self.history.append(msg)
        # 历史是从磁盘读的，可能远超过条数/字符上限（ChatStore 存 200 条）。
        # 不在这里裁一次的话，启动后第一轮会把整份历史发给模型。
        self._trim_history()

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
        pending = self._consume_pending_attachments()  # v10: 取出待发送附件
        if not text and not pending:
            return
        # 限长：>2000 字符警告并截断
        if len(text) > 2000:
            self._append_system_msg(
                f"消息太长（{len(text)} 字符），已截断到 2000")
            text = text[:2000]

        # v10: 主人按发送时, 如果有待发送附件, 把它们的 markdown image
        # 引用拼到 user message 内容里 (render 时走 _inline_local_images_to_data_url
        # 转 base64 嵌图, 跟单图路径一致)。
        if pending:
            lines: list[str] = []
            for p in pending:
                abs_uri = p.resolve().as_posix()
                file_uri = "file:///" + abs_uri.lstrip("/").replace("\\", "/")
                if self._is_image_suffix(p.suffix):
                    lines.append(f"![{p.name}]({file_uri})")
                else:
                    lines.append(f"附件 **{p.name}**")
            attach_block = "\n".join(lines)
            text = (attach_block + ("\n\n" + text if text else "")).strip()

        # v11 主人实测设计: 聊天开头写 `/api/<name> ...` 视为「切换 API
        # provider + 剩余文本作为 user message」。v12 删了 —— config-driven 后
        # 单一组 config 参数, 不再需要路由切换, multimodal tool 走 config.yaml
        # multimodal 段。

        # v12 主人实测设计: 聊天开头写 `/skills/<name> ...` 视为「这一次对话
        # 用这个 skill + 剩余文本作为 user message」—— skill 只对当条 msg 生效,
        # 下一条 user message 没 skill 就不拼进 system_prompt。不再持久到
        # self._active_skill (那是 v9 老的 session 持久姿势)。
        if text.startswith("/skills/"):
            head, _, rest = text.partition(" ")
            # head 形如 /skills/<name>
            name = head[len("/skills/"):].strip().strip("/")
            if name:
                ok, msg = self._activate_skill(name)
                self._append_system_msg(msg)
                # v12: skill 仅对当条消息生效; 设到 msg.skill 而非 self._active_skill
                if ok and rest.strip():
                    self.input_edit.clear()
                    self._quick_send(rest.strip(), skill=name)
                else:
                    # 单纯激活 (无剩余文本), 不发新消息, 清空输入框
                    self.input_edit.clear()
                return
            # /skills/ 但 name 为空, 走 /handle_command 给提示
            self._handle_command(text)
            return

        # 快捷命令
        if text.startswith("/"):
            self._handle_command(text)
            return

        self.input_edit.clear()
        self._quick_send(text)

    def _handle_command(self, text: str) -> None:
        """处理 / 快捷命令（chat 顶上的输入框以 / 开头时走这里）。

        2026-10-15 v12: 删了 v11 的 /api[/<name>] —— config-driven 后不需要
        路由切换; 加 /image /video /audio 三个命令 —— 跟 input_edit 前 insert
        前缀 + 多模态按钮是同一姿势, 但支持从输入框直接打命令。
        2026-10-15 v6: 命令字面量统一改成英文，避开中文输入法切换的问题。
        后续若新增命令, 仍按英文命名; 内部说明文案 / 给主人看的内容用中文。
        """
        self.input_edit.clear()
        cmd = text.split()[0].lower()
        args = text[len(cmd):].strip()

        # /help 或 /?: 列出可用命令
        if cmd in ("/help", "/?"):
            self._append_system_msg(
                "可用命令: /help /status /time /remember /say /memory "
                "/reminders /clear /demo /debug /open /plan /skills"
            )
            self._render_message(Message(
                role="assistant",
                content=(
                    "常用命令:\n"
                    "• /help                 — 显示本帮助\n"
                    "• /status               — 显示桌宠状态\n"
                    "• /time                 — 显示当前时间\n"
                    "• /remember <text>      — 记到长期记忆\n"
                    "• /say <text>           — 让桌宠念一句话（冒泡显示）\n"
                    "• /memory               — 列出长期记忆\n"
                    "• /reminders            — 查看/管理提醒\n"
                    "• /clear                — 清空当前聊天上下文（不影响长期记忆）\n"
                    "• /demo                 — 打开 Live2D 演示页\n"
                    "• /debug                — 打开 Agent Trace 调试面板（开发）\n"
                    "• /open <name>          — 打开应用（例如 /open Notepad）\n"
                    "• /plan [text]          — 给出计划草稿（不调工具）\n"
                    "• /skills               — 列出已加载的 skill 文档"
                ),
            ))
            return

        # /memory: 列出最近几条长期记忆
        if cmd == "/memory":
            items = self.context_provider().split("\n") if self.context_provider else []
            mem = [l for l in items if l.strip().startswith("- [")]
            self._render_message(Message(
                role="assistant",
                content="最近长期记忆:\n" + ("\n".join(mem[:10]) if mem
                                   else "（暂无）"),
            ))
            return

        # /reminders: 调工具列提醒
        if cmd == "/reminders":
            if self.registry is None:
                self._render_message(Message(
                    role="assistant", content="工具注册表不可用"))
            else:
                tool = self.registry.get("list_reminders")
                if tool is None:
                    self._render_message(Message(
                        role="assistant", content="没有 list_reminders 工具"))
                else:
                    result = tool.fn()
                    self._render_message(Message(
                        role="assistant", content=f"提醒列表:\n{result}"))
            return

        # /demo: 打开 Live2D 演示页
        if cmd == "/demo":
            self._open_demo()
            return

        # /debug: 打开 Agent Trace 面板
        if cmd == "/debug":
            self._open_dashboard()
            return

        # /open <name>: 通过工具打开应用
        if cmd == "/open" and args:
            if self.registry:
                tool = self.registry.get("open_app")
                if tool:
                    r = tool.fn(app_name=args)
                    self._render_message(Message(role="assistant", content=r))
                    return
            self._render_message(Message(
                role="assistant", content="工具不可用"))
            return

        # /plan: 把主人问题转成「计划模式」交给 agent —— 只给 markdown 草稿，不调工具
        if cmd == "/plan":
            if not hasattr(self, "_lc_agent") or self._lc_agent is None:
                self._render_message(Message(
                    role="assistant", content="LangChain agent 还没初始化, /plan 暂不可用"))
                return
            plan_ask = args or "（主人没给具体问题，给一份通用计划模板）"
            self.input_edit.clear()
            self._quick_send_plan(plan_ask)
            return

        # /image <prompt> / /video <prompt> / /audio <prompt>:
        # 直接从输入框打命令调多模态 tool 拿 URL 嵌图。跟点 生成图片 按钮 insert 前缀是
        # 同一姿势的键盘版 —— 主人不用鼠标也能用。v12 加 (v11 走 /api 切换)。
        if cmd in ("/image", "/video", "/audio") and args:
            kind = cmd[1:]   # "image" / "video" / "audio"
            self._run_multimodal_action(kind, args)
            return

        # /skills[/<name>]: 列出所有 / 单独激活某个 skill。
# 主人 2026-10-15 实测设计: 「如果有 /skills/<name> 在聊天开头, 那么这次对话将使用这个 skill」——
# _handle_command 也走同一条激活路径, 让主人在输入框里单独打 `/skills/poem` 也能激活。
        if cmd == "/skills" or cmd.startswith("/skills/"):
            from app.brain.skills import list_skills
            skills_dir = getattr(self, "_skills_dir", None) or "skills"
            # /skills/<name>: 激活 (跟「聊天开头写 /skills/<name>」是同一动作)
            if cmd.startswith("/skills/"):
                skill_name = cmd[len("/skills/"):].strip().strip("/")
                ok, msg = self._activate_skill(skill_name)
                if ok:
                    self._append_system_msg(msg)
                else:
                    self._render_message(Message(
                        role="assistant", content=msg))
                return
            # /skills 列表
            try:
                names = list_skills(skills_dir)
            except Exception as e:  # noqa: BLE001
                self._render_message(Message(
                    role="assistant",
                    content=f"读取 skill 目录失败 {skills_dir!r}: {e}"))
                return
            active = self._active_skill
            if not names:
                self._render_message(Message(
                    role="assistant",
                    content=f"目录 `{skills_dir}` 下没找到 .md skill。"
                            f"（当前项目可能还没建 skills/, 或目录是空的）"
                            f"\n\n想新建一个? 在该目录下加一个 .md 文件, "
                            f"下次跑 /skills 就能看到。"))
            else:
                lines = [f"• {n}{'   ← 当前激活' if n == active else ''}" for n in names]
                self._render_message(Message(
                    role="assistant",
                    content=(
                        f"当前可用 skill（{len(names)} 个）:\n"
                        + "\n".join(lines)
                        + f"\n\n路径: `{skills_dir}`"
                        + (f"\n\n当前激活: **{active}**（本次对话会按它的行为规范回答）"
                           if active else
                           f"\n\n激活方式: 在聊天开头输入 `/skills/{names[0]} ...`，"
                           f"或单独输入 `/skills/{names[0]}`。"
                           f"整个对话期间该 skill 都会生效。")
                    )
                ))
            return

        # /say <text>: 让桌宠念一句话（assistant 视角渲染一个冒泡文本）
        if cmd == "/say" and args:
            self._render_message(Message(
                role="assistant",
                content=args,
            ))
            return

        # /clear: 清空 chat 上下文（不动长期记忆）
        if cmd == "/clear":
            self._on_clear()
            return

        # /remember <text>: 存到长期记忆
        if cmd == "/remember":
            if not args:
                self._append_system_msg("用法: /remember <要记住的内容>")
                return
            if self.memory_store is not None:
                try:
                    self.memory_store.add(args, source="user")
                except Exception as e:  # noqa: BLE001
                    log.warning("memory_store.add 失败: %s", e)
                self._render_message(Message(
                    role="assistant",
                    content=f"已记下: {args}"))
            else:
                self._render_message(Message(
                    role="assistant",
                    content="（长期记忆功能未启用）"))
            return

        # /status / /time / /feed —— v6 时没接通, 这里给个提示告诉主人命令已被识别
        if cmd in ("/status", "/time", "/feed"):
            self._append_system_msg(
                f"{cmd}: 这个命令还没接通，输入 /help 查看当前可用命令。"
            )
            return

        # 未知 /xxx 命令 —— 包成普通提示送给 LLM, 让模型决定怎么跟主人解释
        hint = f"（主人输入了 / 快捷命令 {text}，但当前版本未支持）"
        self._quick_send(hint)

    def _quick_send(self, text: str, skill: Optional[str] = None) -> None:
        """从快捷输入框发送消息（直接传入文本，不走 input_edit）。

        v12: 加 skill kwarg —— 当条 user message 拼 msg.skill, _kickoff_llm
        读 history[-1].skill 拼到 system_prompt; 不传就不拼。per-message 语义,
        下一条 user msg 不传 skill 就不带。
        """
        if self._generating:
            return
        text = text.strip()
        if not text:
            return
        msg = Message(role="user", content=text, skill=skill)
        self.history.append(msg)
        self._trim_history()
        self.chat_store.add("user", text)  # 持久化用户消息
        self._render_message(msg)
        self._kickoff_llm()

    def _quick_send_plan(self, text: str) -> None:
        """Plan-mode 入口:跟 _quick_send 不一样 —— 用户问题不入 history 也不入
        chat_store 的「主对话」流,因为 plan 产物不调工具,只是主人先看一眼草稿,
        后续主人在普通 chat 里说「按计划做」才进主循环。

        但用户问题本身要渲染出来,这样能看到「这是我对哪个问题拿到的计划」。
        """
        if self._generating:
            return
        text = text.strip()
        if not text:
            return
        # 渲染用户问题(用 system 灰框,不写进 history)
        self._render_message(Message(role="user", content=f"[主人笔记] {text}"))
        self._kickoff_llm_plan(text)

    def _kickoff_llm_plan(self, user_text: str) -> None:
        """启动 _PlanWorker —— /plan 命令专用路径。

        用独立的信号协议:plan_chunk / plan_done / plan_failed,不走普通 chat 的
        _on_chunk / _on_done(那些会触发 TTS 朗读 + 加 emotion,plan 模式不需要)。
        产物渲染为单独一张「计划」卡片,不写进 history / chat_store,保持主对话流干净。
        """
        if not hasattr(self, "_lc_agent") or self._lc_agent is None:
            self._render_message(Message(
                role="assistant", content="plan 模式不可用:仅 standard / LangChain 后端支持"))
            return
        self._generating = True
        self.send_btn.setEnabled(False)
        self.stop_btn.setVisible(True)
        self.thinking_started.emit()
        # 只把 user_text 作为单一消息喂给 plan(避免污染主 chat 历史)
        msgs = [{"role": "user", "content": user_text}]
        # plan 模式下我们用一个独立 buffer,worker 的 chunk 不直接渲染,而是
        # 累积,最后通过 done 一次渲染为一张「计划卡片」
        self._plan_buffer: list[str] = []
        self._plan_worker = _PlanWorker(
            self._lc_agent, msgs,
        )
        self._plan_worker.chunk.connect(self._on_plan_chunk)
        self._plan_worker.done.connect(self._on_plan_done)
        self._plan_worker.failed.connect(self._on_plan_failed)
        self._plan_worker.start()

    def _on_plan_chunk(self, text: str) -> None:
        """plan 流式 chunk 累积,不渲染中间过程(plan 文本一次发出来比较干净)。"""
        if not hasattr(self, "_plan_buffer") or self._plan_buffer is None:
            self._plan_buffer = []
        self._plan_buffer.append(text)

    def _on_plan_done(self, full: str) -> None:
        """plan 完成:渲染为一张「[计划]」卡片,不入 history / chat_store。"""
        body = full or ("".join(getattr(self, "_plan_buffer", []) or []))
        self._plan_buffer = []
        self._generating = False
        self.send_btn.setEnabled(True)
        self.stop_btn.setVisible(False)
        self.thinking_stopped.emit()
        if not body.strip():
            self._render_message(Message(
                role="assistant", content="plan 模式没产出内容,可能是模型拒绝"))
        else:
            self._render_plan_card(user_text="", plan_text=body)
        # _plan_worker 已经在 done 后会自动 quit,清引用避免内存泄漏
        self._plan_worker = None

    def _on_plan_failed(self, err: str) -> None:
        """plan 失败:提示主人,不影响普通 chat 后续使用。"""
        self._generating = False
        self.send_btn.setEnabled(True)
        self.stop_btn.setVisible(False)
        self.thinking_stopped.emit()
        self._render_message(Message(
            role="assistant", content=f"plan 模式失败: {err}"))
        self._plan_worker = None

    def _render_plan_card(self, user_text: str, plan_text: str) -> None:
        """把产出的 markdown 计划渲染成一张折叠卡片。

        与普通对话气泡共用 chat_view,但有清晰的视觉边界(紫框 + 锁形 icon),
        让主人一眼区分「这是计划,不是已经做的事」。
        """
        # 转义 + 保留 markdown 块结构(code fence 等)
        from app.ui.chat_window.utils import _html_escape
        safe = _html_escape(plan_text)
        html = (
            '<div style="margin:8px 0 14px 0;padding:10px 12px;'
            'background:#fbf7ff;border:1px solid #d9c9f5;border-radius:8px;">'
            '<div style="color:#7c5cf0;font-weight:600;margin-bottom:6px;">'
            '[计划] (不会自动执行,主人说「按计划做」才会执行)</div>'
            f'<div style="white-space:pre-wrap;color:#2c2c38;line-height:1.55;">{safe}</div>'
            '</div>'
        )
        # 直接插入到 chat_view(不动 history / chat_store)
        self.chat_view.append(html)

    def _trim_history(self) -> None:
        """裁剪送给 LLM 的上下文：条数上限 + 字符预算，两条约束取更严的。

        只按条数裁是不够的：单条用户输入上限 2000 字符，50 条最坏能堆到
        10 万字符 —— 每一轮都要把这坨重新发一遍，成本和首字延迟随对话
        线性增长，而多轮场景里模型实际只需要最近几轮。字符预算按「从最早的
        开始丢，累加到超预算就停」计算，保证留下的是最新上下文。
        """
        before = len(self.history)
        if before > self._MAX_HISTORY:
            self.history = self.history[-self._MAX_HISTORY:]

        # 字符预算：从新到旧累加，超出就截断
        budget = self._CTX_CHAR_BUDGET
        if budget and budget > 0:
            kept: list[Message] = []
            used = 0
            for m in reversed(self.history):
                cost = len(getattr(m, "content", "") or "")
                if kept and used + cost > budget:
                    break
                kept.append(m)
                used += cost
            kept.reverse()
            # 裁剪后可能以 assistant 开头（它的 user 问题被丢了），
            # 开头的孤立助手回复对模型是噪声，去掉
            while kept and kept[0].role == "assistant":
                kept.pop(0)
            self.history = kept

        after = len(self.history)
        if after != before:
            log.debug("上下文裁剪：%d → %d 条（上限 %d / 预算 %d 字符）",
                      before, after, self._MAX_HISTORY, self._CTX_CHAR_BUDGET)

    def _on_stop(self) -> None:
        """停止当前正在进行的流式生成。

        PR1 修复：以前调 requestInterruption() 但 QThread.run 不响应、等同没用。
        现在 worker 持有 cancel_event（threading.Event），UI 线程 set() 后，stream
        端在下一个 chunk 之前会断开 HTTP 连接，正常向上抛「done(partial)」。
        """
        if self._worker is not None:
            self._worker.request_stop()
            self.stop_btn.setVisible(False)

    # ------------------------------------------------------------------
    # v8 上传 / 拖放 / 多模态生成按钮 (2026-10-15 主人实测要求)
    #
    # 设计:
    #   - 拖入图片 / 点 + 上传 → 都走 _handle_uploaded_files,
    #     图片存到 assets/uploads/ (项目根目录下), 用 file:// 路径
    #     渲染进 chat_view。这样下次重启桌宠也能看见。
    #   - 多模态生成 (图 / 视频 / 音效) → 当前 v8 留 stub,
    #     弹一行提示告诉主人"等模型接上后端", 但 UI 按钮先有位置。
    # ------------------------------------------------------------------

    _UPLOAD_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
    _UPLOAD_DIR_NAME = "uploads"
    _MAX_UPLOAD_BYTES = 12 * 1024 * 1024  # 12 MiB cap, 太大会让 chat_view 渲染卡

    # ------------------------------------------------------------------
    # v9 skill 激活路径
    # 主人实测设计: 在聊天开头写 /skills/<name>, 整段对话期间
    # 该 skill 内容会拼到 system_prompt 末尾; 切换别的不丢之前的聊天。
    # ------------------------------------------------------------------

    def _activate_skill(self, name: str) -> tuple[bool, str]:
        """激活一个 skill 进当前 session。返回 (success, message)。

        - 校验名字合法 + 存在于 skills_dir
        - 设置 self._active_skill, 旧值若不同会覆盖
        - 系统消息反映激活结果
        """
        from app.brain.skills import load_skill
        skills_dir = getattr(self, "_skills_dir", None) or "skills"
        name = (name or "").strip().strip("/")
        if not name:
            return False, "用法: /skills/<name>  （name 从 /skills 列表里挑）"
        content = load_skill(skills_dir, name)
        if content is None:
            return False, (
                f"找不到 skill `{name}`。先用 /skills 看现有列表, 确认名字拼写。"
            )
        prev = self._active_skill
        self._active_skill = name
        if prev == name:
            return True, f"skill `{name}` 已经是当前激活状态（重复激活）。"
        if prev:
            return True, (
                f"已把当前 skill 从 `{prev}` 切到 `{name}`。"
                f"本次对话起按 {name} 的行为来回答。"
            )
        return True, (
            f"已激活 skill: **{name}**。"
            f"本次对话起按这个 skill 的行为规范回答（直到主人切换 / 重启桌宠）。"
        )

    def _active_skill_prompt(self) -> str:
        """返回要拼到 system_prompt 末尾的当前激活 skill 文本。无激活时返回空串。

        v12 兜底用 —— 主线读 per-message msg.skill; 这条 session-level 路径
        留给老测试 + 兜底 (主人在 /skills/<name> 单独激活但没跟 user 文本
        一起发的情况下仍生效)。
        """
        if not self._active_skill:
            return ""
        from app.brain.skills import load_skill
        skills_dir = getattr(self, "_skills_dir", None) or "skills"
        body = load_skill(skills_dir, self._active_skill)
        if not body:
            return ""
        return (
            f"\n\n[Active Skill: {self._active_skill}]\n{body}\n[/Active Skill]"
        )

    def _skill_prompt_for(self, name: str) -> str:
        """v12 per-message 主线: 给定 skill 名字, 返回对应的 skill prompt 段。

        跟 _active_skill_prompt 同样的格式 (前端模板是 [Active Skill: <name>]),
        但读 msg.skill 而不是 self._active_skill session 字段。
        """
        if not name:
            return ""
        from app.brain.skills import load_skill
        skills_dir = getattr(self, "_skills_dir", None) or "skills"
        body = load_skill(skills_dir, name)
        if not body:
            return ""
        return (
            f"\n\n[Active Skill: {name}]\n{body}\n[/Active Skill]"
        )

    def _uploads_dir(self) -> Path:
        """计算项目根下的 assets/uploads, 自动 mkdir."""
        root = Path(__file__).resolve().parent.parent.parent.parent
        d = root / "assets" / self._UPLOAD_DIR_NAME
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return d

    @staticmethod
    def _is_image_suffix(suffix: str) -> bool:
        return suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}

    def dragEnterEvent(self, event: "QDragEnterEvent") -> None:
        """鼠标拖着文件 / 图片进入窗口: 看 mimeData 里有没有 file url, 有就 accept."""
        md = event.mimeData()
        if md is None:
            event.ignore()
            return
        if md.hasUrls() and any(u.isLocalFile() for u in md.urls()):
            event.acceptProposedAction()
            return
        # 也接受直接拖 image data (e.g. 从浏览器拖一张图)
        if md.hasImage():
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event: "QDragMoveEvent") -> None:
        md = event.mimeData()
        if md is not None and (md.hasUrls() or md.hasImage()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: "QDropEvent") -> None:
        """主人放下文件 / 图片: 解析 file urls → 走 _handle_uploaded_files."""
        md = event.mimeData()
        if md is None:
            event.ignore()
            return
        paths: list[Path] = []
        if md.hasUrls():
            for url in md.urls():
                if not url.isLocalFile():
                    continue
                local = url.toLocalFile()
                if local:
                    p = Path(local)
                    if p.is_file():
                        paths.append(p)
        # 如果 mimeData 直接含 image (例如浏览器拖图), 不落本地, 转临时文件再走。
        if not paths and md.hasImage():
            img = md.imageData()
            if isinstance(img, QPixmap) and not img.isNull():
                tmp = self._uploads_dir() / f"pasted_{int(time.time())}.png"
                try:
                    img.save(str(tmp), "PNG")
                    paths.append(tmp)
                except Exception:
                    pass
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self._handle_uploaded_files(paths)

    def _on_upload_clicked(self) -> None:
        """主人点 + 按钮: 弹 QFileDialog 选图 / 文件, 走和拖放一样的渲染路径."""
        try:
            selected, _ = QFileDialog.getOpenFileNames(
                self, "选择要发送的文件",
                "", "图片 (*.png *.jpg *.jpeg *.gif *.bmp *.webp);;所有文件 (*)",
            )
        except Exception:
            selected = []
        if not selected:
            return
        self._handle_uploaded_files([Path(p) for p in selected])

    def _handle_uploaded_files(self, paths: list) -> None:
        """主人上传 (拖放 / 点 + 选) 的多个文件, 进入"待发送"区。

        v10 行为 (主人 2026-10-15 实测设计):
          - 拖入 / 选完文件后**不再立刻进 chat**, 而是堆在 input_edit 上方
            的 horizontal 缩略图条 (pending_strip), 主人能继续编辑文字
            / 拖更多 / 按发送。Q iMessage / 微信 / QQ 同款姿势。
          - 主人按「发送」时 (_on_send), 这些 attachment 跟当前 input_edit
            文本一起作为一条 user message 渲染进 chat + 持久化 + kickoff LLM。
          - 单个文件缩略图右上角有 × 按钮, 主人能单独剔除某个 attachment。

        文件落地:
          - 复制到 assets/uploads/<ts>_<name> (v8 那条), 避免原始路径泄露 +
            跨 session 引用稳定。
          - > 12 MiB 跳过 + 渲染一条 assistant 提示。
        """
        if not paths:
            return
        for p in paths:
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size > self._MAX_UPLOAD_BYTES:
                self._render_message(Message(
                    role="assistant",
                    content=f"文件 `{p.name}` 太大 ({size} 字节)，已超过 {self._MAX_UPLOAD_BYTES} 字节，跳过。"
                ))
                continue
            try:
                import shutil as _sh
                target = self._uploads_dir() / f"{int(time.time())}_{p.name}"
                # 防重名
                stem = target.stem
                suf = target.suffix
                n = 1
                while target.exists():
                    target = target.with_name(f"{stem}_{n}{suf}")
                    n += 1
                _sh.copy2(str(p), str(target))
            except Exception as e:  # noqa: BLE001
                self._render_message(Message(
                    role="assistant",
                    content=f"保存 `{p.name}` 失败: {e}"))
                continue
            self._pending_attachments.append(target)
            self._add_pending_thumbnail(target, size)

    def _add_pending_thumbnail(self, target: Path, size: int) -> None:
        """在 pending_strip 上加一个缩略图卡片 (缩略图 + 文件名 + × 按钮)。"""
        from app.core.qt_compat import QLabel
        # 容器: 80x80 圆角白底, 上方放缩略图, 下方文件名
        box = QFrame()
        box.setObjectName("pending_thumb_box")
        box.setFixedSize(80, 76)
        bl = QVBoxLayout(box)
        bl.setContentsMargins(2, 2, 2, 2)
        bl.setSpacing(0)
        # 缩略图 (图片) 或 文档 icon (非图片)
        thumb = QLabel()
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if self._is_image_suffix(target.suffix):
            pix = QPixmap(str(target))
            if not pix.isNull():
                thumb.setPixmap(
                    pix.scaled(72, 56,
                               Qt.AspectRatioMode.KeepAspectRatio,
                               Qt.TransformationMode.SmoothTransformation))
        else:
            thumb.setText("附件")
            thumb.setStyleSheet("font-size: 26pt; color: #7c6cf0;")
        thumb.setFixedSize(72, 56)
        bl.addWidget(thumb)
        # 文件名 (最多 10 字符, 超过截断)
        name_lbl = QLabel(target.name[:10] + ("…" if len(target.name) > 10 else ""))
        name_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name_lbl.setStyleSheet("font-size: 7.5pt; color: #475569;")
        bl.addWidget(name_lbl)
        # 点击 → 移除
        def _on_click(ev, t=target, w=box):
            if ev.button() == Qt.MouseButton.LeftButton:
                self._remove_pending_attachment(t, w)
        box.mousePressEvent = _on_click
        box.setToolTip(f"{target.name}  ·  {size//1024} KiB  (点一下可剔除)")
        box.setCursor(Qt.CursorShape.PointingHandCursor)
        # 插到 layout 的 stretch 之前
        idx = self._pending_strip_layout.count() - 1
        self._pending_strip_layout.insertWidget(idx, box)
        self._pending_thumb_widgets.append(box)
        self.pending_strip.setVisible(True)

    def _remove_pending_attachment(self, target: Path, widget: QFrame) -> None:
        """主人点缩略图 → 从待发送列表剔除这个文件。"""
        try:
            self._pending_attachments.remove(target)
        except ValueError:
            pass
        try:
            self._pending_thumb_widgets.remove(widget)
        except ValueError:
            pass
        widget.setParent(None)
        widget.deleteLater()
        if not self._pending_attachments:
            self.pending_strip.setVisible(False)

    def _consume_pending_attachments(self) -> list[Path]:
        """取出当前待发送列表并清空 (主人按发送时用)。"""
        out = list(self._pending_attachments)
        self._pending_attachments.clear()
        # 清掉所有缩略图
        for w in self._pending_thumb_widgets:
            w.setParent(None)
            w.deleteLater()
        self._pending_thumb_widgets.clear()
        self.pending_strip.setVisible(False)
        return out

    def _on_media_action(self, kind: str) -> None:
        """多模态下拉 (图片 / 视频 / 音频) 入口。

        2026-10-15 v12 owner 实测设计: 主人说「点击生成图片按钮就在输入框前面
        加 /image 之类的命令, 然后再输入内容」 —— 跟 iMessage / 微信 / QQ
        同款姿势, 不弹 QInputDialog 也不直接调 tool。让主人在输入框里接着
        输, 按发送才走 /image 命令 → _handle_command → _run_multimodal_action。
        /image /video /audio 命令在 _handle_command 里处理。
        """
        if kind not in ("image", "video", "audio"):
            # 配置入口 (v12 集中到 config.yaml multimodal 段, 不再弹窗)
            self._render_message(Message(
                role="assistant",
                content=(
                    "**多模态配置**\n\n"
                    "在 `config.yaml` 的 `multimodal:` 段填 base_url / api_key / "
                    "image_model / video_model / audio_model 以及对应的 endpoint "
                    "路径。跟 llm 段同源就能用, 不需要再选 provider。"
                )
            ))
            return

        # image / video / audio: 在 input_edit 前 insert "/{kind} ", focus 回去,
        # 让主人接着输 prompt, 按发送就走 _handle_command 里的 /image /video /audio
        # 命令段调 _run_multimodal_action。
        prefix = f"/{kind} "
        current = self.input_edit.toPlainText()
        if not current.startswith(prefix):
            self.input_edit.setPlainText(prefix + current.lstrip())
        self.input_edit.setFocus()
        # 移动 cursor 到文本末尾
        from app.core.qt_compat import QTextCursor
        cursor = self.input_edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        self.input_edit.setTextCursor(cursor)

    def _run_multimodal_action(self, kind: str, prompt: str) -> None:
        """/image /video /audio 命令的真正干活入口 (v12) —— 跟 _on_media_action
        按钮插前缀是同一姿势的键盘版。

        走 self.registry.get(f"generate_{kind}") 调 tool, 拿 URL, 嵌图。
        registry 不可用 / tool 没注册时, 弹清晰提示。
        """
        labels = {"image": "生成图片", "video": "生成视频",
                  "audio": "生成音频"}
        label = labels.get(kind, kind)
        prompt = (prompt or "").strip()
        if not prompt:
            self._render_message(Message(
                role="assistant",
                content=f"用法: /{kind} <prompt>   (例: /{kind} 一只橘猫趴在窗台)"
            ))
            return
        if not hasattr(self, "registry") or self.registry is None:
            self._render_message(Message(
                role="assistant",
                content=(
                    f"{label} 需要 agent tool registry。当前 chat 没接 tool registry，"
                    f"无法调 `generate_{kind}`。"
                ),
            ))
            return
        tool_name = f"generate_{kind}"
        tool = self.registry.get(tool_name)
        if tool is None:
            self._render_message(Message(
                role="assistant",
                content=(
                    f"{label}：当前 tool registry 没注册 `{tool_name}`。\n\n"
                    f"接入方法: 确认 `app/engine/tools/_multimodal.py` 已被 load "
                    f"且在 `setup_config` 里注入了 config.yaml multimodal 段。"
                ),
            ))
            return
        try:
            self._append_system_msg(f"已发出{label}请求, 等模型回...")
            result = tool.fn(prompt=prompt)
        except Exception as e:  # noqa: BLE001
            log.exception("multimodal %s tool failed", kind)
            self._render_message(Message(
                role="assistant",
                content=f"{label} 调用失败: {e}"))
            return

        url: Optional[str] = None
        if isinstance(result, str):
            url = result
        elif isinstance(result, dict):
            url = result.get("url") or result.get("image_url") or result.get("video_url")
        if not url:
            self._render_message(Message(
                role="assistant",
                content=f"{label} 工具没返回 URL，原始结果: `{result!r}`"))
            return

        # 渲染成 markdown image 进 chat (跟上传图片同款姿势, 走 _msg_html)
        kind_cn = {"image": "图片", "video": "视频", "audio": "音频"}.get(kind, kind)
        content = f"![{kind_cn}: {prompt}]({url})"
        msg = Message(role="assistant", content=content)
        self.history.append(msg)
        self._trim_history()
        try:
            self.chat_store.add("assistant", content)
        except Exception:
            log.exception("chat_store.add failed (multimodal %s)", kind)
        self._render_message(msg)

    def _on_mic_pressed(self) -> None:
        """按住麦克风按钮：开始录音。"""
        if self._mic_busy or self.asr is None:
            return
        self._mic_busy = True
        self.mic_btn.setText("录音中…")
        self.mic_btn.setStyleSheet("background-color: #ffcccc;")
        self.asr.start_recording()

    def _on_mic_released(self) -> None:
        """松开麦克风按钮：停止录音并开始识别。"""
        if self.asr is None:
            return
        self.mic_btn.setText("识别中…")
        self.mic_btn.setEnabled(False)
        self.asr.stop_and_recognize()

    def _on_recording_start(self) -> None:
        """录音已开始（UI 反馈）。"""
        # 已在 _on_mic_pressed 处理

    def _on_recording_finish(self) -> None:
        """录音已停止（等待识别结果）。"""
        # 已在 _on_mic_released 处理

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
        # 2026-10-15 修复 Bug 7 第二处旁路:
        # 之前 `name not in DANGEROUS_TOOLS` 直接 return True,
        # 但 RiskyToolConfirm 用的是 hallucination_guard._RISKY_TOOLS —— 两者
        # 不一致(例如 send_email 在 _RISKY_TOOLS 但不在 DANGEROUS_TOOLS)。
        # 中间件拦下来 → 调到本 callback → 看到不在 DANGEROUS_TOOLS 就放行
        # → 完全不弹窗就执行了。
        # 修法:既然走到了本 callback(RiskyToolConfirm 已经拦下来),就是
        # 高风险工具,不区分 DANGEROUS_TOOLS vs _RISKY_TOOLS,统一弹窗。
        # 把早 return 删掉,让所有情况都进入下面的弹窗逻辑。
        # (历史兼容:对未接 standard middleware 的轻量 backend,AgentLoop
        #  仍然自己用 DANGEROUS_TOOLS 判定要不要调到这里 —— 即「非 DANGEROUS
        #  工具」根本不会进本方法,所以这个删除安全。)

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
        result: dict[str, bool] = {}    # 主线程把 ok 写进来,子线程读

        def _on_result(ok: bool):
            self._confirm_in_progress = False
            result["ok"] = ok
            result_event.set()    # 唤醒子线程 wait()

        # 2026-10-15 关键修复(用户实测 + probe 复现):
        # 之前 QTimer.singleShot(0, lambda) 跨 child thread → main thread
        # 投递,在 PyQt5 5.x 上静默失败 —— QTimer 是 child thread-owned,
        # child thread 没 event loop,永远不 fire。这是用户反馈
        # 「高危工具没弹窗」的真因:模型调 lock_screen,中间件拦下来 →
        # _confirm_dangerous_tool 走 QTimer.singleShot → 静默挂死 → 60s 后
        # 子线程 wait 超时 → 拒绝执行,弹窗根本不会出现。
        #
        # 唯一可靠的跨线程投递方式: QMetaObject.invokeMethod + Qt.QueuedConnection。
        # 这是 Qt 官方保证的方式 —— Qt 在主线程 event queue 投递一个
        # QEvent::MetaCall,主线程派发时调 slot。child thread 不需要 event loop。
        # 关键约束:被 invokeMethod 调的方法必须是已定义的 slot(@pyqtSlot 装饰),
        # 用动态字符串 invokeMethod 是 PyQt5 不支持的。
        # 解法:chat_window 上挂一个固定 slot `_invoke_confirm_dispatch_slot`,
        # 内部读 `self._pending_confirm_dispatch` 决定要调哪个 lambda。
        # 同时只能有一个 _confirm_dangerous_tool 在飞(被 _confirm_in_progress 锁
        # 保证),所以单 slot 够用。
        self._pending_confirm_dispatch = lambda: self._show_confirm_box(
            name, args_pretty, _on_result,
        )
        from PyQt5.QtCore import QMetaObject
        from app.core.qt_compat import Qt as _Qt
        # PyQt5 5.x 上 invokeMethod 返回值不可靠(可能 None/0/1 都出现),
        # 但实际 slot 派发是工作正常的(slot 被主线程调到了)。所以不去看 ok 值,
        # 而是在 slot 里检查 _pending_confirm_dispatch 是否还在,如果 invokeMethod
        # 真的派发了,主线程派发 slot 时会调 fn,result_event 会被 set。
        # 如果 invokeMethod 失败,主线程不会派发 slot,result_event 永远不 set,
        # 子线程 wait 超时后走 60s 超时路径。
        QMetaObject.invokeMethod(
            self,
            "_invoke_confirm_dispatch_slot",
            _Qt.QueuedConnection,
        )
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
        return result["ok"]

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

    @pyqtSlot()
    def _invoke_confirm_dispatch_slot(self) -> None:
        """主线程槽:QMetaObject.invokeMethod + QueuedConnection 跨线程投递后,
        主线程 event loop 派发到这里。读 self._pending_confirm_dispatch 调真版。

        为什么不用 QTimer.singleShot:PyQt5 5.x 在 child thread 调
        QTimer.singleShot(0, lambda) 时,内部创建的 QTimer 是 child thread-owned,
        child thread 没 event loop → 永远不 fire,弹窗静默挂死(用户实测)。
        QMetaObject.invokeMethod 是 Qt 官方保证的跨线程 slot 调用方式。
        """
        fn = getattr(self, "_pending_confirm_dispatch", None)
        if fn is None:
            return
        # 清掉 attribute 后再调 fn,避免 fn 内重入导致 attribute 还在
        self._pending_confirm_dispatch = None
        try:
            fn()
        except Exception:  # noqa: BLE001
            log.exception("_invoke_confirm_dispatch_slot 异常")

    def _show_confirm_box(self, name: str, args_pretty: str, on_done) -> None:
        """主线程槽：弹危险工具确认弹窗 → 调 on_done（Event.set() 唤醒子线程）。

        2026-10-15 改用自定义 _DangerConfirmDialog(QDialog):
        - QMessageBox 在 FramelessWindowHint + WA_TranslucentBackground 父窗口
          下被 Qt 当 transient child 立刻 dismiss(用户实测「只闪一下」)
        - 自定义 QDialog 显式控制所有 flags,不被父窗口属性干扰

        2026-10-15 v2 修正:parent 改回 self(chat_window)而不是 None。
        用户反馈「弹窗不能在聊天窗口弹出进行确认」,希望弹窗在 chat 窗口内
        显示。parent=self 时:
          - z-order 跟随 chat_window(在 chat 上面)
          - 焦点自动回到 chat
          - 关闭 chat 时弹窗自动关
        _DangerConfirmDialog 自己有完整 window flags(不是依赖父),
        所以不会触发 QMessageBox 那种「transient child 被 dismiss」的问题。
        """
        try:
            from app.ui.chat_window.confirm_dialog import _DangerConfirmDialog
            dlg = _DangerConfirmDialog(name=name, args_pretty=args_pretty, parent=self)
            self._confirm_box = dlg      # 供超时路径 _close_confirm_box 用
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
            # 调 exec() 阻塞主线程直到用户点按钮 / 关闭 / 超时
            try:
                result = dlg.exec()
            finally:
                self._confirm_box = None
            on_done(result == 1)   # Accepted = 1(Yes),Rejected = 0(No)
        except Exception:  # noqa: BLE001
            log.exception("confirm dialog failed")
            on_done(False)

    def _on_speech_text(self, text: str) -> None:
        """语音识别完成：填入输入框并自动发送。"""
        self._mic_busy = False
        self.mic_btn.setText("语音")
        self.mic_btn.setStyleSheet("")
        self.mic_btn.setEnabled(True)

        if not text.strip():
            self.chat_view.append("未识别到语音")
            return

        # 填入输入框并自动发送
        self.input_edit.setText(text)
        self._on_send()

    def _on_asr_error(self, err: str) -> None:
        """语音识别出错：恢复按钮状态。"""
        self._mic_busy = False
        self.mic_btn.setText("语音")
        self.mic_btn.setStyleSheet("")
        self.mic_btn.setEnabled(True)
        self.chat_view.append(f"语音识别出错：{err}")

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
        # v12: 主人激活的 skill 内容拼到 system_prompt 末尾 —— 但**只在当条
        # user message 生效** (per-message 语义, 下一条不带就不拼)。
        # 主线: 读 self.history[-1].skill (v12 设的); 兜底: 老的 self._active_skill
        # 字段 (v9 之前留下的, 测试 + 老调用还能用)。
        skill_prompt = ""
        if self.history:
            last_user = next(
                (m for m in reversed(self.history) if m.role == "user"), None
            )
            per_msg_skill = getattr(last_user, "skill", None) if last_user else None
            if per_msg_skill:
                skill_prompt = self._skill_prompt_for(per_msg_skill)
        if not skill_prompt:
            # 兜底: v9 老路径, session 内激活的 skill 整段对话期间生效
            skill_prompt = self._active_skill_prompt()
        if skill_prompt:
            system_prompt = system_prompt + skill_prompt
        client = LLMClient(
            self.llm_cfg, system_prompt,
            request_timeout_s=self._chat_timeout_s,
        )

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
        # 2026-10-15:新一轮 chat 起始,工具步骤 reset 成折叠
        # v6: 不再 reset 全局, 旧消息保留各自状态 (dict 留着)
        # v6 之前有 self._tools_collapsed = True 这种 reset; 现在已经不需要

        self._generating = True
        self.send_btn.setEnabled(False)
        self.stop_btn.setVisible(True)
        self.thinking_started.emit()

        if self.registry is not None and self.registry.names():
            # 根据 backend 选择 Agent：手写轻量级 或 LangChain 标准实现
            if self.backend == "standard":
                lc_agent = LangChainAgent(
                    llm_cfg=self.llm_cfg,
                    registry=self.registry,
                    persona=system_prompt,
                    cfg=self.langchain_cfg,
                    # 接线(a):把 phase 2/3 的引擎层上下文(项目目录 / 会话 / 长期记忆 / skills / 文档)
                    # 全部透传给 LangChainAgent。这些都是 best-effort —— 留 None 时走 agent 默认。
                    cwd=self._cwd,
                    session_id=self._session_id,
                    session_store=self._session_store,
                    memory_store=self.memory_store,
                    skills_dir=self._skills_dir,
                    auto_load_skills=self._auto_load_skills or None,
                    extra_docs_paths=self._extra_docs_paths or None,
                )
                # 把现有的 Qt confirm 弹窗挂到 LangChain 的 RiskyToolConfirm 拦截器
                # 上 —— 否则标准后端高风险工具只能靠 chat 里说"确认"二字,体验差。
                # 桥接:_confirm_dangerous_tool 返回 Optional[bool](True=执行,False=拒绝,
                # None=超时也算拒绝);RiskyToolConfirm 的回调返回 True=放行,False/None=拦。
                def _confirm_bridge(tool_call_id: str, tool_name: str, args) -> bool:
                    import json as _json
                    args_json = _json.dumps(args, ensure_ascii=False) \
                        if isinstance(args, dict) else str(args)
                    r = self._confirm_dangerous_tool(tool_name, args_json)
                    return bool(r)
                lc_agent.on_confirm_needed(_confirm_bridge)
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

    def _on_chat_anchor_clicked(self, url):
        """chat_view 上 <a href> 被点击,处理内部命令 (toggle_tools) + 外部链接。

        2026-10-15 v5: cmd:toggle_tools 处理改用 _rebuild_chat_view_from_history
        全量重建 chat_view, 不再调 _refresh_streaming_message —— 后者的
        「从 anchor 删到末尾再 insertHtml」模式在某些状态下会让 chat 区域
        被擦白 (用户实测 v4 反馈「点击后聊天窗口信息清空」)。

        2026-10-15 v6: 按消息分别切换折叠状态。URL 用 fragment (#N) 编码 msg id,
        这里从 fragment 读回, 只切换那条消息的 _tools_collapsed_by_msg[id]。

        v4 的 _NoCmdNavQTextBrowser 仍然有效 —— 它挡 QTextBrowser 默认
        doSetSource 对 cmd: scheme 错误地调 setSource 试图本地解析。
        """
        try:
            scheme = url.scheme() if hasattr(url, "scheme") else ""
        except Exception:  # noqa: BLE001
            scheme = ""
        if scheme == "cmd":
            try:
                cmd = url.path().lstrip("/") if hasattr(url, "path") else str(url).split(":", 1)[-1]
            except Exception:  # noqa: BLE001
                cmd = ""
            if cmd == "toggle_tools":
                # v6: fragment 是 msg_id 整数, 默认 0
                try:
                    msg_id = int(url.fragment() or 0) if hasattr(url, "fragment") else 0
                except (TypeError, ValueError):
                    msg_id = 0
                cur = self._tools_collapsed_by_msg.get(
                    msg_id, self._tools_collapsed_default)
                self._tools_collapsed_by_msg[msg_id] = not cur
                # v5: 全量重建, 不动 anchor/cursor
                self._rebuild_chat_view_from_history()
            return
        # 外部链接:手动打开(setOpenExternalLinks(False) 不会自动开)
        if scheme in ("http", "https", "mailto"):
            try:
                from PyQt5.QtGui import QDesktopServices
                QDesktopServices.openUrl(url)
            except Exception:  # noqa: BLE001
                log.warning("打开外部链接失败: %s", url)

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
                # 2026-10-15:LangChainAgent._astream_with_retry yield 工具事件时
                # args 是空字符串("")(它只携带 ToolMessage.name + content,真实
                # args 在 AIMessage.tool_calls 上但 stream 模式没保留)。_json.loads("")
                # 会抛 JSONDecodeError,被 except 吞 → trace 里看不到 tool 事件
                # → dashboard 调试面板「执行过程」不显示工具。修法:空字符串
                # 视为空 dict,非空才解析。
                if isinstance(args, str):
                    _args = _json.loads(args) if args.strip() else {}
                else:
                    _args = args or {}
                self.trace.record(self._trace_run_id, "tool", {
                    "name": name,
                    "args": _args,
                    "result": (result or "").replace("\n", " ")[:80],
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
                        "正在调用工具，主人稍等一下~"
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
                        "核对了一下执行结果，好像不太对，让我重新确认一下~"
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
        """为单句启动后台 QThread 调 tts.prepare(sentence)，完成后 emit (seq, sentence)
        给 _on_sentence_ready 入队。

        2026-10-15 Bug 6 修复把 done 信号由 (sentence) → (seq, sentence)
        —— 这里补上对 seq 的分配 + 传递,避免 slot 报
        "missing 1 required positional argument: 'sentence'"。
        """
        from app.core.qt_compat import QThread, Signal

        seq = self._tts_seq_next
        self._tts_seq_next += 1

        class _PrepareWorker(QThread):
            done = Signal(int, str)

            def __init__(self, tts_obj, seq_id, txt):
                super().__init__()
                self.tts_obj = tts_obj
                self.seq_id = seq_id
                self.txt = txt

            def run(self):
                try:
                    self.tts_obj.prepare(self.txt)
                except Exception:  # noqa: BLE001
                    log.exception("TTS.prepare 异常: %r", self.txt[:60])
                self.done.emit(self.seq_id, self.txt)

        w = _PrepareWorker(self.tts, seq, sentence)
        w.done.connect(self._on_sentence_ready)
        # 保留引用防 GC，等线程结束自动清理
        self._sentence_workers.append(w)
        w.finished.connect(lambda ww=w: self._sentence_workers.remove(ww))
        w.start()

    def _on_sentence_ready(self, seq: int, sentence: str) -> None:
        """单句 TTS prepare 完成:把该句入队等按序播放。

        2026-10-15:不再直接 speak —— 入队到 _pending_speak[seq],让
        _drain_speak_queue 按编号连续性消费,避免并发 prepare 导致朗读
        顺序颠倒（短句先完成先播，长句后播;模型长输出时尤其明显）。
        """
        self._committed_text = (self._committed_text or "") + sentence
        self._pending_speak[seq] = sentence
        self._drain_speak_queue()

    def _drain_speak_queue(self) -> None:
        """按 _seq 顺序把 _pending_speak 里「连续编号」的句子依次 speak。

        任意句子准备完成就调用一次;只在编号严格连续时才继续消费,
        缺号时不消费(等缺失的更早句子 prepare 完)。
        """
        while self._tts_seq_drain in self._pending_speak:
            sentence = self._pending_speak.pop(self._tts_seq_drain)
            self._tts_seq_drain += 1
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
        self.stop_btn.setVisible(False)
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
        self.stop_btn.setVisible(False)
        self.thinking_stopped.emit()
        self.reply_ready.emit(self._current_bot_msg.content, Emotion.SAD, False)
        self.streaming_done.emit()

    # 气泡内联样式（Qt 富文本对多 class 选择器 / td border-radius 支持不稳定，
    # 直接内联最可靠）
    _BUBBLE_STYLE_USER = (
        "background:#8b7cf6;border:1px solid #7c6cf0;padding:9px 13px;"
        "color:#ffffff;font-size:10pt;line-height:1.55;")
    _BUBBLE_STYLE_BOT = (
        "background:#ffffff;border:1px solid #e9e6f7;padding:9px 13px;"
        "color:#2c2c38;font-size:10pt;line-height:1.55;")

    # -------------------- 窗口位置持久化(2026-10-15 修复 Bug 3) --------------------

    def _restore_or_position_window(self, parent):
        """默认位置策略:
        1. 先看 QSettings(跨 session 记忆)有没有保存位置,有就恢复
        2. 没保存过:如果桌宠位置可知,摆在桌宠右侧
        3. 都没有:居中在主屏
        4. 任何方案都得裁回当前屏幕内(双屏拔了一根 / 改了分辨率也不会跑丢)
        """
        from app.core.qt_compat import QSettings, QGuiApplication
        settings = QSettings("MiniMax", "DesktopPet")
        saved = settings.value("chat_window/pos", None)
        if saved and isinstance(saved, str):
            try:
                x_str, y_str = saved.split(",")
                x, y = int(x_str), int(y_str)
                if self._on_any_screen(x, y):
                    self.move(x, y)
                    return
            except Exception:
                pass
        anchor_x = None
        if parent is not None:
            try:
                geo = parent.frameGeometry()
                anchor_x = geo.x() + geo.width() + 12
                anchor_y = geo.y()
            except Exception:
                anchor_x = None
        screen = QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else None
        if anchor_x is not None and avail is not None:
            x = anchor_x
            y = max(avail.y(), anchor_y)
            if x + self.width() > avail.right():
                x = max(avail.x(), anchor_x - self.width() - 240)
            self.move(x, y)
            return
        if avail is not None:
            x = avail.x() + (avail.width() - self.width()) // 2
            y = avail.y() + (avail.height() - self.height()) // 3
            self.move(x, y)

    def _on_any_screen(self, x, y):
        from app.core.qt_compat import QGuiApplication
        for screen in QGuiApplication.screens():
            geo = screen.availableGeometry()
            if geo.contains(x, y):
                return True
        return False

    def moveEvent(self, ev):
        try:
            from app.core.qt_compat import QSettings
            QSettings("MiniMax", "DesktopPet").setValue(
                "chat_window/pos", f"{self.x()},{self.y()}")
        except Exception:
            pass
        super().moveEvent(ev)

    def changeEvent(self, event):
        """窗口状态变化(最大化)时,同步更新右上角 max 按钮图标。

        2026-10-15 v3: 全屏按钮已移除, changeEvent 只剩 max 同步。
        拖动窗口到屏幕顶端触发系统最大化时, 按钮文字也要跟着切到「还原」,
        否则按钮状态和实际窗口状态对不上。"""
        try:
            from app.core.qt_compat import QEvent
            if event.type() == QEvent.Type.WindowStateChange:
                if hasattr(self, "btn_max"):
                    if self.isMaximized():
                        self.btn_max.setText("[ ]")
                        self.btn_max.setToolTip("还原")
                    else:
                        self.btn_max.setText("▢")
                        self.btn_max.setToolTip("最大化")
        except Exception:
            pass
        super().changeEvent(event)

    def _tool_steps_html(self, tools: list, msg_id: int = 0) -> str:
        """把工具调用渲染成步骤卡片（编号 + 中文名 + 动作目标 + 成败状态）。

        工具先于最终回答执行，故卡片置于气泡正文上方；工具结果不进 TTS。

        2026-10-15 v3:保留点击折叠/展开的 toggle 功能 (cmd:toggle_tools),
        但 head 文本里只显示「已执行操作 · N ▾ / ▴」,不再出现"点击展开"/
        "点击收起"字样 —— 用户实测 2026-10-15「去掉点击展开这4个文字,
        功能还要」。Bug 1 (_current_bot_msg=None 时点链接 chat 变空白)
        由 _on_chat_anchor_clicked 里的守卫挡住, 不影响功能。

        2026-10-15 v6: 改成按消息分别记折叠状态。msg_id 是 id(msg) 整数,
        URL 用 `cmd:toggle_tools#{msg_id}` 编码, _on_chat_anchor_clicked
        从 fragment 读回, 找到对应消息的折叠状态。dict 里没记录的消息
        走默认 (self._tools_collapsed_default, 折叠)。
        """
        if not tools:
            return ""
        rows = []
        for i, t in enumerate(tools):
            name, args, result = _unpack_tool(t)
            cn, target = _tool_label(name, args)
            status = _tool_status(result)
            num = _CIRCLED_NUMS[i] if i < len(_CIRCLED_NUMS) else str(i + 1)
            if status == "cancel":
                mark = '<span style="color:#9ca3af;">⊘ 取消</span>'
            elif status == "fail":
                mark = '<span style="color:#dc2626;">[失败]</span>'
            else:
                mark = '<span style="color:#16a34a;">[完成]</span>'
            target_html = (
                f'&nbsp;<span style="color:#a1a1aa;">·</span>&nbsp;<b>{_html_escape(target)}</b>'
                if target else ""
            )
            rows.append(
                '<div class="tool-row">'
                f'<span class="tool-num">{num}</span>'
                f'<span class="tool-name">{_html_escape(cn)}</span>'
                f'{target_html}'
                f'<span class="tool-mark">{mark}</span>'
                '</div>'
            )
        n = len(tools)
        collapsed = self._tools_collapsed_by_msg.get(
            msg_id, self._tools_collapsed_default)
        # URL 编码 msg_id 用 fragment (#N), 走 cmd:toggle_tools#123
        url = f"cmd:toggle_tools#{msg_id}"
        if collapsed:
            # 折叠态:head + 第一条工具预览;head 文字「已执行操作 · N ▾」
            # 不出现"点击展开"字样,只靠箭头指示
            first_name, first_args, first_result = _unpack_tool(tools[0])
            first_cn, first_target = _tool_label(first_name, first_args)
            preview = (
                '<div style="color:#9ca3af;font-size:9pt;margin:2px 0 0 0;">'
                '▸ ' + _html_escape(first_cn) +
                (' · ' + _html_escape(first_target[:40]) if first_target else '') +
                '  ' + (_unpack_status_mark(first_result)) +
                '</div>'
            )
            return (
                '<div class="tools">'
                f'<a href="{url}" class="tools-head-link">'
                f'已执行操作 · {n}&nbsp;&nbsp;▾'
                '</a>'
                + preview +
                '</div>'
            )
        # 展开态:head + 完整列表 + 收起箭头
        return (
            '<div class="tools">'
            f'<a href="{url}" class="tools-head-link">'
            f'已执行操作 · {n}&nbsp;&nbsp;▴'
            '</a>'
            '<div class="tools-body">' + "".join(rows) + '</div>'
            '</div>'
        )

    def _thoughts_html(self, thoughts: list) -> str:
        """思考块：折叠展示（默认收起，主人点开看推理过程）。

        2026-10-15 跟随 MiniMax Code 风格升级：用 div.thinking 类 + <details>
        嵌套实现折叠；MiniMax Code 的 markdown 也用 blockquote 风格的引用，
        但「思考过程」是内部的, 不放进 markdown 流, 单独走自定义 div。"""
        if not thoughts:
            return ""
        joined = "\n".join(f"> {t.strip()}" for t in thoughts if t.strip())
        # 折叠（<details>）：最多 6 行可折叠，避免铺满屏幕
        return (
            '<div class="thinking">'
            '<details>'
            '<summary class="thinking-head">'
            f'思考过程（{len(thoughts)} 步）</summary>'
            '<pre style="margin:4px 0 2px 0;white-space:pre-wrap;font-family:'
            'Consolas, Menlo, monospace;font-size:9pt;color:#374151;">'
            + _html_escape(joined) + '</pre>'
            '</details>'
            '</div>'
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
            ec_icon = "[完成]" if ec == 0 else "[失败]"
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
                f'<b>{_html_escape(e["path"])}</b>'
                f'&nbsp;<span style="color:#92400e;">· {e["size"]} 字节</span>'
                '</div>'
            )
        head = (
            '<div class="file-edits-head">文件编辑 · '
            + str(len(edits)) + '</div>'
        )
        return '<div class="file-edits">' + head + "".join(rows) + '</div>'

    def _msg_html(self, msg: Message, *, streaming_meta: Optional[str] = None) -> str:
        """按已完成的 Message 渲染成 HTML 字符串 (MiniMax Code 风格线性流)。

        布局 (CSS 驱动, 2026-10-15 重做):
            <div class="msg msg-{role}">
                <div class="msg-label">
                    <span class="who">{role_label}</span>
                    <span class="ts">{time}</span>
                </div>
                <div class="msg-body">
                    <!-- 工具/思考/命令/文件块 (assistant only, prepend) -->
                    <!-- markdown-rendered 文本 -->
                </div>
            </div>

        角色标识:
            - user: "You" (紧凑中性灰)
            - assistant: character name (紫)
            - system: 走 system-msg 居中浅灰条

        头像不再放在 chat 内 (MiniMax Code 同款)。桌宠头顶像仍然由 pet_window
        显示; chat 里只保留 markdown 渲染块, 让人/桌宠对话视觉跟其它 chat-style
        AI 一致 ("跟 minimaxcode 一样" 用户实测指定)。
        """
        if msg.role == "system":
            raw = (msg.content or "").replace("&", "&amp;").replace(
                "<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")
            return (
                '<div style="text-align:center;margin:10px 0;">'
                f'<span class="system-msg">{raw}</span></div>'
            )

        is_user = msg.role == "user"
        role_label = "You" if is_user else _html_escape(
            self.char_cfg.name or "桌宠")

        # 清理内容
        raw = msg.content or ""
        raw = self._clean_content(raw)
        # 2026-10-15 v9: 上传 / 拖图产生的 file:// URL 在 markdown 里只是
        # 一段文字, QTextBrowser 不一定 load (PyQt5 5.x 对 file:// 嵌图不稳)。
        # 渲染前把 markdown 里的 ![alt](file://...) 替换成 data: URL,
        # 这样 QTextBrowser 自包含 base64 嵌图, 不会显示裸路径。
        raw = self._inline_local_images_to_data_url(raw)

        # HTML 转义 + markdown 渲染 (MiniMax Code 风格两边都做 markdown;
        # user 消息内容一般很短, 走 markdown 没副作用)
        body_html = self._render_markdown(raw)

        # 流式输出中：正文末尾追加「三点跳动」等待动画
        if not is_user and streaming_meta and not (msg.emotion and msg.role == "assistant"):
            body_html = body_html + self._typing_dots_html()

        # 工具/思考/命令/文件块 (assistant only, prepend 到 body 前)
        if not is_user:
            thoughts_html = self._thoughts_html(getattr(msg, "thoughts", []) or [])
            execs_html = self._execs_html(getattr(msg, "execs", []) or [])
            file_edits_html = self._file_edits_html(msg.tools)
            # v6: 传 id(msg) 让 _tool_steps_html 把折叠状态按消息分别存
            tools_html = (
                self._tool_steps_html(msg.tools, msg_id=id(msg))
                if (msg.tools and self._show_tool_steps) else ""
            )
            prepend = thoughts_html + execs_html + file_edits_html + tools_html
            if prepend:
                body_html = prepend + body_html

        time_str = _format_time_short(msg.ts)
        label_html = (
            f'<div class="msg-label">'
            f'<span class="who">{role_label}</span>'
            f'<span class="ts">{time_str}</span>'
            f'</div>'
        )
        role_class = "msg-user" if is_user else "msg-assistant"
        return (
            f'<div class="msg {role_class}">'
            f'{label_html}'
            f'<div class="msg-body">{body_html}</div>'
            f'</div>'
        )

    def _clean_content(self, text: str) -> str:
        """清理消息内容：去掉首尾空白，字面量 \\n 转实际换行，折叠连续空行。

        2026-10-15:折叠 3+ 连续空行 → 2 行,避免 read_file PDF/Word 解析输出
        时多个段落间空行被放大成整段空白(用户实测「内容排版有问题,太多
        换行」)。`\n\n` 段间隔保留(语义需要),但 `\n\n\n\n+` 折叠。
        """
        import re
        # 字面量 \n（反斜杠+n）→ 实际换行
        text = text.replace("\\n", "\n")
        # 折叠 3+ 连续空行 → 2 行
        text = re.sub(r"\n{3,}", "\n\n", text)
        # 去掉首尾空白
        text = text.strip()
        return text

    def _inline_local_images_to_data_url(self, text: str) -> str:
        """v9: 渲染前预处理 — 把 markdown 里的本地图片引用 (file:///) 替换
        成 data: URL, 让 QTextBrowser 真的嵌图, 而不是只显示一段路径文字。

        为什么不直接 setHtml + file:// :
          PyQt5 5.x 的 QTextBrowser 对 file:// 嵌图支持不稳, 经常只显示 alt
          文字 (用户实测「图片拖进去是这样子」截图)。data: URL 是 QTextBrowser
          内置 base64 解码, 稳定可靠。

        范围:
          - 只处理 markdown 形式 `![alt](file:///...)`, 扫的是 image 链接
          - file 存在 + size < 8 MiB 才 inline (太大直接显示 file:// 文字)
          - 非 file:// 引用 (http/https/data:) 原样保留
          - 失败 fallback: 保持原 file:// 文字, 不抛异常
        """
        import re as _re
        import base64 as _b64
        # markdown image 形 ![alt](file:///...) 或 ![alt](file:///...xxx)
        pattern = _re.compile(r'!\[([^\]]*)\]\((file:///[^)\s]+)\)')

        def _replace(m: "re.Match[str]") -> str:
            alt = m.group(1)
            url = m.group(2)
            # url 形如 file:///C:/Users/.../xxx.png
            local = url[len("file:///"):]
            if not local:
                return m.group(0)
            # file:// 在 Windows 上以 `/C:/...` 形式, file:/// 后接盘符; pathlib 会正确解
            try:
                p = Path(local)
            except Exception:
                return m.group(0)
            if not p.is_file():
                return m.group(0)
            try:
                if p.stat().st_size > 8 * 1024 * 1024:
                    return m.group(0)
            except OSError:
                return m.group(0)
            try:
                data = p.read_bytes()
            except OSError:
                return m.group(0)
            ext = p.suffix.lower().lstrip(".")
            mime = {
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "gif": "image/gif",
                "bmp": "image/bmp",
                "webp": "image/webp",
            }.get(ext, "application/octet-stream")
            b64 = _b64.b64encode(data).decode("ascii")
            return f"![{alt}](data:{mime};base64,{b64})"

        return pattern.sub(_replace, text)

    def _render_markdown(self, text: str) -> str:
        """把 Markdown 文本渲染为 HTML（MiniMax Code 风格的 CommonMark 子集）。

        支持:
          - ```fenced code``` 代码块 (映射到 <pre><code>)
          - `inline code` 行内代码
          - **bold** / *italic* / ~~strike~~ (忽略 strike,treat as italic 防止 XSS-like pattern)
          - ## / ### / #### 标题 (H2/H3/H4，MiniMax Code 不真的用 H1 给正文)
          - - / * 无序列表 (整段包成 <ul><li>)
          - 1. / 2. 有序列表 (整段包成 <ol><li>)
          - > blockquote 引用块
          - --- hr 水平分隔线
          - [text](url) 链接
          - | col | col | 表格 (简化 GFM 表格)

        实现约束：块元素 (代码块 / 标题 / 列表 / 引用 / 表格) 必须在 HTML 转义
        之前抽出来匹配 (因为 `>` 转义后会变成 `&gt;` 而 blockquote 的 `^>` 模式
        看不见了), 用 placeholder 保留位置, 最后再还原成 HTML。
        """
        import re

        if not text:
            return ""

        # 1. 在 HTML 转义前先把所有块元素抽出来, 留给最后还原
        blocks: list[str] = []
        def _stash(html: str) -> str:
            blocks.append(html)
            return f"\x00B{len(blocks) - 1}\x00"

        # 1a. ```fenced code``` —— 同时保留原 char 内容, 不转义
        def _save_code(m: "re.Match[str]") -> str:
            lang = m.group(1) or ""
            body = m.group(2)
            return _stash(
                f'<pre><code data-lang="{lang}">{body}</code></pre>'
            )
        text = re.sub(r'```(\w+)?\n(.*?)```', _save_code, text, flags=re.DOTALL)

        # 1b. 水平分隔线 (3+ 个 -)
        text = re.sub(r'(?m)^-{3,}\s*$',
                      lambda _: _stash('<hr/>'), text)

        # 1c. 标题 (## ... / ### ... / #### ...)
        text = re.sub(r'(?m)^####\s+(.+?)\s*$',
                      lambda m: _stash(f'<h4>{m.group(1)}</h4>'), text)
        text = re.sub(r'(?m)^###\s+(.+?)\s*$',
                      lambda m: _stash(f'<h3>{m.group(1)}</h3>'), text)
        text = re.sub(r'(?m)^##\s+(.+?)\s*$',
                      lambda m: _stash(f'<h2>{m.group(1)}</h2>'), text)
        text = re.sub(r'(?m)^#\s+(.+?)\s*$',
                      lambda m: _stash(f'<h1>{m.group(1)}</h1>'), text)

        # 1d. 引用块 (> ...)
        def _save_blockquote(m: "re.Match[str]") -> str:
            inner = m.group(0)
            inner = re.sub(r'(?m)^>\s?', '', inner)
            inner_html = inner.replace("\n", "<br/>")
            return _stash(f'<blockquote>{inner_html}</blockquote>')
        text = re.sub(r'(?:^>.*\n?)+', _save_blockquote, text, flags=re.MULTILINE)

        # 1e. 无序列表 (整段连续 - / * 行包一个 <ul>)
        def _save_ul(m: "re.Match[str]") -> str:
            items = re.findall(r'^[-*]\s+(.+?)\s*$', m.group(0), flags=re.MULTILINE)
            return _stash('<ul>' + ''.join(f'<li>{it}</li>' for it in items) + '</ul>')
        text = re.sub(r'(?:^[-*]\s+.+\n?)+', _save_ul, text, flags=re.MULTILINE)

        # 1f. 有序列表 (整段连续 "1. ..." 行包一个 <ol>)
        def _save_ol(m: "re.Match[str]") -> str:
            items = re.findall(r'^\d+\.\s+(.+?)\s*$', m.group(0), flags=re.MULTILINE)
            return _stash('<ol>' + ''.join(f'<li>{it}</li>' for it in items) + '</ol>')
        text = re.sub(r'(?:^\d+\.\s+.+\n?)+', _save_ol, text, flags=re.MULTILINE)

        # 1g. 表格 (简化 GFM: | col | col | / |---|...|)
        def _save_table(m: "re.Match[str]") -> str:
            lines = [ln for ln in m.group(0).splitlines() if ln.strip()]
            if len(lines) < 2:
                return m.group(0)
            header_cells = [
                c.strip() for c in lines[0].strip().strip('|').split('|')
            ]
            sep = lines[1].strip()
            if not re.match(
                r'^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$', sep,
            ):
                return m.group(0)
            body_rows = []
            for ln in lines[2:]:
                cells = [c.strip() for c in ln.strip().strip('|').split('|')]
                body_rows.append(
                    '<tr>' + ''.join(f'<td>{c}</td>' for c in cells) + '</tr>'
                )
            head = (
                '<tr>' + ''.join(f'<th>{c}</th>' for c in header_cells) + '</tr>'
            )
            return _stash('<table>' + head + ''.join(body_rows) + '</table>')
        text = re.sub(
            r'(?:^[ \t]*\|.*\n){2,}',
            _save_table, text, flags=re.MULTILINE,
        )

        # 2. 现在可以安全 HTML 转义 (因为块元素都已抽走, 不会误伤)
        text = text.replace("&", "&amp;")
        text = text.replace("<", "&lt;")
        text = text.replace(">", "&gt;")

        # 3. 行内元素 —— 先做 code (避免被误判)
        text = re.sub(r'`([^`\n]+)`', r'<code>\1</code>', text)

        # 3b. 2026-10-15 v8: markdown 图片 ![alt](url) → <img src=url>
        # v9 升级: 放行 http(s):// / file:// / data: 三种 scheme。
        # data: 是 _inline_local_images_to_data_url 的产物, 渲染稳定;
        # file:// 是降级路径 (file 太大 / 失败), QTextBrowser 不一定 load。
        def _img(m: "re.Match[str]") -> str:
            alt = m.group(1)
            url = m.group(2)
            if re.match(r'^(https?|file|data):', url, re.IGNORECASE):
                return (
                    f'<img src="{url}" alt="{alt}" '
                    f'style="max-width:320px;border-radius:8px;'
                    f'margin:4px 0;"/>'
                )
            return m.group(0)
        text = re.sub(r'!\[([^\]]*)\]\(([^)\s]+)\)', _img, text)

        # 4. 链接 [text](url)  (只允许 http/https/mailto 防止 javascript: 注入)
        def _link(m: "re.Match[str]") -> str:
            label = m.group(1)
            url = m.group(2)
            if re.match(r'^(https?|mailto):', url, re.IGNORECASE):
                return f'<a href="{url}">{label}</a>'
            return m.group(0)
        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', _link, text)

        # 5. 加粗 (必须先于斜体,避免被斜体提前消耗)
        text = re.sub(r'\*\*([^*\n]+)\*\*', r'<strong>\1</strong>', text)
        text = re.sub(r'__([^_\n]+)__', r'<strong>\1</strong>', text)

        # 6. 斜体
        text = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<em>\1</em>', text)
        text = re.sub(r'(?<!_)_([^_\n]+)_(?!_)', r'<em>\1</em>', text)

        # 7. 还原所有占位 (代码块 + 其他块元素)
        for i, blk in enumerate(blocks):
            text = text.replace(f"\x00B{i}\x00", blk)

        # 8. 段落: 双换行分段, 块元素跳过 <p> 包裹
        paragraphs = re.split(r'\n{2,}', text)
        out_paragraphs: list[str] = []
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            if (para.startswith('<h') or para.startswith('<ul')
                    or para.startswith('<ol') or para.startswith('<pre')
                    or para.startswith('<blockquote') or para.startswith('<hr')
                    or para.startswith('<table')):
                out_paragraphs.append(para)
            else:
                out_paragraphs.append(
                    '<p>' + para.replace("\n", "<br/>") + '</p>'
                )
        return "".join(out_paragraphs)

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

    def _rebuild_chat_view_from_history(self) -> None:
        """完全重建 chat_view:从 self.history 重新渲染所有消息,再加上
        当前 streaming 占位 (如果有)。

        2026-10-15 v5: 这是 cmd:toggle_tools 点击处理用的最稳路径。
        _refresh_streaming_message 的「从 anchor 删到末尾再 insertHtml」模式
        在某些状态下会让 chat 区域被擦白 (用户实测 v4 反馈「点击后
        聊天窗口信息清空」)。重建整个 chat_view 慢一点但安全 —— 不动
        anchor, 不动 cursor, 用 setHtml-style 全量重渲染。

        性能: history 长度通常 < 200, 每条 _msg_html 跑一次 markdown 渲染,
        实测 < 50ms, 在 60 fps 桌宠上下文完全无感。
        """
        # 1. 滚到底 (重渲染后还要在底部)
        sb = self.chat_view.verticalScrollBar()
        was_at_bottom = (sb.value() >= sb.maximum() - 4)
        # 2. 清空 chat_view
        self.chat_view.clear()
        # 3. 重新渲染 history 里所有消息
        #    注意: 这里不能调 _on_done 之后才有的 _current_bot_msg = None 路径,
        #    只渲染 history 里的 (历史 assistant 消息 tools 已带)
        for msg in self.history:
            self._render_message(msg)
        # 4. 重建 streaming 占位 (如果还在 streaming)
        if (self._current_bot_msg is not None
                and getattr(self, "_streaming_anchor_pos", None) is not None):
            self._streaming_anchor_pos = self._insert_placeholder(
                self._current_bot_msg
            )
        # 5. 滚到底 (如果之前就在底部) 或保留位置
        if was_at_bottom:
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
        2026-10-15: 改用 CHAT_MD_CSS 里定义的 .typing-dot 类, 颜色统一交给 CSS;
        当前高亮那个用 .typing-dot.active 标。MiniMax Code 风格: 浅灰底 + 紫高亮。
        """
        frame = getattr(self, "_typing_frame", 0) % 3
        dots = []
        for i in range(3):
            cls = "typing-dot active" if i == frame else "typing-dot"
            dots.append(f'<span class="{cls}">●</span>')
        return (
            '<span style="font-size:13px;letter-spacing:1px;">'
            + ''.join(dots) + '</span>'
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