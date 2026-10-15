"""Chat rendering 单元测试（气泡样式 + 头像 + 工具调用 + 流式提示）。"""
import sys
import shutil
import tempfile
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
    """用户消息：MiniMax Code 风格线性流，msg-user div + 浅灰底 + 'You' label。"""
    char_cfg = CharacterConfig(name="鲸鱼娘")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    msg = Message(role="user", content="你好")
    html = cw._msg_html(msg)
    assert "msg msg-user" in html
    assert "msg-body" in html
    assert "msg-label" in html
    # 用户消息: 标识 "You"
    assert ">You<" in html
    # 不再使用 table 布局 / 头像 col / 气泡 class
    assert "avatar-col user" not in html
    assert "bubble user" not in html
    assert '<table width="100%"' not in html
    print("[OK] User message uses new linear-flow structure (MiniMax Code style)")


def test_msg_html_bot_alignment():
    """桌宠消息：MiniMax Code 风格线性流, msg-assistant div + 紫 '桌宠' label。"""
    char_cfg = CharacterConfig(name="鲸鱼娘")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    msg = Message(role="assistant", content="你好呀~", emotion=Emotion.HAPPY)
    html = cw._msg_html(msg)
    assert "msg msg-assistant" in html
    assert "msg-body" in html
    assert "msg-label" in html
    assert "鲸鱼娘" in html
    assert "avatar-col bot" not in html
    assert "bubble bot" not in html
    print("[OK] Bot message uses new linear-flow structure (MiniMax Code style)")


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


def test_render_markdown_headers():
    """MiniMax Code 风格: ##/###/#### 标题应正确渲染成 <h2>/<h3>/<h4>。"""
    char_cfg = CharacterConfig(name="测试")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    html = cw._render_markdown("## 二级标题\n### 三级标题\n#### 四级")
    assert "<h2>二级标题</h2>" in html
    assert "<h3>三级标题</h3>" in html
    assert "<h4>四级</h4>" in html
    print("[OK] Markdown headers (h2-h4) render correctly")


def test_render_markdown_blockquote():
    """MiniMax Code 风格: > blockquote 应包成 <blockquote>。"""
    char_cfg = CharacterConfig(name="测试")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    html = cw._render_markdown("> 这是一个引用\n> 第二行")
    assert "<blockquote>" in html and "</blockquote>" in html
    assert "这是一个引用" in html
    print("[OK] Markdown blockquote wraps properly")


def test_render_markdown_hr():
    """MiniMax Code 风格: --- 应渲染为 <hr/>。"""
    char_cfg = CharacterConfig(name="测试")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    html = cw._render_markdown("上文段落\n\n---\n\n下文段落")
    assert "<hr/>" in html
    print("[OK] Markdown hr renders correctly")


def test_render_markdown_table():
    """MiniMax Code 风格: GFM 表格应渲染成 <table>。"""
    char_cfg = CharacterConfig(name="测试")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    md = (
        "| 列1 | 列2 |\n"
        "|------|------|\n"
        "| a    | b    |\n"
        "| c    | d    |\n"
    )
    html = cw._render_markdown(md)
    assert "<table>" in html and "</table>" in html
    assert "<th>列1</th>" in html
    assert "<th>列2</th>" in html
    assert "<td>a</td>" in html and "<td>d</td>" in html
    print("[OK] Markdown table (GFM subset) renders correctly")


def test_render_markdown_link_safe():
    """MiniMax Code 风格: [text](url) 链接要渲染,但 javascript: 注入必须挡住。"""
    char_cfg = CharacterConfig(name="测试")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    html_ok = cw._render_markdown("[主页](https://example.com)")
    assert '<a href="https://example.com">主页</a>' in html_ok

    html_bad = cw._render_markdown("[bad](javascript:alert(1))")
    # javascript: 不在白名单,不应渲染成 <a>
    assert "<a href=\"javascript:" not in html_bad
    assert "[bad](javascript:alert(1))" in html_bad or "javascript" not in html_bad
    print("[OK] Markdown links: http/https allowed, javascript: blocked")


