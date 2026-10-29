"""智能中枢控制器：封装 MemoryStore + ToolRegistry + ProactiveBrain + AgentLoop + MCP。"""
from __future__ import annotations

import atexit
import logging
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from app.core.qt_compat import QObject, Signal, is_qobject_alive
from app.brain.agent import AgentLoop
from app.brain.llm_client import LLMClient
from app.brain.memory import MemoryCurator, MemoryStore
from app.brain.proactive import ProactiveBrain
from app.engine.tools import build_default_tools
from app.engine.works import ItemStore

log = logging.getLogger(__name__)


# 多种可能的 skill 路径（DSH 默认结构 + 简化结构）
_SKILL_PATH_CANDIDATES = [
    ".agents/skills/desktop-pet-tool-usage/SKILL.md",
    "agents/skills/desktop-pet-tool-usage/SKILL.md",
    ".dsh/skills/desktop-pet-tool-usage/SKILL.md",
]


def _read_skill_md(root: Path) -> str | None:
    """从 root 下读 SKILL.md 的 Markdown body，找不到返回 None。

    SKILL.md 格式：YAML frontmatter (`---\\n...\\n---\\n`) + Markdown body，
    只取 body 拼到系统 prompt 里。
    """
    for rel in _SKILL_PATH_CANDIDATES:
        path = root / rel
        if path.is_file():
            try:
                raw = path.read_text(encoding="utf-8")
            except Exception as e:  # noqa: BLE001
                log.warning("读 SKILL.md 失败 %s: %s", path, e)
                continue
            # 剥 frontmatter
            if raw.startswith("---"):
                parts = raw.split("---", 2)
                if len(parts) >= 3:
                    return parts[2].strip()
            return raw.strip()
    return None


# SKILL.md 找不到时的内嵌兜底版
_FALLBACK_SKILL = """\
# Tool Usage — 31 tools, 你必须**真的调**它们才能「做」事

## ⚠️ HARD RULES
1. 用户要工具能做的事时（打开应用/网页/提醒/记事实等），**必须调工具**。
2. 调了工具后，按工具返回值简短告诉主人。
3. 工具返回「错误」就告诉主人出了什么问题，不要假装成功。
4. 写「已打开/已启动/已发送」时，**这一轮必须有对应的工具调用**。

## 📚 何时调哪个工具
- 「打开 XX」（XX 不是网址）→ `open_app(app_name="XX")`
- 「打开 https://...」/「打开 baidu.com」→ `open_website(url="...")`
- 「XX 分钟后提醒我 YY」→ `add_reminder(text="YY", delay_seconds=NN)`
- 「记住 XX」/分享个人信息 → `remember_fact(content="XX", category="preference", importance=0.8)`
- 「你还记得 XX 吗」→ `recall_memory(keyword="XX")`
- 「几点」→ `get_current_time()`
- 「你怎么样」→ `get_pet_status()`
- 「XX+YY 是多少」→ `calculate(expression="XX+YY")`
- 「打开任务管理器 / 控制面板 / 设置 / 文件管理器 / 终端 / 记事本 / 计算器」→ 对应 `open_*` 工具

## ❌ 禁止（hallucination）
写「已打开 QQ」但**没调** `open_app` → 主人看不到任何效果 = 体验崩溃。
"""


#: Stage 2.3：每轮都在的【记忆规则】。
#:
#: 对齐 qq-bridge `src/bridge.js` 的 `memoryRuleLine`（记忆规则放唤醒提示里，
#: 因为「什么该记、变了怎么改」是**行为规则**，工具描述只能讲清参数）。
#:
#: 为什么放**稳定通道**（system prompt）而不是易变通道：
#:   这段文字是**常量** —— 不随本轮 query / 时间 / 状态变，
#:   所以留在 system prompt 里**不会**让 MiniMax 的 8192-token 前缀缓存
#:   第一块每轮作废（实测缓存命中率 98.9% → 1.5%，见 P0-11）。
#:   真正每轮都变的是**召回的记忆块**，它走易变通道（独立 user 消息）。
MEMORY_RULE_BLOCK = (
    "【记忆规则】以后还用得上的具体事，用 remember_fact 记一条：\n"
    "① 主人讲过的事（偏好、约定、作息口味）；\n"
    "② **你自己的承诺、立场、你想变成的样子**（我答应他送礼物、"
    "我不想再只会附和了）"
    "——②必须记，只记主人的事等于把自己的成长丢在会过期的短期记忆里；\n"
    "事实变了用 remember_fact(supersedes=旧id) 改写旧说法"
    "（旧条会自动标记为过时，不会新旧两条并存），"
    "别新记一条造成互相矛盾；\n"
    "随口闲聊不用记；当下的情绪波动也别记成 fact（那是 pendingThought 的事）；\n"
    "召回给你的记忆（【你记得的事】）只是**线索**：相关就用，"
    "不相关就当没看见，别硬扯。"
)


