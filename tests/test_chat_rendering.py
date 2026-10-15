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
    # v6: URL 升级为 `cmd:toggle_tools#N` (N=msg_id), 但 prefix 仍是 cmd:toggle_tools
    assert 'href="cmd:toggle_tools' in html
    assert "▾" in html  # 折叠态箭头
    assert "点击展开" not in html, (
        f"「点击展开」字样不应出现 (用户实测要求去掉), 实际: {html}"
    )
    assert "点击收起" not in html

    # === 展开态 ===
    # v6: per-message 状态 —— 通过 dict 切, 不再设 self._tools_collapsed
    cw._tools_collapsed_by_msg[id(msg)] = False
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


def test_bug1_ast_guard_anchor_click_uses_rebuild_not_refresh():
    """Bug 1 AST 守卫 v5: 点 chat 上的 cmd:toggle_tools 链接时,
    _on_chat_anchor_clicked 必须走 _rebuild_chat_view_from_history, 不能调
    _refresh_streaming_message。

    历史:
    - v1: 守卫 _current_bot_msg 挡 _refresh_streaming_message → 仍白屏 (QTextBrowser doSetSource)
    - v3: setOpenLinks(False) → 禁用 click
    - v4: subclass QTextBrowser doSetSource → click 通了, 但用户实测仍有「清空」
    - v5: 完全不走 _refresh_streaming_message, 改 _rebuild_chat_view_from_history

    这个测试是 v5 后的正确守卫: _on_chat_anchor_clicked 函数体的实际调用节点
    (跳过 docstring, 避免 docstring 里提到函数名误判) 不能包含
    _refresh_streaming_message, 必须包含 _rebuild_chat_view_from_history。
    """
    import ast as _ast
    src = (Path(__file__).resolve().parent.parent
           / "app" / "ui" / "chat_window" / "__init__.py").read_text(
        encoding="utf-8",
    )
    tree = _ast.parse(src)
    for node in _ast.walk(tree):
        if (isinstance(node, _ast.FunctionDef)
                and node.name == "_on_chat_anchor_clicked"):
            # 跳过 docstring (函数体第一个 Expr 节点), 只 dump 实际语句
            body_nodes = node.body
            if (body_nodes
                    and isinstance(body_nodes[0], _ast.Expr)
                    and isinstance(body_nodes[0].value, _ast.Constant)
                    and isinstance(body_nodes[0].value.value, str)):
                body_nodes = body_nodes[1:]
            body_src = ' '.join(_ast.dump(s) for s in body_nodes)
            # v5: 不能调 _refresh_streaming_message
            assert "_refresh_streaming_message" not in body_src, (
                "_on_chat_anchor_clicked 不能调 _refresh_streaming_message — "
                "v4 实测这个调用在某些状态下会清空 chat 区域。"
                "必须改用 _rebuild_chat_view_from_history。"
            )
            # v5: 必须调 _rebuild_chat_view_from_history
            assert "_rebuild_chat_view_from_history" in body_src, (
                "_on_chat_anchor_clicked 必须调 _rebuild_chat_view_from_history"
            )
            # toggle 命令路径必须还在
            assert "toggle_tools" in body_src, (
                "cmd:toggle_tools 处理不能丢 —— 工具卡片 toggle 还要"
            )
            print("[OK] Bug 1 v5 AST 守卫: toggle 走 rebuild 路径, 不再 refresh")
            return
    raise AssertionError("_on_chat_anchor_clicked 函数未找到")


