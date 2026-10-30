"""聊天窗口：消息渲染、工具卡片、命令补全、Agent/Stream Worker、拖放上传。"""
from __future__ import annotations

import asyncio
import inspect
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Optional

#: 模型这一轮没给出文字、但确实跑了工具时，**界面**替它补的一句。
#: 只在工具真的成了时才成立 —— 工具失败时这句是假的（主人以为成功了），
#: 渲染层会按工具真实状态换成诚实的说法（落库的原文不动）。
_UI_TOOL_DONE_TEXT = "好的，已经帮主人搞定啦~"

#: 人设口癖 → 开场白语气词（顺序即优先级，先认出哪个用哪个）。
#: 认不出来就不加语气词 —— 宁可平一点，也不能替人设乱加口癖
#: （原来写死「喵~」，大肥鱼是条鲸鱼，对着主人喵喵叫是把人设说错了）。
_PERSONA_TONES = (
    ("喵", "喵~"),
    ("哼", "哼~"),
    ("呜", "呜~"),
    ("嗷", "嗷~"),
    ("诶", "诶~"),
)

#: 上面那句在工具失败时的诚实版本。
_UI_TOOL_DONE_FAIL_TEXT = (
    "唔…刚才那一步没能成功，主人看看上面的操作记录，"
    "要的话我再试一次~"
)

# 头像目录：随代码提交（assets/ 默认进 git）。
# 预设头像 assets/avatars/presets/, 用户上传 assets/avatars/uploads/
# （uploads 已在 .gitignore 排除）。
# 当前选中头像放在 assets/avatars/bot.png, 跟代码一起入库。
AVATAR_DIR = Path("assets") / "avatars"
AVATAR_DIR.mkdir(parents=True, exist_ok=True)
AVATAR_PRESETS_DIR = AVATAR_DIR / "presets"
AVATAR_UPLOADS_DIR = AVATAR_DIR / "uploads"
AVATAR_PRESETS_DIR.mkdir(parents=True, exist_ok=True)
AVATAR_UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


def _safe_qapp():
    """返回当前 QApplication 实例（无 GUI 环境返回 None）。

    用于「阻塞等 worker 完成 + 处理 Qt 事件」场景：processEvents 必须有 QApplication。
    """
    try:
        from PyQt5.QtWidgets import QApplication
        return QApplication.instance()
    except Exception:  # noqa: BLE001
        return None


def _convert_loaded_tools(stored_tools):
    """把 ChatStore 读回的 tools（JSON 序列化的 list-of-lists）转为
    Message dataclass 期望的 list[tuple] 形态。

    SQLite 里存的是 tools_json = JSON，每个工具是 [name, args, result] 三元 list；
    Stage 3.5 之后是 [name, args, result, status] 四元 list（第 4 元 = 信封状态）。
    兼容旧版 2 元组 (name, summary)；损坏项跳过。
    """
    out = []
    for t in (stored_tools or []):
        if isinstance(t, (list, tuple)) and len(t) >= 4:
            # 新版：带信封状态（回放历史时也能显示真实的成败标记）
            out.append((str(t[0]), str(t[1]), str(t[2]), str(t[3])))
        elif isinstance(t, (list, tuple)) and len(t) == 3:
            out.append((str(t[0]), str(t[1]), str(t[2])))
        elif isinstance(t, (list, tuple)) and len(t) == 2:
            # 旧版 2 元组 (name, summary)
            out.append((str(t[0]), "", str(t[1])))
        # 其他形态：跳过（防御性，不抛）
    return out


from app.core.qt_compat import (
    QApplication, QFileDialog, QFont, QFrame, QHBoxLayout, QKeyEvent,
    QLabel, QMenu, QMessageBox, QPixmap, QPlainTextEdit, QPushButton,
    QDialog, QDialogButtonBox, QListWidget, QListWidgetItem,
    Qt, QTextBrowser, QTextCursor, QToolButton,
    QVBoxLayout, QWidget, Signal, QTimer, QColor, QRect,
    QGraphicsDropShadowEffect, QPainter, QPainterPath, QLinearGradient,
    QBuffer, QByteArray, pyqtSlot,
    QDragEnterEvent, QDragMoveEvent, QDropEvent, QMimeData, QUrl,
    QScrollArea, QSplitter, QStatusBar, QThread,
)

from app.voice.character import Emotion, guess_emotion, parse_reply
from app.core.config import (CharacterConfig, LLMConfig, list_characters,
                             switch_character as _switch_character)
from app.core.api_keys import is_placeholder_key
from app.brain.llm_client import ChatMessage, LLMClient
from app.brain.agent import AgentLoop, DANGEROUS_TOOLS
# Stage 4.3：上下文压缩（摘要并遗忘）—— 阈值 / 保留比例 / 摘要请求
from app.brain.compaction import (
    SUMMARY_MARKER,
    SUMMARY_PERSONA,
    CompactionSettings,
    apply_compaction,
    history_chars,
    load_compaction_settings,
    plan_compaction,
)
# LangChainAgent 本体**不**在这里 import：它会连带拉起 langchain.agents /
# langchain_openai（实测 1.84 s + 1.00 s），而 lightweight 后端根本用不到。
# 需要时在 _kickoff_llm 里延迟 import（见该函数）。
# 配置 dataclass 是零依赖的，从轻量模块拿。
from app.brain.langchain_config import LangChainAgentConfig
from app.engine.tools import ToolRegistry
from app.voice.asr import SpeechRecognizer, asr_available
from app.engine.chat_store import SHARED_BUCKET, ChatStore

# 危险工具的中文标签（弹窗里给主人看）+ 等待上限 — 拆到 chat_window.utils
from .utils import _DANGEROUS_LABELS, _CONFIRM_TIMEOUT_S  # noqa: F401
from app.brain.trace import TraceRecorder
from app.ui import ui_style
from app.ui.elided_label import ElidedLabel
from app.ui.memory_panel import MemoryDialog

log = logging.getLogger(__name__)


class _NoCmdNavQTextBrowser(QTextBrowser):
    """chat_view 专用 QTextBrowser —— 覆盖 doSetSource 让内部 cmd: 链接不导航。"""
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
    _tool_step_class, _tool_fail_reason, _voice_result_note,
)


# Message — 拆到 chat_window.message
from .message import Message  # noqa: F401


# _AgentWorker / _StreamWorker / _PlanWorker — 拆到 chat_window.worker
from .worker import _AgentWorker, _StreamWorker, _PlanWorker  # noqa: F401


#  / 命令补全（QPlainTextEdit 没有 setCompleter，自己写一个轻量版）


# _html_escape / _format_time_short — 拆到 chat_window.utils
from .utils import _html_escape, _format_time_short, _img_open_key  # noqa: F401
# _CommandCompleter — 拆到 chat_window.command_completer
from .command_completer import _CommandCompleter  # noqa: F401


# _TitleBarDrag / _AvatarHoverHook — 拆到 chat_window.chrome
from .chrome import _TitleBarDrag, _AvatarHoverHook  # noqa: F401


#: 正在跑的摘要 worker（模块级引用）。
#:
#: 为什么要有它：窗口在摘要跑完之前被关掉时，QThread 对象会被销毁 ——
#: Qt 会报 "QThread: Destroyed while thread is still running"（退出时可能直接 abort）。
#: 模块级列表把 worker 钉住，等它自己跑完。
_LIVE_WORKERS: list = []


class _CompactionWorker(QThread):
    """后台摘要 worker：把最旧的一段历史摘要成几句话。

    摘要要走 LLM（可能慢、可能失败、可能超时）——**绝不能在 GUI 线程里等**：
    它跑在独立线程里，失败只发 failed 信号，
    异常**绝不**抛进聊天路径（红线见 app/brain/compaction.py 的模块文档）。

    done 信号带摘要正文（空字符串 = 失败 / 不可用），
    调用方据此退回「丢最旧」（今天的行为）。
    """

    done = Signal(str)      # 摘要正文（空 = 不可用）
    failed = Signal(str)    # 失败原因（只进日志，不给主人看）

    def __init__(self, summariser: Callable, payload) -> None:
        super().__init__()
        self._summariser = summariser
        self._payload = payload

    def run(self) -> None:
        try:
            text = asyncio.run(self._call())
        except Exception as e:  # noqa: BLE001 - 摘要失败不许炸聊天路径
            self.failed.emit(str(e))
            return
        self.done.emit(text or "")

    async def _call(self) -> str:
        """调摘要器（同步 / 异步都认）。"""
        out = self._summariser(self._payload)
        if inspect.isawaitable(out):
            out = await out
        return str(out or "")


