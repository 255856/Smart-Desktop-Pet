"""系统音量和亮度（Windows 实现 + 其他平台的降级）。"""
from __future__ import annotations

import logging
import os
import platform

from ._core import Tool, ToolRegistry

log = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"


def _set_volume_windows(level: float) -> str:
    """0.0~1.0。Windows 用 pycaw；若没装则尝试 ctypes 直接调 IAudioEndpointVolume。"""
    level = max(0.0, min(1.0, level))
    try:
        from ctypes import cast, POINTER, c_float
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        device = AudioUtilities.GetSpeakers()
        interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        volume.SetMasterVolumeLevelScalar(level, None)
        return f"音量已调到 {int(level * 100)}%"
    except ImportError:
        log.warning("pycaw 未安装，音量调节回退到静音切换")
        return _mute_via_mmsysvol_windows(level)
    except Exception as e:  # noqa: BLE001
        log.exception("音量调节失败")
        return f"错误：{e}"


def _get_volume_windows() -> str:
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        device = AudioUtilities.GetSpeakers()
        interface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        scalar = volume.GetMasterVolumeLevelScalar()
        return f"当前音量 {int(scalar * 100)}%"
    except Exception as e:  # noqa: BLE001
        log.debug("get_volume failed: %s", e)
        return "无法读取系统音量"


def _mute_via_mmsysvol_windows(level: float) -> str:
    """无 pycaw 的 fallback：只支持 0/1 静音切换。

    注意：非 0 音量**并没有真正被设置**。早先这里直接返回
    「已取消静音（不支持细粒度音量调节）」，看起来像成功，
    于是 set_volume(50) 会让桌宠对主人说「音量已调到 50%」，
    实际只是取消了静音。必须如实说明没设成。
    """
    import ctypes
    mute = 1 if level == 0 else 0
    ctypes.windll.winmm.waveOutSetVolume(0, 0 if mute else 0xFFFFFFFF)
    if mute:
        return "已静音（音量 0%）"
    return (f"无法设置音量到 {int(level * 100)}%：缺少 pycaw，当前只能静音/取消静音，"
            f"已取消静音。安装 pycaw 后可精确调节。")


def _set_brightness_windows(level: int) -> str:
    level = max(0, min(100, level))
    try:
        import subprocess
        # PowerShell 调 WMI：Brightness
        ps = (
            f"$b = Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightnessMethods; "
            f"$b[0].wmiSetBrightness(1, {level})"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=5, check=False,
        )
        return f"亮度已调到 {level}%"
    except Exception as e:  # noqa: BLE001
        log.exception("调亮度失败")
        return f"错误：{e}（需要管理员权限）"


def _get_brightness_windows() -> str:
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance -Namespace root/wmi -ClassName WmiMonitorBrightness).CurrentBrightness"],
            capture_output=True, timeout=5, check=False, text=True,
        )
        v = int(out.stdout.strip())
        return f"当前亮度 {v}%"
    except Exception as e:  # noqa: BLE001
        return f"无法读取亮度：{e}"


def register(reg: ToolRegistry) -> None:
    """注册音量 / 亮度工具。"""

    def set_volume(level: int) -> str:
        """把系统主音量调到 0~100。0=静音，100=最大。"""
        if not IS_WINDOWS:
            return "非 Windows 平台：调音量未实现（Linux 用 amixer / pactl）"
        try:
            lvl = float(level)
        except (TypeError, ValueError):
            return f"错误：level 必须是数字（收到 {level!r}）"
        # 越界在工具层就夹取并说明，别让 999% 这种值静默变成 100%
        if not 0 <= lvl <= 100:
            clamped = max(0, min(100, int(round(lvl))))
            log.info("set_volume level=%s 越界，夹取到 %d", level, clamped)
            return _set_volume_windows(clamped / 100.0) + f"（{level} 已按 0~100 夹取）"
        return _set_volume_windows(lvl / 100.0)

    def get_volume() -> str:
        """查询当前系统主音量（百分比）。"""
        return _get_volume_windows() if IS_WINDOWS else "非 Windows 平台未实现"

    def set_brightness(level: int) -> str:
        """把屏幕亮度调到 0~100。需要管理员权限（首次）。"""
        return _set_brightness_windows(level) if IS_WINDOWS else (
            "非 Windows 平台未实现")

    def get_brightness() -> str:
        """查询屏幕当前亮度。"""
        return _get_brightness_windows() if IS_WINDOWS else "非 Windows 平台未实现"

    reg.register(Tool(name="set_volume",
        description="把系统主音量调到 0~100（0=静音，100=最大）。"
                    "Windows 通过 pycaw 控制；其他平台未实现。",
        parameters={"type": "object",
                    "properties": {"level": {"type": "integer",
                                              "minimum": 0, "maximum": 100}},
                    "required": ["level"]},
        fn=set_volume))
    reg.register(Tool(name="get_volume",
        description="查询当前系统主音量（百分比）。",
        parameters={"type": "object", "properties": {}},
        fn=get_volume))
    reg.register(Tool(name="set_brightness",
        description="把屏幕亮度调到 0~100。Windows 走 WMI（需要管理员权限）。",
        parameters={"type": "object",
                    "properties": {"level": {"type": "integer",
                                              "minimum": 0, "maximum": 100}},
                    "required": ["level"]},
        fn=set_brightness))
    reg.register(Tool(name="get_brightness",
        description="查询屏幕当前亮度（百分比）。",
        parameters={"type": "object", "properties": {}},
        fn=get_brightness))


__all__ = ["register"]