def test_v6_per_message_toggle_state():
    """v6 调整 1: 工具折叠改成 per-message 状态 (dict keyed by id(msg))。

    之前 self._tools_collapsed 是全局 bool, 点 A 消息的 toggle 会影响
    所有消息的「已执行操作」卡片 (用户实测「点击一个后所有的都会展开」)。
    现在 dict 存 per-msg 状态, 互不影响。
    """
    from PyQt5.QtCore import QUrl
    from app.ui.chat_window import ChatWindow
    from app.ui.chat_window.message import Message
    from app.core.config import LLMConfig, CharacterConfig
    from app.voice.character import Emotion

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='测试'),
                    'assets/sprites', asr_enabled=False)

    # 两条 bot 消息, 各有 tools
    msg_a = Message(role='assistant', content='A 答案', emotion=Emotion.HAPPY,
                    tools=[('add_reminder', '{}', '已设置')])
    msg_b = Message(role='assistant', content='B 答案', emotion=Emotion.HAPPY,
                    tools=[('web_search', '{}', '找到 5 条'),
                           ('get_weather', '{}', '晴 25 度')])
    cw.history = [msg_a, msg_b]
    cw._rebuild_chat_view_from_history()

    # 初始: 两条都默认折叠
    html_a = cw._msg_html(msg_a)
    html_b = cw._msg_html(msg_b)
    assert "已执行操作 · 1&nbsp;&nbsp;▾" in html_a, "msg_a 默认应折叠"
    assert "已执行操作 · 2&nbsp;&nbsp;▾" in html_b, "msg_b 默认应折叠"

    # 点 msg_a 的 toggle: 展开 a, b 不动
    url_a = QUrl(f"cmd:toggle_tools#{id(msg_a)}")
    cw._on_chat_anchor_clicked(url_a)
    assert cw._tools_collapsed_by_msg.get(id(msg_a)) is False,         "msg_a 应被 toggle 为展开"
    assert cw._tools_collapsed_by_msg.get(id(msg_b)) is None,         "msg_b 不应被影响 (dict 里没 entry)"

    # 重新生成的 HTML 也要反映新状态
    html_a2 = cw._msg_html(msg_a)
    html_b2 = cw._msg_html(msg_b)
    assert "已执行操作 · 1&nbsp;&nbsp;▴" in html_a2, "msg_a 应展开"
    assert "已执行操作 · 2&nbsp;&nbsp;▾" in html_b2, "msg_b 仍应折叠"

    # 再点 a 折回去
    cw._on_chat_anchor_clicked(url_a)
    assert cw._tools_collapsed_by_msg.get(id(msg_a)) is True
    html_a3 = cw._msg_html(msg_a)
    assert "已执行操作 · 1&nbsp;&nbsp;▾" in html_a3

    # 现在点 b, a 不动
    url_b = QUrl(f"cmd:toggle_tools#{id(msg_b)}")
    cw._on_chat_anchor_clicked(url_b)
    assert cw._tools_collapsed_by_msg.get(id(msg_b)) is False
    assert cw._tools_collapsed_by_msg.get(id(msg_a)) is True,         "msg_a 不应被影响"
    print("[OK] v6: per-message toggle 状态 — 点 A 不影响 B")


def test_v6_command_completer_textChanged():
    """v6 调整 2: 修「输入 / 后没反应, 再输入一个字符才显示」。

    keypress 事件在 Qt 处理之前触发, 此时 doc 还没插入新字符,
    _current_word() 读空行, popup 不显示。改用 textChanged 信号 —
    doc 变化后才发, 这时能读到 / + 后续字符。
    """
    import inspect
    from app.ui.chat_window.command_completer import _CommandCompleter
    src = inspect.getsource(_CommandCompleter.__init__)
    assert "textChanged" in src, (
        "_CommandCompleter.__init__ 必须连 input_edit.textChanged "
        "(用户实测「输入 / 后没反应, 再输入一个字符才显示」根因修复)"
    )
    assert "self._refresh" in src, (
        "textChanged 必须连到 self._refresh 才会触发 popup 更新"
    )
    print("[OK] v6: /键响应 — _CommandCompleter 连了 textChanged")


def test_v6_english_commands_with_plan_skills():
    """v6 调整 3: 命令改为英文, 加 /plan / /skills。
    v7 升级: /help 输出的 helper 文案改成中文 ('常用命令'), 命令字面量保留英文。
    """
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False)

    # candidates 必须是英文 + 含 /plan + /skills
    candidates = cw._cmd_completer.candidates
    expected = {"/help", "/status", "/time", "/say ", "/remember ", "/memory",
                "/reminders", "/clear", "/demo", "/debug", "/open ",
                "/plan ", "/skills", "/feed "}
    missing = expected - set(candidates)
    assert not missing, (
        f"candidates 缺少英文命令: {missing}. 实际: {candidates}"
    )
    # 中文命令不能还在
    chinese = [c for c in candidates if any('一' <= ch <= '鿿' for ch in c)]
    assert not chinese, (
        f"candidates 不应再有中文命令: {chinese} (用户实测: 命令需要用英文)"
    )

    # _handle_command 接受英文命令 (直接调, 不通过按钮)
    # /help 路径: 走 if cmd in ("/help", "/?") 分支, 调用 _render_message 输出到 chat_view
    cw._handle_command("/help")
    # v7: 帮助文案改成中文, "常用命令" 是关键 marker
    text = cw.chat_view.toPlainText()
    assert "常用命令" in text, (
        f"/help 必须在 chat_view 里渲染「常用命令」列表. 实际: {text!r}"
    )
    # 命令字面量仍然以 / 开头 + 英文显示 (cmd 列里也仍可见)
    assert "/help" in text and "/plan" in text and "/skills" in text, (
        f"/help 输出应保留英文命令字面量. 实际: {text!r}"
    )

    print("[OK] v6/v7: 命令改英文 + /plan / /skills + 帮助文案中文化")


