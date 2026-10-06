"""电源 / 锁屏 / 进程 / 网络开关 / 剪贴板。"""
from __future__ import annotations

import logging
import platform
import subprocess
from typing import Optional

from ._core import Tool, ToolRegistry

log = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"


def _as_bool(v) -> Optional[bool]:
    """把模型可能传来的各种「布尔」归一化成 True / False / None(不可识别)。

    不能直接 `if v:` —— JSON 里 "false" 是非空字符串，恒为真。
    开关类工具（Wi-Fi/蓝牙/音量）踩过这个坑：主人说「关掉WiFi」，
    桌宠反而执行了 Enable。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in ("true", "1", "yes", "on", "y", "是", "开"):
            return True
        if s in ("false", "0", "no", "off", "n", "否", "关"):
            return False
    return None


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
        # 显式归一化布尔。模型经常把 false 发成 JSON 字符串 "false"，
        # 而 `if enable` 对非空字符串恒为真 → 主人说「关掉WiFi」，
        # 桌宠反而执行 Enable-NetAdapter 并回报「已尝试 enable」。
        enable = _as_bool(enable)
        if enable is None:
            return "错误：enable 必须是 true 或 false（收到的值无法识别）"
        action = "enable" if enable else "disable"
        verb = "Enable-NetAdapter" if enable else "Disable-NetAdapter"
        # 早先这里无视 enable 参数，脚本里写死 Disable-NetAdapter，
        # 于是「打开WiFi」实际把 Wi-Fi 关了，而返回文案还报 enable —— 行为与承诺相反。
        # 网卡名匹配：中文 Windows 上叫「WLAN」「无线局域网连接」，
        # 原来的 '*Wi-Fi*' 匹配为空 → 管道空转却 returncode=0 → 报成功。
        # 改成按物理介质（11 = Wireless）筛，跨语言可靠，并检查有没有真的命中。
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "$a = Get-NetAdapter | Where-Object "
                 "{$_.NdisPhysicalMedium -eq 11 -or $_.Name -like '*Wi-Fi*' "
                 "-or $_.Name -like '*WLAN*' -or $_.Name -like '*无线*'}; "
                 f"if (-not $a) {{ Write-Output 'NO_ADAPTER'; exit 2 }}; "
                 f"$a | ForEach-Object {{ {verb} -Name $_.Name -Confirm:$false }}"],
                capture_output=True, timeout=20, check=False, text=True,
                encoding="utf-8", errors="replace",
            )
            out = (proc.stdout or "").strip()
            if "NO_ADAPTER" in out or not out:
                return "错误：没有找到无线网卡，未做任何修改"
            if proc.returncode != 0:
                err = (proc.stderr or "").strip().splitlines()
                log.warning("set_wifi %s 失败: %s", action, proc.stderr)
                return (f"错误：{action} Wi-Fi 失败（可能需要管理员权限运行）"
                        + (f"：{err[-1][:80]}" if err else ""))
            return f"已{('开启' if enable else '关闭')} Wi-Fi（{out[:80]}）"
        except Exception as e:  # noqa: BLE001
            # 不把 str(e) 回给模型：subprocess 异常里含完整命令行
            log.exception("set_wifi 异常")
            return f"错误：{action} Wi-Fi 失败（{type(e).__name__}），可能需要管理员权限"

    def set_bluetooth(enable: bool = True) -> str:
        """开 / 关蓝牙（危险工具，需要管理员）。"""
        if not IS_WINDOWS:
            return "非 Windows 平台未实现"
        enable = _as_bool(enable)
        if enable is None:
            return "错误：enable 必须是 true 或 false（收到的值无法识别）"
        action = "开" if enable else "关"
        try:
            ps = (
                f"$dev = Get-PnpDevice | Where-Object {{$_.Class -eq 'Bluetooth'}}; "
                f"if ($dev) {{ $dev | "
                f"{'Enable' if enable else 'Disable'}-PnpDevice -Confirm:$false "
                f"}} else {{ '无蓝牙设备' }}"
            )
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, timeout=10, check=False, text=True,
            )
            if proc.returncode != 0:
                err = (proc.stderr or "").strip().splitlines()
                log.warning("set_bluetooth 失败: %s", proc.stderr)
                return (f"错误：{action}蓝牙失败（可能需要管理员权限运行）"
                        + (f"：{err[-1][:80]}" if err else ""))
            return f"已尝试 {action}蓝牙"
        except Exception as e:  # noqa: BLE001
            # 不把 str(e) 回给模型：subprocess 异常里含完整 PowerShell 命令
            log.exception("set_bluetooth 异常")
            return f"错误：{action}蓝牙失败（{type(e).__name__}），可能需要管理员权限"

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
        """隐藏桌宠头顶气泡（用户说"别说了" / 屏幕太乱）。"""
        # hook 由 build_default_tools 统一注入（见 app/engine/tools/__init__.py）。
        # 原实现只 log.info 级别的兜底文案、没有「错误」前缀，agent 的矛盾检测
        # 拦不住；而主程序压根没注入过这个 hook，等于一个永久空转的工具。
        clear_hook = _bubble_clear_hook
        if not callable(clear_hook):
            return "错误：气泡通道未接入（主程序没注入 clear_bubble hook），气泡没消失。"
        try:
            clear_hook()
            return "已隐藏气泡"
        except Exception as e:  # noqa: BLE001
            return f"错误：隐藏气泡失败（{type(e).__name__}）"

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