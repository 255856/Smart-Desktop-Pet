"""智能中枢控制器：封装 MemoryStore + ToolRegistry + ProactiveBrain + AgentLoop + MCP。"""
from __future__ import annotations

import datetime
import logging
import threading
from pathlib import Path

from app.core.qt_compat import QObject, Signal, is_qobject_alive
from app.brain.agent import AgentLoop
from app.brain.llm_client import LLMClient
from app.brain.memory import MemoryStore
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
    """从 root 下读 SKILL.md 文件，找不到返回 None。

    SKILL.md 格式：YAML frontmatter (`---\\n...\\n---\\n`) + Markdown body。
    我们的工具 schema 是 OpenAI Function Calling 格式——把 SKILL 的 body
    拼到系统 prompt 里就够了，frontmatter 是给 harness 看的元数据。
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


# 兜底：找不到 SKILL.md 时用这个内嵌版本（不依赖文件系统）
# 注意：保持精简，SKILL.md 文件版更完整（有 examples 表）
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
    # 信号：倒计时 / 提醒到点（文本, 类型标题）。UI 层接它弹置顶闹钟窗 +
    # 提示音 + 播报 + 系统通知。原先 countdown 到点只冒一个气泡，主人常听不到。
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
        self.memory = MemoryStore(mem_path)

        foods_path = root / "data" / "foods.json"
        self.items = ItemStore.load(foods_path) if foods_path.is_file() else None

        # 清掉上次运行残留的系统级闹钟任务（桌宠崩了/被强杀会留下）。
        # 不清的话任务计划程序里会越攒越多，而且那些提醒主人已经不需要了。
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
            )
            log.info("智能中枢：注册 %d 个工具 %s",
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
            )
            self.proactive.remark_ready.connect(self._on_proactive_remark)
            self.proactive.emotion_hint.connect(self.emotion_hint)
            self.proactive.start()
        else:
            self.proactive = None

    # ---- 给工具用的安全信号出口 ------------------------------------------------
    # 工具在 asyncio.to_thread 的工作线程里跑，倒计时提醒更是在自己的后台
    # 线程里跑。它们随时可能在 QObject 析构之后才回来 emit 信号——直接
    # `self.bubble_requested.emit` 会在 Windows 上 access violation 打崩进程
    # （测试里必现）。这里统一过一道存活性检查。
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
        """倒计时到点：走闹钟通道（置顶窗 + 提示音 + 播报 + 系统通知）。

        在后台线程里被调用，所以必须先过 _alive()——QObject 析构后 emit
        会直接 access violation。
        """
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
        """每次发消息前生成动态 system 上下文：时间 / 状态 / 记忆 / SKILL。

        系统提示分三块：
            1. **SKILL**（来自 .agents/skills/.../SKILL.md）—— 何时调哪个工具的明确规则
            2. 当前时间 / 桌宠状态 / 长期记忆
            3. **工具列表**（供 LLM 知道有哪些 tool 名可用）

        顺序关键：SKILL 在最前，模型第一眼就看到「何时调哪个工具」。
        """
        now = datetime.datetime.now()
        wd = "一二三四五六日"[now.weekday()]
        parts = [self._skill_text]    # SKILL.md 内容（最重要，放在最前）
        parts.append(
            f"【当前时间】{now.strftime('%Y-%m-%d %H:%M')} 星期{wd}\n\n"
            f"【你的实时状态】{self.state.stats_summary()}"
        )
        mem_block = self.memory.system_block()
        if mem_block:
            parts.append(mem_block)
        if self.tool_registry is not None:
            tool_names = "、".join(self.tool_registry.names())
            parts.append(self._tool_usage_instruction(tool_names))
        return "\\n\\n".join(parts)

    @staticmethod
    def _tool_usage_instruction(tool_names: str) -> str:
        """列出可用工具名（SKILL.md 已经在 chat_context 前面讲了何时调哪个工具，
        这里只是让模型知道有哪些 tool_name 可以用）。

        注意：核心的「何时调 / 怎么调」规则都在 SKILL.md 里（前面已经注入），
        这里只是工具清单。
        """
        return (
            "【可用工具列表】\n"
            f"你**真正可调用**的工具名（共 {len(tool_names.split('、'))} 个）：{tool_names}。\n"
            "**何时调哪个工具**的规则见上面的 SKILL 文档。"
        )

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

        MCP server 是独立子进程，启动 + initialize 握手可能秒级；
        放后台线程避免卡启动。桥接完成后 AgentLoop 下一条消息就能用
        （chat_window 每轮实时取 registry.names() / to_openai()）。
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