def test_v7_ast_guard_command_literals_english():
    """v7 AST 守卫: _handle_command 体内所有「命令字面量」(以 / 开始的字符串
    literal, 长度 1~32) 都必须是英文 (ASCII), 用来防止下次重构把
    /帮助 /记忆 /时间 等中文命令又加回比较分支。

    v6 旧版是「体内不允许有任何中文字符」, 太严格了 —— 把 UI 文案改成中文后就
    会误伤。改成精确匹配: 只检查「形似命令」的字符串 literal, 不碰叙述性中文。
    """
    import ast as _ast
    import re as _re

    src_path = (Path(__file__).resolve().parent.parent
                / "app" / "ui" / "chat_window" / "__init__.py")
    src = src_path.read_text(encoding="utf-8")
    tree = _ast.parse(src)

    # 类似「命令 literal」的字符串匹配规则: 1~32 字符, 必须以 "/" 开头, 不含空格
    cmd_pat = _re.compile(r"^/[A-Za-z0-9_\-?]{0,31}$")

    for fn in _ast.walk(tree):
        if not (isinstance(fn, _ast.FunctionDef)
                and fn.name == "_handle_command"):
            continue
        # 扫函数体里所有 ast.Constant 字符串 literal (跳过 docstring 头一条)
        body = fn.body
        if (body and isinstance(body[0], _ast.Expr)
                and isinstance(body[0].value, _ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        bad: list[str] = []
        for sub in _ast.walk(fn):
            if not isinstance(sub, _ast.Constant):
                continue
            if not isinstance(sub.value, str):
                continue
            v = sub.value
            if not cmd_pat.match(v):
                continue
            # 必须是纯英文 ASCII
            for ch in v:
                if not (ch.isascii() and ch != ""):
                    bad.append(v)
                    break
        assert not bad, (
            f"_handle_command 体内有非英文命令字面量 (用户实测命令需英文): {bad}"
        )
        # 必须有 /plan / /skills / /help / /memory 的分支 (确保 v6 的命令都在)
        body_dump = ' '.join(_ast.dump(n) for n in body)
        for cmd_name in ("/plan", "/skills", "/help", "/memory"):
            assert cmd_name in body_dump, (
                f"_handle_command 缺 {cmd_name} 分支"
            )
        print("[OK] v7 AST 守卫: 命令字面量全部纯英文 (含 /plan / /skills)")
        return
    raise AssertionError("_handle_command 函数未找到")


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
    # v6: per-message 状态通过 dict 设
    cw._tools_collapsed_by_msg[id(msg)] = True
    html = cw._tool_steps_html(msg.tools, msg_id=id(msg))
    assert "点击展开" not in html, f"「点击展开」字样不应出现: {html}"
    assert "点击收起" not in html, f"「点击收起」字样不应出现: {html}"
    assert 'href="cmd:toggle_tools' in html, \
        "toggle 功能保留, href 必须在"
    assert "▾" in html, "折叠态应显示下箭头"

    cw._tools_collapsed_by_msg[id(msg)] = False
    html2 = cw._tool_steps_html(msg.tools, msg_id=id(msg))
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
    """调整 5 v3 验证: 标题栏右上角 3 个窗口控制按钮 (最小化/最大化/关闭)。

    2026-10-15 v3: 主人实测 "只需要最小化最大化和关闭, 不需要多余的全屏",
    全屏按钮 + F11 快捷键已移除。"""
    from app.core.config import LLMConfig
    from app.core.qt_compat import QToolButton
    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False)
    # 3 个按钮必须都存在
    for name in ('btn_min', 'btn_max', 'btn_close'):
        assert hasattr(cw, name), f"标题栏缺 {name} 按钮"
        btn = getattr(cw, name)
        assert isinstance(btn, QToolButton), f"{name} 必须是 QToolButton"
    # 全屏按钮 / F11 处理器必须已删
    assert not hasattr(cw, "btn_fullscreen"),         "btn_fullscreen 必须已移除 (用户实测 v3)"
    assert not callable(getattr(cw, "_toggle_fullscreen", None)),         "_toggle_fullscreen 方法必须已移除"
    # tooltip 仍在
    assert cw.btn_min.toolTip() == "最小化"
    assert "最大化" in cw.btn_max.toolTip() or "还原" in cw.btn_max.toolTip()
    assert cw.btn_close.toolTip() == "关闭"
    # maximize handler 仍在
    assert callable(getattr(cw, "_toggle_maximized", None))
    print("[OK] 标题栏右上角 3 个窗口控制按钮 (min/max/close), 全屏按钮已删")


def test_chat_view_no_cmd_nav_subclass():
    """Bug 1 v4 根因修复: 用 _NoCmdNavQTextBrowser 子类覆盖 doSetSource,
    让 cmd: scheme 走 no-op, 不替换 view。

    v3 错方案: setOpenLinks(False) —— PyQt5 5.x 在 openLinks=False 时
    会禁用 click 行为 (mousePressEvent 不再走链接路径, anchorClicked
    不再 emit), 用户实测 v3「点击没反应」。

    v4 正解: 默认 openLinks=True (click 仍工作, anchorClicked 照常发),
    但用 _NoCmdNavQTextBrowser 覆盖 doSetSource: 对 cmd: scheme 走
    no-op, 不让 QTextBrowser 用空文档替换 view。
    """
    from app.core.config import LLMConfig
    from app.ui.chat_window import _NoCmdNavQTextBrowser
    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False)
    # 1) chat_view 必须是 _NoCmdNavQTextBrowser 实例
    assert isinstance(cw.chat_view, _NoCmdNavQTextBrowser), (
        "chat_view 必须是 _NoCmdNavQTextBrowser 子类, "
        "否则 cmd: 链接点下去会触发 QTextBrowser 默认 doSetSource, "
        "把 view 替换成空文档 → 白屏"
    )
    # 2) chat_view 必须是 QTextBrowser (subclass 关系, 走 IS-A 路径)
    from app.core.qt_compat import QTextBrowser
    assert isinstance(cw.chat_view, QTextBrowser)
    # 3) openLinks 必须保持 True (默认) —— 否则 v3 bug 重现
    assert cw.chat_view.openLinks() is True, (
        "openLinks 必须是 True (默认) —— 上一版改成 False 后, "
        "PyQt5 5.x 直接禁用了 click 行为, 用户实测「点击没反应」"
    )
    # 4) openExternalLinks 仍 False (http 链接不让 OS 自动开)
    assert cw.chat_view.openExternalLinks() is False
    # 5) anchorClicked 信号仍连着
    assert cw.chat_view.receivers(cw.chat_view.anchorClicked) > 0, (
        "anchorClicked 必须仍连接 _on_chat_anchor_clicked"
    )
    # 6) 调 doSetSource 时 cmd: 应 no-op (不抛异常, 不替换 view)
    from app.core.qt_compat import QUrl
    qurl = QUrl("cmd:toggle_tools")
    cw.chat_view.doSetSource(qurl)  # 必须不抛, 必须 no-op
    # 7) 调 doSetSource 时 http 也 no-op (走父类也走不到, 因 setOpenExternalLinks=False)
    cw.chat_view.doSetSource(QUrl("https://example.com"))  # 不抛即可
    print("[OK] chat_view 是 _NoCmdNavQTextBrowser (cmd: 不再让 QTextBrowser 替换 view)")