def _get_skill_text(root: Path) -> str:
    """读 SKILL.md 或返回兜底内容（绝不返回空字符串）。"""
    text = _read_skill_md(root)
    if text:
        return text
    log.debug(
        "未找到 SKILL.md（尝试过 %s），用内置兜底 skill",
        [str(root / r) for r in _SKILL_PATH_CANDIDATES],
    )
    return _FALLBACK_SKILL


# ---------------------------------------------------------------------------
# Stage 2.5：MemoryCurator（后台记忆整理线程）
#
# 现状：MemoryCurator 在全仓**从未被实例化** —— 整理线程从来没启动过，
#       forget_if_expired（**删行**）/ merge_similar（**改写**）一次都没跑过。
#
# ⚠️ 数据安全红线：run_once() 会真的动 live 库（删行 + 改写），所以
#    **每次真动手之前先备份**。备份策略：**同一个库一天只备一份**
#    （看备份文件名里的日期）—— 桌宠天天启动也不会把磁盘塞满，
#    而当天第一次整理前一定有可回滚的副本。
# ---------------------------------------------------------------------------

#: 整理间隔（小时）。默认 12 —— 与 MemoryCurator 自身的默认值一致。
CURATOR_INTERVAL_HOURS = 12


def _curator_backup_dir(db_path: Path) -> Path:
    """Curator 备份目录：<db 所在目录>/backups/<db 名>/。"""
    return db_path.parent / "backups" / db_path.stem


def _copy_db_consistent(src: Path, dest: Path) -> None:
    """一致性拷贝 sqlite 库。

    优先用 sqlite 的 backup API（事务级一致快照）；源文件不是合法 sqlite 库
    时退化成普通文件拷贝。
    """
    try:
        uri = src.resolve().as_uri() + "?mode=ro"
        s = sqlite3.connect(uri, uri=True)
        d = sqlite3.connect(str(dest))
        try:
            with d:
                s.backup(d)
        finally:
            d.close()
            s.close()
        return
    except Exception as e:  # noqa: BLE001
        log.debug("sqlite backup API 失败(%s)，退化成文件拷贝", e)
    shutil.copy2(src, dest)


def backup_memory_db(db_path: Path, backup_dir: Path | None = None) -> Path | None:
    """把记忆库备份成时间戳副本，返回备份路径（失败 → None）。

    幂等：**每个库每天最多一份备份**（按备份文件名里的日期判断）。
    桌宠每天启动都不会重复写备份，磁盘不会被塞满。
    """
    db_path = Path(db_path)
    if not db_path.is_file():
        return None
    dest_dir = Path(backup_dir) if backup_dir else _curator_backup_dir(db_path)
    today = time.strftime("%Y%m%d")
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        existing = sorted(dest_dir.glob(f"{db_path.name}.{today}-*.db"))
        if existing:
            # 今天已经备过 —— 不重复写
            return existing[0]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = dest_dir / f"{db_path.name}.{stamp}.db"
        _copy_db_consistent(db_path, dest)
        log.info("记忆库已备份：%s → %s", db_path, dest)
        return dest
    except Exception as e:  # noqa: BLE001
        log.warning("记忆库备份失败(%s)：%s", db_path, e)
        return None


