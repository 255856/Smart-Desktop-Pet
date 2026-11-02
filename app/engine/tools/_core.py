"""Tool / ToolRegistry 核心类型。"""
from __future__ import annotations

import contextvars
import ipaddress
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def is_under_any(path: Path, roots: list[Path]) -> bool:
    """判断 path 是否位于 roots 任意一棵子树的**内部**（按路径语义，非字符串前缀）。

    禁止用 ``str(p).startswith(str(root))``：那是纯字符串比较，
    ``C:\\Users\\alice-backup\\x.txt`` 会被 ``C:\\Users\\alice`` 前缀命中，
    导致沙箱被同前缀兄弟目录绕过。这里统一用 ``relative_to``。

    Args:
        path: 待判定路径（会先 ``resolve()`` 消解 ``..`` 与符号链接）。
        roots: 允许的根目录列表。
    """
    try:
        p_resolved = Path(path).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    for root in roots:
        try:
            r_resolved = Path(root).resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        try:
            p_resolved.relative_to(r_resolved)
            return True
        except ValueError:
            continue
    return False


_BLOCKLISTED_HOSTNAMES = frozenset({
    "localhost", "localhost.localdomain",
    "ip6-localhost", "ip6-loopback",
    "0.0.0.0", "::", "::1",
})


def is_safe_url(url: str, allowed_hosts: Optional[set[str]] = None) -> bool:
    """URL 安全检查：仅 http/https；host 不在环回 / 私有 / 保留地址里。

    Args:
        url: 待检查的 URL。
        allowed_hosts: 若非空，仅这些 host 通过；
                       host 不在白名单 → 一律拒绝（无论公私）。
    """
    try:
        parsed = urlparse(url)
    except (ValueError, TypeError):
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    # 白名单模式：host 不在白名单 → 拒
    if allowed_hosts is not None:
        if host in allowed_hosts:
            return True
        return False
    # 无白名单：走 IP 校验
    if host in _BLOCKLISTED_HOSTNAMES:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # host 是域名（无法静态判定 IP），调用方负责用 socket.gethostbyname 再校验一次
        return True
    if (ip.is_private or ip.is_loopback or ip.is_reserved
            or ip.is_link_local or ip.is_multicast or ip.is_unspecified):
        return False
    return True


# ===========================================================================
# Stage 3 —— 工具可信：结构化信封 / 统一裁剪 / 执行期白名单 / 能力分组
# ===========================================================================


# ---------------------------------------------------------------------------
# 3.2 结构化结果信封
# ---------------------------------------------------------------------------

class ToolResult(dict):
    """工具结果信封（Stage 3.2）。

    字段：status / data / truncated / error_code

    - status："ok" | "error" | "denied"
      （denied = 没过闸：白名单 / 等级 / 人工确认；error = 跑了但失败）
    - data：给模型看的文本（失败时也保留原始文案，便于模型自己判断）
    - truncated：数据是否被裁剪过（统一裁剪或整轮预算裁剪）
    - error_code：稳定的机器可读错误码（not_found / timeout / denied /
      tool_error / level_locked / user_cancelled …），**不是中文短语** ——
      调用方按码分支，不猜关键词。

    兼容旧调用方：app/agents/base.py 与部分老测试还按**字符串**用它
    （result[:4000] / "错误" in result / result == "ok"）。
    这里让 dict 子类保留字符串语义，边界外的文件不用改。
    """

    __slots__ = ()

    def __init__(self, status: str = "ok", data: str = "",
                 truncated: bool = False, error_code: str = "") -> None:
        super().__init__(status=status or "ok", data=data or "",
                         truncated=bool(truncated), error_code=error_code or "")

    # -- 便捷访问 ----------------------------------------------------------
    @property
    def status(self) -> str:
        return self["status"]

    @property
    def data(self) -> str:
        return self["data"]

    @property
    def truncated(self) -> bool:
        return self["truncated"]

    @property
    def error_code(self) -> str:
        return self["error_code"]

    @property
    def ok(self) -> bool:
        return self["status"] == "ok"

    # -- 旧字符串语义兼容 --------------------------------------------------
    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return (self.get("data") or "")[key]
        return super().__getitem__(key)

    def __contains__(self, key) -> bool:
        if super().__contains__(key):
            return True
        if isinstance(key, str):
            return key in (self.get("data") or "")
        return False

    def __eq__(self, other) -> bool:
        if isinstance(other, str):
            return (self.get("data") or "") == other
        return super().__eq__(other)

    def __ne__(self, other) -> bool:
        return not self.__eq__(other)

    def __getattr__(self, item):
        # 其余字符串方法（lower / strip / split …）一律转给 data
        return getattr(self.get("data") or "", item)

    __hash__ = None  # type: ignore[assignment]