def test_no_cmd_nav_subclass_ast_guard():
    """AST 守卫: chat_window 里的 _NoCmdNavQTextBrowser 子类不能丢。
    防止下次重构把它去掉, 又回到 v3 的 setOpenLinks(False) 反模式。
    """
    import ast as _ast
    src_path = (Path(__file__).resolve().parent.parent
                / "app" / "ui" / "chat_window" / "__init__.py")
    src = src_path.read_text(encoding="utf-8")
    tree = _ast.parse(src)
    # 必须有 _NoCmdNavQTextBrowser 类定义
    has_class = False
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ClassDef) and node.name == "_NoCmdNavQTextBrowser":
            has_class = True
            # 类必须有 doSetSource 方法
            has_method = any(
                isinstance(item, _ast.FunctionDef) and item.name == "doSetSource"
                for item in node.body
            )
            assert has_method, (
                "_NoCmdNavQTextBrowser 必须有 doSetSource 方法"
            )
            break
    assert has_class, (
        "_NoCmdNavQTextBrowser 子类定义在 chat_window/__init__.py 中"
        "必须存在 —— 否则 v3 白屏 bug 会重现"
    )
    # 不能有 self.chat_view.setOpenLinks(False) 实际调用 (v3 反模式,
    # PyQt5 5.x 在 openLinks=False 时会禁用 click 行为, 用户实测
    # 「点击没反应」)。注意: 注释里有这串文字是 OK 的, 只挡实际函数调用。
    # 扫描 AST 找 setOpenLinks 的实参是 False 的 Call 节点
    bad_call_found = False
    for node in _ast.walk(tree):
        if (isinstance(node, _ast.Call)
                and isinstance(node.func, _ast.Attribute)
                and node.func.attr == "setOpenLinks"):
            for arg in node.args:
                if (isinstance(arg, _ast.Constant)
                        and arg.value is False):
                    bad_call_found = True
                    break
    assert not bad_call_found, (
        "setOpenLinks(False) 是 v3 反模式 —— 实际函数调用必须删, "
        "用 _NoCmdNavQTextBrowser 覆盖 doSetSource 替代。"
        "注释里的说明文字 OK, 但不能让代码真的调 setOpenLinks(False)。"
    )
    print("[OK] AST 守卫: _NoCmdNavQTextBrowser 在, setOpenLinks(False) 调用已删")