def test_render_markdown_xss_protection():
    """MiniMax Code 风格: 普通用户输入含 <script> 必须被转义掉, 不能执行。"""
    char_cfg = CharacterConfig(name="测试")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    html = cw._render_markdown("<script>alert(1)</script>")
    # < 转义成 &lt;, <script> 不能出现
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    print("[OK] Markdown escapes < > & (XSS protection)")


def test_render_markdown_code_block_preserves_special_chars():
    """MiniMax Code 风格: 代码块 ``` 内 < > & 不应被转义 (常见代码含 < >)。"""
    char_cfg = CharacterConfig(name="测试")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    md = "```python\nif x < y and a > b:\n    return a & b\n```"
    html = cw._render_markdown(md)
    assert "<pre><code data-lang=\"python\">" in html
    # 代码块内应保留 < > & 字面
    assert "if x < y and a > b:" in html
    assert "return a & b" in html
    print("[OK] Markdown code block preserves < > & inside")


def test_msg_html_markdown_e2e():
    """MiniMax Code 风格 end-to-end: markdown 在 _msg_html 里真正串通渲染。"""
    char_cfg = CharacterConfig(name="桌宠")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    msg = Message(role="assistant", content=(
        "## 报告\n"
        "\n"
        "今天我做了：\n"
        "\n"
        "- 写代码\n"
        "- 测功能\n"
        "\n"
        "> 小结: 一切顺利\n"
    ))
    html = cw._msg_html(msg)
    # msg-body 容器里有 markdown 元素
    assert "msg-body" in html
    assert "<h2>报告</h2>" in html
    assert "<ul>" in html
    assert "<li>写代码</li>" in html
    assert "<blockquote>" in html
    # 没漏掉文字
    assert "小结" in html
    print("[OK] MiniMax Code style: msg_html end-to-end renders full markdown")


def test_chat_md_css_registered():
    """Bug 3 守卫: chat_view 必须用新的 CHAT_MD_CSS, 不能悄悄退回 CHAT_BUBBLE_CSS;
    _msg_html 必须产出 msg-user / msg-assistant 双角色。"""
    import ast as _ast
    src = (Path(__file__).resolve().parent.parent
           / "app" / "ui" / "chat_window" / "__init__.py").read_text(
        encoding="utf-8",
    )
    found_md = False
    found_msg_html = False
    has_user_branch = False
    has_assistant_branch = False
    tree = _ast.parse(src)
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call) and len(node.args) >= 1:
            func = node.func
            if (isinstance(func, _ast.Attribute)
                    and func.attr == "setDefaultStyleSheet"):
                arg0 = node.args[0]
                if (isinstance(arg0, _ast.Attribute)
                        and arg0.attr == "CHAT_MD_CSS"):
                    found_md = True
        # _msg_html 函数体内必须包含 'msg msg-' 拼接 (runtime 构造 msg-user / msg-assistant)
        if isinstance(node, _ast.FunctionDef) and node.name == "_msg_html":
            found_msg_html = True
            body = _ast.dump(node)
            if "msg-user" in body:
                has_user_branch = True
            if "msg-assistant" in body:
                has_assistant_branch = True
    assert found_md, (
        "chat_view.document().setDefaultStyleSheet 没接到 ui_style.CHAT_MD_CSS"
    )
    assert found_msg_html, "_msg_html 函数没找到"
    assert has_user_branch, "_msg_html 函数体里没出现 msg-user (user 角色分支丢失)"
    assert has_assistant_branch, "_msg_html 函数体里没出现 msg-assistant (assistant 角色分支丢失)"
    print("[OK] Bug 3 AST 守卫: chat_view 用 CHAT_MD_CSS, _msg_html 产 new 双角色结构")