#: 工具失败契约（docs/tools-reference.md）：失败必须带这些前缀
_ERROR_PREFIXES = ("错误", "失败", "未安装")

#: 工具 → 错误码的**声明式**映射表（Stage 3.2）
#:
#: 工具层知道每个工具「失败长什么样」，所以在这里把它翻译成稳定的
#: error_code；agent 层不再拿 26 个中文关键词去猜成败。
#:
#: 键 = 工具名；值 = ((错误码, 命中片段), …) —— 命中任一片段即判失败。
#: 没登记的工具走「错误：/失败：」前缀契约（见 _ERROR_PREFIXES）。
_TOOL_ERROR_SPECS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "feed_self": (
        ("insufficient_funds", ("钱不够", "金币不够")),
        ("unknown_item", ("没有叫",)),
    ),
    "play_animation": (
        ("unknown_animation", ("没有叫",)),
    ),
    "convert_units": (
        ("unsupported_conversion", ("不支持的换算",)),
    ),
    "set_volume": (
        ("missing_dependency", ("缺少 pycaw", "无法设置音量")),
    ),
    "set_brightness": (
        ("unsupported_display", ("没有支持 WMI 亮度",)),
    ),
    "open_app": (
        ("not_installed", ("未找到与", "未找到应用", "没装这个")),
    ),
    "recall_memory": (
        ("no_match", ("没有找到匹配的记忆",)),
    ),
    "memory_search": (
        ("no_match", ("没有找到匹配的记忆",)),
    ),
    "ocr_image": (
        ("missing_backend", ("没装", "无法识别这张图")),
    ),
    "web_search": (
        ("search_failed", ("搜索失败", "网络错误", "连接失败")),
    ),
}


def classify_legacy_result(text: str) -> tuple[str, str]:
    """裸字符串结果 → (status, error_code)（**旧路径兜底**）。

    只服务「结果不是信封」的旧调用方（第三方后端 / 测试里的假 registry
    直接返回字符串）。判据 = 前缀契约 + 上面那张错误码映射表的**全部**
    片段（因为没有工具名，只能取并集）。

    新路径（ToolResult 信封）一律读 status / error_code，不走这里。
    """
    raw = text or ""
    if not raw.strip():
        return "ok", ""
    if raw.lstrip().startswith(_ERROR_PREFIXES):
        return "error", "tool_error"
    for specs in _TOOL_ERROR_SPECS.values():
        for _code, patterns in specs:
            for p in patterns:
                if p in raw:
                    return "error", "tool_error"
    return "ok", ""

def classify_tool_result(name: str, text: str) -> tuple[str, str]:
    """工具返回文本 → (status, error_code)。

    status："ok" | "error"；error_code：""（成功）或稳定错误码。
    """
    raw = text or ""
    if not raw.strip():
        return "ok", ""
    for code, patterns in _TOOL_ERROR_SPECS.get(name or "", ()):
        for p in patterns:
            if p in raw:
                return "error", code
    if raw.lstrip().startswith(_ERROR_PREFIXES):
        return "error", "tool_error"
    return "ok", ""


# ---------------------------------------------------------------------------
# 3.3 统一裁剪 + 工具阶段预算
# ---------------------------------------------------------------------------

#: 统一裁剪预算（写死，对齐 qq-bridge 挂载的 dsh-compaction-tool-result-pruner：
#: thresholdChars 50000 / headChars 45000 / tailChars 2000）
TOOL_RESULT_MAX_CHARS = 50000
TOOL_RESULT_HEAD_CHARS = 45000
TOOL_RESULT_TAIL_CHARS = 2000

#: 单个工具的墙钟超时（与 MCP 桥接现状一致：60s）
TOOL_CALL_TIMEOUT_SECONDS = 60.0
#: 整轮工具阶段的墙钟总预算
TOOL_PHASE_BUDGET_SECONDS = 120.0