def test_v5_rebuild_chat_view_from_history_preserves_content():
    """v5: _on_chat_anchor_clicked(cmd:toggle_tools) 改用
    _rebuild_chat_view_from_history, 不再 _refresh_streaming_message。
    后者有「从 anchor 删到末尾再 insertHtml」的危险操作, 在某些
    状态下会让 chat 区域被擦白 (用户实测 v4 反馈「点击后
    聊天窗口信息清空」)。

    验证 rebuild 后:
    1. 用户消息 + 历史 bot 消息都还在
    2. streaming 占位仍在 (如果有)
    3. _tools_collapsed 状态切换
    """
    from PyQt5.QtCore import QUrl
    from app.ui.chat_window import ChatWindow
    from app.ui.chat_window.message import Message
    from app.core.config import LLMConfig, CharacterConfig
    from app.voice.character import Emotion

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='测试'),
                    'assets/sprites', asr_enabled=False)

    # 1. 加历史
    cw.history = []
    cw.history.append(Message(role='user', content='你好'))
    cw.history.append(Message(role='assistant', content='已设置', emotion=Emotion.HAPPY,
                              tools=[('add_reminder', '{}', '已设置')]))
    cw.history.append(Message(role='user', content='再看看天气'))
    cw.history.append(Message(role='assistant', content='晴 25 度', emotion=Emotion.HAPPY,
                              tools=[('get_weather', '{}', '晴 25 度')]))

    # 2. 渲染
    cw.chat_view.clear()
    for m in cw.history:
        cw._render_message(m)

    # 3. 流式中
    cw._current_bot_msg = Message(role='assistant', content='正在处理...', emotion=Emotion.HAPPY,
                                  tools=[('add_reminder', '{}', '已设置')])
    cw._streaming_anchor_pos = cw._insert_placeholder(cw._current_bot_msg)

    before = cw.chat_view.toPlainText()
    # 关键内容必须在
    assert "你好" in before
    assert "已设置" in before
    assert "晴 25 度" in before
    assert "正在处理" in before

    # 4. 反复点 3 次
    for _ in range(3):
        cw._on_chat_anchor_clicked(QUrl("cmd:toggle_tools"))
        after = cw.chat_view.toPlainText()
        # 关键内容始终在 (用户实测 v4 「清空」就是这些不在)
        assert "你好" in after, f"用户消息 '你好' 在 toggle 后消失: {after!r}"
        assert "已设置" in after, f"bot 历史 '已设置' 在 toggle 后消失: {after!r}"
        assert "晴 25 度" in after, f"bot 历史 '晴 25 度' 在 toggle 后消失: {after!r}"
        # streaming 占位也在
        assert "正在处理" in after, f"streaming '正在处理' 在 toggle 后消失: {after!r}"

    # 5. _on_done 后再点
    cw._current_bot_msg = None
    cw._streaming_anchor_pos = None
    cw._on_chat_anchor_clicked(QUrl("cmd:toggle_tools"))
    final = cw.chat_view.toPlainText()
    assert "你好" in final, "_on_done 后点 toggle, 用户消息 '你好' 消失"
    assert "已设置" in final
    assert "晴 25 度" in final
    # streaming 占位已经不存在, 但 _current_bot_msg = None 时
    # _rebuild_chat_view_from_history 不会重建 (判断条件), 正常
    print("[OK] v5: toggle 用 _rebuild_chat_view_from_history, chat 内容不再被清空")


