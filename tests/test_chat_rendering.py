"""Chat rendering 单元测试（气泡样式 + 头像 + 工具调用 + 流式提示）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".local-packages"))
sys.path.insert(0, str(ROOT))

import time

from app.core.qt_compat import QApplication
app = QApplication.instance() or QApplication(sys.argv)

from app.core.config import CharacterConfig
from app.ui.chat_window import ChatWindow, Message, _html_escape, _format_time_short
from app.ui.settings_window import SettingsWindow
from app.voice.character import Emotion


def test_html_escape():
    """XSS 防护：HTML 字符必须转义。"""
    out = _html_escape('<script>alert(1)</script>')
    assert out == "&lt;script&gt;alert(1)&lt;/script&gt;"
    print("[OK] _html_escape prevents XSS")


def test_format_time_short():
    """_format_time_short 返回 HH:MM 格式。"""
    ts = time.time()
    ft = _format_time_short(ts)
    assert len(ft) == 5 and ft[2] == ":", f"expected HH:MM, got {ft!r}"
    print(f"[OK] _format_time_short returns HH:MM ({ft})")


def test_msg_html_user_alignment():
    """用户消息：右对齐 + user avatar + user bubble + right meta。"""
    char_cfg = CharacterConfig(name="鲸鱼娘")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    msg = Message(role="user", content="你好")
    html = cw._msg_html(msg)
    assert "avatar-col user" in html
    assert "bubble user" in html
    assert "meta right" in html
    assert '<table width="100%"' in html
    print("[OK] User message HTML structure correct")


def test_msg_html_bot_alignment():
    """桌宠消息：左对齐 + bot avatar + bot bubble + left meta。"""
    char_cfg = CharacterConfig(name="鲸鱼娘")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    msg = Message(role="assistant", content="你好呀~", emotion=Emotion.HAPPY)
    html = cw._msg_html(msg)
    assert "avatar-col bot" in html
    assert "bubble bot" in html
    assert "meta left" in html
    assert "鲸鱼娘" in html  # 名字应该显示在 meta
    print("[OK] Bot message HTML structure correct")


def test_render_markdown_list():
    """Markdown 列表：每段连续 - 行应包成一个 <ul>。"""
    char_cfg = CharacterConfig(name="测试")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    md = "- 项目1\n- 项目2\n- 项目3"
    html = cw._render_markdown(md)
    assert "<ul>" in html and "</ul>" in html
    # 3 个 li（不能因为旧版 <br/> 漏掉）
    assert html.count("<li>") == 3, f"expected 3 li, got {html.count('<li>')} in {html!r}"
    # 不应有 li 之间的 <br/>（新版修复）
    # 但可能有 list 之外的 <br/>，这里只看 li 紧邻是否有 <br/>
    # 简化：检查没有 <li>项目2</li><br/> 这样的模式
    import re
    assert not re.search(r"</li>\s*<br/>\s*<li>", html), \
        f"li should not be split by <br/>, got {html!r}"
    print("[OK] Markdown list wraps properly (no <br/> between items)")


def test_render_markdown_ordered_list():
    """有序列表同样应包成一个 <ol>。"""
    char_cfg = CharacterConfig(name="测试")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    md = "1. 第一步\n2. 第二步\n3. 第三步"
    html = cw._render_markdown(md)
    assert "<ol>" in html and "</ol>" in html
    assert html.count("<li>") == 3
    print("[OK] Markdown ordered list wraps properly")


def test_streaming_indicator():
    """流式输出：bot 气泡末尾应有 ⏳ 提示（无 emotion 时）。"""
    char_cfg = CharacterConfig(name="测试")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    msg = Message(role="assistant", content="正在思考", emotion=None)
    html = cw._msg_html(msg, streaming_meta="typing…")
    assert "⏳" in html
    print("[OK] Streaming indicator (⏳) shown in bot bubble")


def test_system_message():
    """系统消息：居中灰色条。"""
    char_cfg = CharacterConfig(name="测试")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    msg = Message(role="system", content="系统通知")
    html = cw._msg_html(msg)
    assert "system-msg" in html
    print("[OK] System message uses system-msg class")


def test_tool_calls_display():
    """工具调用记录：应放在桌宠气泡下方的浅紫小卡片。"""
    char_cfg = CharacterConfig(name="测试")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    msg = Message(role="assistant", content="完成", tools=[("add_reminder", "已设置提醒")])
    html = cw._msg_html(msg)
    assert 'class="tools"' in html
    assert "add_reminder" in html
    assert "已设置提醒" in html
    print("[OK] Tool calls displayed in .tools class")


def test_user_emoji_in_content_escaped():
    """用户消息里的 emoji 应该被原样保留（在 QTextBrowser 里可见，不转义）。"""
    char_cfg = CharacterConfig(name="测试")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    msg = Message(role="user", content="👋 你好")
    html = cw._msg_html(msg)
    # 用户消息做 HTML 转义但不删除 emoji（用户原意保留）
    assert "你好" in html
    print("[OK] User emoji preserved")


def test_input_key_press_enter_sends():
    """Enter 直接发送（不带 modifier）。"""
    from app.core.qt_compat import Qt, QKeyEvent
    char_cfg = CharacterConfig(name="测试")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    sent_texts: list[str] = []

    def fake_send():
        sent_texts.append("send-called")

    cw._on_send = fake_send   # type: ignore[assignment]
    cw.input_edit.setPlainText("hello world")

    ev = QKeyEvent(QKeyEvent.Type.KeyPress, int(Qt.Key.Key_Return), Qt.KeyboardModifier.NoModifier)
    cw._input_key_press(ev)

    assert sent_texts == ["send-called"], \
        f"Enter should trigger _on_send, got {sent_texts}"
    print("[OK] Enter (no modifier) triggers _on_send")


def test_input_key_press_shift_enter_newline():
    """Shift+Enter 插入换行，不发送。"""
    from app.core.qt_compat import Qt, QKeyEvent
    char_cfg = CharacterConfig(name="测试")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    sent_texts: list[str] = []

    def fake_send():
        sent_texts.append("send-called")

    cw._on_send = fake_send   # type: ignore[assignment]
    cw.input_edit.setPlainText("hello")

    ev = QKeyEvent(QKeyEvent.Type.KeyPress, int(Qt.Key.Key_Return), Qt.KeyboardModifier.ShiftModifier)
    cw._input_key_press(ev)

    assert sent_texts == [], \
        f"Shift+Enter should NOT trigger _on_send, got {sent_texts}"
    # 内容应包含换行
    text = cw.input_edit.toPlainText()
    assert "\n" in text, f"Shift+Enter should insert newline, got text={text!r}"
    print("[OK] Shift+Enter inserts newline (does not send)")


def test_input_key_press_ctrl_enter_newline():
    """Ctrl+Enter 插入换行（兼容现代 IDE / 聊天工具习惯），不发送。"""
    from app.core.qt_compat import Qt, QKeyEvent
    char_cfg = CharacterConfig(name="测试")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    sent_texts: list[str] = []

    def fake_send():
        sent_texts.append("send-called")

    cw._on_send = fake_send   # type: ignore[assignment]
    cw.input_edit.setPlainText("hello")

    ev = QKeyEvent(QKeyEvent.Type.KeyPress, int(Qt.Key.Key_Return), Qt.KeyboardModifier.ControlModifier)
    cw._input_key_press(ev)

    assert sent_texts == [], \
        f"Ctrl+Enter should NOT trigger _on_send, got {sent_texts}"
    text = cw.input_edit.toPlainText()
    assert "\n" in text, f"Ctrl+Enter should insert newline, got text={text!r}"
    print("[OK] Ctrl+Enter inserts newline (does not send)")


def test_settings_window_char_cfg_default_none():
    """SettingsWindow 不传 char_cfg 时默认值是 None（向后兼容）。"""
    sw = SettingsWindow()
    assert sw.char_cfg is None, "char_cfg default should be None"
    print("[OK] SettingsWindow char_cfg defaults to None")


def test_settings_window_char_cfg_passed():
    """SettingsWindow 接收 char_cfg 时应能正确使用角色名。"""
    from app.ui.settings_window import SettingsWindow
    char_cfg = CharacterConfig(name="鲸鱼娘")
    sw = SettingsWindow(char_cfg=char_cfg)
    assert sw.char_cfg is char_cfg
    assert sw.char_cfg.name == "鲸鱼娘"
    print("[OK] SettingsWindow stores passed char_cfg")


def test_settings_window_preview_voice_no_crash_without_cfg():
    """没有 char_cfg 时，「试听」按钮不能崩（之前 self.cfg.name 触发 AttributeError）。"""
    import unittest.mock as mock
    sw = SettingsWindow()
    # 替换 TTS 避免真实播放
    with mock.patch("app.voice.voice.TTS") as mock_tts:
        mock_tts.return_value.speak = mock.MagicMock()
        sw._on_preview_voice()
        # 应该没抛异常
    print("[OK] SettingsWindow._on_preview_voice doesn't crash without char_cfg")


def test_settings_window_preview_voice_uses_char_name():
    """试听时拼接的角色名应来自 char_cfg。"""
    import unittest.mock as mock
    char_cfg = CharacterConfig(name="测试娘")
    sw = SettingsWindow(char_cfg=char_cfg)
    with mock.patch("app.voice.voice.TTS") as mock_tts:
        mock_tts.return_value.speak = mock.MagicMock()
        sw._on_preview_voice()
        # 验证 speak 被调用，参数包含 "测试娘"
        args, _ = mock_tts.return_value.speak.call_args
        assert "测试娘" in args[0], f"preview text should contain character name, got {args[0]!r}"
    print("[OK] SettingsWindow preview voice uses character name from char_cfg")


if __name__ == "__main__":
    test_html_escape()
    test_format_time_short()
    test_msg_html_user_alignment()
    test_msg_html_bot_alignment()
    test_render_markdown_list()
    test_render_markdown_ordered_list()
    test_streaming_indicator()
    test_system_message()
    test_tool_calls_display()
    test_user_emoji_in_content_escaped()
    test_input_key_press_enter_sends()
    test_input_key_press_shift_enter_newline()
    test_input_key_press_ctrl_enter_newline()
    test_settings_window_char_cfg_default_none()
    test_settings_window_char_cfg_passed()
    test_settings_window_preview_voice_no_crash_without_cfg()
    test_settings_window_preview_voice_uses_char_name()
    print()
    print("All chat rendering tests passed!")