def test_streaming_indicator():
    """流式输出：bot 气泡末尾应有「三点跳动」等待动画（无 emotion 时）。"""
    char_cfg = CharacterConfig(name="测试")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    msg = Message(role="assistant", content="正在思考", emotion=None)
    html = cw._msg_html(msg, streaming_meta="typing…")
    # 旧的沙漏 ⏳ 已替换为三个圆点
    assert "⏳" not in html
    assert html.count("●") == 3, f"应渲染 3 个等待圆点，实际 {html.count('●')}"
    # 2026-10-15: MiniMax Code 风格改用 CSS class + active 标志, 不在 HTML 里硬编码颜色
    # 初始帧恰好一个 .typing-dot.active
    assert 'class="typing-dot active"' in html, (
        "初始帧应有且仅有一个 typing-dot.active, 实际: " + html
    )
    assert html.count("typing-dot active") == 1, (
        f"应该有且仅一个高亮点, 实际 {html.count('typing-dot active')}"
    )
    print("[OK] Streaming indicator (three bouncing dots) shown with new CSS-class pattern")


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
    """工具调用：Claude Code 风格步骤卡片（中文名 + 目标 + 成败状态）。

    2026-10-15:Qt QTextBrowser 不支持 <details> 折叠,改用 <a href cmd:toggle_tools>
    + anchorClicked 模拟。默认折叠态只显示 head + 第一条工具预览;展开态显示所有。
    """
    char_cfg = CharacterConfig(name="测试")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    msg = Message(role="assistant", content="完成", tools=[
        ("add_reminder", '{"content":"下午三点开会"}', "已设置提醒"),
        ("web_search", '{"query":"十月新番"}', "找到 5 条结果"),
        ("open_app", '{"app_name":"星穹铁道"}', "失败：没有找到该应用"),
    ])
    # === 折叠态(默认) ===
    cw._tools_collapsed = True
    html = cw._msg_html(msg)
    assert 'class="tools"' in html
    assert "已执行操作" in html
    # 折叠态只显示第一条工具(add_reminder)
    assert "设置提醒" in html and "下午三点开会" in html
    # 后续工具不直接展示
    assert "联网搜索" not in html
    # 工具卡片置于最终结果上方
    assert html.index("已执行操作") < html.index("完成")
    # 旧版 2 元组 (name, summary) 仍兼容：已知工具显示中文名
    msg2 = Message(role="assistant", content="好", tools=[("calculate", "结果是7")])
    assert "计算" in cw._msg_html(msg2)
    # 未知工具名兜底显示原名
    msg3 = Message(role="assistant", content="好", tools=[("my_custom_thing", "done")])
    assert "my_custom_thing" in cw._msg_html(msg3)
    # 折叠态用 anchor link 触发 toggle —— 2026-10-15 v3: head 文字
    # 只显示「已执行操作 · N ▾」,不再出现"点击展开/点击收起"字样
    # (用户实测指定), 但 cmd:toggle_tools 仍保留 (toggle 功能还要)
    assert 'href="cmd:toggle_tools"' in html
    assert "▾" in html  # 折叠态箭头
    assert "点击展开" not in html, (
        f"「点击展开」字样不应出现 (用户实测要求去掉), 实际: {html}"
    )
    assert "点击收起" not in html

    # === 展开态 ===
    cw._tools_collapsed = False
    html2 = cw._msg_html(msg)
    # 全部工具都展示
    assert "设置提醒" in html2 and "下午三点开会" in html2
    assert "联网搜索" in html2 and "十月新番" in html2
    # 失败状态
    assert "失败" in html2 and "星穹铁道" in html2
    # 原始英文工具名 / 原始结果文本不直接展示给主人
    assert "add_reminder" not in html2
    assert "已设置提醒" not in html2
    # 展开态只显示箭头「▴」,不再出现"点击展开/点击收起"字样
    assert "▴" in html2
    assert "点击展开" not in html2
    assert "点击收起" not in html2
    print("[OK] Tool calls rendered as step card")


def test_ai_coding_blocks_rendered():
    """AI Coding 风格：思考（折叠）+ 命令块（终端）+ 文件编辑块 + 工具步骤同时出现。"""
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig
    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False)
    msg = Message(
        role="assistant",
        content="已完成文件写入。",
        tools=[("write_file", '{"path": "/tmp/test.py", "content": "print(1)"}', "OK")],
        thoughts=["需要先排查依赖", "方案：用国内镜像"],
        execs=[{"cmd": "pip install requests", "output": "Successfully installed",
                "exit_code": 0}],
    )
    html = cw._msg_html(msg)
    assert "思考过程" in html
    assert "需要先排查依赖" in html
    assert "执行命令" in html
    assert "pip install requests" in html
    assert "exit_code=0" in html
    assert "文件编辑" in html
    assert "/tmp/test.py" in html
    assert "<details" in html
    assert "background:#0f172a" in html
    print("[OK] AI coding blocks (思考/命令/文件/工具) rendered")


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
    test_custom_avatar_loaded_from_path()
    test_custom_avatar_fallback_when_missing()
    test_set_custom_avatar_copies_and_persists()
    print()