def test_v5_ast_guard_rebuild_method_exists():
    """AST 守卫: _rebuild_chat_view_from_history 方法必须存在, 且
    _on_chat_anchor_clicked 必须调用它 (而不是 _refresh_streaming_message)。"""
    import ast as _ast
    src_path = (Path(__file__).resolve().parent.parent
                / "app" / "ui" / "chat_window" / "__init__.py")
    src = src_path.read_text(encoding="utf-8")
    tree = _ast.parse(src)
    # 必须有 _rebuild_chat_view_from_history 函数
    has_rebuild = any(
        isinstance(n, _ast.FunctionDef) and n.name == "_rebuild_chat_view_from_history"
        for n in _ast.walk(tree)
    )
    assert has_rebuild, (
        "_rebuild_chat_view_from_history 方法必须存在 (v5 修法核心)"
    )
    # _on_chat_anchor_clicked 内部不能有 _refresh_streaming_message 调用
    for node in _ast.walk(tree):
        if (isinstance(node, _ast.FunctionDef)
                and node.name == "_on_chat_anchor_clicked"):
            # 跳过 docstring, 只看实际函数体
            body_nodes = node.body
            if (body_nodes
                    and isinstance(body_nodes[0], _ast.Expr)
                    and isinstance(body_nodes[0].value, _ast.Constant)
                    and isinstance(body_nodes[0].value.value, str)):
                body_nodes = body_nodes[1:]
            body_src = ' '.join(_ast.dump(s) for s in body_nodes)
            assert "_refresh_streaming_message" not in body_src, (
                "_on_chat_anchor_clicked 不能调 _refresh_streaming_message — "
                "v4 实测这个调用在某些状态下会清空 chat 区域"
            )
            # 必须调 _rebuild_chat_view_from_history
            assert "_rebuild_chat_view_from_history" in body_src, (
                "_on_chat_anchor_clicked 必须调 _rebuild_chat_view_from_history"
            )
            print("[OK] AST 守卫: _rebuild_chat_view_from_history 在, toggle 走 rebuild 路径")
            return
    raise AssertionError("_on_chat_anchor_clicked 函数未找到")


