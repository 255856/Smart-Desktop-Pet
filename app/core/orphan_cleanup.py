# -*- coding: utf-8 -*-
"""启动时清理**孤儿进程**（上一轮桌宠没清干净的）。

为什么需要
----------
桌宠的语音功能会拉起 RVC 的推理进程（§infer.cli§ / §rvc_sing.py§）。正常情况下
它们是短命的子进程（合成完就退），但如果桌宠被杀 / 断电 / 强制关窗口，它们会
**变成孤儿继续占着内存** —— 实测见过一个卡死的 §infer.cli§ 占 **4.2 GB**，
而且它用的是 PATH 上另一个解释器（跑不了 RVC 依赖）→ 永远不退 → 越攒越多。

所以：**桌宠启动时把这类"不该活过本轮"的进程清掉**。

安全边界（很重要）
------------------
只杀**命令行匹配我们自己的 RVC/语音辅助脚本**的 python 进程：
- §infer.cli§（RVC 音转音）
- §rvc_sing.py§（按旋律唱）

**绝不碰**：
- §api_v2.py§（GPT-SoVITS TTS 服务 —— 那是常驻服务，主人自己也在用）
- 桌宠自己的进程、记忆 sidecar、MCP server
- 任何不是 python 的进程
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Iterable, List

log = logging.getLogger(__name__)

#: 只清这些 —— 它们都是「一次调用内的短命子进程」，不该活过桌宠那一轮。
_ORPHAN_PATTERNS = ("infer.cli", "rvc_sing.py", "rvc_fetch.py")

#: 这些**不能**碰（常驻服务 / 基础设施）。
_PROTECT_PATTERNS = ("api_v2.py", "memory_sidecar", "app.mcp.", "main.py", "pytest")


def _iter_python_processes() -> Iterable[tuple[int, str]]:
    """产出 (pid, command_line)。拿不到就返回空（Windows 优先用 CIM）。"""
    if sys.platform == "win32":
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                 "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=15,
            ).stdout
        except Exception as e:  # noqa: BLE001
            log.debug("枚举进程失败（跳过清理）: %s", e)
            return
        for line in out.splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            pid_s, _, cmd = line.partition("|")
            try:
                yield int(pid_s), cmd
            except ValueError:
                continue
        return
    # 非 Windows：ps
    try:
        out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_s, _, cmd = line.partition(" ")
        try:
            yield int(pid_s), cmd
        except ValueError:
            continue


def find_orphans() -> List[tuple[int, str]]:
    """找出该清的孤儿进程（不杀）。"""
    me = os.getpid()
    out: List[tuple[int, str]] = []
    for pid, cmd in _iter_python_processes():
        if pid == me or not cmd:
            continue
        low = cmd.lower()
        if any(p in low for p in _PROTECT_PATTERNS):
            continue
        if any(p.lower() in low for p in _ORPHAN_PATTERNS):
            out.append((pid, cmd))
    return out


def sweep_orphan_processes() -> List[int]:
    """杀掉孤儿进程，返回被杀掉的 pid 列表。

    杀不掉不算失败（权限/已退出）—— 只是记一条日志。
    """
    killed: List[int] = []
    for pid, cmd in find_orphans():
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               capture_output=True, timeout=15)
            else:
                os.kill(pid, 9)
            killed.append(pid)
            log.info("清理孤儿进程 %s: %s", pid, cmd[:90])
        except Exception as e:  # noqa: BLE001
            log.debug("清理孤儿 %s 失败: %s", pid, e)
    if killed:
        log.info("启动清理：清掉 %d 个上一轮遗留的 RVC/语音孤儿进程", len(killed))
    return killed


__all__ = ["find_orphans", "sweep_orphan_processes"]
