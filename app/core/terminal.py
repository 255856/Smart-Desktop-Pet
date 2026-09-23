"""终端彩色横幅：分阶段展示启动进度。"""
from __future__ import annotations

import os
import sys


class Banner:
    """启动横幅：title + section + ok/info/warn/fail。"""

    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BLUE = "\033[94m"
    GRAY = "\033[90m"
    BOLD = "\033[1m"
    RESET = "\033[0m"

    def __init__(self, enabled: bool = True):
        self.enabled = enabled and sys.stdout.isatty()
        # Windows Terminal / 现代 PowerShell 支持 ANSI
        if os.name == "nt":
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x4
                kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
            except (OSError, AttributeError):
                self.enabled = False

    def _c(self, color: str, text: str) -> str:
        return f"{color}{text}{self.RESET}" if self.enabled else text

    def title(self, char_name: str, app_tag: str = "v3.0 · Multi-Agent Desktop Companion") -> None:
        bar = "═" * 60
        print()
        print(self._c(self.CYAN + self.BOLD, f"╔{bar}╗"))
        print(self._c(self.CYAN + self.BOLD, f"║{'🐳 桌面宠物 · ' + char_name:^60}║"))
        print(self._c(self.CYAN + self.BOLD, f"╚{bar}╝"))
        print(self._c(self.GRAY, f"  {app_tag}\n"))

    def section(self, title: str) -> None:
        print(self._c(self.BLUE + self.BOLD, f"▶ {title}"))

    def ok(self, label: str, detail: str = "") -> None:
        icon = self._c(self.GREEN, "✓")
        print(f"  {icon} {label}" + (self._c(self.GRAY, f"  · {detail}") if detail else ""))

    def info(self, label: str, detail: str = "") -> None:
        icon = self._c(self.BLUE, "·")
        print(f"  {icon} {label}" + (self._c(self.GRAY, f"  · {detail}") if detail else ""))

    def warn(self, label: str, detail: str = "") -> None:
        icon = self._c(self.YELLOW, "!")
        print(f"  {icon} {label}" + (self._c(self.GRAY, f"  · {detail}") if detail else ""))

    def fail(self, label: str, detail: str = "") -> None:
        icon = self._c(self.RED, "✗")
        print(f"  {icon} {label}" + (self._c(self.GRAY, f"  · {detail}") if detail else ""))

    def done(self, msg: str = "") -> None:
        if msg:
            print(self._c(self.GREEN + self.BOLD, f"\n✓ {msg}\n"))
        else:
            print()