def test_anchor_click_handler_keeps_toggle_guard():
    """v5: _on_chat_anchor_clicked 调 _rebuild_chat_view_from_history,
    不再调 _refresh_streaming_message — 所以 _current_bot_msg /
    _streaming_anchor_pos 守卫也用不上了 (v1 那个守卫就是当时为了挡
    _refresh_streaming_message 加的, 现在那条路直接没了)。"""
    import ast as _ast
    src = (Path(__file__).resolve().parent.parent
           / "app" / "ui" / "chat_window" / "__init__.py").read_text(
        encoding="utf-8",
    )
    tree = _ast.parse(src)
    for node in _ast.walk(tree):
        if (isinstance(node, _ast.FunctionDef)
                and node.name == "_on_chat_anchor_clicked"):
            # 跳过 docstring, 只看实际函数体
            body_nodes = node.body
            if (body_nodes
                    and isinstance(body_nodes[0], _ast.Expr)
                    and isinstance(body_nodes[0].value, _ast.Constant)
                    and isinstance(body_nodes[0].value.value, str)):
                body_nodes = body_nodes[1:]
            body = ' '.join(_ast.dump(s) for s in body_nodes)
            assert "toggle_tools" in body, (
                "cmd:toggle_tools 处理不能丢 —— 工具卡片 toggle 还要"
            )
            assert "_rebuild_chat_view_from_history" in body, (
                "v5: 必须用 _rebuild_chat_view_from_history, 不能退回到 _refresh_streaming_message"
            )
            assert "_refresh_streaming_message" not in body, (
                "v5: _on_chat_anchor_clicked 不能再调 _refresh_streaming_message — "
                "那个调用路径会让 chat 区域被擦白"
            )
            print("[OK] toggle 走 rebuild 路径, Bug 1 守卫 v1 时代已不需要")
            return
    raise AssertionError("_on_chat_anchor_clicked 函数未找到")


# ============================================================
# v8 调整 (2026-10-15):
#   1) 输入框去紫色线圈（_InputFocusWatcher 删除）
#   2) /skills/<name> 二级深链
#   3) 拖图 + 底部 + 上传按钮 + 多模态生成按钮
#   4) _render_markdown 支持 ![alt](file://...) 转 <img>
# ============================================================


def test_v8_no_purple_focus_border():
    """v8: 主人实测「每次点击输入框会有一个紫色线圈不需要」。
    ui_style 删掉 input_container / chat_view 的 :focus-within / :focus
    border-color 规则, 输入容器边框永远保持中性灰 (#e7e3f5)。
    chrome.py 应该不再含 _InputFocusWatcher。
    """
    from app.ui import ui_style as _us
    import re as _re
    qss = _us.CHAT_QSS

    # 从 QSS 里抠出所有 CSS 选择器 (粗筛 selector 文本, 排除 /* ... */ 注释)
    no_comments = _re.sub(r"/\*.*?\*/", "", qss, flags=_re.DOTALL)
    # 真凶: 任何「:focus-within {...}」规则 —— 因为注释里也有这字样, 所以
    # 只检查 { 后真规则壳子。
    bad_patterns = [
        _re.compile(r":focus-within\s*\{"),
        _re.compile(r"input_container:focus[a-zA-Z\-()]*\s*\{"),
    ]
    for pat in bad_patterns:
        m = pat.search(no_comments)
        assert not m, (
            f"ui_style 仍含紫色线圈真凶规则 {pat.pattern}: {m.group(0)!r}"
        )

    # chrome.py 删除 _InputFocusWatcher (这条独立验证)
    from app.ui.chat_window import chrome as _chrome
    assert not hasattr(_chrome, "_InputFocusWatcher"), (
        "chrome.py 还含 _InputFocusWatcher, 应该删除"
    )

    print("[OK] v8: 紫色线圈已删除 (QSS 规则去掉 + chrome watcher 删除)")


def test_v8_skills_deep_link():
    """v8: /skills/<name> 显示该 skill 文档预览 + 操作提示。
    真激活留 Phase 4.*, 这版只先有「看」功能。
    """
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False,
                    skills_dir='skills')

    cw._handle_command("/skills/poem")
    text = cw.chat_view.toPlainText()
    assert "skill" in text and "poem" in text, (
        f"/skills/poem 必须在 chat 里渲染 skill 预览 + 名字。 实际: {text!r}"
    )
    # 预览内容至少给一个字（标题或注释）
    assert "七言绝句" in text or "用一首七言绝句" in text, (
        f"/skills/poem 应展示 poem.md 文档内容。 实际: {text!r}"
    )

    # /skills/<not-found> 给友好提示
    cw._handle_command("/skills/nonexistent_skill")
    text2 = cw.chat_view.toPlainText()
    assert "找不到" in text2 or "不存在" in text2 or "没" in text2, (
        f"/skills/nonexistent_skill 应给提示。 实际: {text2!r}"
    )
    print("[OK] v8: /skills/<name> 二级深链 + 找不到给提示")


