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
        """一个半 = 1.5（口语），所以「一个半小时后」= 1.5 小时 = 5400 秒，不是 1800 秒。"""
        result = parse_quick_reminder("一个半小时后提醒我")
        assert result is not None
        delay, content = result
        assert delay == 5400   # 1.5 hours（口语里「一个半」就是 1.5）

    def test_parse_chinese_multi_half_hours(self):
        """中文数字「X个半小时」= X × 30 分钟（5个半小时 = 2.5 小时 = 9000 秒）。"""
        result = parse_quick_reminder("五个半小时后提醒我遛狗")
        assert result is not None
        delay, content = result
        assert delay == 5 * 1800
        assert "遛狗" in content

    def test_parse_arabic_multi_half_hours(self):
        """阿拉伯数字「X个半小时」= X × 30 分钟。"""
        result = parse_quick_reminder("5个半小时后提醒我吃饭")
        assert result is not None
        delay, content = result
        assert delay == 5 * 1800

    def test_parse_polite_prefix(self):
        """「帮我」「请设置提醒：」等礼貌前缀应被剥掉。"""
        result = parse_quick_reminder("帮我30分钟后提醒我去取快递")
        assert result is not None
        delay, content = result
        assert delay == 30 * 60
        assert "取快递" in content

        result2 = parse_quick_reminder("请设置提醒：30分钟后喝水")
        assert result2 is not None
        assert result2[0] == 30 * 60
        assert "喝水" in result2[1]