class _CharacterSwitchDialog(QDialog):
    """角色切换对话框：列出 characters/*.yaml 里的角色，选一个换人。

    坏文件（缺 character: 段 / 缺 name / 缺 persona）也列出来，但不可选，
    并在行内标出坏在哪 —— 主人能一眼看出哪个文件没写好。
    """

    def __init__(self, entries, current_path: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("切换角色")
        self.setMinimumWidth(380)
        self._entries = list(entries or [])
        lay = QVBoxLayout(self)
        tip = QLabel("人设来自 characters/*.yaml。切换后立即生效，聊天记录保留。")
        tip.setWordWrap(True)
        lay.addWidget(tip)
        self.list = QListWidget()
        for e in self._entries:
            if not e.ok:
                item = QListWidgetItem("%s（坏了：%s）" % (e.file_name, e.error))
                item.setToolTip(e.error)
                try:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                except Exception:  # noqa: BLE001
                    pass
            elif e.path == current_path:
                item = QListWidgetItem("%s（%s）（当前）" % (e.display_name, e.file_name))
            else:
                item = QListWidgetItem("%s（%s）" % (e.display_name, e.file_name))
            self.list.addItem(item)
        lay.addWidget(self.list)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        # 默认选中当前角色（没有就选第一个可用的）
        for i, e in enumerate(self._entries):
            if e.ok and e.path == current_path:
                self.list.setCurrentRow(i)
                break
        if self.list.currentRow() < 0:
            for i, e in enumerate(self._entries):
                if e.ok:
                    self.list.setCurrentRow(i)
                    break

    def selected_entry(self):
        """当前选中的角色条目（CharacterEntry）；没选中 → None。"""
        row = self.list.currentRow()
        if row < 0 or row >= len(self._entries):
            return None
        return self._entries[row]


# ---------------------------------------------------------------------------
# v58: 一只桌宠一份记忆 —— 共享会话 id + 旧线程迁移
# ---------------------------------------------------------------------------

#: 全部人设共用的会话 id。它同时是三样东西：
#:   1) LangGraph 的 thread_id（聊天历史线程）
#:   2) token 台账的 session_id（成本记账 —— 一个钱包）
#:   3) 聊天窗口内部的 session key
#: 切人设不再换线程，也不再换账本桶。
SHARED_SESSION_ID = "char:pet:main"

#: v31~v57 时代按人设名派生的会话 id（形如 char:小汐:main）—— 迁移用。
_LEGACY_CHAR_SESSION_RE = re.compile(r"^char:(.+):main$")


def _backup_checkpoint_db(db_path: Path) -> Path:
    """备份 checkpoint db，返回备份文件路径。

    用 SQLite 的 backup API（不是 shutil.copy）—— 跟在线连接共存时也能拿到
    一致的快照，不会拷到写了一半的 db。
    """
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = backup_dir / ("%s.bak-%s" % (db_path.name, stamp))
    src = sqlite3.connect(str(db_path), timeout=30)
    try:
        out = sqlite3.connect(str(dst))
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()
    return dst


def _migrate_legacy_char_checkpoints(db_path,
                                     shared_id: str = SHARED_SESSION_ID) -> dict:
    """把旧的 per-character checkpoint 线程改名成共享线程。

    v31~v57 时代 session_id 按人设名派生（char:小汐:main），每个人设一条
    LangGraph 线程 —— 切人设 = 换线程 = 记忆分叉。v58 起所有人设共用一条。

    这里只**改名**：checkpoint 链（parent_checkpoint_id）和 blob 一行都不动，
    所以对话历史不丢。改之前先把整个 db 备份到 <db 同级>/backups/。

    返回 {"migrated": [...], "backup": "...", "rows": n}；盘上没有旧线程时
    返回空 dict（不备份、不写盘）。
    """
    result: dict = {"migrated": [], "backup": "", "rows": 0}
    if not db_path:
        return result
    p = Path(db_path)
    if not p.is_file():
        return result
    try:
        conn = sqlite3.connect(str(p), timeout=30)
    except Exception as e:  # noqa: BLE001
        log.warning("合并旧人设聊天线程失败（打不开 db）: %s", e)
        return result
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "checkpoints" not in tables:
            return result
        legacy = [row[0] for row in conn.execute(
            "SELECT DISTINCT thread_id FROM checkpoints "
            "WHERE thread_id LIKE 'char:%'")]
        legacy = [t for t in legacy
                  if t != shared_id and _LEGACY_CHAR_SESSION_RE.match(t)]
        if not legacy:
            return result
        # 改线程名是写操作 —— 先把整个 db 备份一份
        backup = _backup_checkpoint_db(p)
        result["backup"] = str(backup)
        for old in legacy:
            moved = 0
            for table in ("checkpoints", "writes"):
                if table not in tables:
                    continue
                cur = conn.execute(
                    "UPDATE %s SET thread_id = ? WHERE thread_id = ?" % table,
                    (shared_id, old))
                moved += max(0, int(cur.rowcount or 0))
            conn.commit()
            result["migrated"].append(old)
            result["rows"] += moved
        log.info("旧人设聊天线程已并到共享线程 %s：%s（%d 行），备份 %s",
                 shared_id, ", ".join(legacy), result["rows"], backup)
    except Exception as e:  # noqa: BLE001
        log.warning("合并旧人设聊天线程失败: %s", e)
    finally:
        conn.close()
    return result


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
                 # v31: 不传 session_id 时, 从 char_cfg.name 派生 (f"char:{name}:main")。
                 # 切人设 + 重启 pet → 天然换 thread_id, LangGraph checkpoint 不互相串。
                 session_id: Optional[str] = None,
                 session_store: Optional[object] = None,
                 skills_dir: Optional[str] = None,
                 auto_load_skills: Optional[list] = None,
                 extra_docs_paths: Optional[list] = None,
                 # 桌宠状态拉取器 —— 每次 _kickoff_llm 创建新 LangChainAgent 时
                 # 调一次拿当前 PetState 注入。
                 state_provider: Optional[Callable[[], Any]] = None,
                 # 工具解锁:返回当前桌宠等级的回调。
                 # 给 None 时所有工具全部可见 (兼容旧版)。
                 level_provider: Optional[Callable[[], int]] = None):
        super().__init__(parent)
        self.llm_cfg = llm_cfg
        self.char_cfg = char_cfg
        # 把 cwd 加进沙箱白名单,允许 chat 里说「在项目下搜 YY」能跑通
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
        self._last_proc_ticks: int = 0
        self._chat_timeout_s: int | None = (
            chat_cfg.request_timeout_s if chat_cfg is not None else None
        )
        # 在 AI 气泡上方展示「已执行操作 · N」卡片,开关见 ChatConfig.show_tool_steps。
        self._show_tool_steps: bool = (
            chat_cfg.show_tool_steps if chat_cfg is not None else True
        )
        # 工具步骤默认折叠 —— Qt QTextBrowser 不支持 <details>,用 anchorClicked
        # 切回 setHtml 模拟。新一轮 chat 起始 reset 成 True。按消息分别记折叠状态。
        self._tools_collapsed_by_msg: dict = {}
        self._tools_collapsed_default: bool = True  # 新消息默认折叠
        # chat 图片缩略图 → 点击放大,img-open:KEY 锚点用 dict 查真 URL
        self._img_open_map: dict = {}
        # 用户改头像后,要把 chat 段写回 config.yaml
        self._cfg_path: str = cfg_path
        # TTS 引擎引用（用于在 _on_done 时同步 prepare 音频）
        self.tts = tts
        # 长期记忆（sidecar 的 HTTP 客户端，可选）：供标题栏「记忆」管理面板增删
        self.memory_store = memory_store
        self._memory_dlg: Optional[MemoryDialog] = None
        # 引擎层上下文 —— 全存下来,LangChainAgent 构造时透传
        self._cwd = cwd
        # v58: 全部人设共用一个 session id —— 一只桌宠一份记忆。
        # v31 时代按 char_cfg.name 派生 per-character session_id, 让 LangGraph
        # checkpoint 按角色隔离 —— 那正是「切人设 = 换一条记忆线」的根源。
        # 现在固定用共享 id（同时也是 token 台账的 session_id）。
        # 手工传 session_id 时仍以传入值为准。
        self._session_id = session_id or (SHARED_SESSION_ID if char_cfg else None)
        # 磁盘上还留着的旧 per-character 线程 → 改名并到共享线程。
        # 只迁一次（改之前先备份 db），没传 langchain_cfg 的调用方不碰盘。
        if (langchain_cfg is not None
                and self._session_id == SHARED_SESSION_ID
                and getattr(langchain_cfg, "enable_checkpointer", True)):
            try:
                _migrate_legacy_char_checkpoints(
                    getattr(langchain_cfg, "checkpoint_db", None),
                    shared_id=SHARED_SESSION_ID,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("合并旧人设聊天线程失败: %s", e)
        self._session_store = session_store
        self._skills_dir = skills_dir
        self._auto_load_skills = list(auto_load_skills or [])
        self._extra_docs_paths: list = list(extra_docs_paths or [])
        # 桌宠状态拉取器:主程序传 lambda 包 core.state,每次创建新 agent 取最新
        self._state_provider: Optional[Callable[[], Any]] = state_provider
        # 当前会话激活的 skill 名,整段对话期间拼到 system_prompt 末尾
        self._active_skill: Optional[str] = None
        # 从 cfg_path 读整个 yaml, 注入到 _multimodal.setup_config。
        try:
            import yaml as _yaml
            with open(cfg_path, encoding="utf-8") as _f:
                _full_cfg = _yaml.safe_load(_f) or {}
            if not isinstance(_full_cfg, dict):
                _full_cfg = {}
        except Exception:  # noqa: BLE001
            _full_cfg = {}
        try:
            from app.engine.tools import _multimodal as _mm
            _mm.setup_config(_full_cfg)
        except Exception:  # noqa: BLE001
            log.debug("multimodal.setup_config 注入失败 (非致命)", exc_info=True)
        self.sprite_dir = Path(sprite_dir)
        self.asr_enabled = asr_enabled
        self.asr_model = asr_model
        self.asr_language = asr_language
        # 工具注册表：非 None 时走 Agent 循环（能调工具），否则纯聊天
        # 工具解锁:返回当前桌宠等级 int 的回调 (level_provider 形参来自 __init__)
        self.level_provider = level_provider
        if level_provider is not None and registry is not None:
            # 注入到 ToolRegistry.execute 解锁闸,避免 LangChain 后端漏拦
            from app.engine.tools._core import set_tool_filter_level_provider
            set_tool_filter_level_provider(level_provider)
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
        # 送给 LLM 的上下文字符预算,覆盖最近十来个来回
        self._CTX_CHAR_BUDGET = 12000
        self._worker: Optional[_StreamWorker] = None
        self._generating = False
        self._mic_busy = False
        # 流式清洗的节流步长（字符）：每累积这么多字才重算一次整段清洗。
        # 32 字 ≈ 3~4 个 token ≈ 每秒 10 次更新，肉眼无感，
        # 但把 sanitize_text 的调用次数降到 1/5（总开销 O(n²) 的常数因子大降）。
        self._SAN_STEP = 32
        # v58: 共享历史桶 —— 一只桌宠一份记忆, 三个人设读写同一段历史
        self.chat_store = ChatStore(character_name=SHARED_BUCKET)  # 聊天历史持久化

        # 语音识别（ASR）：按住说话 → 松开识别 → 自动发送
        self.asr: Optional[SpeechRecognizer] = None
        if asr_available() and getattr(self, "asr_enabled", True):
            self.asr = SpeechRecognizer(model_size=self.asr_model or "base",
                                        language=self.asr_language or "zh")
            self.asr.text_ready.connect(self._on_speech_text)
            self.asr.error.connect(self._on_asr_error)
            self.asr.recording_started.connect(self._on_recording_start)
            self.asr.recording_finished.connect(self._on_recording_finish)
            # 预热：延迟 1s 后在**后台线程**加载模型，让首次说话瞬时返回。
            # （模型加载绝不能放 GUI 主线程：ctranslate2 在主线程初始化 OpenMP
            #  会触发 Windows fatal access violation，整个进程秒崩。）
            self._asr_warmup_scheduled = False
            self._asr_warmup_delay_ms = 1000
            self._asr_warmup_relay = None
            self._warmup_asr_model()
        else:
            log.warning("ASR 不可用（未安装 sounddevice / faster-whisper，或配置关闭），语音输入按钮将隐藏")

        self.setWindowTitle(f"和 {char_cfg.name} 聊天")
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
        # 送给 LLM 的上下文字符预算,覆盖最近十来个来回
        self._CTX_CHAR_BUDGET = 12000
        self._worker: Optional[_StreamWorker] = None
        self._generating = False
        self._mic_busy = False
        # v58: 共享历史桶 —— 一只桌宠一份记忆, 三个人设读写同一段历史
        self.chat_store = ChatStore(character_name=SHARED_BUCKET)  # 聊天历史持久化
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
        # 整个窗口接收 drag&drop,图片保存到 assets/uploads 后渲染进 chat_view
        self.setAcceptDrops(True)

        # 默认让 chat 出现在桌宠精灵的右侧,用 QSettings 记忆上次位置,下次恢复
        self._restore_or_position_window(parent)

        # 移动事件 → 实时记到 QSettings,下次打开恢复
        self._save_pos_on_close = True

        self._avatar_path = self._pick_avatar()
        self.avatar_label: Optional[QLabel] = None
        # 富文本圆形头像（QTextBrowser 对 document 资源在 clear() 后会失效，
        # 改用自包含的 data URI：<img src="data:image/png;base64,...">）
        self._avatar_data_bot: str = ""
        # 本地图片 → data URL 的记忆化缓存。
        # _msg_html 被 380 ms 的 _typing_timer 驱动 → 流式期间每秒约 2.6 次
        # 重新读盘 + 重新 base64（单文件上限 8 MiB）。按 (路径, mtime, size)
        # 缓存编码结果，重复渲染直接命中。
        self._img_data_url_cache: dict[tuple, str] = {}
        # 文档里是否嵌过 base64 图片：决定关图片弹窗时要不要整份重建文档
        self._doc_has_inline_images = False        # 桌宠头像保留 (chat_cfg.bot_avatar_path)
        self._bot_avatar_path: str = (
            chat_cfg.bot_avatar_path if chat_cfg is not None else ""
        ) or ""
        # Stage 4.3：上下文压缩（摘要并遗忘）。阈值 / 保留比例 / 摘要超时走
        # app/core/companion_config.yaml 的 context_compaction 段。
        self._compaction_settings: CompactionSettings = load_compaction_settings()
        # 正在跑的后台摘要（worker / plan / inflight 三个状态一起管）
        self._compaction_worker: Optional[_CompactionWorker] = None
        self._compaction_plan = None
        self._compaction_inflight = False
        # 摘要器注入点（测试用；生产走 _summarise_history → 真 LLM）
        self._compaction_summariser: Optional[Callable] = None
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
        # 句子朗读串行化：每句分配 _seq 序号,入队 _pending_speak 后按序 speak
        self._tts_seq_next = 0
        self._tts_seq_drain = 0
        self._pending_speak: dict[int, str] = {}
        # 已启动但尚未回报完成的 prepare worker 数。
        # worker 的 done 信号先于 finished 到达,所以不能靠 _sentence_workers 判断是否还有活的。
        self._tts_prepare_inflight = 0
        # 收尾延后状态：还有 prepare 没回来时,回复的「正式收尾」推迟到最后一个
        # prepare 完成时再做（见 _wait_all_sentence_workers / _finalize_reply）。
        # 原实现在这里同步自旋等待,最长卡住主线程 120 秒,整只桌宠冻住。
        self._finalize_pending = False
        self._finalize_parsed = None
        # 兜底 watchdog：prepare 永远不回来时也要收尾（对应原来的超时放弃等待）
        self._finalize_watchdog = QTimer(self)
        self._finalize_watchdog.setSingleShot(True)
        self._finalize_watchdog.timeout.connect(self._on_finalize_watchdog)
        # trace 的 token 事件攒批：原来每个 token 写一行 sqlite（6.5 ms/token，
        # 含 commit 的 fsync），流式时纯白占 GUI 线程。改成攒起来定时批量写。
        self._trace_text_buf: list[str] = []
        self._trace_flush_timer = QTimer(self)
        self._trace_flush_timer.setSingleShot(True)
        self._trace_flush_timer.setInterval(500)
        self._trace_flush_timer.timeout.connect(self._flush_trace_text)
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
        """生成圆形头像 QPixmap（外圈 2px 白边）。"""
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
        """读用户自定义头像(返回 None = 没设/读不到,继续走默认)。"""
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
        """生成并缓存桌宠圆形头像的 data URI（40px）。"""
        self._avatar_data_bot = self._pixmap_to_data_uri(self._make_avatar_pixmap("bot", 40))

    def set_avatar(self, path: Path) -> None:
        """外部更新角色头像（切换角色时用）。"""
        self._avatar_path = path
        # 标题栏头像
        self.avatar_label.setPixmap(self._make_avatar_pixmap("bot", 44))
        self.avatar_label.setFixedSize(44, 44)
        # 富文本头像缓存（只影响之后的新消息）
        self._register_avatar_resources()

    # -- 角色切换 ---------------------------------------------------------
    def _character_root(self) -> Path:
        """角色文件所在的项目根（找得到 characters/ 目录的那个）。"""
        cands: list[Path] = []
        if self._cwd:
            cands.append(Path(self._cwd))
        try:
            cands.append(Path(self._cfg_path).resolve().parent)
        except Exception:  # noqa: BLE001
            pass
        try:
            cands.append(Path(__file__).resolve().parents[3])
        except Exception:  # noqa: BLE001
            pass
        for p in cands:
            try:
                if (p / "characters").is_dir():
                    return p.resolve()
            except Exception:  # noqa: BLE001
                continue
        return cands[0] if cands else Path.cwd()

    def _open_character_switcher(self) -> None:
        """打开角色切换对话框（列表来自 characters/*.yaml）。"""
        root = self._character_root()
        entries = list_characters(root)
        if not entries:
            QMessageBox.warning(
                self, "切换角色",
                "没找到角色文件（%s 目录下没有 *.yaml）" % (root / "characters"))
            return
        dlg = _CharacterSwitchDialog(entries, self.char_cfg.character_file, self)
        if dlg.exec_() != QDialog.DialogCode.Accepted:
            return
        entry = dlg.selected_entry()
        if entry is None:
            return
        ok, msg = self.switch_character(entry.path)
        if ok:
            # 聊天区留一句提示（历史不清）
            self._append_system_msg(msg)
        else:
            QMessageBox.warning(self, "切换角色失败", msg)

    def switch_character(self, rel_path: str) -> tuple[bool, str]:
        """运行时切换角色：改配置 + 重拼 system prompt + 刷新界面。

        聊天历史**不动** —— 人设和历史是两回事。
        返回 (是否成功, 提示信息)；失败时保持当前角色不变。
        """
        ok, msg = _switch_character(self.char_cfg, self._character_root(), rel_path)
        if not ok:
            return False, msg
        self._rebuild_system_prompt()
        self._refresh_character_ui()
        # v58: 聊天历史是共享的 —— 切人设不换历史库的身份（同一个共享桶）
        store = getattr(self, "chat_store", None)
        if store is not None:
            try:
                store.character_name = SHARED_BUCKET
            except Exception as e:  # noqa: BLE001
                log.warning("同步聊天记录的角色标记失败: %s", e)
        return True, msg

    def _rebuild_system_prompt(self) -> str:
        """按新人设重拼 system prompt（两个后端都要看到新人设）。

        - 有活着的 agent（上一轮那个）：直接 set_persona，下一轮就按新人设说话；
        - 没有：按新人设拼一条出来（跟真正发出去的那条同一套共享层）。
        """
        persona = self.char_cfg.persona
        worker = getattr(self, "_worker", None)
        agent = getattr(worker, "agent", None) if worker is not None else None
        # 正在生成时**不碰**活着的 agent：那一轮已经发出去了，改它只会让
        # 同一轮里的后续请求用错人设。下一轮本来就是新建 client/agent，
        # 自然就是新人设。
        busy = bool(getattr(self, "_generating", False))
        if agent is not None and not busy and hasattr(agent, "set_persona"):
            try:
                return str(agent.set_persona(persona) or "")
            except Exception as e:  # noqa: BLE001
                log.warning("重拼 system prompt 失败: %s", e)
        try:
            from app.brain.agent import apply_persona_to_client
            return apply_persona_to_client(None, persona)
        except Exception as e:  # noqa: BLE001
            log.warning("拼 system prompt 失败: %s", e)
            return ""

    def _refresh_character_ui(self) -> None:
        """角色换人后刷新界面：标题 / 窗口标题 / 头像。

        聊天历史**不动**（人设和历史是两回事）。
        """
        name = self.char_cfg.name
        title = getattr(self, "title_label", None)
        if title is not None:
            try:
                title.setText("和 %s 的对话" % name)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.setWindowTitle("和 %s 聊天" % name)
        except Exception:  # noqa: BLE001
            pass
        # 头像：走现成的 set_avatar（重新生成圆形头像 → 换成新角色的首字）
        try:
            self.set_avatar(self._avatar_path)
        except Exception as e:  # noqa: BLE001
            log.warning("刷新角色头像失败: %s", e)
        subtitle = getattr(self, "subtitle_label", None)
        if subtitle is not None:
            try:
                subtitle.setText(self._status_text())
            except Exception:  # noqa: BLE001
                pass

    # 头像自定义入口
    def pick_bot_avatar(self, parent=None) -> bool:
        """弹文件选择对话框让用户给桌宠选头像;选完复制到 data/avatars/bot.png。"""
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
        # 把 chat 段写回 config.yaml,下次启动自动恢复
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
            │ 头像  小汐                                    │
            │       模型 · 工具数 · 记忆数   [清屏] [Demo]  │   ← 标题
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
        # v37: 4 个窗统一阴影 token (跟 settings / memory 一致)
        _shadow.setBlurRadius(ui_style.WINDOW_SHADOW_BLUR)
        _shadow.setOffset(0, ui_style.WINDOW_SHADOW_OFFSET)
        _shadow.setColor(QColor(
            ui_style.WINDOW_SHADOW_COLOR_R,
            ui_style.WINDOW_SHADOW_COLOR_G,
            ui_style.WINDOW_SHADOW_COLOR_B,
            ui_style.WINDOW_SHADOW_ALPHA))
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
        self.title_label = title    # 换角色时要改标题文字
        dz.addWidget(title)
        self.subtitle_label = ElidedLabel(self._status_text())
        self.subtitle_label.setObjectName("titlebar_subtitle")
        dz.addWidget(self.subtitle_label)
        hl.addWidget(drag_zone, 1)

        # 右侧操作按钮（v24 精简：清空 / 记忆 / 设置 —— Demo 改去任务栏入口,
        # 头像改放设置面板。chat 顶部不再塞一堆按钮, 更简洁)
        self.btn_clear = QPushButton("清屏")
        self.btn_clear.setObjectName("ghost_btn")
        # v31: 改成"清屏" — 只清 UI, 历史对话留在内存 + DB, 桌宠仍能
        # 基于全历史回答(不丢"长期对话"能力)。重启 pet 历史会自动加载回来。
        self.btn_clear.setToolTip("清屏 — 只清聊天窗显示, 历史对话仍保留在内存 + 数据库, 桌宠仍能看到")
        self.btn_clear.clicked.connect(self._on_clear)
        hl.addWidget(self.btn_clear)

        self.btn_memory = QPushButton("记忆")
        self.btn_memory.setObjectName("ghost_btn")
        self.btn_memory.setToolTip("管理长期记忆（查看 / 新增 / 删除）")
        self.btn_memory.clicked.connect(self._open_memory)
        hl.addWidget(self.btn_memory)

        # 工具解锁入口：与设置/清屏同行
        self.btn_unlock = QPushButton("工具解锁")
        self.btn_unlock.setObjectName("ghost_btn")
        self.btn_unlock.setToolTip("查看工具解锁进度（每升 1 级解锁新工具）")
        self.btn_unlock.clicked.connect(self._open_tool_unlock)
        hl.addWidget(self.btn_unlock)

        # 角色切换入口 —— 列出 characters/*.yaml 里的角色，换人不用改文件
        self.btn_character = QPushButton("角色")
        self.btn_character.setObjectName("ghost_btn")
        self.btn_character.setToolTip("切换角色（人设来自 characters/*.yaml，切换后立即生效）")
        self.btn_character.clicked.connect(self._open_character_switcher)
        hl.addWidget(self.btn_character)

        # 控制台按钮 —— 打开浏览器控制台（设置都在里面）
        self.btn_settings = QPushButton("控制台")
        self.btn_settings.setObjectName("ghost_btn")
        self.btn_settings.setToolTip("在浏览器打开控制台（设置 / 记忆 / 调试都在里面）")
        self.btn_settings.clicked.connect(self._open_settings)
        hl.addWidget(self.btn_settings)

        self.btn_min = QToolButton()
        self.btn_min.setObjectName("win_btn")
        self.btn_min.setText("─")
        self.btn_min.setToolTip("最小化")
        self.btn_min.clicked.connect(self.showMinimized)
        hl.addWidget(self.btn_min)

        # 最大化/还原按钮 —— 跟常见窗口右上角控件一致
        self.btn_max = QToolButton()
        self.btn_max.setObjectName("win_btn")
        self.btn_max.setText("▢")
        self.btn_max.setToolTip("最大化")
        self.btn_max.clicked.connect(self._toggle_maximized)
        hl.addWidget(self.btn_max)

        # 关闭按钮
        self.btn_close = QToolButton()
        self.btn_close.setObjectName("win_btn_close")
        self.btn_close.setText("X")
        self.btn_close.setToolTip("关闭")
        self.btn_close.clicked.connect(self.close)
        hl.addWidget(self.btn_close)

        # 标题栏文字按钮锁定最小宽度 —— 空间紧张时让标题区省略,而不是把按钮文字裁掉
        for _tb in (self.btn_clear, self.btn_memory, self.btn_unlock,
                    self.btn_character,
                ):
            _tb.setMinimumWidth(_tb.sizeHint().width())

        cl.addWidget(header)

        # 标题栏拖动（头像 / 标题 / 副标题区域）
        self._titlebar = header
        self._drag_filter = _TitleBarDrag(self)
        drag_zone.installEventFilter(self._drag_filter)
        self.avatar_label.installEventFilter(self._drag_filter)
        title.installEventFilter(self._drag_filter)
        # 头像悬浮 → tooltip 显示桌宠状态（不用切回桌宠本体就能瞄一眼）
        self.avatar_label.installEventFilter(_AvatarHoverHook(self))

        self.chat_view = _NoCmdNavQTextBrowser()
        self.chat_view.setObjectName("chat_view")
        # 内部命令用 anchorClicked 拦截,外部链接交给 openExternalLinks event filter
        self.chat_view.setOpenExternalLinks(False)
        # doSetSource 改由 _NoCmdNavQTextBrowser 覆盖, cmd: 不替换 view, 外部链接不动
        self.chat_view.anchorClicked.connect(self._on_chat_anchor_clicked)
        chat_font = QFont()
        chat_font.setFamily("Microsoft YaHei, PingFang SC, Segoe UI, sans-serif")
        chat_font.setPointSize(10)
        self.chat_view.setFont(chat_font)
        # 线性流 markdown 样式表;旧 CHAT_BUBBLE_CSS 保留作历史兼容
        self.chat_view.document().setDefaultStyleSheet(ui_style.CHAT_MD_CSS)
        # 加右侧工具详情面板(默认收起), QSplitter 支持用户拖宽 / 关闭
        self._tools_panel = _NoCmdNavQTextBrowser()
        self._tools_panel.setObjectName("tools_panel")
        self._tools_panel.setOpenExternalLinks(False)
        self._tools_panel.setMaximumWidth(380)   # 防止太大占 chat
        self._tools_panel.setMinimumWidth(0)
        self._chat_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._chat_splitter.setObjectName("chat_splitter")
        self._chat_splitter.addWidget(self.chat_view)
        self._chat_splitter.addWidget(self._tools_panel)
        self._chat_splitter.setSizes([9999, 0])   # 默认藏起
        self._chat_splitter.setHandleWidth(2)
        cl.addWidget(self._chat_splitter, 1)

        input_container = QFrame()
        input_container.setObjectName("input_container")
        il = QVBoxLayout(input_container)
        il.setContentsMargins(12, 8, 12, 8)
        il.setSpacing(4)

        # 拖入 / 上传的图片先堆到 "输入框上方" 的缩略图条;按发送时再作为 user message 渲染
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
        # / 命令补全 —— 让主人输入 "/skills/" 时 popup 列出当前 skills_dir 下所有 skill 名字
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
        # v31: token/CPU/mem 状态 — 跟发送按钮并排, 不再放输入框底部独占一行。
        # 用 QStatusBar 但它没并排能力, 直接用 3 个 QLabel 加到 bottom_row 里
        # (放在 send_btn 前面)。1 Hz 定时刷新 CPU/mem, token 数在 _on_chunk /
        # _on_done 收 finish event 时立刻更新 (用 LLM API 真实 usage)。
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._refresh_status_bar)
        self._status_timer.start()
        self._status_label_token = QLabel("tokens: 0")
        self._status_label_cpu = QLabel("CPU: 0%")
        self._status_label_mem = QLabel("mem: 0 MB")
        for _lb in (self._status_label_token, self._status_label_cpu, self._status_label_mem):
            _lb.setStyleSheet(
                "color:#8e8e93;font-size:8.5pt;padding:0 6px;"
                "background:transparent;border:none;"
            )
        # 累计 token 数 (lightweight backend 走 LLMClient.chat_stream_events 时,
        # 收 finish event 的 usage 累加。standard (LangChain) 路径走 _AgentWorker,
        # 它的 astream 不暴露 usage, 此处 fallback 字符估算 = self.history 长度 / 4。
        self._accum_tokens = 0
        self._accum_prompt = 0
        self._accum_completion = 0
        # psutil 跨平台 CPU 计数器 (Windows 上 /proc 不存在, 走 psutil 路径)
        self._psutil_proc = None
        try:
            import psutil as _ps
            self._psutil_proc = _ps.Process()
            # 第一次 cpu_percent 必返 0 (无 prev), 预热一次
            self._psutil_proc.cpu_percent(interval=None)
        except Exception:
            self._psutil_proc = None

        # 输入区底部：状态 + 按钮
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(4)

        # 底部辅助按钮排:上传 / 多模态 / 语音
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
        # v31: token/CPU/mem 状态在 send_btn 前面 (跟它并排, 跟之前放输入框底部
        # 独占一行不一样)。中间用 stretch 隔开, 状态靠右, 发送按钮最右。
        # 3 个状态点紧凑放, 跟 mic_btn 等高, 不让 input 区下面空一坨
        bottom_row.addStretch(1)
        bottom_row.addWidget(self._status_label_token)
        bottom_row.addWidget(self._status_label_cpu)
        bottom_row.addWidget(self._status_label_mem)
        bottom_row.addWidget(self.send_btn)

        il.addLayout(bottom_row)
        cl.addWidget(input_container)

        # 无边框窗口没有系统边框,补上「拖拽四边/四角调整大小」
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
            self._append_system_msg("当前未接入记忆服务（sidecar），无法管理长期记忆")
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

    def _open_settings(self) -> None:
        """chat 顶部「控制台」按钮 —— 在浏览器打开控制台。

        设置（头像 / 模型 / TTS / Live2D 外观…）都搬进控制台了，
        这里不再有设置窗口要弹。
        """
        self._open_dashboard()

    def _on_dev_level_changed(self, new_level: int) -> None:
        """开发者模式改了 level → 刷新 LLM 工具可见性。

        AgentLoop / LangChainAgent 每轮调 level_provider 读最新等级,
        所以这里只要让下轮重建即可。LangChain 后端的 graph 已按旧等级
        编译了 tool 列表,需要把 _agent 置 None 强制重建。
        """
        try:
            lc = getattr(self, "_lc_agent", None)
            if lc is not None:
                lc._agent = None   # 下一轮 astream 会调 _ensure_agent 重建 tools
            log.info("工具解锁:等级改为 Lv.%d,Agent 下轮重建工具列表", new_level)
        except Exception:  # noqa: BLE001
            log.debug("重建工具列表失败(非致命)", exc_info=True)

    def _open_tool_unlock(self) -> None:
        """chat 顶部「工具解锁」按钮 —— 弹出当前等级的解锁进度对话框。"""
        try:
            from app.engine.tools import _unlock
            from app.engine.tools._risk import TOOL_RISK_LEVEL, risk_name
            from .tool_unlock_dialog import build_tool_unlock_dialog

            # load_unlock_config 内部会回落 companion_config；
            # 必须先加载再取 TOOL_UNLOCK, 早于加载取到的是空 dict 引用。
            _unlock.load_unlock_config()
            TOOL_UNLOCK = _unlock.TOOL_UNLOCK
            rewards = dict(_unlock.UNLOCK_REWARDS)
            celebrate = dict(_unlock.UNLOCK_CELEBRATE)
            MCP_DEFAULT_LEVEL = _unlock.MCP_DEFAULT_LEVEL

            level = 1
            if self.level_provider is not None:
                try:
                    level = int(self.level_provider())
                except Exception:  # noqa: BLE001
                    pass

            mcp_names: list = []
            if self.registry is not None:
                _, mcp_names = _unlock.split_configured_and_mcp(
                    self.registry.names())
            unlocked = _unlock.compute_unlocked(level, mcp_names=mcp_names)
            # 计数用显示名（MCP 走短名），保证跟 level_to_tools 的 key 对得上
            unlocked_display = {_unlock.short_mcp_name(n) for n in unlocked}

            # level -> [(显示名, 风险等级名, 功能说明), ...]
            def _desc_of(real_name: str) -> str:
                """工具的 LLM description —— 对话框用它说明这工具能干什么。"""
                if self.registry is None:
                    return ""
                try:
                    tool = self.registry.get(real_name)
                except Exception:  # noqa: BLE001
                    return ""
                return getattr(tool, "description", "") or ""

            level_to_tools: dict = {}
            for lvl, names in TOOL_UNLOCK.items():
                level_to_tools[int(lvl)] = [
                    (n, risk_name(TOOL_RISK_LEVEL.get(n)), _desc_of(n))
                    for n in names
                ]
            if mcp_names:
                level_to_tools.setdefault(MCP_DEFAULT_LEVEL, []).extend(
                    (_unlock.short_mcp_name(n), "UNKNOWN",
                     _desc_of(n) or f"MCP 外部工具（{n.split('_')[1]} 服务器）")
                    for n in mcp_names)

            dlg = build_tool_unlock_dialog(
                level=level,
                unlocked_display=unlocked_display,
                level_to_tools=level_to_tools,
                rewards=rewards,
                celebrate=celebrate,
                parent=self,
            )
            dlg.exec_()
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("打开工具解锁对话框失败")

    def _toggle_maximized(self) -> None:
        """最大化 / 还原 切换。"""
        # 全屏状态下要先退出全屏, 否则 maximize 会被忽略
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
        """打开纯静态 Live2D Demo（本地 8765）,供托盘菜单调用。"""
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
        """打开开发者控制台（8766）：设置 / 记忆 / 调试都在里面。"""
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
            self._append_system_msg("控制台已打开 (http://127.0.0.1:8766)")
        else:
            self._append_system_msg("控制台启动失败，查看 data/dashboard.log")

    def _find_app(self):
        qa = QApplication.instance()
        return getattr(qa, "_desktop_pet_app", None) if qa else None

    def _append_system_msg(self, text: str) -> None:
        """在聊天区追加一条系统消息（灰色提示）。"""
        self._render_message(Message(role="system", content=text))

    # 本地图片 data URL 的记忆化缓存上限
    # （base64 后体积 ×1.33，所以只缓存小图 + 限制条目数）
    _IMG_CACHE_MAX_BYTES = 2 * 1024 * 1024
    _IMG_CACHE_MAX_ENTRIES = 6

    # 【幻觉检测】匹配「已 XX」类断言性话术
    _HALLUCINATION_PATTERNS = [        (r"已打开\s*[\"「]?([^\s」。,\.]+?)[\"」]?", "open"),
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
        """开场白。

        语气词**按人设走**：原来写死「喵~」—— 可大肥鱼是条鲸鱼，
        对着主人喵喵叫是把人设说错了（小晓是雌小鬼，也不该喵）。
        这里从人设文本里认它自己的口癖（喵 / 哼 / 呜 / 嗷 / 诶），
        认不出来就不加语气词 —— 宁可平一点，也不能替人设乱加口癖。
        """
        self._render_message(Message(
            role="assistant",
            content=f"{self._persona_tone()}主人你好呀！我是 "
                    f"{self.char_cfg.name}，有什么我能帮你的吗？",
            emotion=Emotion.HAPPY,
            speaker=self.char_cfg.name,
        ))

    def _persona_tone(self) -> str:
        """人设自己的口癖（开场白用）。认不出来返回空串。"""
        persona = self.char_cfg.persona or ""
        for keyword, tone in _PERSONA_TONES:
            if keyword in persona:
                return tone
        return ""

    def _load_history(self) -> None:
        """从 ChatStore 加载上次的聊天记录到界面。"""
        # 启动时清掉历史空 content 消息
        self.chat_store.remove_empty_assistant()

        stored = self.chat_store.all()
        if not stored:
            return
        log.info("加载 %d 条历史聊天记录", len(stored))
        for sm in stored:
            # 跳过系统欢迎消息（已在 _append_system_welcome 中添加）
            if sm.role == "assistant" and "主人你好呀" in sm.content:
                continue
            # 兜底再跳一次 (若 remove_empty_assistant SQLite 失败,界面上不渲染空白文件图标)
            if (sm.role == "assistant"
                    and not (sm.content or "").strip()
                    and not (sm.tools or [])):
                continue
            # 解析情绪
            emotion = None
            if sm.emotion:
                try:
                    emotion = Emotion(sm.emotion)
                except ValueError:
                    pass
            # 把 ChatStore 反序列化的 list-of-lists 还原成 tuple
            tools_loaded = _convert_loaded_tools(sm.tools)
            # ts 必须**用库里那条**：Message 的默认值是「现在」，
            # 不传的话昨天说的话会标成今天的时间（界面上骗人）。
            msg = Message(role=sm.role, content=sm.content,
                          emotion=emotion, tools=tools_loaded,
                          ts=float(getattr(sm, "ts", 0) or 0) or time.time())
            self._render_message(msg)
            self.history.append(msg)
        # 历史是从磁盘读的，可能远超过条数/字符上限（ChatStore 存 200 条）。
        # 不在这里裁一次的话，启动后第一轮会把整份历史发给模型。
        self._trim_history()

    def _on_clear(self) -> None:
        """v31+: 清屏 — 聊天窗 UI 清空, 但历史对话**仍保留**在内存 self.history
        和 DB (chat_store) 里, LLM 下次回复仍能基于全历史。

        之前行为 (pre-v31) 是 chat_view + history + chat_store 一起清,
        等于删库。主人反馈这样没"长期对话能力"——LLM 忘了之前说啥。
        现在只清屏, LLM 上下文完整, 重启 pet 仍能加载历史 (除非开了
        _load_history 隐藏模式, 默认不隐藏)。
        """
        if self._generating:
            QMessageBox.information(self, "提示", "生成中，请先停止再清空")
            return
        # 1) 清屏: chat_view DOM 清空, 主人看到的是空对话
        self.chat_view.clear()
        # 2) 重新插欢迎语 (跟 _load_history 末尾一样, 跟第一次开窗的体验)
        self._append_system_welcome()
        # 3) self.history 保留 — _kickoff_llm 仍从这里读, LLM 仍看到全对话
        # 4) self.chat_store 不动 — DB 记录还在, 重启 pet / _load_history 仍能加载
        log.info("ChatWindow: 清屏 (历史保留 %d 条 in-memory, LLM 仍可见)",
                 len(self.history))

    def _on_send(self) -> None:
        if self._generating:
            return
        text = self.input_edit.toPlainText().strip()
        pending = self._consume_pending_attachments()  # 取出待发送附件
        if not text and not pending:
            return
        # 限长：>2000 字符警告并截断
        if len(text) > 2000:
            self._append_system_msg(
                f"消息太长（{len(text)} 字符），已截断到 2000")
            text = text[:2000]

        # 主人按发送时,如果有待发送附件,把它们的 markdown image 引用拼到 user message 内容
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

        # 聊天开头写 `/skills/<name> ...` 时:这一次对话用这个 skill,剩余文本作为 user message
        if text.startswith("/skills/"):
            head, _, rest = text.partition(" ")
            # head 形如 /skills/<name>
            name = head[len("/skills/"):].strip().strip("/")
            if name:
                ok, msg = self._activate_skill(name)
                self._append_system_msg(msg)
                # skill 仅对当条消息生效;设到 msg.skill
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

        命令字面量统一英文；内部说明 / 给主人看的内容用中文。
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
# `/skills/<name>` 跟「聊天开头写 /skills/<name>」是同一条激活路径
        if cmd == "/skills" or cmd.startswith("/skills/"):
            from app.brain.skills import list_skills
            skills_dir = getattr(self, "_skills_dir", None) or "skills"
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
                    written = self.memory_store.add(args, category="fact")
                except Exception as e:  # noqa: BLE001
                    written = None
                    log.warning("memory_store.add 失败: %s", e)
                if not written:
                    # sidecar 不在 / 写失败 —— **不能**对主人说「已记下」
                    self._append_system_msg("记忆服务不可用，这条没能记下")
                    return
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
        """从快捷输入框发送消息（直接传入文本，不走 input_edit）。"""

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
            '<div class="plan-card">'
            '<div class="plan-card-head">'
            '[计划]（不会自动执行，主人说「按计划做」才会执行）</div>'
            f'<div class="plan-card-body">{safe}</div>'
            '</div>'
        )
        # 直接插入到 chat_view(不动 history / chat_store)
        self.chat_view.append(html)

    # ------------------------------------------------------------------
    # Stage 4.3：上下文压缩 —— 摘要并遗忘（不再只是「丢最旧」）
    #
    # 规则（参数口径对齐 qq-bridge 的 dsh-compaction-basic）：
    #   1. 用量 < 预算 × 80% → **一个字都不动**（历史逐字节不变）
    #   2. 用量 >= 80% → 把**最旧**的一段摘要成一条，最近 16% 的消息原文保留
    #      （**最新 8 条永远原文保留**，不参与摘要）
    #   3. 摘要起不来 / 失败 / 超时 / 空结果 → 退回今天的行为（丢最旧的）
    #
    # 两条硬约束：
    #   - 摘要**只发生在对话历史里**，system prompt 一个字节都不动
    #     （MiniMax 的前缀缓存按「最长公共前缀」判定，
    #      见 tests/test_prompt_cache_stability.py）
    #   - 摘要在**后台线程**里跑，聊天永远不阻塞；失败也**不抛异常**
    # ------------------------------------------------------------------

    def _trim_history(self) -> None:
        """裁剪送给 LLM 的上下文（Stage 4.3：先摘要，再丢最旧的）。

        低于阈值时**什么都不做**（历史逐字节不变）；超阈值时把最旧的一段
        交给后台摘要器，回来后换成「一条摘要 + 最近的原文」。

        摘要器起不来（没配 key / 已经在跑）或摘要失败 → 退回
        _drop_oldest_history()，也就是今天的行为。
        """
        if not self.history:
            return
        if self._compaction_inflight:
            # 摘要正在后台跑：这一轮先不动历史，等摘要回来整体换
            # （动了会让摘要对应的消息对不上号）
            return

        budget = self._CTX_CHAR_BUDGET
        settings = self._compaction_settings
        if not budget or budget <= 0:
            # 没有字符预算 → 只按条数上限裁（今天的行为）
            self._drop_oldest_history()
            return
        used = history_chars(self.history)
        if used < settings.threshold_ratio * budget:
            return          # 低于阈值：历史逐字节不变

        plan = plan_compaction(self.history, settings, budget=budget)
        if plan is not None and self._start_compaction(plan):
            return
        # 没东西可摘要（消息太少）或摘要起不来 → 退回丢最旧
        self._drop_oldest_history()

    def _drop_oldest_history(self, protect_tail: int = 0) -> None:
        """今天的做法：超限就丢最旧的（Stage 4.3 之前的唯一裁剪方式）。

        这是**摘要失败时的兜底**：条数上限 + 字符预算，从最旧的开始丢，
        行为跟压缩前完全一样。

        protect_tail > 0 时，最新的 protect_tail 条**永远保留** ——
        压缩之后最新 8 条必须逐字节不变（它们比字符预算更硬）。
        """
        before = len(self.history)
        if before > self._MAX_HISTORY:
            self.history = self.history[-self._MAX_HISTORY:]

        # 字符预算：从新到旧累加，超出就截断
        budget = self._CTX_CHAR_BUDGET
        if budget and budget > 0:
            # 最新的 protect_tail 条单独拎出来（它们永远保留），
            # 字符预算只作用在更旧的那部分上
            n = len(self.history)
            tail_len = max(0, min(int(protect_tail or 0), n))
            head = self.history[:n - tail_len]
            tail = self.history[n - tail_len:]
            kept: list[Message] = []
            used = 0
            for m in reversed(head):
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
            self.history = kept + tail

        after = len(self.history)
        if after != before:
            log.debug("上下文裁剪：%d → %d 条（上限 %d / 预算 %d 字符）",
                      before, after, self._MAX_HISTORY, self._CTX_CHAR_BUDGET)

    def _start_compaction(self, plan) -> bool:
        """起后台摘要。返回 False = 起不来（调用方退回「丢最旧」）。"""
        if self._compaction_inflight:
            return False
        if is_placeholder_key(getattr(self.llm_cfg, "api_key", "") or ""):
            log.info("上下文压缩：没配 API Key，跳过摘要（退回丢最旧）")
            return False
        worker = _CompactionWorker(self._summarise_history, plan.to_summarise)
        worker.done.connect(self._on_compaction_done)
        worker.failed.connect(self._on_compaction_failed)
        self._compaction_plan = plan
        self._compaction_inflight = True
        self._compaction_worker = worker
        # 钉住 worker：窗口先关掉时 QThread 不能被销毁（见 _LIVE_WORKERS）
        _LIVE_WORKERS[:] = [w for w in _LIVE_WORKERS if w.isRunning()]
        _LIVE_WORKERS.append(worker)
        worker.start()
        log.info("上下文压缩：%d 条历史（%d 字符 / 预算 %d）超阈值，"
                 "摘要最旧的 %d 条，保留最近 %d 条原文",
                 len(plan.to_summarise) + len(plan.keep), plan.used_chars,
                 plan.budget, len(plan.to_summarise), len(plan.keep))
        return True

    async def _summarise_history(self, messages: list) -> str:
        """摘要请求（后台线程里跑）。**永不抛异常** —— 失败返回空串。

        失败 = 空串 = 调用方退回「丢最旧」，这一轮照样能聊。
        """
        hook = self._compaction_summariser
        if hook is not None:
            # 注入点（测试 / 以后想换摘要模型时走这里）
            try:
                out = hook(messages)
                if inspect.isawaitable(out):
                    out = await out
                return str(out or "")
            except Exception as e:  # noqa: BLE001 - 摘要失败不许炸聊天路径
                log.warning("上下文压缩：摘要失败（%s）", e)
                return ""
        from app.brain.compaction import summarise_history
        client = None
        try:
            client = LLMClient(self.llm_cfg, SUMMARY_PERSONA,
                               request_timeout_s=self._chat_timeout_s)
            return await summarise_history(client, messages,
                                           self._compaction_settings)
        except Exception as e:  # noqa: BLE001 - 摘要失败不许炸聊天路径
            log.warning("上下文压缩：摘要请求失败（%s）", e)
            return ""
        finally:
            if client is not None:
                try:
                    await client.close()
                except Exception:  # noqa: BLE001
                    pass

    @pyqtSlot(str)
    def _on_compaction_done(self, text: str) -> None:
        """摘要回来了：摘要条目放到最旧的位置，替换掉被摘要的那一段。"""
        plan = self._compaction_plan
        self._compaction_plan = None
        self._compaction_inflight = False
        worker = self._compaction_worker
        self._compaction_worker = None
        self._release_compaction_worker(worker)
        text = (text or "").strip()
        if not text or plan is None:
            log.warning("上下文压缩：摘要不可用，退回「丢最旧」")
            self._drop_oldest_history()
            return
        try:
            before = len(self.history)
            self.history = apply_compaction(
                self.history, plan, text, self._make_summary_entry)
        except Exception:  # noqa: BLE001 - 摘要失败不许炸聊天路径
            log.exception("上下文压缩：应用摘要失败，退回「丢最旧」")
            self._drop_oldest_history()
            return
        log.info("上下文压缩：%d 条 → %d 条（1 条摘要 + %d 条原文）",
                 before, len(self.history), len(self.history) - 1)
        self._enforce_budget_after_compaction()

    @pyqtSlot(str)
    def _on_compaction_failed(self, err: str) -> None:
        """摘要彻底失败（异常）→ 退回「丢最旧」，这一轮照样能聊。"""
        log.warning("上下文压缩：摘要失败（%s），退回「丢最旧」", err)
        self._compaction_plan = None
        self._compaction_inflight = False
        worker = self._compaction_worker
        self._compaction_worker = None
        self._release_compaction_worker(worker)
        self._drop_oldest_history()

    def _release_compaction_worker(self, worker) -> None:
        """摘要把 worker 收尾（从模块级列表里摘掉 + 丢给 Qt 回收）。"""
        if worker is None:
            return
        try:
            _LIVE_WORKERS[:] = [w for w in _LIVE_WORKERS if w is not worker]
        except Exception:  # noqa: BLE001
            pass
        try:
            worker.deleteLater()
        except Exception:  # noqa: BLE001
            pass

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 的命名
        """关窗前等一下后台摘要线程（别让 QThread 在运行时被销毁）。"""
        worker = getattr(self, "_compaction_worker", None)
        if worker is not None:
            try:
                worker.wait(2000)
            except Exception:  # noqa: BLE001
                pass
        super().closeEvent(event)

    def _make_summary_entry(self, content: str) -> Message:
        """摘要条目 —— 作为历史里最老的一条，让对话读起来连续。"""
        return Message(role="user", content=content)

    def _enforce_budget_after_compaction(self) -> None:
        """压缩完再兜一次字符预算（最新 8 条永远保留）。"""
        budget = self._CTX_CHAR_BUDGET
        if not budget or budget <= 0:
            return
        if history_chars(self.history) <= budget:
            return
        self._drop_oldest_history(
            protect_tail=self._compaction_settings.keep_recent)

    def wait_compaction(self, timeout_ms: int = 5000) -> bool:
        """等后台摘要跑完（测试用；生产代码不需要）。

        返回 True = 这一轮摘要已经落地（成功、或失败兜底都算）。
        """
        deadline = time.time() + max(0.0, float(timeout_ms) / 1000.0)
        app = _safe_qapp()
        while time.time() < deadline:
            worker = self._compaction_worker
            if worker is not None:
                worker.wait(50)
            if app is not None:
                app.processEvents()
            if not self._compaction_inflight:
                return True
        return not self._compaction_inflight

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
    # 上传 / 拖放 / 多模态生成按钮
    #
    # 设计:
    #   - 拖入图片 / 点 + 上传 → 都走 _handle_uploaded_files,
    #     图片存到 assets/uploads/ (项目根目录下), 用 file:// 路径
    #     渲染进 chat_view。这样下次重启桌宠也能看见。
    #   - 多模态生成 (图 / 视频 / 音效) → 当前留 stub,
    #     弹一行提示告诉主人"等模型接上后端", 但 UI 按钮先有位置。
    # ------------------------------------------------------------------

    _UPLOAD_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
    _UPLOAD_DIR_NAME = "uploads"
    _MAX_UPLOAD_BYTES = 12 * 1024 * 1024  # 12 MiB cap, 太大会让 chat_view 渲染卡

    # ------------------------------------------------------------------
    # skill 激活路径
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
        """返回要拼到 system_prompt 末尾的当前激活 skill 文本。无激活时返回空串。"""
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
        """per-message 主线: 给定 skill 名字, 返回对应的 skill prompt 段。"""
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

        拖入 / 选完文件后不再立刻进 chat, 而是堆在 input_edit 上方
        的 horizontal 缩略图条 (pending_strip)。
        主人按「发送」时, 这些 attachment 跟当前 input_edit
        文本一起作为一条 user message 渲染进 chat + 持久化 + kickoff LLM。
        单个文件缩略图右上角有 × 按钮, 主人能单独剔除某个 attachment。

        文件落地:
          - 复制到 assets/uploads/<ts>_<name>, 避免原始路径泄露
          - > 12 MiB 跳过 + 渲染一条 assistant 提示
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

        在 input_edit 前 insert /<kind> 前缀, focus 回去, 让主人在输入框里
        接着输, 按发送就走 /image /video /audio 命令路径。
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
        """/image /video /audio 命令的真正干活入口。

        走 self.registry.get(f"generate_{kind}") 调 tool, 拿 URL, 嵌图。
        registry 不可用 / tool 没注册时, 弹清晰提示。
        tool.fn 放后台 worker thread 调, 不阻塞 chat_window 主线程,
        后台用 QMetaObject.invokeMethod + QueuedConnection 跨线程切回主线程渲染。
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

        # 1) 先存 user message —— 关窗后不丢
        user_text = f"/{kind} {prompt}"
        user_msg = Message(role="user", content=user_text)
        self.history.append(user_msg)
        self._trim_history()
        try:
            self.chat_store.add("user", user_text)
        except Exception:
            log.exception("chat_store.add user failed (multimodal %s)", kind)
        self._render_message(user_msg)

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

        # 调 tool 前查桌宠决策(companion_policy 4 维查表)
        # 即使 state=None 也要写 trace_decision 让 multimodal 行为可观察
        # 默认 fallback:sat/mood/level/favor = "MID",决策降级 ALLOW
        risk_int = 2  # 默认 MEDIUM fallback
        try:
            from app.engine.tools._risk import risk_for_tool
            risk_int = risk_for_tool(tool_name, default=2)
        except Exception:
            pass
        decision = "ALLOW"
        sat_q = mood_q = level_q = favor_q = "MID"
        try:
            lc_agent = getattr(self, "_lc_agent", None)
            state = getattr(lc_agent, "_companion_state", None) if lc_agent else None
            if state is not None:
                from app.brain.companion_policy import lookup_decision
                decision = lookup_decision(state, tool_name)
                q = state.quantize()
                sat_q = q.get("satiety", "MID")
                mood_q = q.get("mood", "MID")
                level_q = q.get("level", "MID")
                favor_q = q.get("favor", "MID")
            # 写 trace_decision (无论 state 是否拿到, 都落一行 —— multimodal 行为可观察)
            from app.brain._companion_log import trace_decision
            thread_id = getattr(lc_agent, "thread_id", "default") if lc_agent else "default"
            trace_decision(
                thread_id=thread_id,
                tool=tool_name,
                risk=risk_int,
                decision=decision,
                sat=sat_q,
                mood=mood_q,
                level=level_q,
                favor=favor_q,
                source="multimodal_path",
            )
            # REJECT/REFUSE 直接弹 assistant 消息, 不调 tool
            if decision in ("REJECT", "REFUSE"):
                cn = {"REJECT": "永久拒绝", "REFUSE": "本轮拒绝"}.get(decision, decision)
                self._render_message(Message(
                    role="assistant",
                    content=f"{label} 被桌宠{cn} (心情/好感/状态不允许). prompt={prompt[:40]}"
                ))
                return
        except Exception as e:  # noqa: BLE001
            log.debug("multimodal 决策查表失败, 默认放行: %s", e)

        # 2) tool 放后台 worker thread, 不阻塞 chat_window 主线程
        self._append_system_msg(f"已发出{label}请求, 等模型回...")
        from PyQt5.QtCore import QMetaObject, Qt as _Qt, Q_ARG
        import threading

        def _worker():
            try:
                _result = tool.fn(prompt=prompt)
                _exc: Optional[Exception] = None
            except Exception as _e:
                _result = None
                _exc = _e
            # 3) 跨线程切回主线程（Qt 控件线程安全）
            QMetaObject.invokeMethod(
                self, "_on_multimodal_result", _Qt.QueuedConnection,
                Q_ARG(str, kind), Q_ARG(str, label),
                Q_ARG(str, prompt), Q_ARG(object, _result), Q_ARG(object, _exc),
                Q_ARG(int, risk_int), Q_ARG(str, decision),
            )

        threading.Thread(target=_worker, daemon=True).start()

    @pyqtSlot(str, str, str, object, object, int, str)
    def _on_multimodal_result(
        self, kind: str, label: str, prompt: str,
        result: object, exc: Optional[Exception],
        risk_int: int = 2, decision: str = "ALLOW",
    ) -> None:
        """worker 完成后跨线程切回主线程 → 渲染结果到 chat。"""
        if exc is not None:
            log.exception("multimodal %s tool failed", kind)
            self._render_message(Message(
                role="assistant", content=f"{label} 调用失败: {exc}"))
            try:
                lc_agent = getattr(self, "_lc_agent", None)
                if lc_agent is not None:
                    lc_agent._fire_outcome("failure", f"generate_{kind}", risk=risk_int)
            except Exception:  # noqa: BLE001
                pass
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
            try:
                lc_agent = getattr(self, "_lc_agent", None)
                if lc_agent is not None:
                    lc_agent._fire_outcome("failure", f"generate_{kind}", risk=risk_int)
            except Exception:  # noqa: BLE001
                pass
            return

        # 渲染成 img-open:KEY 锚点 (点击放大原图,不擦白 chat_view)
        # url 既可能是 file:/// 本地路径,也可能是 http(s):// 远程
        import hashlib as _hashlib
        key = _hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
        self._img_open_map[key] = url
        kind_cn = {"image": "图片", "video": "视频", "audio": "音频"}.get(kind, kind)
        announce = f"{label}已生成 (决策: {decision}, 风险等级: {risk_int})\n"
        content = f"{announce}![{kind_cn}: {prompt}]({url})"
        msg = Message(role="assistant", content=content, tools=[(f"generate_{kind}", "", "")])
        self.history.append(msg)
        self._trim_history()
        try:
            self.chat_store.add("assistant", content)
        except Exception:
            log.exception("chat_store.add failed (multimodal %s)", kind)
        self._render_message(msg)

        # 走 yaml 钩子 + trace_outcome,让 multimodal 路径跟主 LangChain run 一样被养成系统感知
        try:
            lc_agent = getattr(self, "_lc_agent", None)
            if lc_agent is not None:
                lc_agent._fire_outcome("success", f"generate_{kind}", risk=risk_int)
        except Exception:  # noqa: BLE001
            log.debug("multimodal _fire_outcome failed", exc_info=True)

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

        **模型加载绝不能跑在 GUI 主线程**：faster-whisper / ctranslate2 在主线程
        初始化 OpenMP 时会触发 *Windows fatal exception: access violation*，
        try/except 接不住，整个桌宠进程秒崩（pytest 直接 exit 139）。

        所以这里只做「排程」：延迟 1s（别跟聊天窗首屏抢 CPU）后把真正的加载
        丢给后台线程；GUI 线程只在收到完成信号时收尾（记一条日志）。

        - 幂等：一个窗口只预热一次（_asr_warmup_scheduled 标记）。
        - 窗口已析构时直接放弃，绝不碰已删除的 Qt 对象。
        - 失败时仅 debug log（首次说话仍可触发加载）。
        """
        if self.asr is None:
            return
        if getattr(self, "_asr_warmup_scheduled", False):
            return                      # 幂等：一个窗口只预热一次
        self._asr_warmup_scheduled = True
        try:
            from app.core.qt_compat import QTimer
            QTimer.singleShot(getattr(self, "_asr_warmup_delay_ms", 1000),
                              self._launch_asr_warmup)
        except Exception as e:  # noqa: BLE001
            log.debug("ASR 模型预热启动失败：%s", e)

    def _launch_asr_warmup(self) -> None:
        """（GUI 线程）拉起后台线程做 ASR 模型预热。

        真正的模型加载跑在 worker 线程里；GUI 线程只负责发信号 / 收信号，
        绝不自己碰 ctranslate2 / OpenMP 初始化。
        """
        if self.asr is None:
            return
        from app.core.qt_compat import QObject, Signal, is_qobject_alive
        if not is_qobject_alive(self):
            # 窗口已经被销毁（sip.isdeleted）→ 放弃预热，别碰已释放的 Qt 对象
            log.debug("ASR 模型预热：窗口已析构，放弃预热")
            return

        class _WarmupRelay(QObject):
            """完成信号的中继：在 GUI 线程创建，跨线程 emit 是安全的。"""

            done = Signal(bool, str)      # ok, error

        relay = _WarmupRelay()
        relay.done.connect(self._on_asr_warmup_done)
        self._asr_warmup_relay = relay    # 保留引用（worker 线程也持有一份）

        def _worker():
            model = None
            err = ""
            # 重试一次：首次加载容易受 OpenMP/MKL 初始化影响
            for attempt in (1, 2):
                try:
                    model = self.asr._get_model()    # noqa: SLF001
                    break
                except Exception as e:  # noqa: BLE001
                    err = str(e)
                    log.debug("ASR 模型预热第 %d 次失败：%s", attempt, e)
            if model is not None:
                log.info("ASR 模型预热完成（首次加载已缓存）")
            else:
                log.debug("ASR 模型预热 2 次都失败（首次说话时会再尝试）：%s", err)
            relay.done.emit(model is not None, err)

        threading.Thread(target=_worker, daemon=True, name="asr-warmup").start()

    def _on_asr_warmup_done(self, ok: bool, error: str) -> None:
        """（GUI 线程）预热结束：只记日志，不碰任何 UI。"""
        if not ok:
            log.debug("ASR 模型预热失败：%s", error)

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
        # 走进了本 callback 就是高风险工具,统一弹窗,不区分 DANGEROUS_TOOLS vs _RISKY_TOOLS

        # 解析参数（子线程做，没 Qt 依赖）
        try:
            import json as _json
            args_obj = _json.loads(args_json) if isinstance(args_json, str) else args_json
        except Exception:  # noqa: BLE001
            args_obj = {"raw": args_json}
        args_pretty = "\n".join(f"  {k}: {v}" for k, v in (args_obj or {}).items())

        self._confirm_in_progress = True
        # 用 threading.Event 跨线程等结果(不能用 QEventLoop.exec:子线程用会卡死)
        result_event = threading.Event()
        result: dict[str, bool] = {}    # 主线程把 ok 写进来,子线程读

        def _on_result(ok: bool):
            self._confirm_in_progress = False
            result["ok"] = ok
            result_event.set()    # 唤醒子线程 wait()

        # 跨线程投递用 QMetaObject.invokeMethod + Qt.QueuedConnection
        # (PyQt5 5.x 的 QTimer.singleShot 在 child thread 上静默失败)
        self._pending_confirm_dispatch = lambda: self._show_confirm_box(
            name, args_pretty, _on_result,
        )
        from PyQt5.QtCore import QMetaObject
        from app.core.qt_compat import Qt as _Qt
        QMetaObject.invokeMethod(
            self,
            "_invoke_confirm_dispatch_slot",
            _Qt.QueuedConnection,
        )
        # 子线程阻塞等主线程弹完弹窗（60s 超时防卡死）
        if not result_event.wait(timeout=_CONFIRM_TIMEOUT_S):
            # 超时：把还留在屏幕上的弹窗关掉,避免留一个僵尸模态框
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

        用自定义 _DangerConfirmDialog(QDialog)：QMessageBox 在 FramelessWindowHint
        父窗口下被 Qt 当 transient child 立刻 dismiss。
        parent=self(chat_window) 让弹窗在 chat 内显示,z-order 跟随 chat。
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

    def _dynamic_turn_context(self, query: str = "") -> str:
        """本轮易变上下文（当前时间 + 桌宠实时状态 + 召回的记忆）。

        为什么不能放 system prompt：system message 落在 MiniMax 的
        前缀缓存*最长公共前缀*的起点，里面每轮变一次（时间戳！）后面全部不再匹配 ——
        实测缓存命中率 98.9% → 1.5%。所以易变内容一律走 user 消息。

        长期记忆块（Stage 2.1）也走这条通道：它按**本轮主人说的话**召回，
        每轮都变 ⇒ 更不能进 system prompt（还会让 Stage 2.8 的 agent
        指纹每轮变 → 每轮重建 agent）。

        query: 本轮主人说的话（这里唯一知道原文的地方）。
        来源：context_provider 绑着的 BrainController.chat_context_dynamic()
        （只有它手里有桌宠实时状态）；拿不到就退化成「只有时间」。
        """
        from app.brain.llm_client import build_dynamic_context
        owner = getattr(self.context_provider, "__self__", None)
        for holder in (owner, getattr(owner, "brain", None)):
            fn = getattr(holder, "chat_context_dynamic", None)
            if callable(fn):
                try:
                    try:
                        ctx = fn(query)
                    except TypeError:
                        # 老签名（无参）→ 无参调用
                        ctx = fn()
                except Exception as e:  # noqa: BLE001
                    log.warning("chat_context_dynamic 失败：%s", e)
                    ctx = ""
                if ctx:
                    return ctx
        # 兜底：只有时间（state_provider 拿得到时带上实时状态）
        state = None
        if self._state_provider is not None:
            try:
                state = self._state_provider()
            except Exception:  # noqa: BLE001
                state = None
        return build_dynamic_context(state=state, query=query)

    def _inject_dynamic_context(self, msgs: list) -> list:
        """把本轮易变上下文作为**独立的 user 消息**插在主人本轮发言前面。

        用独立消息而不是拼进主人那句话：主人原话保持字节不变，意图判定
        （detect_action_intent）读到的仍是主人的原话。

        本轮主人说的话（最后一条 user 消息）作为 query 传下去 ——
        长期记忆块按它召回（Stage 2.1）。
        """
        from app.brain.llm_client import inject_dynamic_context
        if not msgs:
            return msgs
        query = ""
        for m in reversed(msgs):
            role = (m.get("role") if isinstance(m, dict)
                    else getattr(m, "role", ""))
            if role == "user":
                content = (m.get("content") if isinstance(m, dict)
                           else getattr(m, "content", ""))
                query = content if isinstance(content, str) else ""
                break
        return inject_dynamic_context(
            msgs, context=self._dynamic_turn_context(query))

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
        # 本轮易变上下文（当前时间 / 桌宠实时状态）作为**独立的 user 消息**
        # 插在主人本轮发言前面 —— 不能进 system prompt（原因见
        # _dynamic_turn_context 的注释）。两条后端共用这一份 msgs。
        msgs = self._inject_dynamic_context(msgs)

        # 稳定的 system 上下文：SKILL / 长期记忆 / 工具列表（都不随轮次变）
        system_prompt = self.char_cfg.persona
        if self.context_provider is not None:
            try:
                extra = self.context_provider()
                if extra:
                    system_prompt = system_prompt + "\n\n" + extra
            except Exception as e:  # noqa: BLE001
                log.warning("context_provider 失败：%s", e)
        # 主人激活的 skill 内容拼到 system_prompt 末尾(per-message 语义,当条 message 生效)
        # 主线:读 history[-1].skill;兜底:老的 self._active_skill 字段(老测试兼容)
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
        # standard 后端走 LangChainAgent（自带 ChatOpenAI），根本用不到这个
        # LLMClient —— 每轮白建一个 httpx 连接池。轻量后端 / 无工具才需要。
        _need_llm_client = not (
            self.registry is not None and self.registry.names()
            and self.backend == "standard"
        )
        client = None
        if _need_llm_client:
            # v31: token 台账按 session 分桶。轻量路径的 LLMClient 不知道
            # 会话 id（标准路径用的是同一个 self._session_id, 见下面
            # LangChainAgent 的构造），这里把窗口知道的 id 写进台账的当前
            # 会话 —— 两条后端路径因此落进同一个 session 桶。
            try:
                from app.brain.token_ledger import (
                    set_session as _set_ledger_session,
                )
                _set_ledger_session(self._session_id or "")
            except Exception:  # noqa: BLE001
                pass
            client = LLMClient(
                self.llm_cfg, system_prompt,
                request_timeout_s=self._chat_timeout_s,
            )

        # 占位气泡：插入一个占位 div 并记下 cursor 作为 streaming patch anchor
        self._current_bot_msg = Message(role="assistant", content="",
                                         speaker=self.char_cfg.name)
        self._streaming_raw = ""   # 流式原始累积（最终规范前），供逐帧清洗显示
        # 流式清洗的节流状态：sanitize_text 是 O(累积长度)，每 token 都整段重算
        # 会让总开销变成 O(n²)（实测 2088 字回复累计 199 ms GUI 占用，
        # 4176 字 785 ms）。改成按「累积增长量」节流：同一个函数、同一个输入，
        # 只是更新点变稀（默认每 _SAN_STEP 字一次，约 10 次/秒，肉眼无感）。
        self._san_len = 0        # 上次清洗时的累积长度
        self._streaming_san = ""  # 上次清洗的结果
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
        # 工具步骤折叠状态:按消息分别记(dict),新一轮 chat 起始不 reset

        self._generating = True
        self.send_btn.setEnabled(False)
        self.stop_btn.setVisible(True)
        self.thinking_started.emit()

        if self.registry is not None and self.registry.names():
            # 根据 backend 选择 Agent：手写轻量级 或 LangChain 标准实现
            if self.backend == "standard":
                # 延迟 import：LangChain 那一坨依赖（langchain.agents /
                # langchain_openai）很重，模块级 import 会拖慢启动
                # （见 app/brain/langchain_agent.py 的延迟导入说明）。
                from app.brain.langchain_agent import LangChainAgent
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
                    level_getter=self.level_provider,
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
                # 新 LangChainAgent 立刻注当前 PetState
                if self._state_provider is not None:
                    try:
                        _state_now = self._state_provider()
                        if _state_now is not None:
                            lc_agent.set_companion_state(_state_now)
                    except Exception:  # noqa: BLE001
                        log.debug("state_provider 拉取失败, agent 走无 state 路径",
                                  exc_info=True)
                else:
                    log.warning(
                        "_kickoff_llm: self._state_provider is None, "
                        "PostLLMStateGuard / RiskyToolConfirm 走无 state 路径"
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
                # 轻量 backend:直接用 AgentLoop ReAct 循环
                self._worker = _AgentWorker(
                    AgentLoop(client, self.registry,
                              confirm_tool=self._confirm_dangerous_tool,
                              level_getter=self.level_provider),
                    [{"role": m.role, "content": m.content} for m in msgs])
                self._worker.chunk.connect(self._on_chunk)
                self._worker.tool_used.connect(self._on_tool)
                self._worker.meta.connect(self._on_meta)
        else:
            self._worker = _StreamWorker(client, msgs)
            self._worker.chunk.connect(self._on_chunk)
            # v31: 纯聊天路径的 usage 事件也走 _on_meta —— 状态栏 token 数
            # 和 Trace 里的 usage 记录, 以前这条路径一个都拿不到。
            self._worker.meta.connect(self._on_meta)
        # v31: token usage 走 _on_meta 路径 (AgentLoop 把 LLM API usage 当 meta
        # 事件 emit, 抓进 self._accum_tokens) — done 仍然只传文本, 保持兼容
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
        """chat_view 上 <a href> 被点击,处理内部命令 (toggle_tools) + 外部链接
        + 图片点击放大 (img-open)。

        cmd:toggle_tools 用 _rebuild_chat_view_from_history 全量重建,
        不再调 _refresh_streaming_message(后者某些状态会擦白 chat 区域)。
        按消息分别切换折叠状态,URL 用 fragment (#N) 编码 msg id。
        img-open:KEY 走 _show_image_popup 弹原图。
        """
        try:
            scheme = url.scheme() if hasattr(url, "scheme") else ""
        except Exception:  # noqa: BLE001
            scheme = ""
        # img-open:KEY 在前,不要被 cmd: 分支吞掉
        if scheme == "img-open":
            try:
                key = url.path().lstrip("/") if hasattr(url, "path") else ""
            except Exception:  # noqa: BLE001
                key = ""
            if not key:
                key = str(url).split(":", 1)[-1].split("#", 1)[0].split("?", 1)[0]
            real = self._img_open_map.get(key)
            if real:
                self._show_image_popup(real)
            return
        if scheme == "cmd":
            try:
                cmd = url.path().lstrip("/") if hasattr(url, "path") else str(url).split(":", 1)[-1]
            except Exception:  # noqa: BLE001
                cmd = ""
            if cmd == "toggle_tools":
                # fragment 是 msg_id 整数,默认 0
                try:
                    msg_id = int(url.fragment() or 0) if hasattr(url, "fragment") else 0
                except (TypeError, ValueError):
                    msg_id = 0
                cur = self._tools_collapsed_by_msg.get(
                    msg_id, self._tools_collapsed_default)
                self._tools_collapsed_by_msg[msg_id] = not cur
                # 全量重建,不动 anchor/cursor
                self._rebuild_chat_view_from_history()
            return
        # 外部链接:手动打开(setOpenExternalLinks(False) 不会自动开)
        if scheme in ("http", "https", "mailto"):
            try:
                from PyQt5.QtGui import QDesktopServices
                QDesktopServices.openUrl(url)
            except Exception:  # noqa: BLE001
                log.warning("打开外部链接失败: %s", url)

    def _show_image_popup(self, url: str) -> None:
        """点击 chat 缩略图弹原图 QDialog, Esc / 点 × 关闭。

        支持三种 src:
          - file:/// 绝对路径 (本机落盘的 multimodal 文件)
          - data:image/...;base64,... (chat 历史嵌图)
          - http(s):// (网络图, 不下载, 提示"在线图无法放大")

        对应不上 / 加载失败 / 空 QPixmap → 静默 return, 不弹错误框打扰主人。

        v20 改为非模态 (show + 自管关闭)：旧版 exec_() 阻塞主事件循环,
        关 × 时触发 chat_view 文档资源释放, 偶现整个 chat 区域擦白。
        """
        try:
            from PyQt5.QtGui import QPixmap
            from PyQt5.QtWidgets import (
                QDialog, QVBoxLayout, QLabel, QScrollArea, QMessageBox,
            )
        except Exception:  # noqa: BLE001
            return
        if url.startswith(("http://", "https://")):
            try:
                QMessageBox.information(
                    self, "图片预览",
                    f"网络图片无法直接放大:\n{url}\n\n"
                    "主人可在浏览器里打开, 或让桌宠先把图下载到本地。",
                )
            except Exception:
                pass
            return
        pix = QPixmap()
        try:
            if url.startswith("data:"):
                import base64, re as _re
                m = _re.match(r"data:image/[^;]+;base64,([^,]*)", url)
                if m:
                    pix.loadFromData(base64.b64decode(m.group(1)))
            elif url.startswith("file:///"):
                local = url[len("file:///"):]
                if (local.startswith("/")
                        and len(local) > 2
                        and local[2] == ":"):
                    local = local.lstrip("/")
                from urllib.parse import unquote
                local = unquote(local)
                pix = QPixmap(local)
        except Exception as e:  # noqa: BLE001
            log.warning("图片放大加载失败: %s (%s)", url, e)
            return
        if pix.isNull():
            return
        # top-level + 非模态:跟 chat_window 互不阻塞,关闭时由 _on_image_popup_closed
        # 强制 chat_view 重绘,避免文档资源释放导致 chat 区域擦白。
        dlg = QDialog(None)
        dlg.setWindowTitle("图片预览")
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        try:
            dlg.setWindowFlags(
                dlg.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint
            )
        except Exception:
            pass
        layout = QVBoxLayout(dlg)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(dlg)
        scroll.setWidgetResizable(True)
        label = QLabel()
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setPixmap(pix)
        scroll.setWidget(label)
        layout.addWidget(scroll)
        try:
            screen = (
                QApplication.primaryScreen().availableGeometry()
                if QApplication.primaryScreen() else None
            )
            if screen:
                sw, sh = screen.width(), screen.height()
                target_w = min(pix.width(), int(sw * 0.85))
                target_h = min(pix.height(), int(sh * 0.85))
            else:
                target_w, target_h = pix.width(), pix.height()
            target_w = max(320, target_w)
            target_h = max(240, target_h)
            dlg.resize(target_w, target_h)
        except Exception:
            dlg.resize(640, 480)
        dlg.destroyed.connect(self._on_image_popup_closed)
        dlg.show()

    def _on_image_popup_closed(self, *_args) -> None:
        """图片预览关闭后强制 chat_view 重绘,避免文档资源释放导致空白。

        含 base64 图片的文档：仍然整份重建（setHtml(toHtml())）——这是
        「资源被释放后整块擦白」的兜底，不能省。
        纯文字文档：直接重绘即可。原来无条件做整份往返，文档里带图片时
        是几 MB 的序列化 + 重新解析，全在 GUI 线程上。
        """
        try:
            if hasattr(self, "chat_view") and self.chat_view is not None:
                if getattr(self, "_doc_has_inline_images", False):
                    self.chat_view.document().setHtml(self.chat_view.toHtml())
                self.chat_view.viewport().update()
        except Exception:  # noqa: BLE001
            pass

    def _on_tool(self, name: str, args: str, result: str, status: str = "") -> None:
        """一次工具调用完成：在当前气泡里追加一行工具记录。

        status 是工具信封里的**权威成败**（ok / error / denied / cancel，Stage 3.5）：
        界面拿它渲染成败标记，不再从结果文本猜成败（文本没前缀的失败曾被显示成「完成」）。
        老后端不带 status → 空串 → 渲染端回退到文本判定。
        """
        if self._current_bot_msg is None:
            return
        # run_script：把 [OK] path\n--- stdout ---\n…\n--- stderr ---\n… 的结构化结果
        # 拆成命令块（meta）和简化结果，渲染端识别后展示终端风格块
        if name == "run_script" and "--- stdout ---" in result:
            self._parse_run_script_into_blocks(name, args, result, status)
        else:
            self._current_bot_msg.tools.append(
                (name, args or "", result or "", status or ""))
            # multimodal 工具结果自动追加到 msg.content,渲染时显示产物
            self._maybe_inject_multimodal_to_content(name, result)
        # Trace
        if self.trace is not None and self._trace_run_id:
            try:
                import json as _json
                # LangChainAgent._astream_with_retry yield 工具事件时 args 可能是空字符串,
                # _json.loads("") 抛 JSONDecodeError;空字符串视为空 dict
                if isinstance(args, str):
                    _args = _json.loads(args) if args.strip() else {}
                else:
                    _args = args or {}
                self.trace.record(self._trace_run_id, "tool", {
                    "name": name,
                    "args": _args,
                    "result": (result or "").replace("\n", " ")[:80],
                    "status": status or "",
                })
            except Exception:  # noqa: BLE001
                pass
        self._refresh_streaming_message()

    def _maybe_inject_multimodal_to_content(self, name: str, result: str) -> None:
        """v24: 若工具是 generate_image/video/audio 且结果是 URL/路径,
        自动拼 markdown 嵌入到当前 bot 消息 content 末尾, 让产物显示在
        聊天窗口 (跟 /image 命令走同一条路径)。

        跳过条件:
          - result 不是 http(s):// 或 file:/// 开头 (非产物, 比如错误信息)
          - content 里已经有同样的 URL (避免重复注入, 比如 chunk 同 tool 来两次)
          - 不在 _kickoff_llm 流式期间 (msg 可能是历史消息, 不该追加)

        加在 content 末尾而不是开头: 跟主人读 LLM 回复习惯一致 (工具卡片在最
        前面,产物紧跟模型文字后跟 自然章节顺序)。

        注: 流式期间 _on_chunk / _on_done 都会用 sanitized 文本覆盖 content
        (line 2974 / 3150), 这里的写入会被冲掉。_on_done 里再从 msg.tools
        扫一遍重新注入 (见 _reinject_multimodal_from_tools)。
        """
        if self._current_bot_msg is None or not result:
            return
        kind_map = {
            "generate_image": "图片",
            "generate_video": "视频",
            "generate_audio": "音频",
        }
        kind = kind_map.get(name)
        if not kind:
            return
        url = result.strip()
        # 只处理路径/URL 结果 (跳过 "调用失败: ..." 这种错误文本)
        if not (url.startswith("file:///")
                or url.startswith("http://")
                or url.startswith("https://")
                or url.startswith("data:")):
            return
        # 已经在 content 里就不重复
        if url in (self._current_bot_msg.content or ""):
            return
        # 标题: 从 args JSON 里取 prompt, 失败就空
        prompt = ""
        try:
            import json as _json2
            _a = _json2.loads(args) if (isinstance(args, str) and args.strip()) \
                 else (args or {})
            if isinstance(_a, dict):
                prompt = str(_a.get("prompt") or "")[:60]
        except Exception:
            pass
        alt = f"{kind}: {prompt}" if prompt else kind
        # 用 markdown 图片/视频/音频语法; video/audio 也走 ![](URL) 通用渲染
        # 至少会显示链接或占位 (跟 v14 _img_open_map 配合点开看原图/原视频)
        inline = f"\n\n![{_html_escape(alt)}]({url})"
        self._current_bot_msg.content = (
            (self._current_bot_msg.content or "") + inline
        )

    def _reinject_multimodal_from_tools(self, msg) -> None:
        """v24: 流式期间 _on_chunk / _on_done 都会 content = sanitized 覆盖,
        把 _on_tool 里注入的图片 markdown 冲掉。_on_done 在覆盖完正文后,
        从 msg.tools 倒着扫一遍 multimodal, 按调用顺序重新拼到 content
        末尾。这样无论 _on_chunk / _on_done 怎么覆盖都不会丢图片。

        入参 msg 显式传, 这样即使 self._current_bot_msg 已被清空也能复用
        (用于回放历史消息: 历史消息没 _on_tool 注入过但 msg.tools 可能
         也有 multimodal result, 同样应该展示 — 现在 _load_history 渲染
         直接通过 _msg_html → _render_markdown → _inline_local_images_to_data_url
         也能加载, 这条路径是保险, 不要指望它救场)。
        """
        if msg is None:
            return
        tools = getattr(msg, "tools", None) or []
        if not tools:
            return
        # 按工具调用顺序追加 (跟 _tool_steps_html 的展示顺序一致)
        addition = []
        for t in tools:
            try:
                name, args, result, _tool_status_raw = _unpack_tool(t)
            except Exception:
                name, args, result = ("", "", "")
            kind_map = {
                "generate_image": "图片",
                "generate_video": "视频",
                "generate_audio": "音频",
            }
            kind = kind_map.get(name)
            if not kind or not result:
                continue
            url = result.strip()
            if not (url.startswith("file:///")
                    or url.startswith("http://")
                    or url.startswith("https://")
                    or url.startswith("data:")):
                continue
            # 已经存在就不重复 (比如 _on_tool 已经注入了的)
            if url in (msg.content or ""):
                continue
            prompt = ""
            try:
                import json as _json3
                _a = _json3.loads(args) if (isinstance(args, str) and args.strip()) \
                     else (args or {})
                if isinstance(_a, dict):
                    prompt = str(_a.get("prompt") or "")[:60]
            except Exception:
                pass
            alt = f"{kind}: {prompt}" if prompt else kind
            addition.append(f"\n\n![{_html_escape(alt)}]({url})")
        if addition:
            msg.content = (msg.content or "") + "".join(addition)

    def _parse_run_script_into_blocks(self, name: str, args: str, result: str,
                                       status: str = "") -> None:
        """run_script 结果拆成：命令块（execs）+ 简化结果（tools）。

        status 一起带进简化结果（Stage 3.5）：脚本退出码非 0 时卡片要显示「失败」，
        不能因为简化文本没有「错误：」前缀就显示成「完成」。
        """
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
        self._current_bot_msg.tools.append(
            (name, args or "", simple, status or ""))

    def _on_meta(self, kind: str, payload: dict) -> None:
        """内部事件（plan / reflection / meta / thought / exec）。

        Args:
            kind: "plan" | "reflection" | "meta" | "thought" | "exec"
            payload: 事件载荷
        """
        # v31: AgentLoop 把 LLM API 返回的 usage 字典当作 meta 事件透传过来
        # (event="usage", 含 prompt_tokens / completion_tokens / total_tokens / got_real)
        # 累加到 self._accum_tokens, 状态栏用真实值显示 (替代旧 "字符/4" 估算)
        if isinstance(payload, dict) and payload.get("event") == "usage":
            try:
                p = int(payload.get("prompt_tokens") or 0)
                c = int(payload.get("completion_tokens") or 0)
                t = int(payload.get("total_tokens") or 0)
                if t > 0:
                    self._accum_prompt += p
                    self._accum_completion += c
                    self._accum_tokens += t
            except Exception:
                pass
            # v31: usage 也要入 trace —— 原来这里直接 return，usage 不入 trace。
            # 载荷里除了三个 token 数，还有缓存命中 / 未命中 / 写入三个桶；
            # 「缓存经济性」只有落进 trace 才查得到。
            if self.trace is not None and self._trace_run_id:
                try:
                    self.trace.record(self._trace_run_id, "usage", dict(payload))
                except Exception:  # noqa: BLE001
                    pass
            return  # usage 不再往下走 UI 事件映射（状态栏累加在上面已经做完）

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

    def _refresh_status_bar(self) -> None:
        """1 Hz 刷新: 桌宠进程的 CPU/内存占用。

        v31 之前: 走 /proc/{pid}/stat, Windows 上直接抛 → 一直 CPU 0%。
        现在: 优先 psutil (跨平台), 失败再 fallback 到 /proc (Linux)。
        token 数由 _on_done 收 finish event 立刻更新 (用 LLM API 真实 usage,
        got_real=True; 某些老 API 不返 usage 时再 fallback 字符估算)。
        """
        # CPU: psutil 跨平台 (Windows / Linux / macOS 都行)
        cpu_pct = 0.0
        if self._psutil_proc is not None:
            try:
                cpu_pct = self._psutil_proc.cpu_percent(interval=None)
            except Exception:
                # psutil Process 句柄可能过期 (子线程等), 重建一次
                try:
                    import psutil as _ps
                    self._psutil_proc = _ps.Process()
                    cpu_pct = self._psutil_proc.cpu_percent(interval=None)
                except Exception:
                    cpu_pct = 0.0
        # 内存: 同 psutil
        mem = 0.0
        if self._psutil_proc is not None:
            try:
                mem = self._psutil_proc.memory_info().rss / 1024 / 1024
            except Exception:
                pass
        try:
            self._status_label_cpu.setText(f"CPU: {cpu_pct:.0f}%")
            self._status_label_mem.setText(f"mem: {mem:.0f} MB")
            # token 累计 (本轮会立刻 + 一次, 这里只负责显示历史累计)
            self._status_label_token.setText(
                f"tokens: {self._accum_tokens:,}"
                f" ({self._accum_prompt}↓+{self._accum_completion}↑)"
                if self._accum_tokens else "tokens: 0"
            )
        except Exception:
            pass

    def _on_chunk(self, tok: str) -> None:
        if self._current_bot_msg is None:
            return
        from app.brain.llm_client import sanitize_text
        # 原始累积（worker 来的 token 已做流式安全清洗）；最终文本以 _on_done(full) 为准
        self._streaming_raw = (self._streaming_raw or "") + tok
        # 流式渲染统一走最终规范：聊天窗也不闪现 CoT / 规则复读 / 英文思考。
        # 占位气泡的 content 直接存清洗后的显示文本（CoT 阶段保持空 → 三点动画）。
        #
        # 清洗整段累积文本是 O(累积长度)；每个 token 都做一遍 → 总开销 O(n²)。
        # 改成按累积增长量节流：值跟以前完全一样（同一个函数、同一个输入），
        # 只是更新点变稀。第一块 token 必算（气泡要立刻有内容）。
        if (len(self._streaming_raw) - self._san_len) >= self._SAN_STEP or self._san_len == 0:
            self._san_len = len(self._streaming_raw)
            sanitized = sanitize_text(self._streaming_raw).strip()
            self._streaming_san = sanitized
        else:
            sanitized = self._streaming_san
        # 累积 content 但**不**实时刷 chat_view ——等 TTS prepare 完成才显示
        # （实现「文字与语音同步」：声音准备好后文字才一起出现）
        self._current_bot_msg.content = sanitized
        # chat_view 保持「准备语音…」占位（_refresh_streaming_message 故意不调）
        # Trace：累计的原始文本。原先每个 token 写一行（一次 sqlite commit + fsync，
        # 实测 6.5 ms/token → 40 tok/s 时每秒白占 260 ms GUI 线程）。
        # 现在攒批 + 定时 flush：Dashboard 只取最后一条 accumulated
        # （app/web/dashboard.py:409 遍历 text 事件取最后一个 accumulated），
        # 所以中途合并不影响显示效果。
        if self.trace is not None and self._trace_run_id and tok:
            self._trace_text_buf.append(tok)
            if not self._trace_flush_timer.isActive():
                self._trace_flush_timer.start()
        # 同步桌宠头顶气泡（与聊天窗同一份清洗后文本，口型同步也用它）
        if sanitized:
            self.streaming_chunk.emit(sanitized)
            # **逐句 prepare**：每收完一句话立即启动后台 QThread 准备 TTS
            self._tts_drain_sentences(sanitized)

    def _flush_trace_text(self) -> None:
        """把攒下的 token 批量写成一条 trace 事件（一次 commit 顶 N 次）。"""
        self._trace_flush_timer.stop()
        if not self._trace_text_buf:
            return
        delta = "".join(self._trace_text_buf)
        self._trace_text_buf = []
        if self.trace is None or not self._trace_run_id:
            return
        try:
            self.trace.record(self._trace_run_id, "text", {
                "delta": delta,
                "accumulated": (self._streaming_raw or "")[:500],
            })
        except Exception:  # noqa: BLE001
            log.debug("trace flush text 失败", exc_info=True)

    # 中文/英文/数字标点都算句末边界。GPT-SoVITS 一次合成一句短句的体感最自然
    _TTS_SENT_END = re.compile(r"[。！？!?\n;；]+")

    def _voice_auto_read_enabled(self) -> bool:
        """自动朗读开关（默认**关**）。

        语音改成「桌宠**自己决定**要不要发」（走 MCP 工具 mcp_voice_speak / mcp_voice_sing），
        所以这里默认**不**再逐句朗读每条回复。开关在 companion_config.yaml 的
        voice.auto_read（默认 false）——改成 true 就恢复原来的逐句朗读。

        读的是 app/voice/voice.py 的 load_voice_settings：按 yaml 的 mtime 缓存，
        主人改完 yaml 自动生效，不用重启桌宠。读不到就按「关」处理（宁可安静，
        也不要对着每句话聒噪）。
        """
        try:
            from app.voice.voice import load_voice_settings
            return bool((load_voice_settings() or {}).get("auto_read", False))
        except Exception:  # noqa: BLE001
            return False

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
        # 默认不朗读：语音改成桌宠自己决定要不要发（voice.auto_read，默认 false）
        if not self._voice_auto_read_enabled():
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
        给 _on_sentence_ready 入队。"""
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
        self._tts_prepare_inflight += 1
        w.start()

    def _on_sentence_ready(self, seq: int, sentence: str) -> None:
        """单句 TTS prepare 完成:把该句入队等按序播放。"""
        self._committed_text = (self._committed_text or "") + sentence
        self._pending_speak[seq] = sentence
        self._drain_speak_queue()
        self._tts_prepare_inflight = max(0, self._tts_prepare_inflight - 1)
        # 最后一个 prepare 回来了 → 收尾（若之前被延后）
        if self._finalize_pending and self._tts_prepare_inflight <= 0:
            self._finalize_reply()

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
        if (tail and self.char_cfg.tts_enabled and self.tts is not None
                and self._voice_auto_read_enabled()):
            self._launch_sentence_prepare(tail)
        # 重置（避免下次会话污染）
        self._tts_tail = ""
        self._tts_sent_tail = 0

    def _wait_all_sentence_workers(self, timeout_s: float = 120.0) -> None:
        """等所有 sentence prepare 完成 —— 但**不阻塞主线程**。

        原来的实现在这里用 processEvents()+sleep 自旋,最长卡 120 秒:
        桌宠动画 / 气泡 / 托盘 / 输入框全部冻住（而且 processEvents 会重入
        事件循环,让 _on_chunk 插进 _on_done 执行途中）。

        现在改成：
          - 没有 prepare 在跑 → 立刻收尾（和原来一样）
          - 还有 prepare 在跑 → 记下待收尾状态,等最后一个 prepare 回来
            （_on_sentence_ready）时再收尾,「文字等语音」的观感不变
          - 同时挂 watchdog：prepare 永远不回来时也能收尾（对应原来的超时放弃）
        """
        if self._tts_prepare_inflight <= 0:
            self._finalize_reply()
            return
        self._finalize_pending = True
        self._finalize_watchdog.start(int(max(1.0, timeout_s) * 1000))

    def _on_finalize_watchdog(self) -> None:
        """watchdog：prepare 迟迟不回来 → 放弃等待,照常收尾。"""
        if not self._finalize_pending:
            return
        log.warning("TTS prepare 超时（%d 个 worker 未完成），放弃等待",
                    self._tts_prepare_inflight)
        self._finalize_reply()

    def _finalize_reply(self) -> None:
        """回复收尾：正式写入 history / chat_store / chat_view / trace。

        原本这段紧跟在「等 TTS prepare 完成」之后同步执行；现在可能被延后到
        最后一个 prepare 完成时（_on_sentence_ready）才调用，对用户表现一致。
        """
        parsed = self._finalize_parsed
        if parsed is None:
            return
        self._finalize_pending = False
        self._finalize_parsed = None
        self._finalize_watchdog.stop()
        if self._current_bot_msg is None:
            return
        try:
            self._do_finalize_reply(parsed)
        except Exception:  # noqa: BLE001
            log.exception("回复收尾失败")

    def _do_finalize_reply(self, parsed) -> None:
        """_finalize_reply 的实际收尾逻辑（原来内联在 _on_done 里）。"""
        self.history.append(self._current_bot_msg)
        self._trim_history()
        tools_list = [list(t) for t in self._current_bot_msg.tools] if self._current_bot_msg.tools else []
        # 持久化用 msg.content(已 re-inject),不是 parsed.text;否则历史看不到图
        self.chat_store.add(
            "assistant", self._current_bot_msg.content or "",
            emotion=parsed.emotion.value if parsed.emotion else "",
            tools=tools_list,
        )
        self._refresh_streaming_message(finished=True,
                                         emotion=parsed.emotion.value if parsed.emotion else "")

        self._detect_hallucination(parsed.text, self._current_bot_msg.tools)

        # Trace：先把攒着的 token 冲掉，再写 finish（flush 依赖 _trace_run_id）
        self._flush_trace_text()
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
        # LLM 通过 generate_image/video/audio 工具生成产物时,从 msg.tools 倒着扫
        # 把 multimodal URL 重新拼到 content 末尾(扛住上面 content=parsed.text 的覆盖)
        self._reinject_multimodal_from_tools(self._current_bot_msg)
        self._flush_tts_tail_to_prepare()  # 启动尾部 prepare
        # 收尾（写 history / 渲染 / 通知桌宠）。若还有 prepare 没回来，
        # 这里不阻塞主线程，改由 _finalize_reply 延后执行。
        self._finalize_parsed = parsed
        self._wait_all_sentence_workers(timeout_s=120.0)

    def _on_failed(self, err: str) -> None:
        # 作废可能还挂着的延后收尾（_on_done 已经排上、但还没等到 prepare 回来），
        # 避免失败分支和延后收尾各渲染一次
        self._finalize_pending = False
        self._finalize_parsed = None
        self._finalize_watchdog.stop()
        # LangChain 标准后端：释放 SqliteSaver 连接
        if self.backend == "standard" and getattr(self, "_lc_agent", None) is not None:
            try:
                self._lc_agent.close()
            except Exception:  # noqa: BLE001
                log.exception("close lc agent on fail")
            self._lc_agent = None
        self._current_bot_msg.content = f"[错误] {err}"
        self._current_bot_msg.emotion = Emotion.SAD
        # 即使失败也尝试把 multimodal 产物补回来
        self._reinject_multimodal_from_tools(self._current_bot_msg)
        self._refresh_streaming_message(finished=True, emotion="sad")
        # Trace：失败（先把攒着的 token 冲掉）
        self._flush_trace_text()
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

    # -------------------- 窗口位置持久化 --------------------

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

        拖动窗口到屏幕顶端触发系统最大化时, 按钮文字也要跟着切到「还原」。
        """
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

    @staticmethod
    def _tools_failed(tools: list) -> bool:
        """这一轮的工具有没有没成的（按信封状态判，不看文本）。

        用来判断界面补的那句「已经帮主人搞定啦~」还成不成立。
        """
        for t in (tools or []):
            try:
                _name, _args, result, status = _unpack_tool(t)
            except Exception:  # noqa: BLE001
                continue
            if _tool_status(result, status) != "ok":
                return True
        return False

    def _tool_note_html(self, name: str, status: str, result: str) -> str:
        """工具步骤下面的附注：失败原因 / 语音产物。

        - 不是成功 → 工具自己给的**原因**（第一行，截断）；
          原来卡片只写一个「[失败]」，主人看不到失败在哪一步。
        - 语音工具成功 → 时长 / 体积 / 音频落盘路径；
          语音是听得见的动作，界面上必须留痕。

        两样都来自工具自己的结果文本，不是界面编的；解析不出来就不显示。
        """
        if status != "ok":
            reason = _tool_fail_reason(result)
            if not reason:
                return ""
            return '<div class="tool-reason">%s</div>' % _html_escape(reason)
        note = _voice_result_note(name, result)
        if not note:
            return ""
        return '<div class="tool-note">%s</div>' % _html_escape(note)

    @staticmethod
    def _tools_tail_summary(states: list) -> str:
        """折叠态预览行尾的汇总：后面几步有没有没成的。

        原来折叠态只显示第一条工具的状态 —— 第一步成功、第五步失败时，
        主人只看到一片成功标记，失败被藏起来了。这里如实说出来。
        """
        rest = list(states[1:]) if len(states) > 1 else []
        words = []
        for st, label in (("fail", "失败"), ("cancel", "取消"),
                          ("rejected", "桌宠拒绝")):
            c = rest.count(st)
            if c:
                words.append("%d 步%s" % (c, label))
        if not words:
            return ""
        return '<span class="tools-tail">（另有 %s）</span>' % "、".join(words)

    def _tool_steps_html(self, tools: list, msg_id: int = 0) -> str:
        """把工具调用渲染成步骤卡片（编号 + 中文名 + 动作目标 + 成败状态）。

        工具先于最终回答执行，故卡片置于气泡正文上方；工具结果不进 TTS。
        head 文本里只显示「已执行操作 · N ▾ / ▴」,不再出现"点击展开"字样。
        按消息分别记折叠状态:URL 用 `cmd:toggle_tools#{msg_id}` 编码。
        """
        if not tools:
            return ""
        rows = []
        states = []
        for i, t in enumerate(tools):
            name, args, result, tool_status = _unpack_tool(t)
            cn, target = _tool_label(name, args)
            # 成败用**信封状态**（有的话），不再从结果文本猜（Stage 3.5）
            status = _tool_status(result, tool_status)
            states.append(status)
            # 状态类名（.ok / .failed / .cancelled / .rejected）→ 颜色走 CSS，
            # 两套主题同步；原来失败行和成功行长得一模一样（CSS 里的
            # .tool-row.failed 一直没被渲染端用上）。
            state = _tool_step_class(status)
            num = _CIRCLED_NUMS[i] if i < len(_CIRCLED_NUMS) else str(i + 1)
            if status == "cancel":
                mark = '<span class="tool-mark tool-cancelled">⊘ 取消</span>'
            elif status == "fail":
                mark = '<span class="tool-mark tool-failed">[失败]</span>'
            elif status == "rejected":
                # 桌宠拒绝（companion REJECT/REFUSE）——不是「完成」
                mark = '<span class="tool-mark tool-rejected">[桌宠拒绝]</span>'
            else:
                mark = '<span class="tool-mark tool-ok">[完成]</span>'
            target_html = (
                f'<span class="tool-target">· <b>{_html_escape(target)}</b></span>'
                if target else ""
            )
            rows.append(
                f'<div class="tool-row {state}">'
                f'<span class="tool-num">{num}</span>'
                f'<span class="tool-name">{_html_escape(cn)}</span>'
                f'{target_html}'
                f'<span class="tool-mark-cell">{mark}</span>'
                '</div>'
                + self._tool_note_html(name, status, result)
            )
        n = len(tools)
        collapsed = self._tools_collapsed_by_msg.get(
            msg_id, self._tools_collapsed_default)
        # URL 编码 msg_id 用 fragment (#N), 走 cmd:toggle_tools#123
        url = f"cmd:toggle_tools#{msg_id}"
        if collapsed:
            # 折叠态:head + 第一条工具预览;head 文字「已执行操作 · N ▾」
            # 不出现"点击展开"字样,只靠箭头指示
            first_name, first_args, first_result, first_status = _unpack_tool(tools[0])
            first_cn, first_target = _tool_label(first_name, first_args)
            preview = (
                '<div class="tools-preview">'
                '▸ ' + _html_escape(first_cn) +
                (' · ' + _html_escape(first_target[:40]) if first_target else '') +
                '  ' + (_unpack_status_mark(first_result, first_status)) +
                self._tools_tail_summary(states) +
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

        用 div.thinking 类 + <details> 嵌套实现折叠;思考过程单独走自定义 div。
        """
        if not thoughts:
            return ""
        joined = "\n".join(f"> {t.strip()}" for t in thoughts if t.strip())
        # 折叠（<details>）：最多 6 行可折叠，避免铺满屏幕
        return (
            '<div class="thinking">'
            '<details>'
            '<summary class="thinking-head">'
            f'思考过程（{len(thoughts)} 步）</summary>'
            '<pre class="thinking-body">'
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
            # 颜色交给 CSS（.execs-exit.ok / .execs-exit），两套主题同步
            ec_state = "ok" if ec == 0 else "failed"
            ec_icon = "[完成]" if ec == 0 else "[失败]"
            rows.append(
                '<div class="execs-row">'
                f'<div class="execs-cmd-line">$ {cmd}</div>'
                f'<pre class="execs-output">{output}</pre>'
                f'<div class="execs-exit {ec_state}">'
                f'{ec_icon} exit_code={ec}</div>'
                '</div>'
            )
        head = (
            '<div class="execs-head">执行命令 · '
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
                '<div class="file-edit-row">'
                f'<b>{_html_escape(e["path"])}</b>'
                f'&nbsp;<span class="file-edit-size">· {e["size"]} 字节</span>'
                '</div>'
            )
        head = (
            '<div class="file-edits-head">文件编辑 · '
            + str(len(edits)) + '</div>'
        )
        return '<div class="file-edits">' + head + "".join(rows) + '</div>'

    def _msg_mood_to_border(self) -> str:
        """v38: 把桌宠当前 mood 量化 → 心情色 token (signature 元素).

        state 为 None / quantize 失败 → MOOD_NONE (暖灰)
        mood HIGH → 樱粉 (热情)
        mood MID  → 蜜桃 (温和)
        mood LOW  → 雾蓝 (低落)

        配合 CHAT_MD_CSS 的 .msg-assistant border-left + 内联 style 覆盖,
        主人看到桌宠心情直接呈现在聊天气泡左边, 不需要专门看状态栏.
        """
        try:
            state = self._attached_state
            if state is None:
                return ui_style.MOOD_NONE
            q = state.quantize()
            mood = q.get("mood", "MID")
            if mood == "HIGH":
                return ui_style.MOOD_HIGH
            if mood == "LOW":
                return ui_style.MOOD_LOW
            return ui_style.MOOD_MID
        except Exception:  # noqa: BLE001
            return ui_style.MOOD_NONE

    def _msg_html(self, msg: Message, *, streaming_meta: Optional[str] = None) -> str:
        """按已完成的 Message 渲染成 HTML 字符串 (MiniMax Code 风格线性流)。

        布局 (CSS 驱动):
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
        头像不再放在 chat 内,桌宠头顶像由 pet_window 显示。
        """
        if msg.role == "system":
            raw = (msg.content or "").replace("&", "&amp;").replace(
                "<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")
            return (
                '<div style="text-align:center;margin:10px 0;">'
                f'<span class="system-msg">{raw}</span></div>'
            )

        is_user = msg.role == "user"
        # 主人的消息标「主人」：中文应用，而且人设里桌宠就是这么称呼主人的
        # 桌宠自己：显示**说话当时**的人设名（换人设后老消息不会被改成新人设）
        role_label = "主人" if is_user else _html_escape(
            getattr(msg, "speaker", "") or self.char_cfg.name or "桌宠")

        # 空 content + 无 tools → 灰色 placeholder 提示
        # 流式时跳过 placeholder,由 _typing_dots_html 负责显示 dots
        raw = msg.content or ""

        # 压缩摘要条目：role 仍是 "user"（它要进 LLM 的历史，role 不能改），
        # 但**显示上绝不能冒充主人说话** —— 它是系统生成的上下文摘要，
        # 标成「主人」会让主人以为刚才那句是自己说的。渲染成一条独立的居中说明。
        #
        # 判定用**前缀**（app/brain/compaction.py::summary_entry_content 生成的正文
        # 一定以标记开头）：原来用 in 匹配，主人自己写「【历史摘要】」四个字
        # 也会被当成摘要条 —— 主人说的话被改标成「历史摘要」，等于替主人改口。
        if raw.lstrip().startswith(SUMMARY_MARKER):
            body = _html_escape(raw).replace("\n", "<br/>")
            return (
                '<div class="msg msg-summary">'
                '<div class="msg-label">'
                '<span class="who">历史摘要</span>'
                '<span class="ts">更早的对话已压缩</span>'
                "</div>"
                f'<div class="msg-body summary-body">{body}</div>'
                "</div>"
            )
        if (not raw.strip() and not (msg.tools or []) and not streaming_meta):
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
                '<div class="msg-body">'
                '<span class="empty-msg-hint">'
                '[此消息内容为空，可能是早期生成失败]</span>'
                '</div>'
                '</div>'
            )

        # 清理内容
        raw = self._clean_content(raw)
        # 上传 / 拖图产生的 file:// URL 在 markdown 里 QTextBrowser 不一定 load;
        # 渲染前把 ![alt](file://...) 替换成 data: URL,让 QTextBrowser 自包含 base64 嵌图
        raw = self._inline_local_images_to_data_url(raw)

        # HTML 转义 + markdown 渲染 (两边都做 markdown;user 消息内容一般很短, 走 markdown 没副作用)
        body_html = self._render_markdown(raw)

        # 界面自己补的「已经帮主人搞定啦~」只在工具**真的成了**时才成立。
        # 工具失败时这句是假的（主人以为成功了，其实没成）—— 显示层换成
        # 诚实的说法；落库的原文不动（模型看到的历史保持原样）。
        if (not is_user) and raw.strip() == _UI_TOOL_DONE_TEXT \
                and self._tools_failed(getattr(msg, "tools", None)):
            body_html = (
                '<div class="msg-notice">%s</div>'
                % _html_escape(_UI_TOOL_DONE_FAIL_TEXT)
            )

        # 流式输出中:正文末尾追加「三点跳动」等待动画
        # 只要 streaming_meta 非空就显示 dots, 与 emotion 无关
        if not is_user and streaming_meta:
            body_html = body_html + self._typing_dots_html()

        # 工具/思考/命令/文件块 (assistant only, prepend 到 body 前)
        if not is_user:
            thoughts_html = self._thoughts_html(getattr(msg, "thoughts", []) or [])
            execs_html = self._execs_html(getattr(msg, "execs", []) or [])
            file_edits_html = self._file_edits_html(msg.tools)
            # 传 id(msg) 让 _tool_steps_html 把折叠状态按消息分别存
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
        # v38: signature 元素 — 桌宠消息左边 3px 心情色带
        # _msg_mood_to_border(quantize → mood HIGH/MID/LOW) 映射到 MOOD_HIGH/MID/LOW
        # 没量化数据时 (state=None) 走 MOOD_NONE (暖灰)
        msg_style = ""
        if not is_user:
            try:
                border_color = self._msg_mood_to_border()
            except Exception:  # noqa: BLE001
                border_color = ui_style.MOOD_NONE
            msg_style = f' style="border-left:3px solid {border_color};"'
        return (
            f'<div class="msg {role_class}"{msg_style}>'
            f'{label_html}'
            f'<div class="msg-body">{body_html}</div>'
            f'</div>'
        )

    def _clean_content(self, text: str) -> str:
        """清理消息内容:去掉首尾空白,字面量 \\n 转实际换行,折叠连续空行。

        \n\n 段间隔保留(语义需要),但 \n\n\n\n+ 折叠。
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
        """把 markdown 里的本地图片引用 (file:///) 替换成 data: URL,
        让 QTextBrowser 自包含 base64 嵌图。
        PyQt5 5.x 的 QTextBrowser 对 file:// 嵌图支持不稳,data: URL 稳定可靠。

        范围:
          - 只处理 markdown 形式 `![alt](file:///...)`, 扫的是 image 链接
          - file 存在 + size < 8 MiB 才 inline (太大直接显示 file:// 文字)
          - 非 file:// 引用 (http/https/data:) 原样保留
          - 失败 fallback: 保持原 file:// 文字, 不抛异常

        v14: 还支持项目内相对路径 (assets/uploads/xxx.png), 按 self._uploads_dir()
        解析; 文件找不到时替换成「[图片已丢失: <basename>]」让主人知道是哪张图
        没了, 不再是空白方块。
        """
        import re as _re
        import base64 as _b64
        # markdown image 形 ![alt](URL) — 抓 alt + url
        # URL 可以是 file:///, http(s)://, data:, 或项目内相对路径 (含 assets/ 开头)
        pattern = _re.compile(r'!\[([^\]]*)\]\(([^)\s]+)\)')

        uploads_root = self._uploads_dir() if hasattr(self, "_uploads_dir") else None
        # _uploads_dir = project_root / "assets" / "uploads", 解相对路径用
        # project_root (uploads_root.parent.parent), 不是 uploads_root.parent
        project_root = uploads_root.parent.parent if uploads_root is not None else None

        def _resolve_local_path(url: str) -> Optional[Path]:
            """把 url 解析成可读 file path; 非本地或解析失败返 None。"""
            if url.startswith("file:///"):
                local = url[len("file:///"):]
                if not local:
                    return None
                try:
                    return Path(local)
                except Exception:
                    return None
            # 项目内相对路径 (assets/uploads/xxx.png)
            if project_root is not None and (
                url.startswith("assets/")
                or url.startswith("./assets/")
            ):
                rel = url.lstrip("./")
                return project_root / rel
            return None

        def _replace(m: "re.Match[str]") -> str:
            alt = m.group(1) or "图片"
            url = m.group(2)

            # http(s):// data: → 原样保留
            if url.startswith(("http://", "https://", "data:")):
                return m.group(0)

            p = _resolve_local_path(url)
            if p is None:
                return m.group(0)
            if not p.is_file():
                # 文件丢失给清晰提示,不再空白
                return f"*[图片已丢失: {p.name}]*"
            try:
                st = p.stat()
            except OSError:
                return f"*[图片读取失败: {p.name}]*"
            if st.st_size > 8 * 1024 * 1024:
                return m.group(0)
            # 记忆化：同一张图在流式期间会被反复编码（每秒 ~2.6 次），
            # 按 (路径, mtime, size) 命中缓存就不用再读盘 + base64。
            try:
                cache_key = (str(p.resolve()), st.st_mtime_ns, st.st_size)
            except OSError:
                cache_key = (str(p), st.st_mtime_ns, st.st_size)
            cached = self._img_data_url_cache.get(cache_key)
            if cached is not None:
                self._doc_has_inline_images = True
                return f"![{alt}]({cached})"
            try:
                data = p.read_bytes()
            except OSError:
                return f"*[图片读取失败: {p.name}]*"
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
            data_url = f"data:{mime};base64,{b64}"
            # 只缓存小图（base64 后体积翻倍），条目数也封顶，别把内存吃光
            if len(data) <= self._IMG_CACHE_MAX_BYTES:
                if len(self._img_data_url_cache) >= self._IMG_CACHE_MAX_ENTRIES:
                    self._img_data_url_cache.clear()
                self._img_data_url_cache[cache_key] = data_url
            self._doc_has_inline_images = True
            return f"![{alt}]({data_url})"

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

        def _inline_md(src: str) -> str:
            """行内 markdown → HTML（行内代码 / 图片 / 链接 / 加粗 / 斜体）。

            提成函数是因为**块级元素里的行内标记也要渲染**：
            列表项里的加粗、引用里的行内代码，原来显示成原样标记
            （块元素是在行内处理之前被抽走的，行内规则够不着它们）。
            """
            # 行内元素 —— 先做 code (避免被误判)
            src = re.sub(r"\x60([^\x60\n]+)\x60", r"<code>\1</code>", src)

            # v8 markdown 图片 ![alt](url) → <img src=url>
            # 缩略图固定上限 (340px) + 点击放大看原图。KEY 用 sha1[:10] 稳定，
            # 同一 URL 同一 key，重渲染不丢映射，避免 QUrl 对 data: 长 base64 截断
            def _img(m: "re.Match[str]") -> str:
                alt = m.group(1)
                url = m.group(2)
                if not re.match(r"^(https?|file|data):", url, re.IGNORECASE):
                    return m.group(0)
                # 上限 340 防大屏时图过大，下限 200 防小屏时图过窄
                try:
                    vp_w = self.chat_view.viewport().width()
                except Exception:
                    vp_w = 0
                if vp_w and vp_w > 200:
                    thumb_w = max(200, min(340, int(vp_w * 0.85)))
                else:
                    thumb_w = 340  # 没 layout / 测试环境 fallback
                key = _img_open_key(url)
                self._img_open_map[key] = url
                return (
                    f'<a href="img-open:{key}" title="点击放大" '
                    f'style="text-decoration:none;display:inline-block;">'
                    f'<img src="{url}" alt="{alt}" width="{thumb_w}" '
                    f'style="display:block;width:{thumb_w}px;height:auto;'
                    f'max-width:100%;border-radius:8px;margin:2px 0;'
                    f'cursor:zoom-in;"/>'
                    f'</a>'
                )
            src = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)\)", _img, src)

            # 链接 [text](url)  (允许 http/https/mailto/img-open; 后者由
            #    _on_chat_anchor_clicked 路由到 _show_image_popup 弹原图 QDialog)
            def _link(m: "re.Match[str]") -> str:
                label = m.group(1)
                url = m.group(2)
                if re.match(r"^(https?|mailto|img-open):", url, re.IGNORECASE):
                    return f'<a href="{url}">{label}</a>'
                return m.group(0)
            src = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link, src)

            # 加粗 (必须先于斜体,避免被斜体提前消耗)
            src = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", src)
            src = re.sub(r"__([^_\n]+)__", r"<strong>\1</strong>", src)

            # 斜体
            src = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", src)
            src = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<em>\1</em>", src)
            return src

        # 1a. ```fenced code``` —— 同时保留原 char 内容, 不转义
        def _save_code(m: "re.Match[str]") -> str:
            lang = m.group(1) or ""
            body = m.group(2)
            # 代码正文必须**转义**：它是代码，不是 HTML。
            # 原来原文直接塞进去 → "if a < b and c > d:" 里的 "< b and c >"
            # 被当成标签吞掉，代码显示成 "if a  d:"（内容凭空少一截）。
            return _stash(
                f'<pre><code data-lang="{_html_escape(lang)}">'
                f'{_html_escape(body)}</code></pre>'
            )
        text = re.sub(r'```(\w+)?\n(.*?)```', _save_code, text, flags=re.DOTALL)

        # 1b. 水平分隔线 (3+ 个 -)
        text = re.sub(r'(?m)^-{3,}\s*$',
                      lambda _: _stash('<hr/>'), text)

        # 1c. 标题 (## ... / ### ... / #### ...)
        text = re.sub(r'(?m)^####\s+(.+?)\s*$',
                      lambda m: _stash(
                          f'<h4>{_inline_md(_html_escape(m.group(1)))}</h4>'), text)
        text = re.sub(r'(?m)^###\s+(.+?)\s*$',
                      lambda m: _stash(
                          f'<h3>{_inline_md(_html_escape(m.group(1)))}</h3>'), text)
        text = re.sub(r'(?m)^##\s+(.+?)\s*$',
                      lambda m: _stash(
                          f'<h2>{_inline_md(_html_escape(m.group(1)))}</h2>'), text)
        text = re.sub(r'(?m)^#\s+(.+?)\s*$',
                      lambda m: _stash(
                          f'<h1>{_inline_md(_html_escape(m.group(1)))}</h1>'), text)

        # 1d. 引用块 (> ...)
        def _save_blockquote(m: "re.Match[str]") -> str:
            inner = m.group(0)
            inner = re.sub(r'(?m)^>\s?', '', inner)
            # 引用块正文同样要转义（块元素是在转义**之前**抽出来的），
            # 块里的行内标记也要渲染
            inner_html = _inline_md(_html_escape(inner)).replace("\n", "<br/>")
            return _stash(f'<blockquote>{inner_html}</blockquote>')
        text = re.sub(r'(?:^>.*\n?)+', _save_blockquote, text, flags=re.MULTILINE)

        # 1e. 无序列表 (整段连续 - / * 行包一个 <ul>)
        def _save_ul(m: "re.Match[str]") -> str:
            items = re.findall(r'^[-*]\s+(.+?)\s*$', m.group(0), flags=re.MULTILINE)
            return _stash('<ul>' + ''.join(
                f'<li>{_inline_md(_html_escape(it))}</li>' for it in items) + '</ul>')
        text = re.sub(r'(?:^[-*]\s+.+\n?)+', _save_ul, text, flags=re.MULTILINE)

        # 1f. 有序列表 (整段连续 "1. ..." 行包一个 <ol>)
        def _save_ol(m: "re.Match[str]") -> str:
            items = re.findall(r'^\d+\.\s+(.+?)\s*$', m.group(0), flags=re.MULTILINE)
            return _stash('<ol>' + ''.join(
                f'<li>{_inline_md(_html_escape(it))}</li>' for it in items) + '</ol>')
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
                    '<tr>' + ''.join(
                        f'<td>{_inline_md(_html_escape(c))}</td>'
                        for c in cells) + '</tr>'
                )
            head = (
                '<tr>' + ''.join(
                    f'<th>{_inline_md(_html_escape(c))}</th>'
                    for c in header_cells) + '</tr>'
            )
            return _stash('<table>' + head + ''.join(body_rows) + '</table>')
        # 表格行末的换行是**可选**的：原来要求每行都以 \n 结尾，
        # 消息正好以表格结尾时（最后一行后面没有换行），整张表只剩表头，
        # 数据行变成一串 "|" 原文漏在下面 —— 表格数据凭空少一截。
        text = re.sub(
            r'(?:^[ \t]*\|.*\n?){2,}',
            _save_table, text, flags=re.MULTILINE,
        )

        # 2. 现在可以安全 HTML 转义 (因为块元素都已抽走, 不会误伤)
        text = text.replace("&", "&amp;")
        text = text.replace("<", "&lt;")
        text = text.replace(">", "&gt;")

        # 3. 行内元素（行内代码 / 图片 / 链接 / 加粗 / 斜体）
        text = _inline_md(text)

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
        """渲染一条消息到 chat_view 末尾，强制独立段落。

        v23: 滚动到最底改用 QTimer.singleShot(0, ...) 延迟到下一事件循环。
        老代码直接 sb.setValue(sb.maximum()) 在 insertHtml 后立即调 —— 这时
        layout 还没完成, sb.maximum() 可能是旧值, 看着像「没滚到底」。
        延迟 0ms 让 chat_view 先 layout 完再算滚动条, 之后 setValue(maximum) 准。
        """
        html = self._msg_html(msg)
        cursor = self.chat_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertHtml(html)
        cursor.insertBlock()
        # 延后到下一事件循环再滚, 让 layout 把 scroll bar maximum 算准
        QTimer.singleShot(0, self._scroll_to_bottom)

    def _scroll_to_bottom(self) -> None:
        """滚到 chat_view 底部(配合 _render_message / _insert_placeholder 延后用)。
        仅当用户当前在底部 4px 范围内时强制滚 —— 否则保留他滚动到中间
        看历史的位置, 避免被新消息「踢下去」打断阅读。
        """
        sb = self.chat_view.verticalScrollBar()
        if sb.value() < sb.maximum() - 4:
            return
        sb.setValue(sb.maximum())
        sb = self.chat_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _rebuild_chat_view_from_history(self) -> None:
        """完全重建 chat_view:从 self.history 重新渲染所有消息,再加上
        当前 streaming 占位 (如果有)。

        这是 cmd:toggle_tools 点击处理用的最稳路径,不动 anchor/cursor,
        用全量重渲染替代「从 anchor 删到末尾再 insertHtml」(后者某些状态会擦白 chat)。
        """
        # 1. 滚到底 (重渲染后还要在底部)
        sb = self.chat_view.verticalScrollBar()
        was_at_bottom = (sb.value() >= sb.maximum() - 4)
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
            finished: True = 流式结束,渲染最终完整消息(不加 dots)
                      False = 仍是流式占位,显示三点跳动等待动画
            emotion: 已完成时的情绪,目前仅供 trace 等外部使用,不在 chat 里渲染
                     (避免之前 bug: 把 emotion 当 streaming_meta 传下去,
                      _msg_html 看到 truthy 就追加 dots → 已完成的消息尾巴还有 ..)
        """
        _ = emotion  # emotion 不再当 streaming_meta 用,避免跟"是否还在打字"耦合
        if finished:
            # 流式结束: 不传 streaming_meta → 跳过 dots / placeholder 分支
            html = self._msg_html(self._current_bot_msg)
        else:
            # 流式打字机期间一律显示三点跳动（typing 动画）
            html = self._msg_html(self._current_bot_msg, streaming_meta="typing…")

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

        Qt 富文本不支持 CSS @keyframes，动画由 QTimer 定时推进 _typing_frame
        并整体重绘占位气泡。颜色交给 CHAT_MD_CSS 里定义的 .typing-dot 类。
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