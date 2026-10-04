"""危险硬件工具的行为正确性回归（不真正改动系统：用 spy 拦截 subprocess）。

重点回归两个真实 bug：
1. set_wifi 无论 enable=True/False 都执行 Disable-NetAdapter（行为与承诺相反）
2. set_bluetooth 早先同样无视 enable，且错误信息里泄漏完整 PowerShell 命令
3. set_volume 在缺 pycaw 时静默降级为静音切换，却回报「已取消静音」像成功
"""
from __future__ import annotations

import json

import pytest

from app.brain.agent import _tool_ack_sentence, _tool_result_state
from app.engine.tools import _audio as audio_tools
from app.engine.tools import _power as power_tools
from app.engine.tools import build_default_tools


class _State:
    def get(self, *a, **k):
        return None


def _reg():
    return build_default_tools(state=_State(), reminders=None, memory=None)


def _spy_popen(monkeypatch, recorder):
    """拦截 powershell 调用，记录命令行，返回可控结果。"""
    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *a, **k):
        if isinstance(cmd, list) and len(cmd) > 2 and "powershell" in str(cmd[0]).lower():
            recorder.append(" ".join(str(c) for c in cmd))
        return _P()

    monkeypatch.setattr(power_tools.subprocess, "run", fake_run)


# --------------------------------------------------------------------------
# set_wifi：enable 必须真的影响执行的命令
# --------------------------------------------------------------------------

def test_set_wifi_enable_actually_enables(monkeypatch):
    calls = []
    _spy_popen(monkeypatch, calls)
    out = _reg().execute("set_wifi", json.dumps({"enable": True}))
    assert calls, "没有调用 powershell"
    cmd = calls[-1]
    assert "Enable-NetAdapter" in cmd, f"enable=True 却执行了关闭：{cmd}"
    assert "Disable-NetAdapter" not in cmd, f"enable=True 却含 Disable：{cmd}"
    assert "enable" in out


def test_set_wifi_disable_actually_disables(monkeypatch):
    calls = []
    _spy_popen(monkeypatch, calls)
    out = _reg().execute("set_wifi", json.dumps({"enable": False}))
    cmd = calls[-1]
    assert "Disable-NetAdapter" in cmd, f"enable=False 却执行了开启：{cmd}"
    assert "disable" in out


def test_set_wifi_reports_failure_honestly(monkeypatch):
    class _Fail:
        returncode = 1
        stdout = ""
        stderr = "Access is denied."

    monkeypatch.setattr(power_tools.subprocess, "run",
                        lambda *a, **k: _Fail())
    out = _reg().execute("set_wifi", json.dumps({"enable": False}))
    assert "错误" in out
    assert _tool_result_state(out) == "fail", "失败必须能被 agent 的矛盾检测识别"
    assert "powershell" not in out.lower(), "错误信息泄漏了内部命令"


# --------------------------------------------------------------------------
# set_bluetooth：同样必须尊重 enable
# --------------------------------------------------------------------------

def test_set_bluetooth_enable_actually_enables(monkeypatch):
    calls = []
    _spy_popen(monkeypatch, calls)
    _reg().execute("set_bluetooth", json.dumps({"enable": True}))
    cmd = calls[-1]
    assert "Enable-PnpDevice" in cmd, f"enable=True 却执行了关闭：{cmd}"
    assert "Disable-PnpDevice" not in cmd


def test_set_bluetooth_disable_actually_disables(monkeypatch):
    calls = []
    _spy_popen(monkeypatch, calls)
    _reg().execute("set_bluetooth", json.dumps({"enable": False}))
    cmd = calls[-1]
    assert "Disable-PnpDevice" in cmd, f"enable=False 却执行了开启：{cmd}"


def test_set_bluetooth_error_does_not_leak_command(monkeypatch):
    """回归：早先 f"错误：{e}" 会把整条 PowerShell 命令回给模型。"""
    calls = []
    _spy_popen(monkeypatch, calls)

    def boom(*a, **k):
        raise RuntimeError("Command '['powershell', '-Command', "
                           "'Get-PnpDevice | Disable-PnpDevice']' returned 1")

    monkeypatch.setattr(power_tools.subprocess, "run", boom)
    out = _reg().execute("set_bluetooth", json.dumps({"enable": False}))
    assert "错误" in out
    assert "Disable-PnpDevice" not in out, f"错误信息泄漏内部命令：{out}"
    assert "powershell" not in out.lower(), f"错误信息泄漏内部命令：{out}"
    assert _tool_result_state(out) == "fail"


# --------------------------------------------------------------------------
# set_volume：降级不能伪装成成功
# --------------------------------------------------------------------------

def test_set_volume_fallback_is_not_false_success():
    """缺 pycaw 时无法精确调音量，必须如实说明并能被判定为失败。"""
    out = audio_tools._mute_via_mmsysvol_windows(0.5)
    assert "无法设置音量到 50%" in out, out
    assert _tool_result_state(out) == "fail", (
        "降级结果被判为成功，桌宠会骗主人说「音量已调到 50%」")


def test_set_volume_mute_zero_is_real_success():
    """音量 0 确实做到了静音，可以报成功。"""
    out = audio_tools._mute_via_mmsysvol_windows(0.0)
    assert "已静音" in out
    assert _tool_result_state(out) == "ok", out


def test_set_volume_clamps_out_of_range(monkeypatch):
    """越界值必须在工具层夹取，而不是把 999 当成 999% 传下去。"""
    seen = {}

    def fake(level):
        seen["level"] = level
        return f"音量已调到 {int(level * 100)}%"

    monkeypatch.setattr(audio_tools, "_set_volume_windows", fake)
    out = _reg().execute("set_volume", json.dumps({"level": 999}))
    assert seen["level"] == 1.0, f"999 未被夹取到 100%：{seen}"
    assert "夹取" in out, f"应说明发生了夹取：{out}"


def test_set_volume_rejects_non_numeric(monkeypatch):
    out = _reg().execute("set_volume", json.dumps({"level": "大声点"}))
    assert "错误" in out, out
    assert _tool_result_state(out) == "fail"


# --------------------------------------------------------------------------
# 「取消」不能只看裸子串
# --------------------------------------------------------------------------

def test_unmute_is_not_mistaken_for_user_cancel():
    """回归：「已取消静音」含「取消」，早先被判成用户取消操作。"""
    out = "已取消静音"
    assert _tool_result_state(out) == "ok", (
        "正常的取消静音被误判为用户取消，桌宠会答非所问")
    assert "那就不弄啦" not in _tool_ack_sentence("set_volume", "{}", out)


def test_real_user_cancel_is_still_detected():
    assert _tool_result_state("用户取消了此操作。") == "cancel"
    assert _tool_result_state("主人拒绝执行") == "cancel"
    assert _tool_ack_sentence("lock_screen", "{}", "用户取消了此操作。") \
        == "好的主人，那就不弄啦~"
