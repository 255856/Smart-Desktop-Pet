"""at_time 绝对时间用例的**时序确定性**守卫。

配合 tests/test_reminder_e2e.py::TestAddReminderToolE2E::
test_add_with_at_time_in_future_fires。

那条用例原来用 'HH:MM' 格式，而 parse_absolute_time 对 'HH:MM' 是
「把秒抹成 0 → 已经过了就推到明天」。于是它同时踩在两条分支的刀刃上：
墙钟一动（尤其刚过整分的那 1 秒）断言就飘。

本模块把墙钟**冻结**在几个危险时刻（整分边界、午夜、年末），跑那条
**真正**的测试函数，证明它的断言只跟「传给工具的那个字符串」有关，
跟墙钟落在哪儿无关；再把老写法的失败模式钉成可复现的断言，
免得有人把老写法改回来。

冻结办法：
  · time.time()      —— time 是共享模块对象，import time 的地方全都看得到；
  · datetime.now()   —— 用子类钉死，再把相关模块的 datetime 引用换掉；
  · win_alarm        —— 本模块不许真的去注册 Windows 计划任务。
"""
from __future__ import annotations

import datetime as _real_datetime
import sys
import time as _real_time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core import win_alarm  # noqa: E402
from app.engine.reminder import ReminderStore  # noqa: E402
from app.engine.tools import _reminder, _time  # noqa: E402
from app.engine.tools._core import ToolRegistry  # noqa: E402
from tests import test_reminder_e2e as e2e  # noqa: E402


class _FrozenDatetime(_real_datetime.datetime):
    """now() 被钉死的 datetime 子类（算术运算 / replace 仍返回本子类）。"""

    frozen: "_FrozenDatetime | None" = None

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001, ANN206
        if cls.frozen is None:
            return _real_datetime.datetime.now(tz)
        return cls.frozen


class _FakeDatetimeModule:
    """给测试模块 / 解析器模块用的 datetime 替身。"""

    datetime = _FrozenDatetime
    timedelta = _real_datetime.timedelta


@pytest.fixture
def frozen_clock(monkeypatch):
    """把墙钟钉在指定时刻；返回冻结后的 datetime。"""

    def _freeze(moment: _real_datetime.datetime) -> _FrozenDatetime:
        frozen = _FrozenDatetime(moment.year, moment.month, moment.day,
                                 moment.hour, moment.minute, moment.second,
                                 moment.microsecond)
        _FrozenDatetime.frozen = frozen
        # time 是共享模块对象：import time 的地方全都看到冻结后的钟
        monkeypatch.setattr(_real_time, "time", lambda: frozen.timestamp())
        # 测试模块自己的 datetime + 解析器模块的 datetime
        monkeypatch.setattr(e2e, "datetime", _FakeDatetimeModule)
        monkeypatch.setattr(_time, "datetime", _FakeDatetimeModule)
        # 不许真的去注册 Windows 计划任务（会留个真闹钟在主人机器上）
        monkeypatch.setattr(win_alarm, "is_available", lambda: False)
        return frozen

    yield _freeze
    _FrozenDatetime.frozen = None


def _make_store(tmp_path):
    store = ReminderStore(tmp_path / "reminders.json")
    reg = ToolRegistry()
    _reminder.register(reg, reminders=store)
    return reg, store


# 墙钟的「危险时刻」：整分边界前后、午夜前后、年末
MOMENTS = [
    ("整分前1ms", _real_datetime.datetime(2026, 3, 14, 12, 34, 59, 999000)),
    ("整分上", _real_datetime.datetime(2026, 3, 14, 12, 35, 0, 0)),
    ("整分后1ms", _real_datetime.datetime(2026, 3, 14, 12, 35, 0, 1000)),
    ("当日末尾", _real_datetime.datetime(2026, 3, 14, 23, 59, 59, 999000)),
    ("跨午夜", _real_datetime.datetime(2026, 3, 14, 23, 59, 30, 0)),
    # 跨年：目标分钟落到 2027-01-01 00:05
    ("跨年", _real_datetime.datetime(2026, 12, 31, 23, 55, 0, 0)),
]


@pytest.mark.parametrize("label,moment", MOMENTS, ids=[m[0] for m in MOMENTS])
def test_fixed_at_time_test_passes_at_any_wall_clock(
        qapp, tmp_path, frozen_clock, label, moment):
    """墙钟冻在这些时刻，真正的用例都必须通过。"""
    frozen_clock(moment)
    reg, store = _make_store(tmp_path)
    e2e.TestAddReminderToolE2E().test_add_with_at_time_in_future_fires(
        qapp, (reg, store))


# ---------------------------------------------------------------------------
# 老写法（'HH:MM'）的失败模式：钉成可复现的断言，防止有人改回去
# ---------------------------------------------------------------------------

def _old_style_at_time() -> str:
    """老写法：'HH:MM'，秒被抹掉。

    这里**只**用来复现老用例的失败模式，不是工具行为的新契约。
    """
    future = _real_time.localtime(_real_time.time() + 10)
    return _real_time.strftime("%H:%M", future)


def test_old_hhmm_style_breaks_right_before_a_minute_boundary(
        qapp, tmp_path, frozen_clock):
    """墙钟在整分前 5ms 时，老写法离触发只剩 5ms。

    'HH:MM' 把秒抹成 0 → 工具里 int(fire_ts - now) 秒级取整成 0 →
    fire_at == 现在 → 老断言 `0 < delay` 必然失败；真实机器上测量还会更晚，
    delay 变成负数——就是那条「delay 异常：-0.0077」。
    """
    frozen_clock(_real_datetime.datetime(2026, 3, 14, 12, 34, 59, 995000))
    reg, store = _make_store(tmp_path)
    out = reg.get("add_reminder").fn(text="legacy", at_time=_old_style_at_time())
    assert "已设置" in out, out
    delay = store.list()[0].fire_at - _real_time.time()
    assert delay <= 0, (
        f"老写法在这个时刻本该失败（delay={delay}），复现失效了")


def test_old_hhmm_style_rolls_to_tomorrow_just_after_a_boundary(
        qapp, tmp_path, frozen_clock):
    """墙钟刚过整分时，老写法被推到**明天**同一分钟（delay ≈ 86399）。

    注意：'今天，否则明天' 是 parse_absolute_time 的**正确**语义（UI 也在用），
    只是它不能拿来做「几秒后触发」的断言。
    """
    frozen_clock(_real_datetime.datetime(2026, 3, 14, 12, 35, 0, 5000))
    reg, store = _make_store(tmp_path)
    out = reg.get("add_reminder").fn(text="legacy", at_time=_old_style_at_time())
    assert "已设置" in out, out
    delay = store.list()[0].fire_at - _real_time.time()
    assert 86398 <= delay <= 86400, (
        f"应被推到明天同一分钟，实际 delay={delay}")