class _GuardedMemoryCurator(MemoryCurator):
    """动 live 库之前先备份的 MemoryCurator。

    MemoryCurator.run_once() 会 forget_if_expired（**删行**）+
    merge_similar（**改写**）。这里在每次真正动手之前先确认「今天有备份」，
    保证任何一次破坏性整理之前都留着一份可回滚的副本。
    """

    def __init__(self, memory_store, check_interval_hours: int = CURATOR_INTERVAL_HOURS,
                 backup_dir: Path | None = None):
        super().__init__(memory_store, check_interval_hours=check_interval_hours)
        self.backup_dir = Path(backup_dir) if backup_dir else None
        self.last_backup: Path | None = None

    @property
    def db_path(self) -> Path:
        """被整理的记忆库路径（MemoryStore.data_file）。"""
        return Path(getattr(self.memory, "data_file", "") or "")

    def ensure_backup(self) -> Path | None:
        """确保今天有备份（没有就立刻备一份）。返回备份路径。"""
        db = self.db_path
        if not db.name or not db.is_file():
            return None
        dest = backup_memory_db(db, self.backup_dir)
        if dest is not None:
            self.last_backup = dest
        return dest

    def run_once(self) -> dict:
        """整理一次 —— **先备份，再动库**。"""
        # 记忆库的向量索引是**懒刷新**的（add() 只标脏，检索时才重建）。
        # 不先刷新的话 merge_similar 拿到的是空/旧索引 → 永远合并不了任何东西。
        try:
            self.memory._ensure_backend_fresh()
        except Exception:  # noqa: BLE001
            log.debug("Curator: 刷新向量索引失败（忽略）", exc_info=True)
        self.ensure_backup()
        return super().run_once()


