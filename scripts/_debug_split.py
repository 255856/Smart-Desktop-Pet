"""Debug: 重现切割过程，找出 execs 为什么没切掉。"""
from pathlib import Path

src = Path('app/ui/chat_window_legacy.py.bak').read_text(encoding='utf-8')

cuts = [
    ('DANGEROUS',
     '# 危险工具的中文标签（弹窗里给主人看）\n',
     '_CONFIRM_TIMEOUT_S = 60.0\n',
     '# 危险\nfrom .utils\n'),
    ('TOOL_UI',
     '# 工具名 -> (中文名, 动作目标参数键，按优先级)\n',
     'def _tool_status(result: str) -> str:\n    r = result or ""\n    if "取消" in r:\n        return "cancel"\n    if any(k in r for k in _TOOL_FAIL_MARKERS):\n        return "fail"\n    return "ok"\n',
     '# 工具 UI\nfrom .utils(\n'),
    ('Message',
     '@dataclass\nclass Message:\n',
     '    execs: list[dict] = field(default_factory=list)  # 命令执行：{cmd, output, exit_code}\n',
     '# Message\nfrom .message\n'),
    ('AgentWorker',
     'class _AgentWorker(QThread):\n',
     'class _StreamWorker(QThread):\n',
     ''),
    ('StreamWorker',
     'class _StreamWorker(QThread):\n',
     '#  / 命令补全（QPlainTextEdit 没有 setCompleter，自己写一个轻量版）\n\n\ndef _html_escape(text: str) -> str:\n',
     '# worker\nfrom .worker\n'),
    ('html',
     'def _html_escape(text: str) -> str:\n',
     'class _CommandCompleter(QObject):\n',
     '# html\nfrom .utils\n'),
    ('CommandCompleter',
     'class _CommandCompleter(QObject):\n',
     'class ChatWindow(QWidget):\n',
     '# completer + chrome\nfrom .command_completer\nfrom .chrome\n'),
]


def cut(src, start, end, repl):
    s = src.find(start)
    if s < 0:
        return src, f'START NOT FOUND: {start[:40]!r}'
    e = src.find(end, s + len(start))
    if e < 0:
        return src, f'END NOT FOUND: {end[:40]!r}'
    return src[:s] + repl + src[e + len(end):], f'OK [{s},{e+len(end)})'


for name, s_marker, e_marker, repl in reversed(cuts):
    new_src, msg = cut(src, s_marker, e_marker, repl)
    print(f'{name}: {msg}')
    src = new_src

# 看 from .message 位置
idx = src.find('from .message')
if idx >= 0:
    print('---')
    print(repr(src[idx:idx+200]))
else:
    print('--- no from .message in result')