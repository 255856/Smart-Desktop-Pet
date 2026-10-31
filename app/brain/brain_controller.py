"""智能中枢控制器：封装记忆 sidecar 客户端 + ToolRegistry + ProactiveBrain + AgentLoop + MCP。

长期记忆**不在本进程里**：它在独立进程 memory_sidecar 里（HTTP :8788）。
这里只持有 MemoryClient（客户端）+ MemorySidecarSupervisor（管它的生死）。
"""
from __future__ import annotations

import atexit
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.core.pet_control import PAT_PART_LABELS
from app.core.qt_compat import QObject, Signal, is_qobject_alive
from app.brain.agent import AgentLoop
from app.brain.llm_client import LLMClient
from app.brain.memory_client import default_port as memory_sidecar_port
from app.brain.memory_client import DEFAULT_TIMEOUT as MEMORY_CLIENT_TIMEOUT
from app.brain.memory_client import MemoryClient
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
#:   所以留在 system prompt 里**不会**让 MiniMax 的前缀缓存（最长公共前缀）
#:   第一块每轮作废（实测缓存命中率 98.9% → 1.5%，见 P0-11）。
#:   真正每轮都变的是**召回的记忆块**，它走易变通道（独立 user 消息）。
MEMORY_RULE_BLOCK = (
    "【记忆规则】以后还用得上的具体事，用 remember_fact 记一条：\n"
    "① 主人讲过的事（偏好、约定、作息口味）；\n"
    "② **你自己的承诺、立场、你想变成的样子**（我答应他送礼物、"
    "我不想再只会附和了）"
    "——②必须记，只记主人的事等于把自己的成长丢在会过期的短期记忆里；\n"
    "③ 主人跟你**共同经历 / 共同拥有**的事（给你换模型、起昵称、约好的事）也算；\n"
    "记完**如实**告诉主人结果：没调成工具就不许说「记下了」；\n"
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
# 记忆 sidecar（独立进程）的监督者
#
# 长期记忆住在**独立进程** memory_sidecar 里：
#   python -m memory_sidecar serve --port 8788
# 好处是记忆库（Chroma + BM25 + 精排）崩了不会带崩桌宠，模型只加载一次。
# 代价是**桌宠得管它的生死** —— 就是这个类：
#
#   1. 端口上已经有健康的 sidecar → **直接用它**，不再起第二个（A4）；
#   2. 起子进程后用**有限次**健康轮询等它就绪（绝不无限等、绝不阻塞启动）；
#   3. 子进程死了 → 有界重启（指数退避），连续失败 N 次后进**降级模式**，
#      并且只提醒主人一次（走 vector-index 那条已有的 say hook / toast 通道）；
#   4. 退出时 terminate/kill（atexit 兜底）—— 不留孤儿进程（A5）。
#
# 测试（A4）不需要真 sidecar：spawn / probe / sleep / monotonic 全部可注入。
# ---------------------------------------------------------------------------

#: sidecar 端口（环境变量可覆盖 —— 端口被占时用得上）。
#: 用客户端那同一个函数算：客户端连的和监督者起的必须是同一个端口。
SIDECAR_PORT = memory_sidecar_port()
#: 起进程后最多等多久过健康检查（秒）—— 有限次，绝不无限等。
#: 为什么不是 3~5 秒：sidecar 一起来就在后台预热模型（import torch 是 GIL 密集型），
#: 实测这段时间里 /health 偶尔要 1.2s 才答得上来；窗口太短会**误杀**一个马上就好的
#: 进程（然后重启一遍，反而更慢）。25s 是有界的，而且跑在后台线程里 ——
#: 桌宠自己的启动从来不等它。
SIDECAR_START_TIMEOUT = 25.0
#: 单次健康探测的超时（秒）。探测只要「端口在不在听 / 服务答不答」，
#: 不需要等满聊天路径的超时；但也不能太紧（见上：预热期会长到 1.2s）
SIDECAR_PROBE_TIMEOUT = 2.0
#: 健康轮询间隔（秒）
SIDECAR_POLL_INTERVAL = 0.5
#: 连续失败几次就放弃（进降级模式）
SIDECAR_MAX_RESTARTS = 3
#: 重启退避（秒）；第 n 次失败后睡 backoff[min(n-1, len-1)]
SIDECAR_BACKOFF = (0.5, 1.0, 2.0)
#: 看门狗心跳间隔（秒）
SIDECAR_WATCH_INTERVAL = 5.0
#: terminate 之后等多久再 kill（秒）
SIDECAR_KILL_TIMEOUT = 5.0


def sidecar_base_url(port: int | None = None) -> str:
    """sidecar 的地址（客户端和监督者用的是同一个口径）。"""
    return "http://127.0.0.1:%d" % int(port or SIDECAR_PORT)


def sidecar_autostart_enabled() -> bool:
    """这次启动要不要**真的**拉起 sidecar 子进程。

    pytest 下不拉：每个 BrainController 都起一个子进程的话，一整套用例会
    起上百个、每个都加载真模型（A7：测试不需要真 sidecar）。用例要看监督
    行为时自己注入假的 spawn / probe（见 tests/test_memory_sidecar_supervisor.py）。
    """
    try:
        if "pytest" in sys.modules:
            return False
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return False
    except Exception:  # noqa: BLE001 - 判断不出来就按生产处理
        return True
    return True


def _default_owner_notice(message: str) -> None:
    """告诉主人「长期记忆降级了」。

    走桌宠气泡 say hook → Windows 系统通知兜底；同一个原因只会说一次。
    实现在 app/core/owner_notice.py（原来是借「向量索引」那条通道，
    向量索引删掉之后独立成模块）。
    """
    from app.core import owner_notice
    owner_notice.notify_owner_degraded("memory_sidecar", "长期记忆降级", message)


class MemorySidecarSupervisor:
    """把 `python -m memory_sidecar serve` 当子进程管起来。

    参数都能注入（spawn / probe / say / sleep / monotonic），所以整套行为
    —— 健康轮询、有界重启、放弃进降级、复用已在跑的 sidecar、退出清理 ——
    都能用一个假进程测出来，不需要真 sidecar、不需要网络。
    """

    def __init__(self, root: Path, port: int = SIDECAR_PORT, client=None, *,
                 spawn=None, probe=None, say=None, sleep=None, monotonic=None,
                 max_restarts: int = SIDECAR_MAX_RESTARTS,
                 start_timeout: float = SIDECAR_START_TIMEOUT,
                 poll_interval: float = SIDECAR_POLL_INTERVAL,
                 probe_timeout: float = SIDECAR_PROBE_TIMEOUT,
                 backoff=SIDECAR_BACKOFF,
                 watch_interval: float = SIDECAR_WATCH_INTERVAL,
                 log_dir: Path | None = None,
                 python: str | None = None) -> None:
        self.root = Path(root)
        self.port = int(port or SIDECAR_PORT)
        self.client = client if client is not None else MemoryClient(sidecar_base_url(self.port))
        self.max_restarts = max(1, int(max_restarts))
        self.start_timeout = max(0.0, float(start_timeout))
        self.poll_interval = max(0.0, float(poll_interval))
        self.probe_timeout = max(0.05, float(probe_timeout))
        self.backoff = tuple(float(x) for x in (backoff or (0.0,))) or (0.0,)
        self.watch_interval = max(0.01, float(watch_interval))
        self.python = python or sys.executable
        self.log_dir = Path(log_dir) if log_dir is not None else self.root / "data" / "logs"

        self._spawn_fn = spawn
        self._probe_fn = probe
        self._say = say
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic

        self.process = None          # 自己拉起来的子进程（None = 没起过 / 已收尸）
        self.running = False         # sidecar 现在能用吗
        self.external = False        # True = 复用端口上**别人**的 sidecar
        self.degraded = False        # 放弃重启了（桌宠降级：没有长期记忆）
        self.last_error = ""
        self.failures = 0            # 连续失败次数（成功一次清零）
        self.log_path: Path | None = None
        self._notified: set = set()
        #: 「端口上已经有一个健康的 sidecar → 直接复用」这条**只说一次**。
        #: 以前每次心跳都打一遍 INFO（看门狗 5 秒一次 + 面板轮询），
        #: 主人的控制台被同一条消息刷了几千行 —— 那不是信息，是噪声。
        self._reuse_logged = False
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._atexit_registered = False

    # -- 探测 -------------------------------------------------------------
    def probe(self) -> dict | None:
        """sidecar 活着的证据（GET /health 的 dict）；探不到 → None。"""
        try:
            if self._probe_fn is not None:
                return self._probe_fn() or None
            return self.client.health(timeout=self.probe_timeout) or None
        except Exception as e:  # noqa: BLE001 - 探测本身不许抛
            self.last_error = "健康检查失败：%s: %s" % (type(e).__name__, e)
            return None

    def child_alive(self) -> bool:
        """自己起的子进程还活着吗。"""
        proc = self.process
        if proc is None:
            return False
        try:
            return proc.poll() is None
        except Exception:  # noqa: BLE001
            return False

    def _exit_code(self):
        try:
            return self.process.poll() if self.process is not None else None
        except Exception:  # noqa: BLE001
            return None

    # -- 拉起 / 等待 -------------------------------------------------------
    def spawn_child(self):
        """起 sidecar 子进程；失败 → None（原因进 last_error）。"""
        if self._spawn_fn is not None:      # 测试注入的假进程
            try:
                proc = self._spawn_fn()
            except Exception as e:  # noqa: BLE001
                self.last_error = "拉起 sidecar 失败：%s: %s" % (type(e).__name__, e)
                return None
            self.process = proc
            self.external = False
            return proc

        cmd = [self.python, "-m", "memory_sidecar", "serve", "--port", str(self.port)]
        handle = None
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            self.log_path = self.log_dir / "memory_sidecar.log"
            handle = open(self.log_path, "ab")
        except Exception as e:  # noqa: BLE001 - 写不了日志也要能起进程
            self.log_path = None
            log.warning("记忆 sidecar 日志文件打不开（不影响运行）：%s", e)
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.root),
                stdin=subprocess.DEVNULL,
                stdout=handle if handle is not None else subprocess.DEVNULL,
                stderr=subprocess.STDOUT if handle is not None else subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:  # noqa: BLE001
            self.last_error = "拉起 sidecar 失败：%s: %s" % (type(e).__name__, e)
            log.warning("记忆 sidecar 起不来（降级：没有长期记忆）：%s", e)
            return None
        finally:
            if handle is not None:
                try:
                    handle.close()      # 子进程已经拿到自己的句柄
                except Exception:  # noqa: BLE001
                    pass
        self.process = proc
        self.external = False
        log.info("记忆 sidecar 已拉起：pid=%s cmd=%s", getattr(proc, "pid", "?"),
                 " ".join(cmd))
        return proc

    def _wait_healthy(self) -> bool:
        """有限次轮询 /health：就绪 → True；超时 / 子进程先死 → False。"""
        deadline = self._monotonic() + self.start_timeout
        while True:
            if self.probe():
                return True
            if self.process is not None and not self.child_alive():
                return False
            if self._monotonic() >= deadline:
                return False
            self._sleep(self.poll_interval)

    def _backoff_delay(self) -> float:
        if self.failures <= 0:
            return 0.0
        idx = min(self.failures - 1, len(self.backoff) - 1)
        return self.backoff[idx]

    def _spawn_and_wait(self) -> bool:
        proc = self.spawn_child()
        if proc is None:
            self.failures += 1
            self._degrade_if_exhausted()
            return False
        if self._wait_healthy():
            self.running = True
            self.external = False
            self.degraded = False
            self.last_error = ""
            self.failures = 0
            log.info("记忆 sidecar 就绪：%s（日志 %s）", sidecar_base_url(self.port),
                     self.log_path or "（未写入文件）")
            return True
        self._kill_child(proc)
        self.process = None
        self.running = False
        self.failures += 1
        self.last_error = "sidecar 起来后 %.1fs 内没通过健康检查" % self.start_timeout
        log.warning("记忆 sidecar 没能就绪（第 %d 次）：%s", self.failures, self.last_error)
        self._degrade_if_exhausted()
        return False

    # -- 心跳（看门狗） ----------------------------------------------------
    def tick(self) -> bool:
        """一次心跳：确保 sidecar 活着（复用 / 拉起 / 有界重启 / 放弃）。"""
        if self._stopping.is_set():
            return False
        if self.child_alive():
            self.running = True
            return True
        if self.process is not None:
            # 自己起的那个死了 —— 收尸 + 记一次失败
            self.last_error = "sidecar 进程已退出（退出码 %s）" % (self._exit_code(),)
            self.process = None
            self.running = False
            self.failures += 1
            log.warning("记忆 sidecar 挂了：%s", self.last_error)
        if self.probe():
            # 端口上已经有一个健康的 sidecar（别的实例 / 上一次没退干净）→ 就用它
            self.external = self.process is None
            self.running = True
            self.degraded = False
            self.last_error = ""
            if self._reuse_logged:
                # 已经在跑了 —— 第二次以后降到 debug（别刷主人的控制台）
                log.debug("记忆 sidecar 已在 %s 运行，直接复用（不再起第二个）",
                          sidecar_base_url(self.port))
            else:
                self._reuse_logged = True
                log.info("记忆 sidecar 已在 %s 运行，直接复用（不再起第二个）",
                         sidecar_base_url(self.port))
            return True
        if self.failures >= self.max_restarts:
            self._degrade_if_exhausted()
            return False
        delay = self._backoff_delay()
        if delay > 0:
            self._sleep(delay)
        return self._spawn_and_wait()

    def _watch_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - 看门狗自己不许把进程带崩
                log.exception("记忆 sidecar 看门狗心跳失败（忽略）")
            if self._stopping.wait(self.watch_interval):
                return

    # -- 生命周期 ----------------------------------------------------------
    def start(self, blocking: bool = False) -> bool:
        """启动监督。

        blocking=False（生产）→ 丢给后台线程，**立刻返回**，绝不阻塞桌宠启动。
        blocking=True（测试 / 命令行）→ 同步跑一次心跳，返回是否就绪。
        """
        self._stopping.clear()
        if not self._atexit_registered:
            atexit.register(self.stop)
            self._atexit_registered = True
        if blocking:
            return self.tick()
        self._thread = threading.Thread(target=self._watch_loop,
                                        name="memory-sidecar-watchdog", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """退出：停看门狗 + 掐死子进程（可重复调用，atexit 也走这里）。"""
        self._stopping.set()
        proc, self.process = self.process, None
        self.running = False
        if proc is None:
            return
        self._kill_child(proc)
        log.info("记忆 sidecar 已停止（不留孤儿进程）")

    def _kill_child(self, proc) -> None:
        """terminate → wait → kill（Windows 上 terminate 就是 TerminateProcess）。"""
        try:
            if proc.poll() is not None:
                return
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.terminate()
        except Exception as e:  # noqa: BLE001
            log.debug("终止记忆 sidecar 失败（继续 kill）：%s", e)
        try:
            proc.wait(timeout=SIDECAR_KILL_TIMEOUT)
            return
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.kill()
            proc.wait(timeout=SIDECAR_KILL_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            log.warning("记忆 sidecar 进程收不干净：%s", e)

    # -- 降级 / 提醒 -------------------------------------------------------
    def _degrade_if_exhausted(self) -> bool:
        """连续失败到上限 → 进降级模式（只提醒主人一次）。"""
        if self.failures < self.max_restarts:
            return False
        self.degraded = True
        self.running = False
        message = ("长期记忆暂时用不了了（备份服务重试 %d 次都没起来）。"
                   "我还能正常聊天，只是不记得以前的事。" % self.failures)
        self.last_error = self.last_error or message
        log.warning("记忆 sidecar 放弃重启：%s", message)
        self.notify_owner_once(message)
        return True

    def notify_owner_once(self, message: str) -> bool:
        """同一条消息只提醒主人一次。返回是否真的说了。"""
        if message in self._notified:
            return False
        self._notified.add(message)
        say = self._say or _default_owner_notice
        try:
            say(message)
            return True
        except Exception as e:  # noqa: BLE001 - 提醒失败不许影响任何东西
            log.info("记忆降级的提醒发不出去（不影响功能）：%s", e)
            return False

    # -- 状态（面板 / 测试） ------------------------------------------------
    def status(self) -> dict:
        return {
            "running": bool(self.running or self.child_alive()),
            "managed": self.process is not None,
            "external": bool(self.external),
            "degraded": bool(self.degraded),
            "failures": int(self.failures),
            "max_restarts": int(self.max_restarts),
            "port": self.port,
            "url": sidecar_base_url(self.port),
            "log": str(self.log_path) if self.log_path else "",
            "error": self.last_error,
        }


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
        # 「拍一拍」写记忆的后台线程（最后一次的句柄，测试/排障用）
        self._pat_memory_thread = None
        # SKILL.md 加载（缓存到实例避免每轮重读）
        self._skill_text = _get_skill_text(root)

        # 长期记忆住在**独立进程** memory_sidecar 里：这边只留一个 HTTP
        # 客户端 + 一个监督者（拉起 / 健康检查 / 有界重启 / 退出清理）。
        # 客户端**绝不抛异常**：sidecar 不在，记忆块就是空串，桌宠照常说话。
        self.memory_client = MemoryClient(sidecar_base_url(SIDECAR_PORT),
                                          timeout=MEMORY_CLIENT_TIMEOUT)
        self.sidecar = MemorySidecarSupervisor(root, port=SIDECAR_PORT,
                                               client=self.memory_client)
        if sidecar_autostart_enabled():
            # 后台线程起 + 只做有限次健康轮询：**绝不用 sidecar 卡住桌宠启动**
            self.sidecar.start()
        else:
            log.debug("测试进程：不真的拉起记忆 sidecar（用例自己注入假客户端）")

        # 客户端就是 memory 槽位：面板（app/ui/memory_panel.py）、聊天窗口的
        # /remember、主动发言（app/brain/proactive.py）、standard 后端的
        # memory_store 都拿它，接口是 count/add/all/recent/system_block。
        # **没有第二个记忆后端**：sidecar 不在就是一个都读不到，绝不是「退回老库」。
        self.memory = self.memory_client

        # Stage 1.1：长期记忆块交给共享层（app/brain/prompt_layer.py）。
        # 两个后端都从这里拿**同一份**记忆块 —— ReAct 后端没有 memory_store，
        # standard 后端有；不统一的话两边 system prompt 不可能逐字节相同。
        try:
            from app.brain.prompt_layer import register_memory_block_provider
            # 长期记忆块按**本轮主人这句话**检索（query 由调用方传入）。
            # 拿不到 query 时 sidecar 没有「按重要性列出」的接口 → 返回空块：
            # 这轮就是没有可用记忆，而不是硬塞一堆无关的旧事。
            register_memory_block_provider(
                lambda query="": self.memory_client.system_block(query))
        except Exception as e:  # noqa: BLE001
            log.warning("注册长期记忆块提供者失败：%s", e)

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
                # 记忆三件套走 sidecar 客户端（不再用进程内的老记忆库）
                memory=self.memory_client,
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
            # Stage 3.1：冻结「存量工具」基线 —— 从这一刻起，白名单外的
            # 工具在**执行期**一律拒绝（模型硬点名也拦得住）。
            self.tool_registry.seal_baseline()
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
                recent_turns_provider=self._recent_chat_turns,
            )
            self.proactive.remark_ready.connect(self._on_proactive_remark)
            self.proactive.emotion_hint.connect(self.emotion_hint)
            self.proactive.start()
        else:
            self.proactive = None
        # 运行时换角色：主动发言拿的是**启动时**的人设快照，这里注册一个
        # 回调，配置层换完角色就同步过来（不用重启）。
        try:
            from app.core.config import register_character_switch_hook
            register_character_switch_hook(self._on_character_switched)
        except Exception as e:  # noqa: BLE001
            log.warning("注册角色切换回调失败：%s", e)

    # ---- 记忆 sidecar（独立进程）的状态 / 收尾 ---------------------------------
    def sidecar_status(self) -> dict:
        """记忆 sidecar 现在什么样（面板 / 测试用）。

        running  : 能连上吗（自己起的 / 复用别人的都算）
        cards    : 记忆库里有多少条（sidecar 报的）
        degraded : 是不是已经放弃重启了（桌宠降级：没有长期记忆）
        last_error / log : 出什么事了、日志在哪
        """
        status = self.sidecar.status() if getattr(self, "sidecar", None) else {}
        health = {}
        try:
            # 只读探测 + 短超时：面板刷新不许被 sidecar 卡住
            health = self.memory_client.health(timeout=0.5) or {}
        except Exception as e:  # noqa: BLE001
            status["probe_error"] = "%s: %s" % (type(e).__name__, e)
        status["reachable"] = bool(health.get("ok"))
        status["cards"] = health.get("cards")
        status["cards_total"] = health.get("cardsTotal")
        status["models"] = health.get("models") or {}
        status["sidecar_degraded"] = bool(health.get("degraded"))
        status["client_error"] = self.memory_client.last_error
        if not status.get("error"):
            status["error"] = self.memory_client.last_error
        return status

    def stop_sidecar(self) -> None:
        """停掉记忆 sidecar（进程退出前调用；atexit 也会兜一次）。"""
        supervisor = getattr(self, "sidecar", None)
        if supervisor is None:
            return
        try:
            supervisor.stop()
        except Exception:  # noqa: BLE001
            log.debug("停止记忆 sidecar 失败（忽略）", exc_info=True)

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
        system message 是前缀缓存*最长公共前缀*的起点，里面每轮
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
            # Stage 3.4：按能力分组，不再平铺 68 个工具名
            tool_groups = self.tool_registry.grouped_names()
            tool_list = self._tool_usage_instruction(tool_groups)
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
    def _tool_usage_instruction(tool_groups) -> str:
        """按能力分组列出可用工具；「何时调 / 怎么调」规则见上方 SKILL 文档。

        Stage 1.1：文案搬进共享层（app/brain/prompt_layer.py），
        两个后端的 system prompt 因此长得一样。
        Stage 3.4：入参从「平铺的工具名」改成「分组 → 工具名」。
        """
        from app.brain.prompt_layer import build_tool_list
        return build_tool_list(tool_groups)

    def connect_tool_animation(self, pet) -> None:
        """连接工具动画信号到桌宠。"""
        self.animation_requested.connect(self._do_play_animation)
        self._pet_ref = pet
        # 「拍一拍」也顺手接上（主程序只调这一个方法）
        self.connect_pet_interaction(pet)

    def connect_pet_interaction(self, pet) -> None:
        """连接桌宠的「拍一拍」事件 —— 互动要能进长期记忆。

        拍一拍只绑双击模型（部位=model）；控制台也能按部位触发。
        """
        signal = getattr(pet, "pat_requested", None)
        if signal is None:
            signal = getattr(pet, "reaction_requested", None)
        if signal is None:
            return
        try:
            signal.connect(self._on_pat_requested)
        except Exception as e:  # noqa: BLE001
            log.warning("连接拍一拍事件失败：%s", e)

    def _on_pat_requested(self, part: str) -> None:
        """主人拍了一下桌宠（双击模型）→ 把这次互动记进长期记忆。

        写 sidecar 是网络 IO，不能卡住 GUI 线程 → 丢后台线程。
        """
        if not self._alive():
            return
        text = self._pat_memory_text(part)
        if not text:
            return
        thread = threading.Thread(target=self._write_pat_memory, args=(text,),
                                  name="pet-pat-memory", daemon=True)
        self._pat_memory_thread = thread
        thread.start()

    def _pat_memory_text(self, part: str) -> str:
        """「拍一拍」的长期记忆正文：今天被拍了几次（分部位）。"""
        state = self.state
        today = int(getattr(state, "pat_today", 0) or 0)
        total = int(getattr(state, "pat_total", 0) or 0)
        parts = dict(getattr(state, "pat_parts_today", {}) or {})
        detail = "、".join(
            "%s %d 次" % (PAT_PART_LABELS.get(k, k), v)
            for k, v in sorted(parts.items()))
        return "主人今天拍了我 %d 次%s，累计 %d 次。" % (
            today, ("（%s）" % detail) if detail else "", total)

    def _write_pat_memory(self, text: str) -> None:
        """把「拍一拍」写进长期记忆（每天一条：计数变了更新同一条，不堆卡）。"""
        try:
            client = self.memory_client
            card = client.make_card(text, key="pet_pat", kind="event",
                                    importance=0.4, category="event")
            # 固定 id（按天）→ 计数变化时更新同一条
            card["id"] = "mem_pat_%s" % time.strftime("%Y-%m-%d")
            client.upsert([card])
        except Exception:  # noqa: BLE001
            log.debug("写拍一拍记忆失败（忽略）", exc_info=True)

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

    def _recent_chat_turns(self, limit: int = 4) -> list:
        """最近几轮对话（给主动搭话用）—— 桌宠要能接着刚才的话冒泡。

        取法：读 ChatStore（跟聊天窗口同一个库 data/chat_history.db）。
        聊天窗口是独立组件，brain 这边没有它的引用 —— 走 DB 是最短路径。
        读不到就当没有（不能因为取不到就不冒泡）。
        """
        try:
            from app.engine.chat_store import SHARED_BUCKET, ChatStore
            store = ChatStore(character_name=SHARED_BUCKET)
            msgs = store.all()
        except Exception:  # noqa: BLE001
            log.debug("读最近对话失败（忽略）", exc_info=True)
            return []
        turns = []
        for m in msgs[-max(1, int(limit)):]:
            role = str(getattr(m, "role", "") or "")
            content = str(getattr(m, "content", "") or "").strip()
            if not content:
                continue
            turns.append((role, content))
        return turns

    # -- 运行时换角色 -------------------------------------------------------
    def _on_character_switched(self, name: str, persona: str) -> None:
        """角色换了：把新人设同步给主动发言子系统（ProactiveBrain）。

        ProactiveBrain 构造时拿的是启动时的 persona / char_name 快照，
        这里换掉 → 之后主动搭话也按新人设来。
        """
        if not self._alive():
            return
        proactive = getattr(self, "proactive", None)
        if proactive is None:
            return
        try:
            proactive.persona = persona
            proactive.char_name = name
            log.info("角色已切换 → 主动发言人设同步为 %s", name)
        except Exception as e:  # noqa: BLE001
            log.warning("同步主动发言人设失败：%s", e)

    def switch_character(self, rel_path: str) -> tuple[bool, str]:
        """运行时切换角色（UI 调它）：改配置 + 同步主动发言的人设。

        返回 (是否成功, 提示信息)。失败时配置保持原样，不崩。
        """
        from app.core.config import switch_character as _switch
        return _switch(self.cfg, self.root, rel_path)

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
        from app.mcp.protocol import MCPClientRegistry, MCPServerConfig
        servers_file = Path(cfg.mcp.servers_file)
        if not servers_file.is_absolute():
            servers_file = root / servers_file

        registry = MCPClientRegistry()
        n = registry.load_from_yaml(servers_file)
        # 内置 voice server：语音从「每条回复都朗读」改成「桌宠自己决定发不发」。
        # 它不走 mcp_servers.yaml —— 那是主人的外部扩展清单；语音是本项目自带的、
        # 用本机已有模型（GPT-SoVITS + RVC）的能力，开关在 companion_config.yaml
        # 的 voice.mcp_enabled。
        if self._voice_mcp_enabled():
            registry.register(MCPServerConfig(
                name="voice",
                command=sys.executable,
                args=["-m", "app.mcp.voice_server"],
                cwd=str(root),
                description="本机语音（念 / 唱 / 查音色）",
                # 唱歌要跑一次 RVC 推理（~30 秒），默认 30 秒会先超时
                call_timeout=240.0,
            ))
            n += 1
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
                # MCP 工具也进基线（白名单里它们按 mcp_aw_ / mcp_fs_ / mcp_voice_ 前缀放行）
                self.tool_registry.seal_baseline()
                log.info("MCP：已桥接 %d 个外挂工具 %s",
                         bridged, list(ok.keys()))
            except Exception:  # noqa: BLE001
                log.exception("MCP 加载失败（不影响桌宠本体功能）")

        threading.Thread(target=_load, daemon=True, name="mcp-load").start()

    @staticmethod
    def _voice_mcp_enabled() -> bool:
        """内置 voice MCP server 是否启用。

        读 companion_config.yaml 的 voice.mcp_enabled（默认开）。
        读不到就按「开」处理 —— 语音工具挂了不影响桌宠本体。
        """
        try:
            from app.voice.voice import load_voice_settings
            return bool((load_voice_settings() or {}).get("mcp_enabled", True))
        except Exception:  # noqa: BLE001
            return True

    def shutdown_mcp(self) -> None:
        """停止所有 MCP server 子进程 + 关闭常驻 loop（进程退出前调用）。"""
        if self.mcp_registry is None:
            return
        try:
            self.mcp_registry.stop_all_sync()
        except Exception:  # noqa: BLE001
            log.debug("MCP 关闭异常（忽略）", exc_info=True)
