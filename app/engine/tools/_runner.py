"""run_script：在沙箱目录运行本地脚本（.py / .ps1 / .bat / .sh）。危险工具。"""
from __future__ import annotations

import logging
import platform
import subprocess
from pathlib import Path

from ._core import Tool, ToolRegistry, is_under_any

log = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"

# 沙箱：仅允许在 _HOME 目录树下运行脚本（家目录本身 + ~/scripts/）
_HOME = Path.home()


def _sandbox_roots() -> list[Path]:
    """沙箱根目录（每次调用时读 _HOME，便于测试注入临时家目录）。"""
    return [_HOME, _HOME / "scripts"]


def _is_under_sandbox(p: Path) -> bool:
    """脚本路径是否在沙箱内。

    用 relative_to 做路径语义比较；早先的 str.startswith 会让
    C:\\Users\\alice-backup\\x.py 通过 C:\\Users\\alice 的沙箱检查。
    """
    return is_under_any(p, _sandbox_roots())


def _command_for(script: Path) -> list[str]:
    """根据扩展名构造执行命令。"""
    ext = script.suffix.lower()
    if ext == ".py":
        return ["python", str(script)]
    if ext == ".ps1":
        return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    if ext in (".bat", ".cmd"):
        return ["cmd", "/c", str(script)]
    if ext == ".sh":
        return ["bash", str(script)]
    # 默认：尝试当可执行文件直接跑
    return [str(script)]


def register(reg: ToolRegistry) -> None:
    """注册脚本执行工具。"""

    def run_script(script_path: str, timeout_s: int = 60, args: str = "") -> str:
        """执行本地脚本（沙箱限定家目录）。危险工具，需用户确认。
        支持 .py / .ps1 / .bat / .sh。
        args 是字符串，会用 shlex 切分（空格分隔的引号参数）。
        """
        if not script_path or not script_path.strip():
            return "错误：script_path 不能为空"
        p = Path(script_path).expanduser()
        if not p.exists():
            return f"脚本不存在：{p}"
        if not p.is_file():
            return f"不是文件：{p}"
        if not _is_under_sandbox(p):
            return (f"安全策略拒绝：脚本必须在用户主目录下。"
                    f"路径 {p} 在沙箱外。")
        cmd = _command_for(p)
        if args.strip():
            import shlex
            try:
                # posix=False：Windows 上必须。POSIX 模式会把反斜杠当转义符，
                # 实测 shlex.split(r'C:\Users\test\a.txt') → ['C:Userstesta.txt']，
                # 路径被静默改坏而脚本只看到「找不到文件」。
                extra = shlex.split(args, posix=False)
            except ValueError as e:
                return f"错误：args 解析失败：{e}"
            cmd.extend(extra)
        try:
            timeout = max(1, min(int(timeout_s if timeout_s else 60), 300))
        except (TypeError, ValueError):
            return f"错误：timeout_s 必须是数字，收到 {timeout_s!r}"
        log.info("run_script: %s (timeout=%ds)", cmd, timeout)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                # 不指定就用系统 locale（中文 Windows 是 cp936），脚本只要吐一点
                # UTF-8（emoji / 某些路径）就会 UnicodeDecodeError 让整个工具失败。
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                cwd=str(p.parent),
            )
        except subprocess.TimeoutExpired:
            return f"错误：执行超时（>{timeout}s）"
        except Exception as e:  # noqa: BLE001
            log.exception("run_script failed")
            return f"错误：{e}"
        out = proc.stdout or ""
        err = proc.stderr or ""
        if len(out) > 4000:
            out = out[:4000] + f"\n... (截断，原 {len(out)} 字)"
        if len(err) > 2000:
            err = err[:2000] + f"\n... (截断，原 {len(err)} 字)"
        status = "OK" if proc.returncode == 0 else f"exit={proc.returncode}"
        body = (f"[{status}] {p.name}\n--- stdout ---\n{out}\n"
                f"--- stderr ---\n{err}").rstrip()
        if proc.returncode != 0:
            # 非零退出码必须显式标成失败：原实现只返回 "[exit=1] …"，
            # 既没有「错误」也没有任何失败关键词，agent 的矛盾检测完全
            # 拦不住，模型会照着 stdout 报「脚本已执行完成」。
            return f"错误：脚本执行失败（退出码 {proc.returncode}），\n{body}"
        return body

    reg.register(Tool(name="run_script",
        description="在用户主目录下的脚本执行本地脚本（.py / .ps1 / .bat / .sh）。"
                    "危险工具，需用户确认。仅沙箱目录：家目录、~/scripts。",
        parameters={"type": "object",
                    "properties": {
                        "script_path": {"type": "string",
                                        "description": "脚本路径"},
                        "timeout_s": {"type": "integer", "minimum": 1, "maximum": 300,
                                      "description": "超时秒数，默认 60"},
                        "args": {"type": "string",
                                 "description": "传给脚本的参数，shlex 切分"},
                    },
                    "required": ["script_path"]},
        fn=run_script))


__all__ = ["register"]