def _omission_marker(omitted: int) -> str:
    """中间省略标记（明确写出省略了多少字符）。"""
    return ("\n\n…【中间省略 " + str(omitted) + " 字符：工具结果超出 "
            + str(TOOL_RESULT_MAX_CHARS) + " 字符上限，已裁剪】…\n\n")


def trim_tool_result(text: str) -> tuple[str, bool]:
    """统一后置裁剪（Stage 3.3）：超阈值时保头保尾，中间插省略标记。

    返回 (裁剪后的文本, 是否裁剪过)。
    """
    t = text or ""
    if len(t) <= TOOL_RESULT_MAX_CHARS:
        return t, False
    omitted = len(t) - TOOL_RESULT_HEAD_CHARS - TOOL_RESULT_TAIL_CHARS
    return (t[:TOOL_RESULT_HEAD_CHARS] + _omission_marker(omitted)
            + t[-TOOL_RESULT_TAIL_CHARS:]), True


def fit_tool_payload(contents: list[str]) -> tuple[list[str], bool]:
    """整轮工具结果的**注入预算**（Stage 3.3）。

    一轮里所有工具结果的总量超过 TOOL_RESULT_MAX_CHARS 时：保头
    （前 45000 字符）、保尾（后 2000 字符），中间整段省略并留明确标记。

    返回 (裁剪后的内容列表, 是否裁剪过)。
    """
    items = list(contents)
    total = sum(len(c or "") for c in items)
    if total <= TOOL_RESULT_MAX_CHARS:
        return items, False

    out: list[str] = ["" for _ in items]
    # 头：从前往后吃满 head
    used = 0
    head_end = len(items)
    for i, c in enumerate(items):
        c = c or ""
        room = TOOL_RESULT_HEAD_CHARS - used
        if room <= 0:
            head_end = i
            break
        if len(c) <= room:
            out[i] = c
            used += len(c)
        else:
            out[i] = c[:room]
            used += room
            head_end = i + 1
            break
    # 尾：从后往前吃满 tail（不越过头）
    used = 0
    tail_start = len(items)
    for i in range(len(items) - 1, head_end - 1, -1):
        c = items[i] or ""
        room = TOOL_RESULT_TAIL_CHARS - used
        if room <= 0:
            tail_start = i + 1
            break
        if len(c) <= room:
            out[i] = out[i] + c
            used += len(c)
        else:
            out[i] = out[i] + c[-room:]
            used += room
            tail_start = i
            break
    # 中间：整段省略（第一条放完整标记，其余放省略号）
    omitted = total - TOOL_RESULT_HEAD_CHARS - TOOL_RESULT_TAIL_CHARS
    if head_end < tail_start:
        out[head_end] = _omission_marker(omitted) + out[head_end]
        for i in range(head_end + 1, tail_start):
            out[i] = "…"
    return out, True


# ---------------------------------------------------------------------------
# 3.4 标准后端（LangChain）工具调用护栏
# ---------------------------------------------------------------------------
#
# 标准后端的工具是**直接**交给 LangChain 的（StructuredTool.func = tool.fn），
# LangChain 的 ToolNode 直接调它 → 回来的只是一段裸文本：
#   · 工具抛异常 → LangChain 包成 "Error: ..." 之类，界面按前缀判不出失败 → 显示 ✓；
#   · 超大结果 → 整段塞进上下文，没有统一裁剪；
#   · 卡死的工具 → 整轮对话跟着卡住，没有单工具超时。
#
# 下面这个薄适配器把工具函数包一层，让每次调用都走上面那套 helper
# （超时 / 失败前缀 / 统一裁剪），与 ReAct 路径（app/brain/agent.py +
# ToolRegistry.execute）语义一致。工具本体、schema、注册表都不动。


