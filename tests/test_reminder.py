"""提醒系统单元测试。"""
import pytest
import time
from app.engine.reminder import parse_quick_reminder


class TestReminder:
    def test_parse_minutes(self):
        result = parse_quick_reminder("30分钟后提醒我喝水")
        assert result is not None
        delay, content = result
        assert delay == 30 * 60
        assert "喝水" in content

    def test_parse_hours(self):
        result = parse_quick_reminder("2小时后提醒我休息")
        assert result is not None
        delay, content = result
        assert delay == 2 * 3600

    def test_parse_seconds(self):
        result = parse_quick_reminder("5秒后提醒我")
        assert result is not None
        delay, content = result
        assert delay == 5

    def test_parse_no_reminder(self):
        result = parse_quick_reminder("你好")
        assert result is None

    def test_parse_half_hour(self):
        result = parse_quick_reminder("一个半小时后提醒我")
        assert result is not None
        delay, content = result
        assert delay == 1800