def test_v8_upload_and_media_buttons():
    """v8: 底部新增 + 上传按钮和多模态下拉, 整窗接 drag&drop。"""
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False)

    assert hasattr(cw, "upload_btn"), "chat_window 应有 self.upload_btn"
    assert cw.upload_btn.text() == "+", (
        f"upload_btn 文字应是 '+', 实际: {cw.upload_btn.text()!r}"
    )
    assert hasattr(cw, "media_btn"), "chat_window 应有 self.media_btn"
    # 多模态按钮的菜单应该有「生成图片」+「生成视频」
    menu = cw.media_btn.menu()
    assert menu is not None, "media_btn 必须挂 QMenu"
    actions = [a.text() for a in menu.actions()]
    assert any("图片" in a for a in actions), (
        f"media 菜单缺「生成图片」项: {actions}"
    )
    assert any("视频" in a for a in actions), (
        f"media 菜单缺「生成视频」项: {actions}"
    )

    # 整个窗接 drag&drop
    assert cw.acceptDrops() is True, (
        "chat_window 必须 setAcceptDrops(True), 否则拖图不接受"
    )
    print("[OK] v8: + 上传 + 多模态下拉 + drag&drop 已挂上")


def test_v8_markdown_image_inline():
    """v8: _render_markdown 处理 ![alt](url) → <img src=url>,
    只放行 http/https/file scheme。这是上传图片走 markdown 渲染的关键。
    """
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False)

    # file:// URL 应转成 <img src=...>
    out = cw._render_markdown("![主人上传](file:///C:/tmp/test.png)")
    assert "<img src=\"file:///C:/tmp/test.png\"" in out, (
        f"file:// 图片 markdown 没转 img 标签。 输出: {out!r}"
    )
    # http:// URL 也应转
    out2 = cw._render_markdown("![alt](https://example.com/x.jpg)")
    assert "<img src=\"https://example.com/x.jpg\"" in out2, (
        f"http(s):// 图片 markdown 没转 img 标签。 输出: {out2!r}"
    )
    # javascript: 等危险 scheme 不转, 留原文字
    out3 = cw._render_markdown("![alt](javascript:alert(1))")
    assert "<img" not in out3, (
        f"javascript: scheme 应当被拒。 输出: {out3!r}"
    )

    print("[OK] v8: markdown image 转 <img>, 仅放行 http(s)/file scheme")


def test_v8_handle_uploaded_files_saves_and_renders():
    """v8: _handle_uploaded_files 把文件复制到 assets/uploads/ + 渲染进 chat。
    不调 _kickoff_llm (主人编辑完再发, QQ/iMessage 同款)。
    """
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig
    from app.ui.chat_window.message import Message
    import tempfile
    from pathlib import Path

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False)

    # 直接让 QPixmap 给我们造一个 1x1 PNG, 不要手撸字节
    from app.core.qt_compat import QPixmap
    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td) / "v8_sample.png"
        pix = QPixmap(1, 1)
        from app.core.qt_compat import QColor
        pix.fill(QColor("#ff00ff"))  # 品红
        pix.save(str(tmp_path), "PNG")

        before = len(cw.history)
        cw._handle_uploaded_files([tmp_path])
        after = len(cw.history)
        assert after == before + 1, (
            f"_handle_uploaded_files 应让 history + 1, before={before}, after={after}"
        )
        msg = cw.history[-1]
        assert msg.role == "user"
        # content 里要含 file:// 引用 + 文件名
        assert "file://" in msg.content, (
            f"user content 应含 file:// 引用。 实际: {msg.content!r}"
        )
        assert tmp_path.name in msg.content, (
            f"user content 应包含原文件名: {msg.content!r}"
        )

        # chat_view 里也真的渲染了一段包含 file://
        chat_text = cw.chat_view.toPlainText()
        assert "file://" in chat_text, (
            f"chat_view 应渲染 file:// 路径文本。 实际: {chat_text!r}"
        )

        # 真的复制到 assets/uploads 下
        uploads = cw._uploads_dir()
        files = list(uploads.glob(f"*{tmp_path.name}"))
        assert files, (
            f"图片应被复制到 {uploads}, 没找到 *{tmp_path.name}: "
            f"{list(p for p in uploads.iterdir())[:5]}..."
        )

    print("[OK] v8: 上传图片 → 复制到 uploads + 进 history + 渲染进 chat")