class BrainController(QObject):
    """封装 memory + tools + proactive + agent 的管理逻辑。"""

    # 信号：主动发言就绪（文本）
    remark_ready = Signal(str)
    # 信号：主动发言携带的情绪（Emotion 枚举）
    emotion_hint = Signal(object)
    # 信号：工具触发的气泡请求（文本）
    bubble_requested = Signal(str)
    # 信号：工具请求收起桌宠气泡（clear_bubble 用）
    bubble_cleared = Signal()
    # 信号：工具触发的动画请求（动画名）
    animation_requested = Signal(str)
    # 信号：倒计时 / 提醒到点（文本, 类型标题），UI 层弹置顶闹钟窗 + 提示音 + 播报 + 系统通知
    alarm_fired = Signal(str, str)

    def __init__(self, root: Path, cfg, state, reminders,
                 parent: QObject | None = None) -> None:
        """
        Args:
            root: 项目根目录
            cfg:  Config 对象
            state: PetState 实例
            reminders: ReminderStore 实例
            parent: Qt 父对象
        """
        super().__init__(parent)
        self.root = root
        self.cfg = cfg
        self.state = state
        # SKILL.md 加载（缓存到实例避免每轮重读）
        self._skill_text = _get_skill_text(root)

        mem_path = cfg.brain.memory_file
        if not Path(mem_path).is_absolute():
            mem_path = root / mem_path
        # v31: 长期记忆 per-character 隔离 — 切人设 + 重启后, 每个角色各一份 db
        # (MemoryStore 内部 data_file 派生 data/memory_{name}.db)
        self.memory = MemoryStore(
            mem_path,
            character_name=cfg.character.name if cfg.character.name else "",
        )

        # Stage 1.1：长期记忆块交给共享层（app/brain/prompt_layer.py）。
        # 两个后端都从这里拿**同一份**记忆块 —— ReAct 后端没有 memory_store，
        # standard 后端有；不统一的话两边 system prompt 不可能逐字节相同。
        try:
            from app.brain.prompt_layer import register_memory_block_provider
            # Stage 2.1：长期记忆块按**本轮主人这句话**检索（不再固定 top-N 倾倒）。
            # provider 接受一个**可选** query 参数：
            #   - 共享层 prompt_layer.get_memory_block() 仍然无参调用 → 老路径不变；
            #   - 谁拿到主人这句话，就可以 provider(query)，
            #     或先 memory.set_current_query(text) 再让 provider 取。
            # 拿不到 query 时按重要性取（≤ max_inject 条）。
            register_memory_block_provider(
                lambda query="": self.memory.system_block(query))
        except Exception as e:  # noqa: BLE001
            log.warning("注册长期记忆块提供者失败：%s", e)

        # Stage 2.5：启动记忆整理线程（MemoryCurator）。
        # 以前 MemoryCurator 在全仓**从未被实例化** → 整理线程从来没启动过，
        # forget_if_expired（删行）/ merge_similar（改写）一次都没跑过。
        # 用守护版：每次真动手之前先备份（同一个库一天只备一份，幂等）。
        self.curator = self._start_curator(self.memory)

        # 接线 SessionStore + skills_dir，让 ChatWindow 能挂到 LangChainAgent
        # 两份对象都做 try/except，失败不阻断桌宠启动
        try:
            from app.brain.sessions import SessionStore
            sessions_path = root / "data" / "pet_sessions.db"
            self.session_store = SessionStore(sessions_path)
        except Exception as e:  # noqa: BLE001
            log.warning("SessionStore 初始化失败,会话列表 / 续接 不可用: %s", e)
            self.session_store = None
        # skills 目录约定:项目根/skills/*.md(用户可放,文件不存在就空目录)
        self.skills_dir: Path = root / "skills"
        try:
            self.skills_dir.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass

        foods_path = root / "data" / "foods.json"
        self.items = ItemStore.load(foods_path) if foods_path.is_file() else None

        # 清掉上次运行残留的系统级闹钟任务（桌宠崩了 / 被强杀会留下）
        try:
            from app.core import win_alarm
            if win_alarm.is_available():
                win_alarm.cleanup_stale()
        except Exception:  # noqa: BLE001
            log.debug("清理残留系统闹钟失败", exc_info=True)

        if cfg.brain.tools_enabled:
            # web_search 主用 Tavily：优先 cfg.brain.tavily_api_key，否则读环境变量
            tavily_key = getattr(getattr(cfg, "brain", None), "tavily_api_key", None) \
                or __import__("os").environ.get("TAVILY_API_KEY") or None
            self.tool_registry = build_default_tools(
                state=self.state,
                reminders=reminders,
                memory=self.memory,
                items=self.items,
                hooks={
                    "bubble": self._emit_bubble,
                    "animation": self._emit_animation,
                    "hide_bubble": self._emit_bubble_cleared,
                    "alarm": self._fire_alarm,
                },
                tavily_api_key=tavily_key,
                cfg=cfg,
            )
            log.info("智能中枢：注册 %d 个工具（默认全可见,实际 LLM 可见取决于等级）%s",
                     len(self.tool_registry.names()), self.tool_registry.names())
        else:
            self.tool_registry = None

        # MCP 外挂工具（后台线程启动 server + 桥接，不阻塞桌宠启动）
        self.mcp_registry = None
        if (self.tool_registry is not None
                and getattr(cfg, "mcp", None) and cfg.mcp.enabled):
            self._setup_mcp(root, cfg)

        # 默认「不睡觉」：上层没注入前，proactive 不会被睡眠态挡掉
        self._sleeping_checker = lambda: False
        if cfg.brain.proactive_enabled:
            self.proactive = ProactiveBrain(
                llm_cfg=cfg.llm,
                persona=cfg.character.persona,
                state=self.state,
                memory=self.memory,
                char_name=cfg.character.name,
                min_minutes=cfg.brain.proactive_min_minutes,
                max_minutes=cfg.brain.proactive_max_minutes,
                is_sleeping=self._check_sleeping,
                chat_cfg=cfg.chat,
                aw_cfg=getattr(cfg, "activitywatch", None),
            )
            self.proactive.remark_ready.connect(self._on_proactive_remark)
            self.proactive.emotion_hint.connect(self.emotion_hint)
            self.proactive.start()
        else:
            self.proactive = None

    # ---- Stage 2.5：记忆整理线程（MemoryCurator） ----------------------------
    def _start_curator(self, memory_store) -> "_GuardedMemoryCurator | None":
        """构造并启动 MemoryCurator（daemon 线程）。

        失败不影响桌宠启动（整理是后台维护功能，不是本体功能）。
        """
        try:
            interval = int(getattr(getattr(self.cfg, "brain", None),
                                   "curator_interval_hours", CURATOR_INTERVAL_HOURS))
        except Exception:  # noqa: BLE001
            interval = CURATOR_INTERVAL_HOURS
        try:
            curator = _GuardedMemoryCurator(
                memory_store, check_interval_hours=interval)
            curator.start()
            # 进程退出时停线程（daemon 线程本来也会被强杀，这里只是收尾干净）
            atexit.register(curator.stop)
            log.info("MemoryCurator 已启动（每 %d 小时整理一次记忆）", interval)
            return curator
        except Exception as e:  # noqa: BLE001
            log.warning("MemoryCurator 启动失败（不影响桌宠本体）：%s", e)
            return None

    def stop_curator(self) -> None:
        """停止记忆整理线程（进程退出前调用）。"""
        curator = getattr(self, "curator", None)
        if curator is None:
            return
        try:
            curator.stop()
        except Exception:  # noqa: BLE001
            log.debug("停止 MemoryCurator 失败（忽略）", exc_info=True)

    # ---- 给工具用的安全信号出口 ------------------------------------------------
    # 工具在后台线程里跑，emit 前要确保 QObject 还活着（已析构则丢弃回调）
    def _alive(self) -> bool:
        if not is_qobject_alive(self):
            log.debug("BrainController 已析构，丢弃迟到的工具回调")
            return False
        return True

    def _emit_bubble(self, text: str) -> None:
        if self._alive():
            self.bubble_requested.emit(text)

    def _emit_animation(self, name: str) -> None:
        if self._alive():
            self.animation_requested.emit(name)

    def _emit_bubble_cleared(self) -> None:
        if self._alive():
            self.bubble_cleared.emit()

    def _fire_alarm(self, text: str, kind: str = "⏰ 倒计时结束") -> None:
        """倒计时到点：走闹钟通道（置顶窗 + 提示音 + 播报 + 系统通知）。"""
        if self._alive():
            self.alarm_fired.emit(text, kind)

    def set_sleeping_checker(self, checker) -> None:
        """设置是否睡觉的检查函数（由 App 提供）。

        checker 为 None 时恢复默认（始终不睡觉），便于上层主程序无需关心
        「是否已注入」的分支。
        """
        self._sleeping_checker = checker if checker is not None else (lambda: False)

    def _check_sleeping(self) -> bool:
        """检查桌宠是否在睡觉。checker 抛异常时兜底返回 False（不睡觉）。"""
        try:
            return bool(self._sleeping_checker())
        except Exception:  # noqa: BLE001
            return False

    def chat_context(self) -> str:
        """生成**稳定**的 system 上下文：SKILL + 工具列表。

        SKILL 放在最前。

        这里**不能**放每轮变的东西（当前时间 / 桌宠实时状态 / 召回的记忆）——
        system message 落在 MiniMax 的 8192-token 前缀缓存块里，块里每轮
        变一次（时间戳！）整块作废（实测缓存命中率 98.9% → 1.5%）。
        易变部分走 chat_context_dynamic() → 本轮的 user 消息。

        长期记忆块**不在**这里（Stage 2.1 + 记忆通道搬家）：记忆块按**本轮
        query** 召回 ⇒ 每轮都变 ⇒ 放 system prompt 会打掉前缀缓存，
        也会让 Stage 2.8 的 agent 指纹每轮变（每轮重建 agent）。
        它现在走易变通道（chat_context_dynamic(query=...) → 独立 user 消息），
        两个后端拿**同一份**（都由共享层 provider 取）。

        Stage 2.3：但【记忆规则】**在**这里（常量 → 放稳定通道安全）：
        「什么该记、变了怎么改」是行为规则，每轮都得在场（对齐 qq-bridge
        的 memoryRuleLine）。规则本身不随 query 变，所以放进 system prompt
        不会让前缀缓存第一块每轮作废。

        ⚠️ 这段块是**最终 system 字符串的一部分**，但它不是最终字符串 ——
        最终拼装在共享层 app/brain/prompt_layer.py（两个后端共用）。
        这里只提供「数据」（SKILL / 工具名），段的拼法归共享层。
        """
        from app.brain.prompt_layer import build_session_block
        tool_list = ""
        if self.tool_registry is not None:
            tool_names = "、".join(self.tool_registry.names())
            tool_list = self._tool_usage_instruction(tool_names)
        return build_session_block(
            skill=self._skill_text,      # SKILL.md 内容（最重要，放在最前）
            # Stage 2.3：memory= 槽位放的是**常量**的【记忆规则】，
            # 不是召回的记忆 —— 召回的记忆按本轮 query 变，走易变通道
            #（chat_context_dynamic → 独立 user 消息）。
            # 规则是常量 → 放稳定通道不会打掉前缀缓存（P0-11 的不变量）。
            memory=MEMORY_RULE_BLOCK,
            tool_list=tool_list,
        )

    def chat_context_dynamic(self, query: str = "") -> str:
        """生成**易变**上下文：当前时间 + 桌宠实时状态 + 召回的记忆（走 user 消息）。

        见 chat_context() 的注释：易变内容不能进 system prompt。

        query: 本轮主人说的话 —— 长期记忆块**按这句话召回**（Stage 2.1）。
        拿不到 query 时记忆块按重要性取（≤ max_inject 条）。
        """
        from app.brain.llm_client import build_dynamic_context
        return build_dynamic_context(state=self.state, query=query)

    @staticmethod
    def _tool_usage_instruction(tool_names: str) -> str:
        """列出可用工具名；「何时调 / 怎么调」规则见上方 SKILL 文档。

        Stage 1.1：文案搬进共享层（app/brain/prompt_layer.py），
        两个后端的 system prompt 因此长得一样。
        """
        from app.brain.prompt_layer import build_tool_list
        return build_tool_list(tool_names)

    def connect_tool_animation(self, pet) -> None:
        """连接工具动画信号到桌宠。"""
        self.animation_requested.connect(self._do_play_animation)
        self._pet_ref = pet

    def _do_play_animation(self, name: str) -> None:
        """工具触发的动画。"""
        try:
            if name == "eat":
                self._pet_ref.play_emotion("happy")
            elif name == "stretch":
                self._pet_ref.animator.play_stretch()
            elif name == "jump":
                self._pet_ref.animator.play_jump()
            elif name == "spin":
                self._pet_ref.animator.play_spin()
            elif name == "swim":
                self._pet_ref.animator.play_swim()
            elif name == "file":
                self._pet_ref.animator.play_file()
            elif name == "thinking":
                self._pet_ref.animator.set_thinking()
            elif name.startswith("emotion_"):
                self._pet_ref.play_emotion(name.replace("emotion_", ""))
            else:
                # 尝试当作情绪播放
                self._pet_ref.play_emotion(name)
        except Exception as e:  # noqa: BLE001
            log.warning("工具动画播放失败：%s", e)

    def _on_proactive_remark(self, text: str) -> None:
        """转发主动发言信号。"""
        self.remark_ready.emit(text)

    def create_agent(self, client: LLMClient, confirm_tool=None) -> AgentLoop:
        """创建 AgentLoop 实例。"""
        return AgentLoop(
            client=client,
            registry=self.tool_registry,
            confirm_tool=confirm_tool,
        )

    # ---------------------------------------------------------- MCP 外挂工具
    def _setup_mcp(self, root: Path, cfg) -> None:
        """后台加载 MCP servers 并把它们的工具桥接进 tool_registry。

        MCP server 是独立子进程，握手可能秒级，放后台线程避免卡启动。
        """
        from app.mcp.protocol import MCPClientRegistry
        servers_file = Path(cfg.mcp.servers_file)
        if not servers_file.is_absolute():
            servers_file = root / servers_file

        registry = MCPClientRegistry()
        n = registry.load_from_yaml(servers_file)
        if n == 0:
            log.info("MCP：已启用但没有可用的 server 配置（%s）", servers_file)
            return
        self.mcp_registry = registry
        log.info("MCP：已加载 %d 个 server 配置，后台启动中…", n)

        def _load():
            try:
                started = registry.start_all_sync(timeout=30.0)
                ok = {k: len(v) for k, v in started.items() if v}
                if not ok:
                    log.warning("MCP：所有 server 启动失败（检查 %s）", servers_file)
                    return
                from app.brain.agent import DANGEROUS_TOOLS
                bridged = registry.bridge_to(
                    self.tool_registry, dangerous_tools=set(DANGEROUS_TOOLS))
                log.info("MCP：已桥接 %d 个外挂工具 %s",
                         bridged, list(ok.keys()))
            except Exception:  # noqa: BLE001
                log.exception("MCP 加载失败（不影响桌宠本体功能）")

        threading.Thread(target=_load, daemon=True, name="mcp-load").start()

    def shutdown_mcp(self) -> None:
        """停止所有 MCP server 子进程 + 关闭常驻 loop（进程退出前调用）。"""
        if self.mcp_registry is None:
            return
        try:
            self.mcp_registry.stop_all_sync()
        except Exception:  # noqa: BLE001
            log.debug("MCP 关闭异常（忽略）", exc_info=True)