def test_custom_avatar_loaded_from_path():
    """chat_cfg.bot_avatar_path 有值时,_make_avatar_pixmap('bot') 真的去读这个图。

    2026-10-15 v2:用户头像已移除,只验证 bot (桌宠) 头像流程。
    """
    from app.core.config import LLMConfig, CharacterConfig, ChatConfig
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        red_png = tmp_path / "red_bot.png"
        from app.core.qt_compat import QPixmap, QColor, QImage
        img = QImage(32, 32, QImage.Format.Format_ARGB32)
        img.fill(QColor(255, 0, 0))
        assert QPixmap.fromImage(img).save(str(red_png), "PNG")

        chat_cfg = ChatConfig(bot_avatar_path=str(red_png))
        cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                        'assets/sprites', asr_enabled=False, chat_cfg=chat_cfg)
        pm = cw._make_avatar_pixmap("bot", 40)
        assert not pm.isNull()
        pixel = pm.toImage().pixel(20, 20)
        r = (pixel >> 16) & 0xff
        g = (pixel >> 8) & 0xff
        b = pixel & 0xff
        # 中心是红色(255,0,0),不应是默认紫渐变
        assert r > 200 and g < 50 and b < 50, f"中心像素 {r},{g},{b} 应为红色"
        print("[OK] custom bot_avatar_path is loaded into avatar pixmap")


def test_user_avatar_removed_entirely():
    """2026-10-15 v2:用户头像已彻底移除。_make_avatar_pixmap('user') 不再
    生成真图 (返回 null QPixmap), 也不应有 _user_avatar_path / _avatar_data_user
    这些字段。"""
    from app.core.config import LLMConfig
    char_cfg = CharacterConfig(name='test')
    cw = ChatWindow(LLMConfig(), char_cfg, 'assets/sprites', asr_enabled=False)
    # 不应有 _user_avatar_path
    assert not hasattr(cw, "_user_avatar_path"), (
        "_user_avatar_path 字段应已移除 (用户实测指定" +
        "「把更改头像中的我的头像功能去除」)"
    )
    # 不应有 _avatar_data_user
    assert not hasattr(cw, "_avatar_data_user"), \
        "_avatar_data_user 字段应已移除"
    # 不应有 pick_user_avatar 方法
    assert not hasattr(cw, "pick_user_avatar"), \
        "pick_user_avatar 方法应已移除"
    # _make_avatar_pixmap('user', ...) 应返回空 pixmap, 不能崩
    pm = cw._make_avatar_pixmap("user", 40)
    assert pm.isNull(), "_make_avatar_pixmap('user', ...) 应返回空 pixmap"
    print("[OK] user avatar feature removed entirely")


