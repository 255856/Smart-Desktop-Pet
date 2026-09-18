"""工具系统单元测试。"""
import pytest
import json
from app.engine.tools import ToolRegistry, Tool


class TestToolRegistry:
    def test_register_and_names(self):
        reg = ToolRegistry()
        reg.register(Tool(name="test", description="test", parameters={}, fn=lambda: "ok"))
        assert "test" in reg.names()

    def test_execute_unknown(self):
        reg = ToolRegistry()
        result = reg.execute("unknown", "{}")
        assert "错误" in result

    def test_execute_invalid_json(self):
        reg = ToolRegistry()
        reg.register(Tool(name="test", description="test", parameters={}, fn=lambda: "ok"))
        result = reg.execute("test", "invalid json")
        assert "错误" in result

    def test_execute_success(self):
        reg = ToolRegistry()
        reg.register(Tool(name="test", description="test", parameters={}, fn=lambda: "ok"))
        result = reg.execute("test", "{}")
        assert result == "ok"

    def test_to_openai(self):
        reg = ToolRegistry()
        reg.register(Tool(name="test", description="test",
                         parameters={"type": "object", "properties": {}},
                         fn=lambda: "ok"))
        tools = reg.to_openai()
        assert len(tools) == 1
        assert tools[0]["function"]["name"] == "test"


class TestFrameOriginal:
    """验证 Frame.original 属性修复了 crash.log 中的 AttributeError。"""

    def test_frame_has_original(self):
        """Frame 有 original 字段，默认为 None，可赋值。"""
        from app.animation.animations import Frame
        sentinel = object()  # 用普通对象代替 QPixmap（无需 QApplication）
        f = Frame(pixmap=sentinel, duration_ms=100, original=sentinel)
        assert f.original is not None
        assert f.original is sentinel

    def test_frame_original_default_none(self):
        """不传 original 时默认为 None（向后兼容）。"""
        from app.animation.animations import Frame
        sentinel = object()
        f = Frame(pixmap=sentinel, duration_ms=100)
        assert f.original is None
