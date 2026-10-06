"""一次性脚本：精确切分 chat_window_legacy.py.bak。

关键修复：
    - 所有切割都用 Python rfind 找**最后一个**匹配的 end_marker，避免类内同名干扰。
    - 每个切割保留 ChatWindow 起始行（用 `\n\n\n` 作为 end，即空行结束）。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LEGACY = ROOT / "app" / "ui" / "chat_window_legacy.py.bak"
OUT = ROOT / "app" / "ui" / "chat_window" / "__init__.py"

src = LEGACY.read_text(encoding="utf-8")

# 注意 end_marker 都是「切割区间最后一个字符」的字符串。
# 用 rfind 而非 find 避免同类内同名干扰。
cuts_def = [
    {
        "name": "DANGEROUS_LABELS",
        "start": '# 危险工具的中文标签（弹窗里给主人看）\n',
        "end": '_CONFIRM_TIMEOUT_S = 60.0\n',
        "repl": '# 危险工具的中文标签（弹窗里给主人看）+ 等待上限 — 拆到 chat_window.utils\nfrom .utils import _DANGEROUS_LABELS, _CONFIRM_TIMEOUT_S  # noqa: F401\n',
    },
    {
        "name": "TOOL_UI_META",
        "start": '# 工具名 -> (中文名, 动作目标参数键，按优先级)\n',
        "end": '    return "ok"\n',
        "repl": (
            '# 工具 UI 元数据 + 工具结果状态 + 工具元组解包 — 拆到 chat_window.utils\n'
            'from .utils import (  # noqa: F401\n'
            '    _TOOL_UI_META, _CIRCLED_NUMS, _TOOL_FAIL_MARKERS,\n'
            '    _unpack_tool, _tool_label, _tool_status,\n'
            ')\n'
        ),
    },
    {
        "name": "Message",
        "start": '@dataclass\nclass Message:\n',
        "end": '    execs: list[dict] = field(default_factory=list)  # 命令执行：{cmd, output, exit_code}\n',
        "repl": (
            '# Message — 拆到 chat_window.message\n'
            'from .message import Message  # noqa: F401\n'
        ),
    },
    {
        "name": "AgentWorker_StreamWorker",
        "start": 'class _AgentWorker(QThread):\n',
        "end": '            self.failed.emit(f"调用出错：{e!r}")\n',
        "repl": (
            '# _AgentWorker / _StreamWorker — 拆到 chat_window.worker\n'
            'from .worker import _AgentWorker, _StreamWorker  # noqa: F401\n'
        ),
    },
    {
        "name": "html_format_time",
        "start": 'def _html_escape(text: str) -> str:\n',
        "end": '    except Exception:\n        return ""\n\n\n',
        "repl": (
            '# _html_escape / _format_time_short — 拆到 chat_window.utils\n'
            'from .utils import _html_escape, _format_time_short  # noqa: F401\n'
        ),
    },
    # 3 个分离切割，每个都有唯一 end marker
    {
        "name": "_CommandCompleter",
        "start": 'class _CommandCompleter(QObject):\n',
        "end": '        cursor.insertText(full + rest)\n        self.edit.setTextCursor(cursor)\n',
        "repl": (
            '# _CommandCompleter — 拆到 chat_window.command_completer\n'
            'from .command_completer import _CommandCompleter  # noqa: F401\n'
        ),
    },
    {
        "name": "_TitleBarDrag",
        "start": 'class _TitleBarDrag(QObject):\n',
        "end": '        if t == QEvent.Type.MouseButtonRelease:\n            self._dragging = False\n            return True\n        return False\n',
        "repl": (
            '# _TitleBarDrag — 拆到 chat_window.chrome\n'
            'from .chrome import _TitleBarDrag  # noqa: F401\n'
        ),
    },
    {
        "name": "_InputFocusWatcher",
        "start": 'class _InputFocusWatcher(QObject):\n',
        # end 不切 class ChatWindow 那一行 —— 切到 `return False\n\n\n` 即 _InputFocusWatcher 类的最后 + 空行
        "end": '            self._container.setStyleSheet("")\n        return False\n\n\n',
        "repl": (
            '# _InputFocusWatcher — 拆到 chat_window.chrome\n'
            'from .chrome import _InputFocusWatcher  # noqa: F401\n'
        ),
    },
]

positions = []
for cut_def in cuts_def:
    s = src.find(cut_def["start"])
    if s < 0:
        raise SystemExit(f"[{cut_def['name']}] START not found: {cut_def['start'][:50]!r}")
    # rfind 找最后一个匹配（避开类内同名干扰）
    e = src.rfind(cut_def["end"], s + len(cut_def["start"]))
    if e < 0:
        raise SystemExit(f"[{cut_def['name']}] END not found: {cut_def['end'][:50]!r}")
    e_full = e + len(cut_def["end"])
    positions.append((s, e_full, cut_def["repl"], cut_def["name"]))
    print(f"OK [{cut_def['name']}]: {s}-{e_full} (size {e_full - s})")

positions.sort(key=lambda x: x[0])
# 检查重叠
for i in range(len(positions) - 1):
    if positions[i][1] > positions[i + 1][0]:
        raise SystemExit(f"重叠: {positions[i][3]} end={positions[i][1]} vs {positions[i+1][3]} start={positions[i+1][0]}")

# 一次性构造新 src
parts = []
prev_end = 0
for s, e, repl, _name in positions:
    parts.append(src[prev_end:s])
    parts.append(repl)
    prev_end = e
parts.append(src[prev_end:])
new_src = "".join(parts)

# 替换头部 docstring
new_src = re.sub(
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
    new_src,
    count=1,
)

OUT.write_text(new_src, encoding="utf-8")
print(f"Wrote {OUT}: {len(new_src.split(chr(10)))} lines")