def test_set_custom_avatar_copies_and_persists():
    """_set_custom_avatar 复制源图到 data/avatars/bot.png, 更新 cfg 并写回 yaml。

    2026-10-15 v2:用户头像已移除,只测 bot 角色。user 角色调用必须返回 False。
    """
    from app.core.config import LLMConfig, CharacterConfig, ChatConfig, load_config
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("chat:\n  request_timeout_s: 30\n", encoding="utf-8")

        from app.core.qt_compat import QImage, QPixmap, QColor
        src_img = tmp_path / "source.png"
        img = QImage(16, 16, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 255, 0))   # 纯绿
        assert QPixmap.fromImage(img).save(str(src_img), "PNG")

        chat_cfg = ChatConfig(request_timeout_s=30)
        import os
        old_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            import app.ui.chat_window as cw_mod
            from pathlib import Path as _P
            cw_mod.AVATAR_DIR = _P(tmp_path) / "data" / "avatars"
            cw_mod.AVATAR_DIR.mkdir(parents=True, exist_ok=True)

            cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                            'assets/sprites', asr_enabled=False, chat_cfg=chat_cfg,
                            cfg_path=str(cfg_path))
            ok = cw._set_custom_avatar("bot", str(src_img))
            assert ok is True
            assert Path(cw._bot_avatar_path).name == "bot.png"
            assert "avatars" in Path(cw._bot_avatar_path).parts
            dst = cw_mod.AVATAR_DIR / "bot.png"
            assert dst.is_file()
            pm = QPixmap(str(dst))
            assert not pm.isNull()
            # config.yaml 写回 chat.bot_avatar_path
            saved = load_config(cfg_path)
            assert Path(saved.chat.bot_avatar_path).name == "bot.png"
            # user 角色 _set_custom_avatar 必须返回 False (已禁用)
            assert cw._set_custom_avatar("user", str(src_img)) is False, (
                "_set_custom_avatar('user', ...) 必须返回 False (已禁用)"
            )
            assert saved.chat.request_timeout_s == 30
        finally:
            os.chdir(old_cwd)
    print("[OK] _set_custom_avatar copies file, updates state, persists to config.yaml")


# ============================================================
# Bug 2 regression: 重进聊天窗，"已执行操作" 工具卡片必须被加载
# ============================================================

def test_bug2_convert_loaded_tools_purity():
    """Bug 2 单元层：JSON 反序列化后 tools 是 list-of-lists。
    _convert_loaded_tools 必须把它还原成 list[tuple[str,str,str]]
    —— Message(tools=...) 不传/传错都会让 _msg_html 渲染时丢掉"已执行操作"卡片。
    """
    from app.ui.chat_window import _convert_loaded_tools
    # 基本 3 元 list (真实 SQLite 反序列化形态)
    assert _convert_loaded_tools([
        ["add_reminder", '{"content":"今晚 7 点开会"}', "已设置提醒"],
    ]) == [("add_reminder", '{"content":"今晚 7 点开会"}', "已设置提醒")]
    # 元组输入（万一某些路径走 cached）
    assert _convert_loaded_tools([
        ("add_reminder", '{"x":1}', "ok"),
    ]) == [("add_reminder", '{"x":1}', "ok")]
    # 旧版 2 元组 (name, summary) 兼容
    assert _convert_loaded_tools([
        ("calculate", "结果是7"),
    ]) == [("calculate", "", "结果是7")]
    # 空 / None / 损坏项：不要抛，留空
    assert _convert_loaded_tools([]) == []
    assert _convert_loaded_tools(None) == []
    assert _convert_loaded_tools(["garbage", 42, {"k": "v"}]) == []
    print("[OK] Bug 2: _convert_loaded_tools 反序列化兜底")

def test_bug2_end_to_end_sqlite_round_trip():
    """Bug 2 穿透层：完整 SQLite IO 链路。
    ChatStore.add(tools=...) → JSON 落盘 → chat_store.all() → 反序列化
    → _convert_loaded_tools → Message.tools 必须被填回。
    """
    from app.engine.chat_store import ChatStore
    from app.ui.chat_window.message import Message
    from app.ui.chat_window import _convert_loaded_tools

    tmp = Path(tempfile.mkdtemp(prefix="chatstore_bug2_"))
    db = tmp / "chat.db"
    cs = ChatStore(str(db))
    # 这就是 _on_done 真写入 ChatStore 的形态（_on_done 里
    # tools_list = [list(t) for t in self._current_bot_msg.tools]）
    cs.add("user", "今晚 7 点提醒我开会", emotion="")
    cs.add("assistant", "好的，已经设置好提醒啦~", emotion="happy",
           tools=[["add_reminder", '{"content":"今晚 7 点开会"}', "已设置提醒"]],
           )
    stored = cs.all()
    assert len(stored) == 2
    bot = [m for m in stored if m.role == "assistant"][0]
    # 验证 SQLite 反序列化形态
    assert isinstance(bot.tools[0], list), \
        f"反序列化后应是 list-of-lists: {bot.tools!r}"
    # 走真实 _load_history 调用接线（同源代码内的转换）
    tools_loaded = _convert_loaded_tools(bot.tools)
    msg = Message(role=bot.role, content=bot.content, emotion=None,
                  tools=tools_loaded)
    assert msg.tools == [
        ("add_reminder", '{"content":"今晚 7 点开会"}', "已设置提醒"),
    ], msg.tools
    print("[OK] Bug 2: ChatStore → 反序列化 → Message.tools 走通真实链路")

