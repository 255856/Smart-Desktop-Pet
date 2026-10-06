"""系统级闹钟兜底：把提醒注册成 Windows 计划任务，桌宠崩了/关了也照样响。

为什么需要：桌宠自己的定时器在进程里。桌宠崩了、被关掉、机器休眠醒来、
或那条「QTimer 在工作线程里 start 无效」的老 bug 复发——闹钟就哑了。
注册一个 **Windows 计划任务**（schtasks，一次性，当前用户，免管理员），
由 Windows 内核负责在到点那一刻拉起提示，和桌宠进程完全无关。

约束（实测 schtasks 行为）：
  - `/st HH:mm` 只有**分钟**粒度，且必须是将来时间；
  - 剩不到 2 分钟的倒计时不值得注册（1 分钟粒度会不准），直接不注册；
  - 创建需要当前用户权限，实测不必管理员；
  - 任务会留在「任务计划程序」里，必须能被我们找回来清掉——
    启动时 `cleanup_stale()` 会扫掉上次崩溃残留的。

所有函数都返回**如实**的结果，调用方据此告诉主人「有没有装上兜底」，
绝不假装已经注册。
"""
from __future__ import annotations

import atexit
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

TASK_PREFIX = "desktop-pet-alarm-"
_IS_WINDOWS = os.name == "nt"

# 剩不到这么久就不注册：schtasks 只有分钟粒度，短于此注册了也会不准
MIN_LEAD_SECONDS = 120


def is_available() -> bool:
    return _IS_WINDOWS


def _run(args: list[str], timeout: float = 15.0) -> tuple[int, str]:
    try:
        p = subprocess.run(args, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8",
                           errors="replace", creationflags=getattr(
                               subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 1, "schtasks 超时"
    except Exception as e:  # noqa: BLE001
        return 1, f"{type(e).__name__}: {e}"


def _ps_quote(s: str) -> str:
    """PowerShell 单引号串：内部单引号要成对转义。"""
    return "'" + str(s).replace("'", "''") + "'"


_PS_TEMPLATE = """# 桌宠系统级闹钟兜底（由 app/core/win_alarm.py 生成，勿手改）
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$title = {title}
$text  = {text}
# 响铃：3 声，每声 0.6 秒
for ($i = 0; $i -lt 3; $i++) {{
    [console]::beep(880, 600)
    Start-Sleep -Milliseconds 250
}}
try {{ [System.Media.SystemSounds]::Exclamation.Play() }} catch {{}}
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.BalloonTipTitle = $title
$n.BalloonTipText = $text
$n.Visible = $true
$n.ShowBalloonTip(30000)
Start-Sleep -Seconds 25
$n.Dispose()
# 自清理：一次性任务响完就把自己和脚本删掉，不在任务计划程序里留垃圾
Start-Process schtasks -ArgumentList '/delete','/tn','{name}','/f' -WindowStyle Hidden
Start-Sleep -Seconds 2
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
"""


def task_name_for(key: str) -> str:
    """由业务 key（提醒 id / 倒计时 id）**确定性地**推出任务名。

    确定性很关键：删除提醒、倒计时到点、崩溃后清理，都不需要额外存一份映射，
    拿 key 重新算一遍就是同一个名字。
    """
    import hashlib
    h = hashlib.sha1(str(key).encode("utf-8")).hexdigest()[:12]
    return f"{TASK_PREFIX}{h}"


def arm(fire_ts: float, text: str, *, key: str,
        title: str = "⏰ 桌宠闹钟") -> str:
    """为到点时刻注册一次性计划任务。

    Args:
        fire_ts: 绝对时间戳（time.time() 口径）
        text: 提醒内容
        key: 业务标识（提醒 id 等），决定任务名；重复 arm 同一 key 会覆盖。
    Returns:
        任务名；未注册返回空串（调用方要如实告诉主人没装上兜底）
    """
    if not _IS_WINDOWS:
        return ""
    lead = fire_ts - time.time()
    if lead < MIN_LEAD_SECONDS:
        # 1 分钟粒度装不下，留给桌宠自己的定时器
        return ""

    # 向上取整到下一个整分，并保证落在将来
    import datetime
    when = datetime.datetime.fromtimestamp(fire_ts + 59)
    when = when.replace(second=0, microsecond=0)
    try:
        if when.timestamp() <= time.time():
            when += datetime.timedelta(days=1)
    except (OverflowError, OSError, ValueError):
        return ""
    st = when.strftime("%H:%M")

    name = task_name_for(key)
    try:
        script = Path(tempfile.gettempdir()) / f"{name}.ps1"
        script.write_text(
            _PS_TEMPLATE.format(title=_ps_quote(title), text=_ps_quote(text),
                                name=name),
            encoding="utf-8-sig")   # BOM：Windows PowerShell 5.1 认中文
    except OSError as e:
        log.warning("写闹钟脚本失败：%s", e)
        return ""

    rc, out = _run([
        "schtasks", "/create", "/tn", name, "/tr",
        f'powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass '
        f'-File "{script}"',
        "/sc", "once", "/st", st, "/f",
    ])
    if rc != 0:
        log.warning("注册系统闹钟失败：%s", out.strip()[:160])
        _cleanup_script(name)
        return ""
    log.info("已注册系统级闹钟兜底：%s @ %s（%s）", name, st, text[:30])
    return name


def disarm_key(key: str) -> bool:
    """按业务 key 注销（不用记住任务名）。"""
    return disarm(task_name_for(key))


def disarm(name: str) -> bool:
    """注销一个已注册的任务。"""
    if not name or not _IS_WINDOWS:
        return False
    rc, out = _run(["schtasks", "/delete", "/tn", name, "/f"])
    if rc != 0:
        log.debug("注销系统闹钟 %s 失败：%s", name, out.strip()[:120])
        return False
    _cleanup_script(name)
    return True


def _cleanup_script(name: str) -> None:
    try:
        p = Path(tempfile.gettempdir()) / f"{name}.ps1"
        if p.is_file():
            p.unlink()
    except OSError:
        pass


def cleanup_stale() -> int:
    """清掉上次运行残留的任务（桌宠崩了/被强杀时会留下）。返回清理个数。"""
    if not _IS_WINDOWS:
        return 0
    rc, out = _run(["schtasks", "/query", "/fo", "CSV", "/nh"], timeout=25.0)
    if rc != 0:
        return 0
    n = 0
    for line in out.splitlines():
        # `schtasks /query /fo CSV /nh` 没有表头，每行是
        #   "任务名","下次运行时间","状态"
        # 任务名自带一层引号，且本机查询会前缀一个反斜杠（"\任务名"）。
        if TASK_PREFIX not in line:
            continue
        parts = [p.strip('"') for p in line.split('","')]
        raw = parts[0].strip('"')
        name = raw.replace("\\", "").strip()
        if not name:
            continue
        if disarm(name):
            n += 1
    if n:
        log.info("清理残留的系统闹钟任务：%d 个", n)
    return n


@atexit.register
def _cleanup_on_exit() -> None:      # pragma: no cover - 进程退出钩子
    try:
        cleanup_stale()
    except Exception:  # noqa: BLE001
        pass


__all__ = ["arm", "disarm", "disarm_key", "task_name_for", "cleanup_stale",
           "is_available", "TASK_PREFIX", "MIN_LEAD_SECONDS"]
