# -*- coding: utf-8 -*-
"""Chromium(WebEngine) 启动参数配置：主程序与测量脚本共用同一份。

为什么单独抽出来：
* ``QTWEBENGINE_CHROMIUM_FLAGS`` 只在 **QApplication 创建前** 生效，
  主程序（app/main.py）和测量脚本（scripts/measure_live2d_cost.py）
  必须用完全一致的 flag，否则测出来的数没有可比性。
* 实测（Windows / RTX 4060）：不启用 GPU 加速时 Chromium 走 SwiftShader 软渲染，
  桌宠一启动 CPU 就冲高、GPU 几乎不动；强制 ANGLE + D3D11 后主进程与
  QtWebEngineProcess 子进程 CPU 都降下来。

必须在 import PyQtWebEngine / 创建 QApplication 之前调用
``configure_webengine_env()``。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = [
    "detect_nvidia_gpu",
    "chromium_flags",
    "configure_webengine_env",
]


def detect_nvidia_gpu() -> bool:
    """检测系统是否有 NVIDIA 独立 GPU。

    强启 GPU 加速可能让无 GPU / 集显机器渲染崩，所以只对检测到 NVIDIA
    驱动的机器加 --ignore-gpu-blacklist（绕过 Chromium 黑名单）。
    其他情况让 Chromium 默认决策。
    """
    if sys.platform != "win32":
        return True    # macOS / Linux 默认有 GPU
    # 1) NVIDIA 控制面板 / 驱动目录存在？
    for prefix_var in ("ProgramW6432", "ProgramFiles"):
        prefix = os.environ.get(prefix_var)
        if prefix and (Path(prefix) / "NVIDIA Corporation").exists():
            return True
    # 2) NVIDIA 驱动 DLL 存在？
    sys_root = os.environ.get("SystemRoot", r"C:\Windows")
    if (Path(sys_root) / "System32" / "nvapi64.dll").exists():
        return True
    return False


def chromium_flags(trim: bool = True) -> str:
    """本次进程使用的 Chromium 启动 flag 串。

    GPU 相关：
    * ``--use-gl=angle --use-angle=gl-d3d11``：Chromium 在 Windows 上最稳的
      GL 路径（DirectX 11 桥）。
    * ``--ignore-gpu-blacklist``：仅 NVIDIA 机器加（绕过 Chromium 误判）。
    * ``--enable-gpu-rasterization``：2D canvas 也走 GPU 合成（pixi 用 canvas）。
    * ``--enable-zero-copy``：避免 WebView 复制帧到主进程。

    瘦身 flag（降低 Chromium 进程数 / 内存，不影响 Live2D 渲染）：
    * ``--disable-breakpad``：不拉起 crashpad 崩溃收集子进程。
    * ``--disable-background-networking``：关掉后台网络请求（组件更新等）。
    * ``--disable-component-update`` / ``--disable-domain-reliability``：
      关掉无用的后台子系统。
    * ``--disable-sync`` / ``--no-pings`` / ``--no-default-browser-check``：
      本地页面用不到这些。
    * ``--disable-features=Translate,MediaRouter,...``：关掉翻译 / 媒体路由。
    * ``--renderer-process-limit=1``：本场景只有一个页面，别多开渲染进程。
    """
    flags = "--use-gl=angle --use-angle=gl-d3d11"
    if detect_nvidia_gpu():
        flags += " --ignore-gpu-blacklist"
    flags += " --enable-gpu-rasterization --enable-zero-copy"
    if not trim:
        return flags
    # ---- Chromium 瘦身（不影响 Live2D 渲染，只砍后台子系统 / 多余进程）----
    flags += (
        " --disable-breakpad"
        " --disable-background-networking"
        " --disable-component-update"
        " --disable-domain-reliability"
        " --disable-sync"
        " --no-pings"
        " --no-default-browser-check"
        " --disable-features=Translate,MediaRouter,OptimizationHints,"
        "AcceptCHFrame,InterestFeedContentSuggestions"
        " --renderer-process-limit=1"
    )
    return flags


def configure_webengine_env(env: dict | None = None, trim: bool = True) -> str:
    """把 Chromium flag 写进环境变量（已存在则不覆盖），返回最终 flag 串。"""
    target = os.environ if env is None else env
    flags = chromium_flags(trim=trim)
    target.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", flags)
    return target.get("QTWEBENGINE_CHROMIUM_FLAGS", flags)
