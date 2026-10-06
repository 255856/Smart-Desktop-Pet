"""精简版：用 Python 字符串索引精确切割 chat_window_legacy.py.bak。

每个 cut 用 (start_marker, end_marker) 在文件中定位。
先备份 → 切割 → 输出。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEGACY = ROOT / "app" / "ui" / "chat_window_legacy.py.bak"
OUT = ROOT / "app" / "ui" / "chat_window" / "__init__.py"

src = LEGACY.read_text(encoding="utf-8")

# 切割点：每段 [start_pos_inclusive, end_pos_exclusive)
# 用「精确字符序列」作为锚点，避免歧义

cuts = [
    # 1) _DANGEROUS_LABELS 字典
    {
        "name": "DANGEROUS_LABELS",
        "start": '# 危险工具的中文标签（弹窗里给主人看）\n',
        "end": '_CONFIRM_TIMEOUT_S = 60.0\n',
        "repl": '# 危险工具的中文标签（弹窗里给主人看）+ 等待上限 — 拆到 chat_window.utils\nfrom .utils import _DANGEROUS_LABELS, _CONFIRM_TIMEOUT_S  # noqa: F401\n',
    },
    # 2) _TOOL_UI_META + 工具函数块
    {
        "name": "TOOL_UI_META_block",
        "start": '# 工具名 -> (中文名, 动作目标参数键，按优先级)\n',
        "end": 'def _tool_status(result: str) -> str:\n    r = result or ""\n    if "取消" in r:\n        return "cancel"\n    if any(k in r for k in _TOOL_FAIL_MARKERS):\n        return "fail"\n    return "ok"\n',
        "repl": (
            '# 工具 UI 元数据 + 工具结果状态 + 工具元组解包 — 拆到 chat_window.utils\n'
            'from .utils import (  # noqa: F401\n'
            '    _TOOL_UI_META, _CIRCLED_NUMS, _TOOL_FAIL_MARKERS,\n'
            '    _unpack_tool, _tool_label, _tool_status,\n'
            ')\n'
        ),
    },
    # 3) Message dataclass
    {
        "name": "Message",
        "start": '@dataclass\nclass Message:\n',
        "end": '    execs: list[dict] = field(default_factory=list)  # 命令执行：{cmd, output, exit_code}\n',
        "repl": (
            '# Message — 拆到 chat_window.message\n'
            'from .message import Message  # noqa: F401\n'
        ),
    },
    # 4) _AgentWorker 类
    {
        "name": "_AgentWorker",
        "start": 'class _AgentWorker(QThread):\n',
        "end": 'class _StreamWorker(QThread):\n',
        "repl": '',
    },
    # 5) _StreamWorker 类
    {
        "name": "_StreamWorker",
        "start": 'class _StreamWorker(QThread):\n',
        "end": 'def _html_escape(text: str) -> str:\n',
        "repl": (
            '# _AgentWorker / _StreamWorker — 拆到 chat_window.worker\n'
            'from .worker import _AgentWorker, _StreamWorker  # noqa: F401\n'
        ),
    },
    # 6) _html_escape + _format_time_short
    {
        "name": "html_escape_format_time",
        "start": 'def _html_escape(text: str) -> str:\n',
        "end": 'class _CommandCompleter(QObject):\n',
        "repl": (
            '# _html_escape / _format_time_short — 拆到 chat_window.utils\n'
            'from .utils import _html_escape, _format_time_short  # noqa: F401\n'
        ),
    },
    # 7) _CommandCompleter
    {
        "name": "_CommandCompleter",
        "start": 'class _CommandCompleter(QObject):\n',
        "end": 'class _TitleBarDrag(QObject):\n',
        "repl": (
            '# _CommandCompleter — 拆到 chat_window.command_completer\n'
            'from .command_completer import _CommandCompleter  # noqa: F401\n'
        ),
    },
    # 8) _TitleBarDrag + _InputFocusWatcher
    {
        "name": "_TitleBarDrag_and_watcher",
        "start": 'class _TitleBarDrag(QObject):\n',
        "end": 'class ChatWindow(QWidget):\n',
        "repl": (
            '# _TitleBarDrag / _InputFocusWatcher — 拆到 chat_window.chrome\n'
            'from .chrome import _TitleBarDrag, _InputFocusWatcher  # noqa: F401\n'
        ),
    },
]

# 计算每个 cut 的 (start_pos, end_pos_exclusive)，按 start_pos 倒序处理（避免位置偏移）
positions = []
for cut_def in cuts:
    s = src.find(cut_def["start"])
    if s < 0:
        print(f"ERROR [{cut_def['name']}]: start not found: {cut_def['start'][:60]!r}")
        continue
    e = src.find(cut_def["end"], s + len(cut_def["start"]))
    if e < 0:
        print(f"ERROR [{cut_def['name']}]: end not found: {cut_def['end'][:60]!r}")
        continue
    positions.append((s, e, cut_def["repl"]))
    print(f"OK [{cut_def['name']}]: {s}-{e} (size {e-s})")

# 倒序处理
positions.sort(key=lambda x: -x[0])
for s, e, repl in positions:
    src = src[:s] + repl + src[e:]

# 替换头部 docstring
src = re.sub(
    r'^"""聊天窗口：把桌宠和大模型对话接到一起。"""\n',
    (
        '"""聊天窗口：把桌宠和大模型对话接到一起。\n\n'
        '本模块是包 `chat_window/`，主类 ChatWindow 在此。其余辅助代码拆分到子模块：\n'
        '    chat_window.message            - 聊天消息 dataclass (Message)\n'
        '    chat_window.utils              - 常量、HTML 转义、工具元数据\n'
        '    chat_window.worker             - _AgentWorker / _StreamWorker (QThread)\n'
        '    chat_window.command_completer  - _CommandCompleter（/命令弹窗）\n'
        '    chat_window.chrome             - _TitleBarDrag / _InputFocusWatcher\n\n'
        '向后兼容：`from app.ui.chat_window import ChatWindow, Message, _AgentWorker, ...`\n'
        '全部继续工作（re-export）。\n'
        '"""\n'
        'from __future__ import annotations\n'
    ),
    src,
    count=1,
)

OUT.write_text(src, encoding="utf-8")
print(f"Wrote {OUT}: {len(src.split(chr(10)))} lines")