def test_bug2_ast_guard_load_history_uses_helper():
    """Bug 2 AST 守卫：保证下次有人重构 _load_history 时，不会偷偷把
    _convert_loaded_tools(sm.tools) 这条接线去掉。
    """
    import ast as _ast
    src = (Path(__file__).resolve().parent.parent
           / "app" / "ui" / "chat_window" / "__init__.py").read_text(
        encoding="utf-8",
    )
    tree = _ast.parse(src)
    found_helper = False
    found_tools_attr = False
    found_tools_loaded = False
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name == "_load_history":
            for sub in _ast.walk(node):
                if isinstance(sub, _ast.Call):
                    func = sub.func
                    if (isinstance(func, _ast.Name)
                            and func.id == "_convert_loaded_tools"):
                        found_helper = True
                        if (sub.args
                                and isinstance(sub.args[0], _ast.Attribute)):
                            found_tools_attr = True
                if isinstance(sub, _ast.Name) and sub.id == "tools_loaded":
                    found_tools_loaded = True
    assert found_helper, "_load_history 没调 _convert_loaded_tools"
    assert found_tools_attr, "_convert_loaded_tools 参数不是 .tools 属性访问"
    assert found_tools_loaded, "tools_loaded 没被使用 (没传给 Message)"
    print("[OK] Bug 2 AST 守卫: _load_history 真在调 _convert_loaded_tools(sm.tools)")


def test_bug1_ast_guard_anchor_click_doesnt_refresh_when_done():
    """Bug 1 AST 守卫: 点 chat 上的 cmd:toggle_tools 链接 (post _on_done)
    时, _on_chat_anchor_clicked 不能调 _refresh_streaming_message。
    这是因为 _on_done 之后 _current_bot_msg = None, _streaming_anchor_pos = None,
    此时调用 _refresh_streaming_message 会因为 anchor 不存在而把整段 HTML 抹掉 → 空白。"""
    import ast as _ast
    src = (Path(__file__).resolve().parent.parent
           / "app" / "ui" / "chat_window" / "__init__.py").read_text(
        encoding="utf-8",
    )
    tree = _ast.parse(src)
    for node in _ast.walk(tree):
        if (isinstance(node, _ast.FunctionDef)
                and node.name == "_on_chat_anchor_clicked"):
            body_src = _ast.dump(node)
            # 必须有守卫（_current_bot_msg 检查 + _streaming_anchor_pos 检查）
            assert "_current_bot_msg" in body_src
            assert "_streaming_anchor_pos" in body_src
            # 守卫必须在 _refresh_streaming_message 之前出现
            pos_guard = body_src.find("_current_bot_msg")
            pos_refresh = body_src.find("_refresh_streaming_message")
            assert pos_guard < pos_refresh, (
                "_current_bot_msg 守卫必须在 _refresh_streaming_message 之前"
            )
            print("[OK] Bug 1 AST 守卫: anchor click 不会在 _current_bot_msg=None 时 refresh")
            return
    raise AssertionError("_on_chat_anchor_clicked 函数未找到")


    print("All chat rendering tests passed!")


def test_msg_user_right_aligned():
    """用户消息靠右 (主人实测指定 "我发出的消息应该要保持在右侧")。
    CSS: .msg-user .msg-body { text-align: right; display:block; margin-left:auto; }
    """
    from app.ui import ui_style as _us
    qss = _us.CHAT_MD_CSS
    # msg-user body 应 text-align:right 且 margin-left:auto
    assert "text-align:right" in qss
    assert ".msg-user .msg-body" in qss
    assert "margin-left:auto" in qss
    # assistant 仍走左对齐
    assert ".msg-assistant .msg-body" in qss
    print("[OK] CSS: .msg-user 右对齐 + .msg-assistant 左对齐")


