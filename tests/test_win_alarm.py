"""系统级闹钟兜底（schtasks）回归测试。

背景：桌宠自己的定时器在进程里——崩了、关掉、或那条「QTimer 在工作线程
start 无效」的老 bug 复发，闹钟就哑了。所以额外注册一个 Windows 计划任务，
由内核负责到点拉起提示。

要锁住的不只是「能注册」，还有**诚实**：
  - 剩不到 2 分钟时不能注册（schtasks 只有分钟粒度），且必须**说没装上**；
  - 取消提醒 / 取消倒计时时必须同步撤掉兜底，否则 Windows 那边照响；
  - 崩溃残留必须能被下次启动清掉。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".local-packages"))

from app.core import win_alarm  # noqa: E402


def _task_exists(name: str) -> bool:
    p = subprocess.run(["schtasks", "/query", "/tn", name, "/fo", "LIST"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return p.returncode == 0


# =============================================================================
# 纯逻辑（不打真实 schtasks）
# =============================================================================

def test_task_name_is_deterministic():
    """确定性是这套设计的关键：删除/到点/清理都不用额外存映射，
    拿 key 重算一遍就是同一个名字。"""
    a = win_alarm.task_name_for("r12345")
    b = win_alarm.task_name_for("r12345")
    assert a == b
    assert a.startswith(win_alarm.TASK_PREFIX)
    assert win_alarm.task_name_for("r99999") != a


def test_too_close_is_not_armed(monkeypatch):
    """1 分钟粒度装不下近的倒计时，不能假装注册成功。"""
    calls = []
    monkeypatch.setattr(win_alarm, "_run",
                        lambda *a, **k: calls.append(a) or (0, ""))
    assert win_alarm.arm(time.time() + 30, "马上", key="k1") == ""
    assert calls == [], "不够 2 分钟就不该去调 schtasks"


def test_arm_uses_minute_granularity(monkeypatch):
    """注册时刻要向上取整到整分，且落在将来（schtasks 拒绝过去时间）。"""
    seen = {}

    def fake_run(args, timeout=15.0):
        if "/st" in args:
            seen["st"] = args[args.index("/st") + 1]
            seen["tn"] = args[args.index("/tn") + 1]
            return 0, "SUCCESS"
        return 0, "SUCCESS"      # delete 分支

    monkeypatch.setattr(win_alarm, "_run", fake_run)
    name = win_alarm.arm(time.time() + 300, "五分钟", key="k2")
    assert name and name.startswith(win_alarm.TASK_PREFIX)
    assert len(seen["st"]) == 5 and seen["st"][2] == ":"
    assert seen["tn"] == name
    # 脚本应该落在临时目录且带 BOM（PowerShell 5.1 认中文要 BOM）
    script = Path(tempfile.gettempdir()) / f"{name}.ps1"
    try:
        assert script.is_file()
        assert script.read_bytes().startswith(b"\xef\xbb\xbf")
        assert "五分钟" in script.read_text(encoding="utf-8-sig")
    finally:
        Path(script).unlink(missing_ok=True)


def test_arm_failure_is_honest(monkeypatch):
    """schtasks 失败时返回空串，工具才不会谎称装了兜底。"""
    monkeypatch.setattr(win_alarm, "_run", lambda *a, **k: (1, "Access denied"))
    assert win_alarm.arm(time.time() + 600, "x", key="k3") == ""


def test_ps_quote_escapes_single_quotes():
    """提醒内容带单引号（主人的原话常带）不能把 PowerShell 脚本写坏。"""
    assert win_alarm._ps_quote("it's") == "'it''s'"
    assert win_alarm._ps_quote("主人说'快来'") == "'主人说''快来'''"
    script = win_alarm._PS_TEMPLATE.format(
        title=win_alarm._ps_quote("t"), text=win_alarm._ps_quote("it's ok"),
        name="desktop-pet-alarm-test")
    assert "$text  = 'it''s ok'" in script
    # 兜底任务响完要自清理，别在任务计划程序里留垃圾
    assert "/delete" in script and "desktop-pet-alarm-test" in script


def test_non_windows_is_noop(monkeypatch):
    monkeypatch.setattr(win_alarm, "_IS_WINDOWS", False)
    assert win_alarm.arm(time.time() + 600, "x", key="k") == ""
    assert win_alarm.disarm("whatever") is False
    assert win_alarm.cleanup_stale() == 0


# =============================================================================
# 真实 schtasks 集成（建了就必须清干净）
# =============================================================================

@pytest.fixture
def real_task():
    created: list[str] = []
    yield created
    for n in created:
        win_alarm.disarm(n)


def test_real_register_and_disarm(real_task):
    if not win_alarm.is_available():
        pytest.skip("非 Windows")
    win_alarm.cleanup_stale()
    name = win_alarm.arm(time.time() + 10 * 60, "该喝水啦~主人", key="it-real")
    assert name, "真实注册应成功"
    real_task.append(name)
    assert _task_exists(name) is True
    assert win_alarm.disarm(name) is True
    assert _task_exists(name) is False
    real_task.remove(name)


def test_real_cleanup_stale_removes_orphan(real_task):
    """模拟上次崩溃留下的任务：启动时必须能扫掉。"""
    if not win_alarm.is_available():
        pytest.skip("非 Windows")
    win_alarm.cleanup_stale()
    name = win_alarm.arm(time.time() + 15 * 60, "残留测试", key="it-stale")
    assert name
    real_task.append(name)
    assert _task_exists(name) is True
    assert win_alarm.cleanup_stale() >= 1
    assert _task_exists(name) is False
    real_task.remove(name)


def test_disarm_key_derives_name(real_task):
    if not win_alarm.is_available():
        pytest.skip("非 Windows")
    win_alarm.cleanup_stale()
    key = "reminder-it-key"
    name = win_alarm.arm(time.time() + 20 * 60, "按键注销", key=key)
    assert name == win_alarm.task_name_for(key)
    real_task.append(name)
    assert win_alarm.disarm_key(key) is True
    assert _task_exists(name) is False
    real_task.remove(name)


# =============================================================================
# 工具层：如实回报 + 同步撤销
# =============================================================================

def test_add_reminder_reports_backup_honestly(qapp, tmp_path):
    from app.engine.reminder import ReminderStore
    from app.engine.tools import _reminder as R
    from app.engine.tools._core import ToolRegistry

    store = ReminderStore(tmp_path / "reminders.json")
    reg = ToolRegistry()
    R.register(reg, reminders=store)
    try:
        out = reg.execute("add_reminder",
                          '{"text":"喝水","delay_minutes":30}')
        assert "已设置提醒" in out
        # 30 分钟够长 → 一定注册了兜底，文案必须说出来
        assert "系统级兜底" in out, out
    finally:
        from app.core import win_alarm as wa
        for it in store.list():
            wa.disarm_key(it.id)
        win_alarm.cleanup_stale()


def test_add_reminder_short_says_no_backup(qapp, tmp_path):
    """1 分钟提醒装不上兜底，文案要说清，不能让人以为「关了桌宠也会响」。"""
    from app.engine.reminder import ReminderStore
    from app.engine.tools import _reminder as R
    from app.engine.tools._core import ToolRegistry

    store = ReminderStore(tmp_path / "reminders.json")
    reg = ToolRegistry()
    R.register(reg, reminders=store)
    out = reg.execute("add_reminder", '{"text":"喝水","delay_minutes":1}')
    assert "已设置提醒" in out
    assert "系统级兜底" not in out or "没能加" in out, out


def test_delete_reminder_withdraws_backup(qapp, tmp_path):
    from app.core import win_alarm as wa
    from app.engine.reminder import ReminderStore
    from app.engine.tools import _reminder as R
    from app.engine.tools._core import ToolRegistry

    if not wa.is_available():
        pytest.skip("非 Windows")
    wa.cleanup_stale()
    store = ReminderStore(tmp_path / "reminders.json")
    reg = ToolRegistry()
    R.register(reg, reminders=store)
    out = reg.execute("add_reminder", '{"text":"喝水","delay_minutes":25}')
    rid = out.split("id=")[1].split("）")[0]
    name = wa.task_name_for(rid)
    try:
        assert _task_exists(name) is True
        reg.execute("delete_reminder", f'{{"reminder_id":"{rid}"}}')
        assert _task_exists(name) is False, "删提醒必须同步撤掉系统兜底"
    finally:
        wa.disarm(name)
        wa.cleanup_stale()


def test_countdown_cancel_withdraws_backup(qapp):
    from app.core import win_alarm as wa
    from app.engine.tools import _timer as T

    if not wa.is_available():
        pytest.skip("非 Windows")
    wa.cleanup_stale()
    T.set_alarm_hook(lambda t: None)
    try:
        out = T.countdown(seconds=600, message="番茄钟")
        assert "系统级兜底" in out, out
        cid = out.split("id=")[1].split("）")[0]
        name = wa.task_name_for(f"cd{cid}")
        assert _task_exists(name) is True
        T.cancel_countdown(cid)
        assert _task_exists(name) is False, "取消倒计时必须撤掉系统兜底"
    finally:
        T.cancel_all()
        wa.cleanup_stale()
