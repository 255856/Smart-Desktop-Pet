# -*- coding: utf-8 -*-
"""get_weather 的天气正确性回归（2026-10-14 现场）。

现场：主人问广州天气，工具输出里同时有

    当前：晴，33.0°C                      <- current，15:15 实况
    2026-10-14: 毛毛雨，降水概率 31%      <- daily，全天 24 小时汇总

两行说的是**同一天**却互相矛盾。open-meteo 的 daily 是全天汇总，凌晨下过
毛毛雨就把整天标成毛毛雨，哪怕此刻是大太阳。模型采信了 daily 那行，于是
对主人说「广州今天有毛毛雨，31%的降水概率」——窗外是晴天。

所以这里锁死两条：
  1. 当天那行**不能**再出现天气现象 / 降水概率（它们和「当前」冲突）；
  2. 未来几天的行不受影响，仍要完整。
"""
from __future__ import annotations

import json

import pytest

from app.engine.tools import _weather
from app.engine.tools._core import ToolRegistry

# 复刻现场那份矛盾数据（真实抓的，2026-10-14 15:15 广州）
_CONTRADICTORY = {
    "current": {
        "time": "2026-10-14T15:15", "interval": 900,
        "temperature_2m": 33.0, "apparent_temperature": 35.0,
        "weather_code": 0,            # 晴
        "wind_speed_10m": 6.5, "relative_humidity_2m": 42,
    },
    "daily": {
        "time": ["2026-10-14", "2026-10-15", "2026-10-16"],
        "weather_code": [51, 51, 3],  # 今天=毛毛雨，明天=毛毛雨，后天=阴
        "temperature_2m_max": [33.2, 32.7, 33.5],
        "temperature_2m_min": [24.8, 24.2, 23.6],
        "precipitation_probability_max": [31, 6, 4],
    },
}


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


@pytest.fixture()
def patched(monkeypatch):
    """拦在最底层 httpx，返回上面那份矛盾数据。"""
    seen = {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url):
            seen["url"] = url
            return _Resp(_CONTRADICTORY)

    monkeypatch.setattr(_weather.httpx, "Client", _Client)
    monkeypatch.setattr(_weather, "_geocode", lambda loc: (113.25, 23.1167, "广州"))
    return seen


def _run(patched, **kw):
    reg = ToolRegistry()
    _weather.register(reg)
    return reg.execute("get_weather", json.dumps(kw, ensure_ascii=False))


def test_today_line_never_contradicts_current(patched):
    """当天那行不得再出现天气现象 / 降水概率。"""
    out = _run(patched, location="广州", days=3)
    today_line = [ln for ln in out.splitlines() if "今天" in ln]
    assert today_line, f"缺少当天行：\n{out}"
    line = today_line[0]
    assert "毛毛雨" not in line, f"当天行仍报日汇总天气（与「当前：晴」冲突）：{line}"
    assert "31%" not in line, f"当天行仍报日汇总降水概率（与实况冲突）：{line}"
    # 高低温要留着，这个不矛盾
    assert "24.8" in line and "33.2" in line, f"当天行丢了温度区间：{line}"


def test_current_line_still_reports_real_conditions(patched):
    """修冲突不能把实况也弄没了。"""
    out = _run(patched, location="广州", days=3)
    assert "当前：晴" in out, f"当前天气被改坏了：\n{out}"
    assert "33" in out


def test_future_days_keep_full_detail(patched):
    """未来几天的完整信息不受影响。"""
    out = _run(patched, location="广州", days=3)
    assert "2026-10-15: 毛毛雨" in out, f"明天的天气丢了：\n{out}"
    assert "6%" in out, f"明天的降水概率丢了：\n{out}"
    assert "2026-10-16: 阴" in out, f"后天的天气丢了：\n{out}"


def test_days_one_still_shows_today(patched):
    """days=1 时当天行仍然要出现（只是不带天气现象）。

    注意断言范围：**只能查当天那一行**。mock 不理会 forecast_days，
    仍会返回 3 天数据，所以整段输出里出现「毛毛雨」是正常的——
    那是明天（2026-10-15）的天气，必须保留。
    """
    out = _run(patched, location="广州", days=1)
    assert "当前：晴" in out
    today_line = [ln for ln in out.splitlines() if "今天" in ln]
    assert today_line, f"缺少当天行：\n{out}"
    assert "毛毛雨" not in today_line[0], \
        f"当天行不该报日汇总天气：{today_line[0]}"
    # 未来天不受影响
    assert "2026-10-15: 毛毛雨" in out, f"明天的天气被误删：\n{out}"


def test_schema_tells_model_to_request_enough_days_for_future_dates():
    """主人问「明天」时 days 必须传够，否则查不到那一天。

    现场：主人说「提醒我明天带伞」，模型传了 days=1，只查了今天，
    结果压根没有明天的数据，模型只能拿今天的天气去回答。
    """
    reg = ToolRegistry()
    _weather.register(reg)
    tool = reg.get("get_weather")
    desc = (tool.description or "") + json.dumps(
        tool.parameters or {}, ensure_ascii=False)
    assert "明天" in desc, f"工具描述没提未来日期：{desc[:200]}"
