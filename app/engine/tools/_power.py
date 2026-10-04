"""电源 / 锁屏 / 进程 / 网络开关 / 剪贴板。"""
from __future__ import annotations

import logging
import platform
import subprocess
from typing import Optional

from ._core import Tool, ToolRegistry

log = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"


def _battery_windows() -> str:
    """Windows 笔记本电池状态（ps + WMI）。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance -Class BatteryStatus -Namespace root/wmi).Active"
             " | Out-String"],
            capture_output=True, timeout=5, text=True, check=False,
        )
        # WMI BatteryStatus class 不通——回退到 Win32_Battery
        ps = (
            "$b = Get-CimInstance -Class Win32_Battery; "
            "if ($b) { "
            "  '{0}|{1}|{2}|{3}' -f $b.EstimatedChargeRemaining, $b.BatteryStatus, "
            "  ($b.EstimatedRunTime -as [int]),$b.Name "
            "} else { 'N/A|0|0|外接电源' }"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=5, text=True, check=False,
        )
        line = out.stdout.strip()
        if not line or "|" not in line:
            return "未检测到电池（外接电源 / 桌面 PC）"
        parts = line.split("|")
        pct = parts[0].strip()
        status = {"1": "放电中", "2": "已充满", "3": "充电中", "4": "低电量",
                  "5": "严重低电量", "6": "错误", "7": "未知",
                  "8": "充电完成", "9": "充电中"}.get(parts[1].strip(), "未知")
        try:
            mins = int(parts[2].strip())
        except (ValueError, IndexError):
            mins = -1
        name = parts[3].strip() if len(parts) > 3 else "电池"
        if mins > 0 and status == "放电中":
            return f"{name}：电量 {pct}%，{status}，预计续航 {mins} 分钟"
        return f"{name}：电量 {pct}%，{status}"
    except Exception as e:  # noqa: BLE001
        log.warning("battery query failed: %s", e)
        return f"电池信息查询失败：{e}"


def register(reg: ToolRegistry) -> None:
    """注册电源 / 锁屏 / 进程 / 无线 / 剪贴板工具。"""

    def get_battery() -> str:
        """查询笔记本电池电量（百分比 / 状态 / 续航）。"""
        return _battery_windows() if IS_WINDOWS else "非 Windows 平台未实现"

    def lock_screen() -> str:
        """锁屏（WorkStation Lock）。注意：危险工具，需要用户在弹窗里确认。"""
        if IS_WINDOWS:
            try:
                subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"])
                return "已触发锁屏"
            except Exception as e:  # noqa: BLE001
                return f"错误：{e}"
        return "非 Windows 平台未实现"

    def list_processes(filter_name: str = "", limit: int = 30) -> str:
        """列出当前进程名 + PID。filter_name 为空则按内存倒序。"""
        limit = max(1, min(int(limit if limit else 30), 200))
        try:
            import psutil
            procs = []
            for p in psutil.process_iter(["pid", "name", "memory_info"]):
                try:
                    info = p.info
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
                if filter_name and filter_name.lower() not in info["name"].lower():
                    continue
                mem = info["memory_info"].rss if info["memory_info"] else 0
                procs.append((mem, info["pid"], info["name"]))
            procs.sort(reverse=True)
            procs = procs[:limit]
            if not procs:
                return "未找到匹配进程"
            lines = [f"{'PID':>8}  {'内存(MB)':>10}  名称"]
            for mem, pid, name in procs:
                lines.append(f"{pid:>8}  {mem // (1024*1024):>10}  {name}")
            return "\n".join(lines)
        except ImportError:
            return "psutil 未安装"
        except Exception as e:  # noqa: BLE001
            return f"错误：{e}"

    def kill_process(pid: int = 0, name: str = "") -> str:
        """按 PID 或名称杀进程。危险工具，需用户确认。"""
        try:
            import psutil
        except ImportError:
            return "psutil 未安装"
        killed = []
        if pid:
            try:
                p = psutil.Process(int(pid))
                p.terminate()
                killed.append(f"pid={pid} ({p.name()})")
            except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError) as e:
                return f"错误：{e}"
        elif name:
            for p in psutil.process_iter(["pid", "name"]):
                try:
                    if p.info["name"].lower() == name.lower():
                        psutil.Process(p.info["pid"]).terminate()
                        killed.append(f"{p.info['name']}({p.info['pid']})")
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        else:
            return "错误：必须传 pid 或 name 二选一"
        if not killed:
            return "未匹配到进程"
        return "已终止：" + ", ".join(killed)

    def set_wifi(enable: bool = True) -> str:
        """开 / 关 Wi-Fi（危险工具，需要管理员）。"""
        if not IS_WINDOWS:
            return "非 Windows 平台未实现"
        action = "enable" if enable else "disable"
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"Get-NetAdapter | Where-Object {{$_.Name -like '*Wi-Fi*'}} | "
                 f"ForEach-Object {{ Disable-NetAdapter -Name $_.Name -Confirm:$false }}"],
                capture_output=True, timeout=10, check=False,
            )
            return f"已尝试 {action} Wi-Fi（可能需要管理员）"
        except Exception as e:  # noqa: BLE001
            return f"错误：{e}"

    def set_bluetooth(enable: bool = True) -> str:
        """开 / 关蓝牙（危险工具，需要管理员）。"""
        if not IS_WINDOWS:
            return "非 Windows 平台未实现"
        action = "开" if enable else "关"
        try:
            ps = (
                f"$dev = Get-PnpDevice | Where-Object {{$_.Class -eq 'Bluetooth'}}; "
                f"if ($dev) {{ $dev | Disable-PnpDevice -Confirm:$false "
                f"if (-not ${'true' if enable else 'false'}) {{ "
                f"$dev | Enable-PnpDevice -Confirm:$false }} }} else "
                f"{{ '无蓝牙设备' }}"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, timeout=10, check=False,
            )
            return f"已尝试 {action} 蓝牙"
        except Exception as e:  # noqa: BLE001
            return f"错误：{e}"

    def get_clipboard() -> str:
        """读取当前剪贴板文本（区别 clipboard_copy 写）。"""
        if IS_WINDOWS:
            try:
                import win32clipboard
                win32clipboard.OpenClipboard()
                try:
                    data = win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
                finally:
                    win32clipboard.CloseClipboard()
                return data if data else "剪贴板为空"
            except Exception as e:  # noqa: BLE001
                return f"读剪贴板失败：{e}"
        try:
            import subprocess
            r = subprocess.run(
                ["xclip", "-selection", "clipboard", "-o"],
                capture_output=True, timeout=3, text=True,
            )
            return r.stdout or "剪贴板为空"
        except Exception as e:  # noqa: BLE001
            return f"读剪贴板失败：{e}"

    def clear_bubble() -> str:
        """隐藏桌宠头顶气泡（用户说"别说了" / 屏幕太乱）。需要 bubble hook。"""
        from app.engine.tools._pet import _fire_hook
        # clear_bubble 走与 _pet 模块同样的 hooks 协议
        # _fire_hook(hooks_dict, "bubble", text) 是只发气泡；这里传空字符串 + 实际逻辑
        # 由 hook 实现：见 _pet 模块默认实现
        # 简化做法：让 hook 接到空字符串时隐藏气泡（已在 _pet 里约定）
        from app.engine.tools import _pet as _pet_mod
        # 让 hook 看门 — 但 hooks 在 register 时被传入，此处拿不到
        # 用全局 hook 暂存即可
        clear_hook = _bubble_clear_hook
        if callable(clear_hook):
            try:
                clear_hook()
                return "已隐藏气泡"
            except Exception as e:  # noqa: BLE001
                return f"错误：{e}"
        return "未连接气泡 hook（主程序没接入）"

    # 注册
    reg.register(Tool(name="get_battery",
        description="查询笔记本电池电量（百分比 / 状态 / 续航）。Windows 走 WMI。",
        parameters={"type": "object", "properties": {}},
        fn=get_battery))
    reg.register(Tool(name="lock_screen",
        description="锁屏（WorkStation Lock）。注意：危险工具，需用户在弹窗里确认。",
        parameters={"type": "object", "properties": {}},
        fn=lock_screen))
    reg.register(Tool(name="list_processes",
        description="列出当前进程名 + PID + 内存。filter_name 为空按内存倒序，limit 默认 30。",
        parameters={"type": "object",
                    "properties": {
                        "filter_name": {"type": "string",
                                        "description": "按进程名子串过滤，空则全部"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                                  "description": "最多返回几条"},
                    }},
        fn=list_processes))
    reg.register(Tool(name="kill_process",
        description="按 PID 或名称杀进程。危险工具，需用户确认。",
        parameters={"type": "object",
                    "properties": {
                        "pid": {"type": "integer", "description": "进程 PID"},
                        "name": {"type": "string", "description": "进程名（不带 .exe）"},
                    }},
        fn=kill_process))
    reg.register(Tool(name="set_wifi",
        description="开 / 关 Wi-Fi。危险工具，需要管理员权限。",
        parameters={"type": "object",
                    "properties": {"enable": {"type": "boolean",
                                              "description": "True=开，False=关"}}},
        fn=set_wifi))
    reg.register(Tool(name="set_bluetooth",
        description="开 / 关蓝牙。危险工具，需要管理员权限。",
        parameters={"type": "object",
                    "properties": {"enable": {"type": "boolean",
                                              "description": "True=开，False=关"}}},
        fn=set_bluetooth))
    reg.register(Tool(name="get_clipboard",
        description="读取当前剪贴板文本（区别 clipboard_copy 是写）。",
        parameters={"type": "object", "properties": {}},
        fn=get_clipboard))
    reg.register(Tool(name="clear_bubble",
        description="隐藏桌宠头顶气泡（用户说别说了 / 屏幕太乱时用）。",
        parameters={"type": "object", "properties": {}},
        fn=clear_bubble))


# 全局 hook：clear_bubble 需要由主程序注入（调 pet.hide_bubble）
_bubble_clear_hook: Optional[object] = None


def set_bubble_clear_hook(fn: Optional[object]) -> None:
    """由主程序注入：调用 pet 隐藏气泡。"""
    global _bubble_clear_hook
    _bubble_clear_hook = fn


__all__ = ["register", "set_bubble_clear_hook"]