def _fmt_tool_seconds(x: float) -> str:
    """秒数 → 人话（60.0 → "60s"，0.5 → "0.5s"）。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    return f"{int(v)}s" if v.is_integer() else f"{v:.1f}s"


def ensure_failure_prefix(text: str) -> str:
    """失败文案补「失败：」前缀。

    界面（app/ui/chat_window/utils.py::_tool_status）判失败认的正是
    _ERROR_PREFIXES（错误 / 失败 / 未安装）—— 标准后端没有信封 status，
    只有这段文本，所以失败必须带前缀，否则界面会显示 ✓。
    """
    raw = text or ""
    if not raw.strip():
        return "失败：工具没有返回结果。"
    if raw.lstrip().startswith(_ERROR_PREFIXES):
        return raw
    return "失败：" + raw


def _call_tool_with_timeout(fn: Callable[..., Any], kwargs: dict,
                            timeout: Optional[float]
                            ) -> tuple[Any, Optional[BaseException], bool]:
    """在守护线程里跑一次同步工具。

    返回 (返回值, 异常, 是否超时)。超时后**不等**那个跑不动的线程
    （线程没法杀），调用方立刻拿到「超时」继续往下走，线程在后台跑完即弃。
    """
    box: dict = {}

    def _runner() -> None:
        try:
            box["value"] = fn(**kwargs)
        except BaseException as e:  # noqa: BLE001
            box["error"] = e

    # 复制当前 contextvars（LangChain 的回调 / 追踪上下文别丢）
    ctx = contextvars.copy_context()
    th = threading.Thread(target=ctx.run, args=(_runner,), daemon=True,
                         name="tool-call")
    th.start()
    th.join(timeout if (timeout and timeout > 0) else None)
    if th.is_alive():
        return None, None, True
    if "error" in box:
        return None, box["error"], False
    return box.get("value"), None, False


def wrap_tool_fn(name: str, fn: Callable[..., Any],
                 timeout: Optional[float] = None) -> Callable[..., str]:
    """把同步工具函数包成「带结果契约」的调用（标准后端交给 LangChain 用）。

    每次调用依次经过：
      1. 单工具墙钟超时（TOOL_CALL_TIMEOUT_SECONDS）—— 超时按失败报，
         不再让一个卡死的工具把整轮对话拖住；
      2. 异常 → 失败文案（带「失败：」前缀）；
      3. 失败统一加「失败：」前缀（界面按前缀判失败 → 显示 ✕ 而不是 ✓）；
      4. 统一裁剪 trim_tool_result（50000 / 45000 / 2000，与 ReAct 同款）。

    成功且不超预算时**原样返回工具的文本**（不改写、不加前缀）。
    """
    def _guarded(**kwargs) -> str:
        limit = TOOL_CALL_TIMEOUT_SECONDS if timeout is None else timeout
        value, error, timed_out = _call_tool_with_timeout(fn, kwargs, limit)
        if timed_out:
            log.warning("工具 %s 超时（%s）", name, _fmt_tool_seconds(limit))
            return (f"失败：工具 {name} 超时（{_fmt_tool_seconds(limit)}），"
                    "没有返回结果。")
        if error is not None:
            log.warning("工具 %s 执行失败: %r", name, error)
            return f"失败：工具 {name} 执行出错：{error}"
        text = value if isinstance(value, str) else str(value)
        status, _code = classify_tool_result(name, text)
        if status != "ok":
            text = ensure_failure_prefix(text)
        trimmed, _truncated = trim_tool_result(text)
        return trimmed

    _guarded.__name__ = f"guarded_{name}"
    _guarded.__doc__ = f"工具 {name} 的受控调用（超时 / 失败前缀 / 统一裁剪）。"
    return _guarded


# ---------------------------------------------------------------------------
# 3.1 执行期白名单
# ---------------------------------------------------------------------------

#: 默认前缀桶：项目自带的 MCP 命名空间（见 app/mcp/protocol.py 的 bridge_to）
DEFAULT_ALLOWLIST_PREFIXES = ("mcp_aw_", "mcp_fs_")


@dataclass(frozen=True)
class AllowlistPolicy:
    """白名单策略（两级：精确名 + 前缀）。"""

    exact: frozenset[str]
    prefixes: tuple[str, ...]
    from_yaml: bool


_ALLOWLIST_POLICY: Optional[AllowlistPolicy] = None
_ALLOWLIST_LOADED = False


def _read_allowlist_yaml() -> Optional[dict]:
    """读 companion_config.yaml 的 tool_allowlist 段（只读，不改）。

    走 yaml 原文：companion_config 的 dataclass 会丢掉没登记的字段，
    所以这里直接读 yaml（默认表 + 用户覆盖表合并，语义与它一致）。
    """
    try:
        from app.core import companion_config as cc
    except Exception as e:  # noqa: BLE001
        log.debug("读工具白名单失败（companion_config 不可用）：%s", e)
        return None
    try:
        base = cc._read_yaml(cc._default_yaml_path()) or {}
        user = cc._read_yaml(cc._user_yaml_path()) or {}
        merged = cc._merge(base, user)
    except Exception as e:  # noqa: BLE001
        log.warning("读工具白名单失败：%s", e)
        return None
    block = merged.get("tool_allowlist")
    if not isinstance(block, dict):
        return None
    return block


def load_tool_allowlist(*, force: bool = False) -> Optional[AllowlistPolicy]:
    """加载白名单策略（进程内缓存）。返回 None = yaml 里没有该段。

    yaml 段结构::

        tool_allowlist:
          exact: [open_app, ...]        # 精确允许
          prefixes: [mcp_aw_, mcp_fs_]  # 前缀允许
    """
    global _ALLOWLIST_POLICY, _ALLOWLIST_LOADED
    if _ALLOWLIST_LOADED and not force:
        return _ALLOWLIST_POLICY
    _ALLOWLIST_LOADED = True
    block = _read_allowlist_yaml()
    if block is None:
        _ALLOWLIST_POLICY = None
        return None
    exact = frozenset(str(x) for x in (block.get("exact") or [])
                      if isinstance(x, str) and x)
    prefixes = tuple(str(x) for x in (block.get("prefixes") or [])
                     if isinstance(x, str) and x)
    _ALLOWLIST_POLICY = AllowlistPolicy(exact=exact, prefixes=prefixes,
                                        from_yaml=True)
    log.info("工具白名单已加载：精确 %d 个，前缀 %s",
             len(exact), list(prefixes))
    return _ALLOWLIST_POLICY


def reload_tool_allowlist() -> Optional[AllowlistPolicy]:
    """强制重读 yaml（改完配置 / 单测用）。"""
    global _ALLOWLIST_LOADED
    _ALLOWLIST_LOADED = False
    return load_tool_allowlist(force=True)


# ---------------------------------------------------------------------------
# 3.4 工具分组（按 companion_config.yaml 的 tool_unlock 解锁层级对齐）
# ---------------------------------------------------------------------------

#: 解锁层级 → 能力分组。同一层解锁的工具属于同一类能力。
TOOL_GROUPS: dict[int, dict[str, str]] = {
    1: {"name": "基础能力",
        "desc": "时间 / 天气 / 计算 / 自身状态 / 读文件 / 打开应用与网页"},
    2: {"name": "联网检索", "desc": "联网搜索、抓网页正文"},
    3: {"name": "系统控制", "desc": "音量 / 亮度 / 剪贴板 / 提醒查询"},
    4: {"name": "文件与提醒编辑", "desc": "改文件、设 / 删提醒"},
    5: {"name": "记忆与子任务", "desc": "记事实、删记忆、学黑话、派子任务"},
    6: {"name": "计时提醒", "desc": "倒计时、取消倒计时"},
    7: {"name": "多媒体生成", "desc": "生成图片 / 音频"},
    8: {"name": "高级生成与外部扩展",
        "desc": "生成视频、MCP 外部工具（活动记录 / 文件沙箱）"},
    9: {"name": "通知与脚本", "desc": "发通知、跑脚本"},
    10: {"name": "终极能力", "desc": "杀进程 / 锁屏 / Wi-Fi / 蓝牙"},
}

#: MCP 外部工具单独一组（它们在 yaml 里按前缀登记）
MCP_GROUP = "外部扩展（MCP）"
#: 没在 tool_unlock 里配置的工具（默认 SAFE）
OTHER_GROUP = "其他工具"

#: 分组名 → 能力说明（提示词按组展示时用）
TOOL_GROUP_DESCS: dict[str, str] = {
    **{g["name"]: g["desc"] for g in TOOL_GROUPS.values()},
    MCP_GROUP: "MCP 外部扩展（电脑活动记录 / 文件沙箱）",
    OTHER_GROUP: "未分组的小工具",
}

#: 分组在提示词里的固定顺序（按解锁层级）
_GROUP_ORDER: dict[str, int] = {g["name"]: lvl
                              for lvl, g in TOOL_GROUPS.items()}
_GROUP_ORDER[MCP_GROUP] = 99
_GROUP_ORDER[OTHER_GROUP] = 100


def tool_group_name(name: str) -> str:
    """工具名 → 分组名（按解锁层级对齐）。"""
    from . import _unlock
    if _unlock.is_mcp_tool(name):
        return MCP_GROUP
    lvl = _unlock.level_for_tool(name)
    if lvl is None:
        return OTHER_GROUP
    grp = TOOL_GROUPS.get(lvl)
    return grp["name"] if grp else f"Lv.{lvl} 工具"


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict            # JSON Schema
    fn: Callable[..., str]
    group: str = ""             # Stage 3.4：能力分组（按 tool_unlock 解锁层级对齐）
    # 只经**网关**调用的工具（不原生下发给模型）。
    # playwright 那 25 个工具 schema ≈ 9.5k token → 走 browser 网关按需取。
    gateway_only: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        # Stage 3.1：执行期白名单的基线快照（生产注册表启动时 seal 一次）
        self._baseline: Optional[frozenset[str]] = None
        self._sealed: bool = False

    def register(self, tool: Union[Tool, "_DecoratedTool", Callable]) -> None:
        """注册一个 tool,同时支持:
        - `Tool(name=..., description=..., parameters=..., fn=...)` 老风格
        - `@tool` 装饰后的函数(自动从 type hints 推 parameters)
        - 普通 callable(走 @tool 默认推断,无装饰器)

        2026-10-14: 加 `@tool` 支持,与 `app/engine/tools/_decorator.py` 配套。

        注意:`@tool` 装饰后返回的是「带 .name/.description/.parameters/.fn 属性的
        普通函数」(不是 `_DecoratedTool` 实例,这样 `tool_fn()` 直接调用也 OK)。
        所以检测靠「有没有这三个属性」,不只是 `isinstance(_DecoratedTool)`。
        """
        from ._decorator import _DecoratedTool  # 局部 import 避免循环
        if isinstance(tool, Tool):
            self._tools[tool.name] = tool
            return
        if isinstance(tool, _DecoratedTool):
            self._tools[tool.name] = Tool(
                name=tool.name,
                description=tool.description,
                parameters=tool.parameters,
                fn=tool.fn,
            )
            return
        # @tool 装饰过的函数:有 .name / .parameters / .fn 三个属性
        if (callable(tool)
                and hasattr(tool, "name")
                and hasattr(tool, "parameters")
                and hasattr(tool, "fn")):
            self._tools[tool.name] = Tool(
                name=tool.name,
                description=getattr(tool, "description", ""),
                parameters=tool.parameters,
                fn=tool.fn,
            )
            return
        # 兜底:普通 callable,装饰一下
        from ._decorator import tool as _tool_decorator
        decorated = _tool_decorator(tool)
        self._tools[decorated.name] = Tool(
            name=decorated.name,
            description=decorated.description,
            parameters=decorated.parameters,
            fn=decorated.fn,
        )

    # -- Stage 3.1：执行期白名单 --------------------------------------------
    def seal_baseline(self) -> int:
        """把当前注册表**冻结为生产基线**（Stage 3.1）。

        启动时（brain_controller 建完工具、桥接完 MCP 后）调用一次：
        从这一刻起，白名单按 companion_config.yaml 的 tool_allowlist 段
        严格执行 —— 段里没有的工具**执行期一律拒绝**（不是只弹窗确认）。

        没 seal 过的注册表（单测 / 子 agent 的临时注册表）走「快照兜底」：
        以第一次检查时的注册表内容为准，不受 yaml 约束 —— 否则单测里
        随手注册的假工具会被全部拒掉。

        返回基线里的工具数。
        """
        self._baseline = frozenset(self._tools)
        self._sealed = True
        log.info("工具白名单基线已冻结：%d 个存量工具", len(self._baseline))
        return len(self._baseline)

    def _allowlist_allows(self, name: str, *,
                          capture: bool = True) -> tuple[bool, str]:
        """白名单检查 → (是否放行, 拒绝时的 error_code)。

        三种情形：
          1. 生产注册表（seal 过）+ yaml 有 tool_allowlist 段 → yaml 说了算
             （yaml 里没有的工具一律拒绝）；
          2. 生产注册表 + yaml 没该段 → 基线快照说了算（= 当前注册表全量）
             —— 这就是「初始白名单 = 当前注册表快照」；
          3. 未 seal 的注册表（单测 / 临时注册表）→ 基线快照兜底。

        capture=False：只查不放行（列工具清单时用，不落盘快照）。
        """
        policy = load_tool_allowlist()
        exact: frozenset[str] = frozenset()
        prefixes: tuple[str, ...] = DEFAULT_ALLOWLIST_PREFIXES
        if policy is not None and policy.from_yaml:
            exact = policy.exact
            prefixes = policy.prefixes or prefixes
        if self._sealed:
            if not (policy is not None and policy.from_yaml):
                exact = (self._baseline if self._baseline is not None
                         else frozenset(self._tools))
        else:
            base = self._baseline
            if base is None:
                base = frozenset(self._tools)
                if capture:
                    # 第一次执行 = 基线时刻：此后新增的工具默认拒绝
                    self._baseline = base
            exact = exact | base
        if name in exact:
            return True, ""
        for p in prefixes:
            if p and name.startswith(p):
                return True, ""
        return False, "not_allowed"

    def allowed_names(self) -> list[str]:
        """当前白名单放行的工具名（调试 / 提示词用）。"""
        out = []
        for n in self._tools:
            ok, _ = self._allowlist_allows(n)
            if ok:
                out.append(n)
        return out

    # -- Stage 3.4：工具分组 ----------------------------------------------
    def ensure_groups(self) -> None:
        """给每个 Tool 填 group 字段（按解锁层级对齐）。"""
        for t in self._tools.values():
            if not t.group:
                t.group = tool_group_name(t.name)

    def grouped_names(self, level: int | None = None) -> dict[str, list[str]]:
        """按能力分组返回工具名（提示词按组展示，不再平铺 68 个名字）。

        分组见 TOOL_GROUPS（与 companion_config.yaml 的 tool_unlock 层级对齐）。
        """
        self.ensure_groups()
        groups: dict[str, list[str]] = {}
        for t in self.tools_for_level(level):
            groups.setdefault(t.group, []).append(t.name)
        # 固定顺序（按解锁层级）—— 提示词要字节稳定
        return {name: groups[name]
                for name in sorted(groups, key=lambda n: _GROUP_ORDER.get(n, 50))}

    def names(self, level: int | None = None) -> list[str]:
        """返回工具名列表。level=None 时返全部;否则按 yaml tool_unlock 过滤。

        MCP 外部工具(名以 mcp_ 前缀)统一按 MCP_DEFAULT_LEVEL 判定。
        """
        if level is None:
            return list(self._tools)
        from . import _unlock
        configured, mcp_names = _unlock.split_configured_and_mcp(list(self._tools))
        unlocked = _unlock.compute_unlocked(level, mcp_names=mcp_names)
        return [n for n in self._tools if n in unlocked
                and not getattr(self._tools[n], "gateway_only", False)]

    def tools_for_level(self, level: int | None = None) -> list["Tool"]:
        """返 Tool 对象列表(供 LLM tool_spec 拼接),过滤逻辑同 names(level)。

        Stage 3.1：白名单外的工具**不下发**给模型（执行期还会再拦一次，
        模型硬点名也一样被拒）。
        """
        if level is None:
            tools = list(self._tools.values())
        else:
            names_set = set(self.names(level))
            tools = [t for t in self._tools.values() if t.name in names_set]
        return [t for t in tools
                if self._allowlist_allows(t.name, capture=False)[0]]

    def to_openai_for_level(self, level: int) -> list[dict]:
        """按等级过滤的 OpenAI tools 格式(供 LLM tool 字段用)。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self.tools_for_level(level)
        ]

    def to_openai(self) -> list[dict]:
        """转成 OpenAI tools 字段格式。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def _signature_hint(self, tool: Tool) -> str:
        """从 JSON Schema 生成「本工具接受哪些参数」，用于参数错误时告诉模型怎么改。"""
        props = (tool.parameters or {}).get("properties", {}) or {}
        required = (tool.parameters or {}).get("required", []) or []
        if not props:
            return ""
        parts = []
        for key in props:
            parts.append(f"{key}（必填）" if key in required else f"{key}（可选）")
        return "、".join(parts)

    def execute(self, name: str, arguments: str | dict) -> ToolResult:
        """执行一个工具调用，返回**结构化结果信封**（Stage 3.2）。

        返回：{"status": "ok"|"error"|"denied", "data": str,
                "truncated": bool, "error_code": str}

        三道闸的顺序（任一不过都**不执行**工具）：
          1. 白名单闸（Stage 3.1）—— 未登记的工具一律拒绝；
          2. 等级闸（tool_unlock）—— 等级不够就拒绝；
          3. 危险工具人工确认在 agent 层（app/brain/agent.py）。
        """
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult("error", f"错误：未知工具 {name}",
                              error_code="not_found")
        # 第三道闸：执行期白名单（等级闸 / 危险工具确认之外的一道）
        allowed, why = self._allowlist_allows(name)
        if not allowed:
            log.warning("工具 %s 不在白名单内，执行期拒绝", name)
            return ToolResult("denied",
                              f"错误：工具 {name} 未登记到白名单，已拒绝执行。",
                              error_code=why)
        # 解锁闸:ToolRegistry 自带默认 gate,避免 LangChain 后端漏拦
        from . import _unlock
        if _unlock.is_loaded():
            current_lvl = _get_current_tool_level()
            if not _unlock.is_unlocked(name, current_lvl):
                req = _unlock.level_for_tool(name) or 1
                return ToolResult("denied",
                                  (f"错误：{name} 需要升级到 Lv.{req} 才能使用。"
                                   f"当前等级 Lv.{current_lvl}。请告诉主人升级到对应等级后再试，"
                                   "或在聊天窗口顶部点「工具解锁」查看进度。"),
                                  error_code="level_locked")
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        except json.JSONDecodeError as e:
            return ToolResult("error", f"错误：参数不是合法 JSON（{e}）",
                              error_code="bad_args")
        if not isinstance(args, dict):
            return ToolResult("error",
                              f"错误：参数必须是 JSON 对象，收到 {type(args).__name__}",
                              error_code="bad_args")
        try:
            raw = tool.fn(**args)
        except TypeError as e:
            # 不把 TypeError 原文回给模型：里面带 register.<locals> 这类内部信息，
            # 对模型毫无意义。改成给出「本工具接受哪些参数」，模型才知道怎么改。
            log.debug("工具 %s 参数不匹配: %s", name, e)
            hint = self._signature_hint(tool)
            msg = f"错误：{name} 的参数不匹配。"
            if hint:
                msg += f"它接受的参数：{hint}。"
            msg += "请按上述参数名重新调用。"
            return ToolResult("error", msg, error_code="bad_args")
        except Exception as e:  # noqa: BLE001
            log.exception("工具 %s 执行失败", name)
            return ToolResult("error", f"错误：{e}", error_code="tool_error")
        text = raw if isinstance(raw, str) else str(raw)
        # 工具层判定成败 → 稳定错误码（agent 不再猜关键词）
        status, code = classify_tool_result(name, text)
        # 统一后置裁剪（Stage 3.3）：超预算保头保尾 + 省略标记
        data, truncated = trim_tool_result(text)
        return ToolResult(status, data, truncated, code)



__all__ = [
    "Tool", "ToolRegistry", "ToolResult", "is_under_any",
    "AllowlistPolicy", "load_tool_allowlist", "reload_tool_allowlist",
    "classify_tool_result", "classify_legacy_result",
    "trim_tool_result", "fit_tool_payload",
    "wrap_tool_fn", "ensure_failure_prefix",
    "tool_group_name", "TOOL_GROUPS", "MCP_GROUP", "OTHER_GROUP",
    "DEFAULT_ALLOWLIST_PREFIXES",
    "TOOL_RESULT_MAX_CHARS", "TOOL_RESULT_HEAD_CHARS", "TOOL_RESULT_TAIL_CHARS",
    "TOOL_CALL_TIMEOUT_SECONDS", "TOOL_PHASE_BUDGET_SECONDS",
]


# ---- 工具解锁:当前等级读取器 ----
# 主程序通过 set_tool_filter_level_provider() 注入,默认 Lv10 (全部可见)。
_TOOL_LEVEL_PROVIDER = lambda: 10  # noqa: E731


def set_tool_filter_level_provider(fn) -> None:
    """注入当前桌宠等级读取函数,供 ToolRegistry.execute 解锁闸使用。"""
    global _TOOL_LEVEL_PROVIDER
    _TOOL_LEVEL_PROVIDER = fn


def _get_current_tool_level() -> int:
    """读注入的等级读取器,异常 / 越界时钳到 1..10。"""
    try:
        lvl = int(_TOOL_LEVEL_PROVIDER())
    except Exception:  # noqa: BLE001
        return 10
    return max(1, min(10, lvl))