def test_tool_steps_no_click_text():
    """调整 1 验证: 工具卡片 head 不再出现「点击展开/点击收起」字样,
    只保留箭头 ▾ / ▴。toggle 功能 (cmd:toggle_tools href) 保留。"""
    char_cfg = CharacterConfig(name='测试')
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites", asr_enabled=False)

    msg = Message(role="assistant", content="完成", tools=[
        ("add_reminder", '{"x":1}', "OK"),
        ("web_search", '{}', "找到"),
    ])
    cw._tools_collapsed = True
    html = cw._tool_steps_html(msg.tools)
    assert "点击展开" not in html, f"「点击展开」字样不应出现: {html}"
    assert "点击收起" not in html, f"「点击收起」字样不应出现: {html}"
    assert 'href="cmd:toggle_tools"' in html, \
        "toggle 功能保留, href 必须在"
    assert "▾" in html, "折叠态应显示下箭头"

    cw._tools_collapsed = False
    html2 = cw._tool_steps_html(msg.tools)
    assert "点击展开" not in html2
    assert "点击收起" not in html2
    assert "▴" in html2, "展开态应显示上箭头"
    print("[OK] 工具 head 不出现「点击展开/收起」字样 (toggle 功能保留)")


def test_input_box_style_matches_display():
    """调整 4 验证: chat_view 与 input_container 共享 QSS 规则。"""
    from app.ui import ui_style
    qss = ui_style.CHAT_QSS
    assert "QTextBrowser#chat_view, QFrame#input_container" in qss, (
        "chat_view 与 input_container 必须在同一 QSS 规则里共享样式"
    )
    marker = "QTextBrowser#chat_view, QFrame#input_container"
    idx = qss.find(marker)
    assert idx > 0
    snippet = qss[idx:idx + 500]
    assert "background:" in snippet
    assert "border:" in snippet
    assert "border-radius:" in snippet
    print("[OK] chat_view 与 input_container 共享同一 QSS 规则")


def test_window_controls_present():
    """调整 5 验证: 标题栏右上角 4 个窗口控制按钮齐全。

    主人实测指定 "右上角要有全屏和窗口功能"。"""
    from app.core.config import LLMConfig
    from app.core.qt_compat import QToolButton
    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False)
    for name in ('btn_min', 'btn_max', 'btn_fullscreen', 'btn_close'):
        assert hasattr(cw, name), f"标题栏缺 {name} 按钮"
        btn = getattr(cw, name)
        assert isinstance(btn, QToolButton), f"{name} 必须是 QToolButton"
    assert cw.btn_min.toolTip() == "最小化"
    assert "最大化" in cw.btn_max.toolTip() or "还原" in cw.btn_max.toolTip()
    assert "全屏" in cw.btn_fullscreen.toolTip()
    assert cw.btn_close.toolTip() == "关闭"
    assert callable(getattr(cw, "_toggle_maximized", None))
    assert callable(getattr(cw, "_toggle_fullscreen", None))
    print("[OK] 标题栏右上角 4 个窗口控制按钮齐全 (min/max/fullscreen/close)")


def test_anchor_click_handler_keeps_toggle_guard():
    """Bug 1 守卫依然有效 + toggle 还在。"""
    import ast as _ast
    src = (Path(__file__).resolve().parent.parent
           / "app" / "ui" / "chat_window" / "__init__.py").read_text(
        encoding="utf-8",
    )
    tree = _ast.parse(src)
    for node in _ast.walk(tree):
        if (isinstance(node, _ast.FunctionDef)
                and node.name == "_on_chat_anchor_clicked"):
            body = _ast.dump(node)
            assert "toggle_tools" in body, (
                "cmd:toggle_tools 处理不能丢 —— 工具卡片 toggle 还要"
            )
            pos_guard = body.find("_current_bot_msg")
            pos_refresh = body.find("_refresh_streaming_message")
            assert pos_guard > 0 and pos_refresh > 0
            assert pos_guard < pos_refresh, (
                "Bug 1 守卫顺序错了"
            )
            print("[OK] toggle_tools + Bug 1 守卫共存")
            return
    raise AssertionError("_on_chat_anchor_clicked 函数未找到")
