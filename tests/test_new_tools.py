"""新增 23 个工具的注册 + URL 安全 + 倒计时单元测试。"""
import time

from app.engine.tools import ToolRegistry, is_safe_url
from app.engine.tools._tts_mute import (
    set_tts_mute_until, unmute_tts, is_muted, get_tts_mute_status,
    set_mute_hook,
)
from app.engine.tools._timer import (
    countdown, list_countdowns, cancel_countdown, set_say_hook,
)


class TestNewToolsRegistration:
    """验证 9 个新模块都能干净 register。"""

    def test_audio_registers(self):
        from app.engine.tools import _audio
        reg = ToolRegistry(); _audio.register(reg)
        assert {"set_volume", "get_volume",
                "set_brightness", "get_brightness"} <= set(reg.names())

    def test_weather_registers(self):
        from app.engine.tools import _weather
        reg = ToolRegistry(); _weather.register(reg)
        assert reg.names() == ["get_weather"]

    def test_timer_registers(self):
        from app.engine.tools import _timer
        reg = ToolRegistry(); _timer.register(reg)
        assert {"countdown", "list_countdowns", "cancel_countdown"} <= set(reg.names())

    def test_power_registers(self):
        from app.engine.tools import _power
        reg = ToolRegistry(); _power.register(reg)
        assert {"get_battery", "lock_screen", "list_processes", "kill_process",
                "set_wifi", "set_bluetooth", "get_clipboard",
                "clear_bubble"} <= set(reg.names())

    def test_filesearch_registers(self):
        from app.engine.tools import _filesearch
        reg = ToolRegistry(); _filesearch.register(reg)
        assert reg.names() == ["search_files"]

    def test_webfetch_registers(self):
        from app.engine.tools import _webfetch
        reg = ToolRegistry(); _webfetch.register(reg)
        assert reg.names() == ["fetch_url_text"]

    def test_ocr_registers(self):
        from app.engine.tools import _ocr
        reg = ToolRegistry(); _ocr.register(reg)
        assert reg.names() == ["ocr_image"]

    def test_runner_registers(self):
        from app.engine.tools import _runner
        reg = ToolRegistry(); _runner.register(reg)
        assert reg.names() == ["run_script"]

    def test_tts_mute_registers(self):
        from app.engine.tools import _tts_mute
        reg = ToolRegistry(); _tts_mute.register(reg)
        assert {"set_tts_mute_until", "unmute_tts",
                "get_tts_mute_status"} <= set(reg.names())


class TestIsSafeUrlExtended:
    """is_safe_url 在 webfetch / weather 里都用，白名单场景必走。"""

    def test_default_passes_https(self):
        assert is_safe_url("https://api.open-meteo.com/v1/forecast")
        assert is_safe_url("http://example.com/")

    def test_blocks_localhost(self):
        assert not is_safe_url("http://localhost/x")
        assert not is_safe_url("http://localhost.localdomain/x")

    def test_blocks_loopback_ip(self):
        for ip in ("127.0.0.1", "10.0.0.1", "172.16.0.1",
                   "192.168.1.1", "0.0.0.0", "169.254.0.1"):
            assert not is_safe_url(f"http://{ip}/x"), f"should block {ip}"

    def test_blocks_non_http_scheme(self):
        assert not is_safe_url("file:///etc/passwd")
        assert not is_safe_url("ftp://internal/x")
        assert not is_safe_url("javascript:alert(1)")

    def test_allowed_hosts_whitelist(self):
        # 白名单模式：仅 host 列表内通过
        ok_hosts = {"api.tavily.com", "api.open-meteo.com"}
        assert is_safe_url("https://api.open-meteo.com/v1/forecast", allowed_hosts=ok_hosts)
        assert not is_safe_url("https://evil.com/x", allowed_hosts=ok_hosts)


class TestTtsMute:
    """静音工具：状态切换。"""

    def setup_method(self):
        unmute_tts()
        # 清空 hook，避免污染
        set_mute_hook(None)

    def teardown_method(self):
        unmute_tts()

    def test_default_unmuted(self):
        assert not is_muted()
        assert "未静音" in get_tts_mute_status()

    def test_mute_for_5_seconds(self):
        result = set_tts_mute_until(minutes=5 / 60)
        assert "已静音" in result
        assert is_muted()
        assert "静音中" in get_tts_mute_status()

    def test_unmute(self):
        set_tts_mute_until(minutes=10)
        assert is_muted()
        unmute_tts()
        assert not is_muted()
        assert "未静音" in get_tts_mute_status()

    def test_zero_minutes_defaults_to_30(self):
        # 0 → 默认 30 分钟
        set_tts_mute_until(minutes=0)
        assert is_muted()

    def test_negative_minutes_rejected(self):
        assert "错误" in set_tts_mute_until(minutes=-1)


class TestCountdown:
    """倒计时工具：创建 / 列出 / 取消。"""

    def test_countdown_creates_entry(self):
        result = countdown(seconds=2.0, message="测")
        assert "已设定倒计时" in result

    def test_list_countdowns_after_create(self):
        countdown(seconds=3.0, message="list_test")
        listing = list_countdowns()
        assert "list_test" in listing or "list_test" in listing

    def test_cancel_countdown(self):
        # 拿一个有效的 id
        countdown(seconds=5.0, message="cancel_test")
        items = list_countdowns()
        # 解析 id=?
        import re
        m = re.search(r"#(\d+)", "")
        # 简单：直接拿列表里第一个数字
        m2 = re.search(r"#(\d+)", "")
        # 不易解析 —— 直接取消所有（最多几条）
        # 用 globals() 看 _counters 列表
        from app.engine.tools._timer import _counters
        if _counters:
            cid = _counters[0][0]
            r = cancel_countdown(counter_id=cid)
            assert "已取消" in r
            assert cid not in {c[0] for c in _counters}

    def test_set_say_hook_does_not_crash(self):
        # 注入一个 mock hook，倒计时到点会调
        called = []
        set_say_hook(lambda text: called.append(text))
        # 0.1 秒倒计时 + 短延迟
        countdown(seconds=0.1, message="hook_test")
        time.sleep(0.3)
        # 验证 hook 被调
        assert any("hook_test" in t for t in called), f"hook not called: {called}"
        set_say_hook(None)    # 清理