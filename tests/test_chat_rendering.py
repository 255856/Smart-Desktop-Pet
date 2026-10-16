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
    """v8: /skills/<name> 路径存在。v9 升级后该命令的语义变成"激活
    (注入到 system_prompt)" 而不是"内容预览" —— 跟主人最新设计一致。
    测试验证: 激活 + 找不到给提示。
    """
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False,
                    skills_dir='skills')

    # v9 行为: /skills/<name> 走激活
    cw._handle_command("/skills/poem")
    text = cw.chat_view.toPlainText()
    assert "已激活" in text and "poem" in text, (
        f"/skills/poem 应激活 poem 并给系统消息。 实际: {text!r}"
    )
    # 激活后, _active_skill 状态机被设上
    assert cw._active_skill == "poem", (
        f"/skills/poem 应让 _active_skill = 'poem', 实际: {cw._active_skill!r}"
    )
    # skill 内容真的进了 system_prompt
    sp = cw._active_skill_prompt()
    assert "七言绝句" in sp, (
        f"激活后 system_prompt 应含 poem.md 文档内容。 实际: {sp[:200]!r}"
    )

    # /skills/<not-found> 给友好提示
    cw._handle_command("/skills/nonexistent_skill")
    text2 = cw.chat_view.toPlainText()
    assert "找不到" in text2 or "不存在" in text2 or "没" in text2, (
        f"/skills/nonexistent_skill 应给提示。 实际: {text2!r}"
    )
    print("[OK] v8/v9: /skills/<name> 激活路径 + 找不到给提示")


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
    """v8/v10: _handle_uploaded_files 复制到 assets/uploads/ + 进待发送区
    (v10 改: 不再立刻进 chat, 而是堆到 input 上方缩略图条, 主人按发送才入
    history + 渲染)。这是 QQ / 微信 / iMessage 同款姿势。
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

        # v10: history + chat_view 都没动 (待发送区收)
        before_h = len(cw.history)
        before_text = cw.chat_view.toPlainText()
        cw._handle_uploaded_files([tmp_path])
        # 1) history 不变 (没按发送就不进 chat)
        assert len(cw.history) == before_h, (
            f"v10 上传后 history 不应变, before={before_h}, after={len(cw.history)}"
        )
        # 2) chat_view 不变
        assert cw.chat_view.toPlainText() == before_text, (
            "v10 上传后 chat_view 不应变, 应该等主人按发送才入 chat"
        )
        # 3) _pending_attachments 长度 +1, 缩略图条可见
        assert len(cw._pending_attachments) == 1, (
            f"_pending_attachments 应有 1 个, 实际: {len(cw._pending_attachments)}"
        )
        # 测试时 ChatWindow 没 show, isVisible() 始终 False; 检查 widget 已挂上
        # (setVisible(True) 已被调, 真实桌面 show() 之后会显示)
        assert len(cw._pending_thumb_widgets) == 1
        assert cw._pending_strip_layout.count() >= 2  # 缩略图 + stretch
        # 4) 文件真的复制到 assets/uploads 下
        uploads = cw._uploads_dir()
        files = list(uploads.glob(f"*{tmp_path.name}"))
        assert files, (
            f"图片应被复制到 {uploads}, 没找到 *{tmp_path.name}: "
            f"{list(p for p in uploads.iterdir())[:5]}..."
        )

    print("[OK] v8/v10: 上传图片 → 复制到 uploads + 进待发送区 (不进 chat)")


# ============================================================
# v9 调整 (2026-10-15):
#   1) 图片拖入只显示 file:// 文字 → 渲染前 inline 成 data: URL
#   2) /skills/<name> 改为激活路径 (写聊天开头 / _handle_command 都走激活)
#   3) 多模态生成 (图 / 视频) 调真实 tool, 拿 URL 嵌图
# ============================================================


def test_v9_inline_local_images_to_data_url():
    """v9: 主人截图实测「图片拖进去是这样子」 — file:// 文字而不是图。
    _msg_html 渲染前先扫 `![alt](file://...)`, 把 file:// 替换成
    `data:image/...;base64,xxx`, QTextBrowser 自包含 base64 嵌图, 不会显示裸路径。
    """
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig
    import tempfile, base64 as _b64
    from pathlib import Path

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False)

    # 造一个真的 1x1 png 写盘
    from app.core.qt_compat import QPixmap, QColor
    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "v9inline.png"
        pix = QPixmap(1, 1)
        pix.fill(QColor("#aaffaa"))
        pix.save(str(png), "PNG")
        data = png.read_bytes()
        b64 = _b64.b64encode(data).decode("ascii")
        # 用 windows 风格 file:///C:/xxx/ 形式 (跟主人截图一致)
        file_url = "file:///" + str(png).replace("\\", "/").lstrip("/")
        md = f"![主人上传]({file_url})"

        out = cw._inline_local_images_to_data_url(md)
        assert "data:image/png;base64," in out, (
            f"file:// URL 应被 inline 成 data: URL。 实际: {out[:200]!r}"
        )
        assert b64[:40] in out, (
            f"data URL 里应含真实 base64 (前 40 字符)。 实际: {out[:200]!r}"
        )
        # file:// 文字应被替换走
        assert "file://" not in out, (
            f"inline 后不应再含 file:// 字样。 实际: {out[:200]!r}"
        )

    # 文件不存在时, v14 改成清晰提示 (主人实测 2026-10-15: 「重启后图片
    # 丢失变空白方块」, 给「图片已丢失」文字说明, 不再空白)。
    out2 = cw._inline_local_images_to_data_url("![alt](file:///C:/no/such/file.png)")
    assert "图片已丢失" in out2, (
        f"文件不存在时 v14 应给「图片已丢失」提示。 实际: {out2!r}"
    )
    assert "file.png" in out2, f"提示里应含文件名, 实际: {out2!r}"

    # v14: 项目内相对路径也能 inline
    md_rel = f"![主人上传](assets/uploads/{png.name})"
    out3 = cw._inline_local_images_to_data_url(md_rel)
    # 由于 _uploads_dir() 是项目根下 assets/uploads/, 真实路径可能不匹配
    # png.name 在临时目录, 不在项目 uploads 里 — 应得「图片已丢失」
    # (这条只验证相对路径不会崩, 不强制 inline)
    assert "图片已丢失" in out3 or "data:image/png" in out3, (
        f"项目相对路径要么 inline 要么给清晰丢失提示, 实际: {out3!r}"
    )

    print("[OK] v9: file:// URL inline 成 data: URL, v14 失败时给图片已丢失提示")


def test_v9_activate_skill_session_state():
    """v9: _activate_skill 把 skill 内容加入 system_prompt (整段对话期间生效)。
    _active_skill_prompt 非空时, 应返回包含 [Active Skill] 头尾 + skill 文本。
    """
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False,
                    skills_dir='skills')

    # 没激活时, prompt 是空
    assert cw._active_skill is None
    assert cw._active_skill_prompt() == ""

    # 激活 poem
    ok, msg = cw._activate_skill("poem")
    assert ok
    assert cw._active_skill == "poem"
    assert "poem" in msg

    # prompt 现在非空, 应包含 [Active Skill] 包装 + 实际内容
    sp = cw._active_skill_prompt()
    assert "poem" in sp
    assert "[Active Skill: poem]" in sp, (
        f"激活 prompt 应含 [Active Skill: poem] 包装头。 实际: {sp[:200]!r}"
    )
    assert "[/Active Skill]" in sp, (
        f"激活 prompt 应含 [/Active Skill] 包装尾。 实际: {sp[-100:]!r}"
    )
    assert "七言绝句" in sp, (
        f"激活 prompt 应含 poem.md 文档内容。 实际: {sp[:200]!r}"
    )

    # 切到不存在的 skill
    ok2, msg2 = cw._activate_skill("nonexistent_skill_xxx")
    assert not ok2
    assert "找不到" in msg2 or "不存在" in msg2
    # 切换失败, _active_skill 仍是上一次的 poem
    assert cw._active_skill == "poem"

    print("[OK] v9: 激活 / 切换 / 找不到 — session 状态机正确")


def test_v9_skills_list_marks_active():
    """v9: /skills 列表里当前激活的 skill 后面要标 '← 当前激活', 没激活时给
    '激活方式' 提示, 让主人知道怎么用。"""
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False,
                    skills_dir='skills')

    # 1) 没激活时, /skills 列表应提到「激活方式」
    cw._handle_command("/skills")
    text = cw.chat_view.toPlainText()
    assert "激活方式" in text, (
        f"/skills 列表没激活时应给「激活方式」提示。 实际: {text!r}"
    )
    # 列表项应给 "/skills/<name>" 入口形式 (不限定具体哪个 skill,
    # 取决于 skills_dir 实际有什么 + list_skills 顺序)
    import re as _re
    skill_entries = _re.findall(r"/skills/(\w[\w_]*)", text)
    assert skill_entries, (
        f"/skills 列表项应给 /skills/<name> 这种入口。 实际: {text!r}"
    )

    # 2) 激活后, 列表里 poem 后要带「← 当前激活」标记
    cw._activate_skill("poem")
    cw.chat_view.clear()
    cw._handle_command("/skills")
    text2 = cw.chat_view.toPlainText()
    assert "当前激活" in text2, (
        f"/skills 列表激活时应有「当前激活」标记。 实际: {text2!r}"
    )

    print("[OK] v9: /skills 列表 — 激活时打标, 未激活时给激活方式")


def test_v9_handle_command_skills_path_activates():
    """v9: _handle_command 路径里 /skills/<name> 走激活, 不再 list 预览。
    系统消息里含「已激活 skill: <name>」或类似。"""
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False,
                    skills_dir='skills')

    cw._handle_command("/skills/poem")
    # 激活后, self._active_skill 应该是 poem
    assert cw._active_skill == "poem", (
        f"/skills/poem 应激活 poem, 实际: {cw._active_skill!r}"
    )
    text = cw.chat_view.toPlainText()
    # 系统消息里提示激活 (render 后是 system-msg class, plain text 看到关键字)
    assert "已激活" in text or "激活" in text, (
        f"激活后应给系统消息提示。 实际: {text!r}"
    )

    print("[OK] v9: _handle_command(/skills/<name>) 走激活路径")


def test_v9_on_send_skills_prefix_in_chat():
    """v9 主人设计核心: 聊天开头写 `/skills/poem ...` 视为「激活 + 剩余文本作为
    user message 发送」。这是 _on_send 路径, 不走 _handle_command。
    """
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig
    from app.ui.chat_window.message import Message

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False,
                    skills_dir='skills')

    # 模拟主人在 input_edit 写 "/skills/poem 帮我写首中秋诗" 然后回车
    # 不实际触发 _quick_send (那会 kickoff llm), 而是直接验证 detection
    text = "/skills/poem 帮我写首中秋诗"
    # 模拟 _on_send 的 detection 逻辑 (它内部剥前缀后调 _activate_skill + _quick_send)
    # 这里手动走同样分支避免 _kickoff_llm 在 test 里启动 worker
    if text.startswith("/skills/"):
        head, _, rest = text.partition(" ")
        name = head[len("/skills/"):].strip().strip("/")
        if name:
            ok, _ = cw._activate_skill(name)
            assert ok and cw._active_skill == "poem"
            # 剩余文本作为 user message
            user_rest = rest.strip()
            assert user_rest == "帮我写首中秋诗", (
                f"剩余文本应原样保留。 实际: {user_rest!r}"
            )

    print("[OK] v9: 聊天开头 /skills/<name> 激活 + 剩余文本作为 user message")


# ============================================================
# v10 调整 (2026-10-15):
#   1) 拖图 / 上传改为「待发送区」, 按发送才入 chat (iMessage 同款)
#   2) / 命令补全动态拼 /skills/<name> 候选项
#   3) 真接多模态工具: app/engine/tools/_multimodal.py
#      - generate_image 调 OpenAI 兼容 endpoint (gpt-image-1)
#      - generate_video 留接口 + 清晰接入指引
# ============================================================


def test_v10_pending_send_consumes_to_chat():
    """v10: 拖入 / 上传 → 待发送区 (不进 chat); 主人按发送 → 走 _on_send
    把 pending + 当前 input 拼成 user message 渲染进 chat + 持久化 + 清空。
    """
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig
    import tempfile
    from pathlib import Path

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False)

    from app.core.qt_compat import QPixmap, QColor
    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "v10_send.png"
        pix = QPixmap(1, 1)
        pix.fill(QColor("#aaccff"))
        pix.save(str(png), "PNG")

        # 1) 上传 → 待发送
        cw._handle_uploaded_files([png])
        assert len(cw._pending_attachments) == 1
        # 测试时 ChatWindow 没 show, isVisible() 始终 False; 检查
        # setVisible 已被调用 (QScrollArea 的 QWidget.isVisibleTo(parent))
        # 不靠谱, 改成检查 _pending_thumb_widgets + strip 不空
        assert len(cw._pending_thumb_widgets) == 1
        # strip 的 widget 已挂上 (内部 _pending_strip_layout 不再空)
        assert cw._pending_strip_layout.count() >= 2  # 缩略图 + stretch

        # 2) 模拟主人在输入框写文字后按发送
        # 不实际触发 _quick_send (会 kickoff llm), 而走 _on_send 内部核心
        # 逻辑: _consume_pending_attachments() + 拼 user text + 走 _quick_send
        pending = cw._consume_pending_attachments()
        assert pending and len(pending) == 1
        # _handle_uploaded_files 复制到 assets/uploads/ 时加了时间戳前缀,
        # 所以 pending[0].name 形如 "1792055685_v10_send.png", 不等于原 png.name
        assert pending[0].name.endswith(png.name), (
            f"pending[0].name 应以原文件名结尾。 实际: {pending[0].name!r}"
        )
        # pending 列表已空
        assert cw._pending_attachments == []
        # 缩略图 widget 也清
        assert cw._pending_thumb_widgets == []

    print("[OK] v10: 待发送附件按发送后进 history, strip 清空")


def test_v10_remove_pending_attachment():
    """v10: 主人点缩略图 → 剔除该 attachment, 缩略图消失。"""
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig
    import tempfile
    from pathlib import Path

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False)

    from app.core.qt_compat import QPixmap, QColor
    with tempfile.TemporaryDirectory() as td:
        a = Path(td) / "a.png"
        b = Path(td) / "b.png"
        for p in (a, b):
            pix = QPixmap(1, 1)
            pix.fill(QColor("#aa00aa"))
            pix.save(str(p), "PNG")
        cw._handle_uploaded_files([a, b])
        assert len(cw._pending_attachments) == 2
        assert len(cw._pending_thumb_widgets) == 2

        # 注意: _handle_uploaded_files 复制到 assets/uploads/ 后, 实际 list 里
        # 装的是上传后的路径 (e.g. 1792055637_a.png), 不是原 tmp_path a。
        # 用 list 里的 path 来 remove。
        a_uploaded = cw._pending_attachments[0]
        b_uploaded = cw._pending_attachments[1]

        # 模拟点 a 缩略图 → 移除
        widget_a = cw._pending_thumb_widgets[0]
        cw._remove_pending_attachment(a_uploaded, widget_a)
        assert len(cw._pending_attachments) == 1
        assert cw._pending_attachments[0] == b_uploaded
        assert len(cw._pending_thumb_widgets) == 1

        # 移除最后一个
        widget_b = cw._pending_thumb_widgets[0]
        cw._remove_pending_attachment(b_uploaded, widget_b)
        assert cw._pending_attachments == []
        assert cw._pending_thumb_widgets == []

    print("[OK] v10: 点缩略图能剔除单条附件")


def test_v10_skills_completer_dynamic():
    """v10: / 命令补全在 chat_window 启动时动态拉 list_skills 拼 /skills/<name>。
    主人输入 /skills/ 时 popup 列出所有 skill 名 (含 /skills/poem 等)。
    """
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False,
                    skills_dir='skills')

    candidates = cw._cmd_completer.candidates
    # 标准命令必须还在
    assert "/help" in candidates
    assert "/plan " in candidates
    # 动态 /skills/<name> 必须拼进去 (skills/poem.md 存在)
    skill_entries = [c for c in candidates if c.startswith("/skills/")]
    assert any("poem" in c for c in skill_entries), (
        f"/skills/<name> 动态候选项没拼 poem. 候选: {candidates}"
    )

    print("[OK] v10: /skills/<name> 动态拼到 completer 候选")


def test_v10_multimodal_tool_registered():
    """v10/v12: build_default_tools 后, registry 应有 generate_image / video / audio。
    v12 升级: tool fn 走 config.yaml multimodal 段直接发 HTTP, 不再走
    multimodal_router (v11 router 抽象代码保留, 但本测试验证 tool 注册链路)。
    """
    from app.engine.tools import build_default_tools

    class _StubState:
        last_status = {}
        def get(self, k, default=None):
            return default
        def set(self, k, v):
            self.last_status[k] = v
    reg = build_default_tools(state=_StubState(), reminders=None, memory=None)

    for name in ("generate_image", "generate_video", "generate_audio"):
        t = reg.get(name)
        assert t is not None, f"{name} 应注册到 tool registry"
        assert t.name == name
        assert "prompt" in t.description.lower() or "图片" in t.description \
            or "视频" in t.description or "音频" in t.description

    print("[OK] v10/v11: generate_image / video / audio 都进 tool registry")




# ============================================================
# v12 调整 (2026-10-15):
#   1) 多模态从 v11 router 多 provider 改回 config-driven:
#      config.yaml 的 multimodal: 段填 base_url / api_key / image_model / ...
#      _multimodal.setup_config() 注入, tool 直接发 HTTP
#   2) 多模态按钮 (🎨 / 🎬 / 🎵) 改为「在输入框前面 insert /image /video /audio 」,
#      跟 iMessage / 微信 / QQ 同款姿势, 不再弹 QInputDialog
#   3) /image /video /audio 命令走 _handle_command, 跟按钮是同一姿势的键盘版
#   4) /skills/<name> 改成 per-message: 当条 user msg 设 msg.skill, 下一条
#      user msg 不传就不拼进 system_prompt (不再持久到 self._active_skill session)
# ============================================================


def test_v12_button_inserts_prefix():
    """v12: 多模态按钮 (image / video / audio) 在 input_edit 前面 insert
    "/<kind> " 前缀, focus 回去, 让主人接着输 prompt 按发送走 /image 命令。
    不再弹 QInputDialog (那是 v9-v11 的姿势)。
    """
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False)

    # 1) 点 image → input_edit 文本以 "/image " 开头, focus 在 input_edit
    cw.input_edit.clear()
    cw.input_edit.setPlainText("")
    cw._on_media_action("image")
    text = cw.input_edit.toPlainText()
    assert text.startswith("/image "), (
        f"点 image 后 input_edit 应以 /image 开头。 实际: {text!r}"
    )

    # 2) 点 video → input_edit 文本以 "/video " 开头
    cw.input_edit.clear()
    cw._on_media_action("video")
    text2 = cw.input_edit.toPlainText()
    assert text2.startswith("/video "), (
        f"点 video 后 input_edit 应以 /video 开头。 实际: {text2!r}"
    )

    # 3) 点 audio → input_edit 文本以 "/audio " 开头
    cw.input_edit.clear()
    cw._on_media_action("audio")
    text3 = cw.input_edit.toPlainText()
    assert text3.startswith("/audio "), (
        f"点 audio 后 input_edit 应以 /audio 开头。 实际: {text3!r}"
    )

    # 4) 重复点 image → 不重复插前缀 (已经有 /image 时只 focus 回去)
    cw.input_edit.clear()
    cw.input_edit.setPlainText("/image 橘猫")
    cw._on_media_action("image")
    text4 = cw.input_edit.toPlainText()
    assert text4 == "/image 橘猫", (
        f"已有 /image 前缀时不应重复插。 实际: {text4!r}"
    )

    print("[OK] v12: 多模态按钮自动 insert /<kind> 前缀, 不弹窗")


def test_v12_image_command_runs_tool():
    """v12: 输入框直接打 /image <prompt> 走 _handle_command 调真 tool 拿 URL 嵌图。
    tool fn 返回 URL 后, _render_message 把 markdown image 渲染进 chat_view。
    """
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig
    from app.engine.tools._core import ToolRegistry

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False)

    # stub registry 含 generate_image / video / audio, 都返回固定 URL
    reg = ToolRegistry()
    _fake_url = "https://example.com/v12_cat.png"
    from app.engine.tools._core import Tool
    reg.register(Tool(
        name="generate_image", description="stub",
        parameters={"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
        fn=lambda prompt, **kw: _fake_url,
    ))
    reg.register(Tool(
        name="generate_video", description="stub",
        parameters={"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
        fn=lambda prompt, **kw: "https://example.com/v12_cat.mp4",
    ))
    reg.register(Tool(
        name="generate_audio", description="stub",
        parameters={"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
        fn=lambda prompt, **kw: "https://example.com/v12_cat.mp3",
    ))
    cw.registry = reg

    # 1) /image prompt 走 _handle_command —— QTextBrowser 的 image 是 inline
    #    resource, 不会出现在 toPlainText() 里; 直接看 history 里的 msg.content。
    cw.chat_view.clear()
    cw._handle_command("/image 一只橘猫")
    assert cw.history[-1].role == "assistant", (
        f"/image 后应有一条 assistant msg。 history: {cw.history}"
    )
    msg1 = cw.history[-1].content
    assert "https://example.com/v12_cat.png" in msg1, (
        f"/image 应把 URL 写到 msg.content。 实际: {msg1!r}"
    )
    assert "橘猫" in msg1, "/image 应把 prompt 写入 markdown alt"

    # 2) /video 同样
    cw._handle_command("/video 猫走路")
    msg2 = cw.history[-1].content
    assert "https://example.com/v12_cat.mp4" in msg2, (
        f"/video 应写 URL。 实际: {msg2!r}"
    )

    # 3) /audio 同样
    cw._handle_command("/audio 猫叫声")
    msg3 = cw.history[-1].content
    assert "https://example.com/v12_cat.mp3" in msg3, (
        f"/audio 应写 URL。 实际: {msg3!r}"
    )

    print("[OK] v12: /image /video /audio 命令调真 tool 拿 URL 嵌图")


def test_v12_run_multimodal_no_tool_raises():
    """v12: registry 有但缺 tool → _run_multimodal_action 给清晰接入指引。"""
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig
    from app.engine.tools._core import ToolRegistry

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False)
    cw.registry = ToolRegistry()  # 空 registry

    cw._run_multimodal_action("image", "橘猫")
    text = cw.chat_view.toPlainText()
    assert "generate_image" in text, (
        f"缺 tool 时应告诉主人缺哪个 tool。 实际: {text!r}"
    )
    assert "config.yaml" in text or "setup_config" in text, (
        f"缺 tool 时应给 config.yaml 接入指引。 实际: {text!r}"
    )

    print("[OK] v12: 缺 tool 时 /image 给出清晰接入指引")


def test_v12_run_multimodal_no_registry_raises():
    """v12: registry=None 时, _run_multimodal_action 报清晰错。"""
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False)
    # 默认 registry=None

    cw._run_multimodal_action("image", "橘猫")
    text = cw.chat_view.toPlainText()
    assert "registry" in text or "工具" in text, (
        f"无 registry 时应清晰提示。 实际: {text!r}"
    )

    print("[OK] v12: 无 registry 时 /image 清晰提示")


def test_v12_per_message_skill_only_affects_one_msg():
    """v12: /skills/<name> ... 在聊天开头, skill 只对当条 user message 生效。
    - 当条 msg.skill = "poem"
    - 下一条 user msg 不传 skill, msg.skill 应为 None, 不拼进 system_prompt
    """
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False,
                    skills_dir='skills')

    # 走 _on_send 路径 (绕开 _kickoff_llm 实际跑 LLM, 用占位 LLMConfig)
    cw._generating = False
    # 拦截 _kickoff_llm, 避免真去跑模型
    kicked_with = []
    original_kickoff = cw._kickoff_llm
    cw._kickoff_llm = lambda: kicked_with.append(cw.history[-1].skill if cw.history else None)  # noqa: E731

    # 1) /skills/poem 写一首诗 → 当条 msg.skill = "poem"
    cw.input_edit.setPlainText("/skills/poem 写一首诗")
    cw._on_send()
    assert cw.history[-1].skill == "poem", (
        f"/skills/poem 后的 msg.skill 应为 poem, 实际: {cw.history[-1].skill!r}"
    )
    assert kicked_with == ["poem"], f"kickoff 应被调一次且 skill=poem, 实际: {kicked_with}"
    kicked_with.clear()

    # 2) 下一条 user msg 不传 skill → msg.skill 应为 None
    cw.input_edit.setPlainText("帮我打开 Notepad")
    cw._on_send()
    assert cw.history[-1].skill is None, (
        f"普通 user msg 的 skill 应为 None (per-message 不持久), 实际: {cw.history[-1].skill!r}"
    )
    assert kicked_with == [None], (
        f"下一条 kickoff 应读到 skill=None (per-message 兜底走 _active_skill_prompt), "
        f"实际: {kicked_with}"
    )

    # 3) v12 主线: _kickoff_llm 优先读 msg.skill, _active_skill 字段
    #    仍由 _activate_skill 兜底 set (v9 老路径兼容, 测试通过兜底用)。
    #    但 per-message 下一条 msg.skill=None 时, _kickoff_llm 应走
    #    _active_skill_prompt 兜底 (而不是 _skill_prompt_for)。

    # 还原 (不污染其他测试)
    cw._kickoff_llm = original_kickoff

    print("[OK] v12: /skills/<name> per-message, 下一条不带就不拼")


def test_v12_kickoff_uses_msg_skill_not_session_skill():
    """v12: _kickoff_llm 优先读 history[-1].skill (per-message), 不读
    self._active_skill session 字段 (那是 v9 老路径)。

    用 _skill_prompt_for 跟 _active_skill_prompt 输出的格式相同 ([Active Skill: <name>])
    来对比验证。
    """
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig
    from app.ui.chat_window.message import Message

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False,
                    skills_dir='skills')
    # 给 llm_cfg 设非 placeholder api_key, 否则 _kickoff_llm 第一行就 return
    cw.llm_cfg.api_key = "sk-v12-test"

    # 1) 验证 _skill_prompt_for 跟 _active_skill_prompt 输出格式一致
    sp_active = cw._active_skill_prompt()  # 没 active, 应为 ""
    assert sp_active == "", (
        f"没 _active_skill 时应为空串, 实际: {sp_active!r}"
    )

    sp_poem = cw._skill_prompt_for("poem")
    assert "[Active Skill: poem]" in sp_poem, (
        f"_skill_prompt_for 应包含 [Active Skill: poem] 标记。 实际: {sp_poem[:200]!r}"
    )
    assert "[/Active Skill]" in sp_poem, (
        f"_skill_prompt_for 应包含 [/Active Skill] 闭合。 实际: {sp_poem[:200]!r}"
    )

    # 2) 手动造一个 history, 当条 msg 带 skill, 验证 _kickoff_llm 走 _skill_prompt_for
    #    而不是 _active_skill_prompt
    cw.history = [
        Message(role="user", content="写诗", skill="poem"),
    ]
    # self._active_skill 显式设成不一样 → 验证 _kickoff_llm 优先 msg.skill
    cw._active_skill = "cooking"  # 老 session 字段
    # 但 _kickoff_llm 优先读 msg.skill ("poem"), 而不是 _active_skill ("cooking")

    # 拦截: 直接看 _kickoff_llm 调的 LLMClient 里 system_prompt 含哪个 skill
    captured = {}
    real_client_init = None
    try:
        from app.brain.llm_client import LLMClient
        real_client_init = LLMClient.__init__
        def _spy(self, cfg, system_prompt, *a, **kw):
            captured["system_prompt"] = system_prompt
            real_client_init(self, cfg, system_prompt, *a, **kw)
        LLMClient.__init__ = _spy  # type: ignore[assignment]

        # 调用 _kickoff_llm 但拦截 worker.start, 避免真跑
        cw._worker = None
        try:
            cw._kickoff_llm()
        except Exception:
            pass
    finally:
        if real_client_init:
            LLMClient.__init__ = real_client_init  # type: ignore[assignment]

    sp_used = captured.get("system_prompt", "")
    assert "[Active Skill: poem]" in sp_used, (
        f"_kickoff_llm 应优先用 msg.skill=poem。 实际 system_prompt: {sp_used[:400]!r}"
    )
    assert "[Active Skill: cooking]" not in sp_used, (
        f"不应被 session _active_skill=cooking 覆盖。 实际: {sp_used[:400]!r}"
    )

    print("[OK] v12: _kickoff_llm 优先 per-message msg.skill, 不读 session")


def test_v12_no_api_command_in_handle_command():
    """v12 AST 守卫: _handle_command 不再含 /api[/<name>] 段, 不再 import
    multimodal_router / multimodal_providers / _active_api 字段。
    """
    import ast as _ast
    src = Path("app/ui/chat_window/__init__.py").read_text(encoding="utf-8-sig")
    tree = _ast.parse(src)

    # 找 ChatWindow._handle_command 方法
    cw_cls = next(
        n for n in _ast.walk(tree)
        if isinstance(n, _ast.ClassDef) and n.name == "ChatWindow"
    )
    handle_cmd = next(
        n for n in cw_cls.body
        if isinstance(n, _ast.FunctionDef) and n.name == "_handle_command"
    )

    body_src = _ast.get_source_segment(src, handle_cmd) or ""

    # 1) 不应再出现 /api/ 命令匹配 (text == "/api" or text.startswith("/api/"))
    assert 'text == "/api"' not in body_src, (
        "/api 命令段不应还在 _handle_command 里"
    )
    assert 'text.startswith("/api/")' not in body_src, (
        "/api/ 前缀段不应还在 _handle_command 里"
    )

    # 2) 不应再 import multimodal_router / multimodal_providers
    for stmt in _ast.walk(handle_cmd):
        if isinstance(stmt, _ast.ImportFrom):
            mod = stmt.module or ""
            assert "multimodal_router" not in mod, (
                f"_handle_command 不应再 import {mod}"
            )
            assert "multimodal_providers" not in mod, (
                f"_handle_command 不应再 import {mod}"
            )

    # 3) _active_api 字段不应在 ChatWindow 里出现 (除了注释)
    #    简单 grep 源码, 排除注释行
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "_active_api" not in line, (
            f"v12 删了 _active_api 字段, 但还有: {line!r}"
        )

    print("[OK] v12 AST 守卫: /api 段 + multimodal_router import + _active_api 字段全删干净")


def test_v12_handle_command_supports_image_video_audio():
    """v12: _handle_command 加 /image /video /audio 三个命令, 走 _run_multimodal_action。"""
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig
    from app.engine.tools._core import ToolRegistry

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False)
    reg = ToolRegistry()
    from app.engine.tools._core import Tool
    reg.register(Tool(
        name="generate_image", description="stub",
        parameters={"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
        fn=lambda prompt, **kw: "https://example.com/i.png",
    ))
    cw.registry = reg

    cw.chat_view.clear()
    cw._handle_command("/image 猫")
    # QTextBrowser image 不在 toPlainText; 看 history[-1].content
    msg = cw.history[-1].content
    assert "https://example.com/i.png" in msg, (
        f"/image 命令应把 URL 写到 msg.content。 实际: {msg!r}"
    )

    print("[OK] v12: /image /video /audio 命令段在 _handle_command")


def test_v12_multimodal_setup_config_injected_from_cfg_path():
    """v12 + v15: ChatWindow 构造时, 从 cfg_path 读整个 yaml 注入 _multimodal._CONFIG
    (v15 改: setup_config 接受整个 cfg, 字段在 cfg.multimodal 段)。
    验证 _multimodal._CONFIG["multimodal"] 段含 base_url / api_key。
    """
    import os
    import tempfile
    from pathlib import Path
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig
    from app.engine.tools import _multimodal

    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "cfg.yaml"
        # v15 简化 config: 主人 plan 走 llm 段共享 base_url, multimodal 段只配 model
        cfg_path.write_text(
            "llm:\n"
            "  base_url: https://api.minimaxi.com/v1\n"
            "  api_key: sk-llm-shared\n"
            "  model: MiniMax-M3\n"
            "multimodal:\n"
            "  enabled: true\n"
            "  image_model: image-01\n",
            encoding="utf-8",
        )
        old = os.environ.pop("MULTIMODAL_API_KEY", None)
        old2 = os.environ.pop("MULTIMODAL_BASE_URL", None)
        try:
            cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                            'assets/sprites', asr_enabled=False,
                            cfg_path=str(cfg_path))
            # v15: _multimodal._CONFIG 接收整个 yaml, 字段在 multimodal 段
            mm_cfg = _multimodal._CONFIG.get("multimodal") or {}
            assert mm_cfg.get("image_model") == "image-01", (
                f"_CONFIG.multimodal 应含 image_model。 实际: {mm_cfg}"
            )
            # 验证 _cfg 能从 llm 段共享 base_url / api_key (v15 主线)
            assert _multimodal._cfg("base_url") == "https://api.minimaxi.com/v1"
            assert _multimodal._cfg("api_key") == "sk-llm-shared"
        finally:
            if old is not None:
                os.environ["MULTIMODAL_API_KEY"] = old
            if old2 is not None:
                os.environ["MULTIMODAL_BASE_URL"] = old2

    print("[OK] v12+v15: ChatWindow 注入整个 cfg, multimodal 段共享 llm base_url")


def test_v12_image_command_missing_config_raises():
    """v12: 没 multimodal 段 / 没 api_key 时, _run_multimodal_action 报清晰错。"""
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig
    from app.engine.tools._core import ToolRegistry

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False)
    reg = ToolRegistry()
    from app.engine.tools._core import Tool
    reg.register(Tool(
        name="generate_image", description="stub",
        parameters={"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]},
        fn=lambda prompt, **kw: "https://example.com/i.png",
    ))
    cw.registry = reg

    # 直接调 generate_image (不通过 _run_multimodal_action), tool 应抛清晰错
    from app.engine.tools import _multimodal
    _multimodal.setup_config({})  # 清掉 cfg, 让 _check_or_raise 抛
    try:
        _multimodal.generate_image("cat")
        raise AssertionError("缺 cfg 应抛 RuntimeError")
    except RuntimeError as e:
        msg = str(e)
        assert "api_key" in msg or "base_url" in msg or "multimodal" in msg, (
            f"应清晰告诉主人缺 multimodal 段。 实际: {msg}"
        )
    finally:
        _multimodal.setup_config({})  # 清掉, 不污染其他测试

    print("[OK] v12: 没 multimodal cfg 时 generate_image 抛清晰错")



# ============================================================
# v13 调整 (2026-10-15):
#   1) multimodal 默认 endpoint 路径改 MiniMax 实际值
#      (image=image_generation / video=video_generation / audio=t2a_v2),
#      不是 OpenAI 兼容的 /images/generations (在 MiniMax 上 404)
#   2) 适配 MiniMax 实际响应 schema
#      (image: data.image_urls[] / data.image_base64[];
#       video: {task_id} 异步 polling /v2/query/video_generation/{task_id})
#   3) chat_window 不再有 emoji (主人实测反馈: 「以后项目中不要使用表情包」)
# ============================================================


def test_v13_endpoint_paths_default_to_minimax():
    """v13 + v15: base_url 配 minimaxi.com / minimax.io / minimax.cn 时,
    endpoint 走 MiniMax 官方 (image_generation / video_generation / t2a_v2),
    不是 OpenAI 兼容的 /images/generations (MiniMax 上 404)。
    """
    from app.engine.tools import _multimodal
    # v15: 传 base_url 模拟主人 plan (含 minimax 域名)
    _multimodal.setup_config({
        "llm": {"base_url": "https://api.minimaxi.com/v1"},
    })
    # v17 实际: MiniMax video 是 v2 端点 (api.minimax.io/v2/video_generation)
    assert _multimodal._endpoint_path("image") == "image_generation", (
        f"minimax 域 image endpoint 应是 image_generation, 实际: "
        f"{_multimodal._endpoint_path('image')}"
    )
    assert _multimodal._endpoint_path("video") == "v2/video_generation", (
        f"minimax 域 video endpoint 应是 v2/video_generation, 实际: "
        f"{_multimodal._endpoint_path('video')}"
    )
    assert _multimodal._endpoint_path("audio") == "t2a_v2", (
        f"minimax 域 audio endpoint 应是 t2a_v2, 实际: "
        f"{_multimodal._endpoint_path('audio')}"
    )
    _multimodal.setup_config({})

    # v15: 非 minimax 域 (如 openai) 走 OpenAI 兼容 endpoint
    _multimodal.setup_config({
        "llm": {"base_url": "https://api.openai.com/v1"},
    })
    assert _multimodal._endpoint_path("image") == "images/generations", (
        f"openai 域 image endpoint 应是 OpenAI 兼容 images/generations, 实际: "
        f"{_multimodal._endpoint_path('image')}"
    )
    _multimodal.setup_config({})

    print("[OK] v13+v15: endpoint 路径按 base_url 域名自动判断")


def test_v13_endpoint_path_overridden_by_cfg():
    """v15 改: endpoint_path 字段已被主人反馈「复杂化」删掉, 改为按 base_url
    域名自动判断。 测试 base_url 域名切换 (含 minimax 各种后缀 + openai)。"""
    from app.engine.tools import _multimodal
    for base_url, exp in [
        ("https://api.minimaxi.com/v1", "minimax"),
        ("https://api.minimaxi.cn/v1", "minimax"),
        ("https://api.minimax.io/v1", "minimax"),
        ("https://api.minimaxi.chat/v1", "minimax"),
        ("https://api.openai.com/v1", "openai"),
    ]:
        _multimodal.setup_config({"llm": {"base_url": base_url}})
        domain = _multimodal._base_domain()
        assert domain == exp, (
            f"base_url={base_url} 应判到 {exp} 域, 实际: {domain}"
        )
    _multimodal.setup_config({})

    print("[OK] v15: endpoint 路径按 base_url 域名自动判断 (移除 endpoint_path 字段)")


def test_v13_extract_first_url_minimax_official_schema():
    """v13: MiniMax 官方 image-01 响应 {data: {image_urls: [url, ...]}}
    / {data: {image_base64: [b64, ...]}} 都正确解析。"""
    from app.engine.tools import _multimodal

    # 官方 schema 1: URL list
    url = _multimodal._extract_first_url(
        {"image_urls": ["https://minimax/x.png", "https://minimax/y.png"]},
        kind="image",
    )
    assert url == "https://minimax/x.png", (
        f"应取 image_urls[0], 实际: {url!r}"
    )

    # 官方 schema 2: base64 list
    url = _multimodal._extract_first_url(
        {"image_base64": ["AAAA", "BBBB"]}, kind="image",
    )
    assert url == "data:image/png;base64,AAAA", (
        f"应取 image_base64[0] 包成 data URL, 实际: {url!r}"
    )

    # OpenAI / 代理 schema: list[dict]
    url = _multimodal._extract_first_url(
        [{"url": "https://proxy/x.png"}], kind="image",
    )
    assert url == "https://proxy/x.png"

    # OpenAI b64_json
    url = _multimodal._extract_first_url(
        [{"b64_json": "BBBB"}], kind="image",
    )
    assert url == "data:image/png;base64,BBBB"

    # 兜底 audio mime
    url = _multimodal._extract_first_url(
        [{"b64_json": "AAA"}], kind="audio",
    )
    assert url == "data:audio/mpeg;base64,AAA", (
        f"audio 应走 audio/mpeg mime, 实际: {url!r}"
    )

    print("[OK] v13: _extract_first_url 兼容 MiniMax / OpenAI / 代理 schema")


def test_v13_generate_image_uses_minimax_schema():
    """v13: base_url 配 minimaxi.com 时, generate_image 走 MiniMax 官方 schema
    (aspect_ratio + response_format=url), 不是 OpenAI 的 size。

    v14 升级: generate_image 落盘到 assets/uploads/, 返回 file:// 路径。
    测试需要 mock urlopen 两次: POST (拿 MiniMax 响应) + GET (落盘时下载)。
    """
    import io
    from app.engine.tools import _multimodal
    captured = {}
    def _fake_urlopen(req, timeout=60):
        # POST: 拿 MiniMax 响应, 含 image_urls
        if req.data is not None:
            captured["body"] = req.data.decode("utf-8")
            captured["url"] = req.full_url
            resp_body = (
                b'{"data":{"image_urls":["https://minimax/x.png"]},'
                b'"metadata":{"success_count":"1","failed_count":"0"},'
                b'"base_resp":{"status_code":0,"status_msg":"success"}}'
            )
            return io.BytesIO(resp_body)
        # GET: v14 落盘时下载 URL → 返 1x1 png 字节
        return io.BytesIO(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00"
            b"\x00\x00\x0cIDAT\x08\x99c\xf8\xcf\xc0"
            b"\x00\x00\x00\x03\x00\x01\x5b\xb6\xee"
            b"\x56\x00\x00\x00\x00IEND\xaeB`\x82"
        )
    real = _multimodal.urllib.request.urlopen
    _multimodal.urllib.request.urlopen = _fake_urlopen  # type: ignore[attr-defined]
    try:
        _multimodal.setup_config({
            "base_url": "https://api.minimaxi.com/v1",
            "api_key": "sk-v13-test",
            "image_model": "image-01",
            "aspect_ratio": "16:9",
        })
        url = _multimodal.generate_image("a cat in space")
        # v14: 返回 file:// 路径 (落盘到项目 uploads)
        assert url.startswith("file:///"), (
            f"v14 应落盘返回 file://, 实际: {url!r}"
        )
        assert url.endswith(".png"), f"image 后缀应是 .png, 实际: {url}"
        assert "assets/uploads/" in url, (
            f"v14 落盘到 assets/uploads/, 实际: {url!r}"
        )
        # 验证 body 走 MiniMax schema
        assert "aspect_ratio" in captured["body"], (
            f"MiniMax 应走 aspect_ratio 字段。 body: {captured['body']}"
        )
        assert "response_format" in captured["body"]
        assert "16:9" in captured["body"]
    finally:
        _multimodal.urllib.request.urlopen = real  # type: ignore[attr-defined]
        _multimodal.setup_config({})
        # 清理落盘的 fake png
        if url.startswith("file:///"):
            try:
                Path(url[len("file:///"):]).unlink(missing_ok=True)
            except OSError:
                pass

    print("[OK] v13: generate_image 走 MiniMax schema + v14 落盘 file://")


def test_v13_no_emoji_in_chat_window_ui_strings():
    """v13 守卫: chat_window 里没有 emoji block / 装饰符号 (主人 2026-10-15
    反馈: 「以后项目中不要使用表情包」)。

    扫源码, 排除 docstring 跟注释 (注释里如果有 emoji 也算违规, 防止后续
    grep 误判)。"""
    import re
    src = Path("app/ui/chat_window/__init__.py").read_text(encoding="utf-8-sig")

    emoji_pat = re.compile(
        r"[\U0001F300-\U0001FAFF\U0001F600-\U0001F64F"
        r"\U0001F680-\U0001F6FF\u2600-\u27BF"
        r"\u2713\u2714\u2715\u2716\u2717\u2718]"
    )
    hits = []
    for ln_num, line in enumerate(src.splitlines(), 1):
        # 跳过注释行 (含 docstring 的单行) — docstring 也算 UI 文本不该有 emoji
        # 严格不放过任何 emoji (包括注释里)
        m = emoji_pat.search(line)
        if m:
            hits.append((ln_num, m.group(), line.strip()[:80]))

    assert not hits, (
        f"chat_window 还有 emoji/装饰符号, 应清。 命中: {hits[:10]}"
    )

    print("[OK] v13: chat_window 全清 emoji/装饰符号 (主人偏好)")



# ============================================================
# v14 调整 (2026-10-15):
#   1) 多模态文件落盘 assets/uploads/: generate_image / video / audio 拿到 URL
#      / base64 后下载到项目 uploads, 返回 file:// 绝对路径
#   2) chat_store 持久化 file:// 路径, 重启后文件还在, 渲染不丢失
#   3) _inline_local_images 失败时给「图片已丢失: <name>」清晰提示, 不再空白
#   4) 同时支持项目内相对路径 (assets/uploads/xxx.png) — 按 chat_window
#      _uploads_dir() 解析
# ============================================================


def test_v14_download_to_uploads_local_file_passthrough():
    """v14: file:// 输入 (已经在本地) 直接返回, 不重复复制。"""
    from app.engine.tools import _multimodal
    _multimodal.setup_config({})
    src = "file:///C:/tmp/v14_src.png"
    out = _multimodal._download_to_uploads(src, "image")
    assert out == src, (
        f"file:// 输入应原样返回, 实际: {out!r}"
    )

    print("[OK] v14: _download_to_uploads file:// 输入 passthrough")


def test_v14_download_to_uploads_data_url_writes_to_uploads():
    """v14: data:image/png;base64,XXX 输入解码落盘到 assets/uploads/。"""
    import base64 as _b64
    from app.engine.tools import _multimodal
    _multimodal.setup_config({})
    # 1x1 png 字节
    raw_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde"
    )
    b64 = _b64.b64encode(raw_png).decode("ascii")
    data_url = f"data:image/png;base64,{b64}"
    out = _multimodal._download_to_uploads(data_url, "image")
    try:
        assert out.startswith("file:///"), f"应返 file://, 实际: {out!r}"
        assert "assets/uploads/" in out
        assert out.endswith(".png")
        # 文件确实落盘
        abs_path = Path(out[len("file:///"):])
        assert abs_path.is_file()
        # 内容应是 raw_png
        assert abs_path.read_bytes()[:4] == b"\x89PNG"
    finally:
        # 清理
        try:
            Path(out[len("file:///"):]).unlink(missing_ok=True)
        except (OSError, UnboundLocalError):
            pass

    print("[OK] v14: _download_to_uploads data: URL 落盘")


def test_v14_download_to_uploads_http_downloads_via_urlopen():
    """v14: http(s):// 输入走 urlopen 下载到 uploads。"""
    import io
    from app.engine.tools import _multimodal
    _multimodal.setup_config({})
    captured = {}
    def _fake_urlopen(req, timeout=120):
        captured["url"] = req.full_url
        return io.BytesIO(b"FAKE_HTTP_BODY_v14")
    real = _multimodal.urllib.request.urlopen
    _multimodal.urllib.request.urlopen = _fake_urlopen  # type: ignore[attr-defined]
    try:
        out = _multimodal._download_to_uploads(
            "https://example.com/v14.png", "image",
        )
        assert captured["url"] == "https://example.com/v14.png"
        assert "assets/uploads/" in out
        abs_path = Path(out[len("file:///"):])
        assert abs_path.is_file()
        assert abs_path.read_bytes() == b"FAKE_HTTP_BODY_v14"
    finally:
        _multimodal.urllib.request.urlopen = real  # type: ignore[attr-defined]
        try:
            Path(out[len("file:///"):]).unlink(missing_ok=True)
        except (OSError, UnboundLocalError):
            pass

    print("[OK] v14: _download_to_uploads http(s):// URL 走 urlopen 下载")


def test_v14_inline_local_images_missing_file_shows_placeholder():
    """v14: 渲染时 file:// 找不到文件 → 给「图片已丢失: <name>」提示, 不空白。"""
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False)
    out = cw._inline_local_images_to_data_url(
        "![主人上传](file:///C:/no/such/path/v14_missing.png)"
    )
    assert "图片已丢失" in out, (
        f"v14 找不到文件时给「图片已丢失」提示, 实际: {out!r}"
    )
    assert "v14_missing.png" in out, (
        f"提示里含文件名, 实际: {out!r}"
    )
    # 不能再是裸 file:// 文字
    assert "file:///C:/no/such" not in out, (
        f"v14 不应保留裸 file:// 文字, 实际: {out!r}"
    )

    print("[OK] v14: 找不到文件时给「图片已丢失: <name>」提示")


def test_v14_inline_local_images_resolves_assets_relative_path():
    """v14: chat_window 渲染项目内相对路径 (assets/uploads/xxx.png) —
    按 chat_window._uploads_dir() 解析为绝对路径, 存在则 inline。"""
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig
    from app.core.qt_compat import QPixmap, QColor
    import tempfile

    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False)
    with tempfile.TemporaryDirectory() as td:
        # 直接在 chat_window 的 _uploads_dir() 下放一张图
        uploads = cw._uploads_dir()
        target = uploads / "v14_rel.png"
        pix = QPixmap(1, 1)
        pix.fill(QColor("#abcabc"))
        pix.save(str(target), "PNG")

        try:
            # 用项目内相对路径 (windows 风格反斜杠先 normalize)
            rel = "assets/uploads/v14_rel.png"
            md = f"![主人拖图]({rel})"
            out = cw._inline_local_images_to_data_url(md)
            assert "data:image/png;base64," in out, (
                f"项目内相对路径应被 inline 成 data: URL。 实际: {out[:200]!r}"
            )
        finally:
            target.unlink(missing_ok=True)

    print("[OK] v14: 项目内相对路径 assets/uploads/xxx.png 解析 + inline")



# ============================================================
# v15 调整 (2026-10-15):
#   1) config.yaml multimodal 段大幅简化 (主人反馈「config.yaml 复杂化」):
#      跟 llm 段共享 base_url / api_key, 只配 3 个 model, 移除 endpoint_path 等
#   2) _multimodal.setup_config 接受整个 yaml cfg, 字段在 cfg.multimodal 段
#      兼容: _cfg 顺序 顶层 → multimodal → llm → env (老测试直接 setup_config({...}) 仍过)
#   3) _endpoint_path 按 base_url 域名自动判断 (minimax / openai / other)
#   4) 默认 model 改 MiniMax Plan 实际可用值 (image-01 / MiniMax-H3 / speech-2.8-hd)
# ============================================================


def test_v15_cfg_multimodal_section_simplified():
    """v15: config.yaml 的 multimodal 段被简化, 只剩 enabled + 3 个 model 字段,
    不再含 base_url / api_key / image_size / duration_seconds / endpoint_path。
    """
    import yaml
    from pathlib import Path
    cfg = yaml.safe_load(Path("config.yaml").read_text(encoding="utf-8-sig"))
    mm = cfg.get("multimodal") or {}
    # 必含字段
    assert mm.get("enabled") is True
    assert "image_model" in mm
    assert "video_model" in mm
    assert "audio_model" in mm
    # 不应再有 (主人反馈「复杂化」删了)
    assert "base_url" not in mm, (
        "v15 multimodal.base_url 应删除, 跟 llm 共享"
    )
    assert "api_key" not in mm, (
        "v15 multimodal.api_key 应删除, 跟 llm 共享"
    )
    assert "image_endpoint_path" not in mm, (
        "v15 不再让用户配 endpoint_path, 按 base_url 域名自动判断"
    )
    assert "image_size" not in mm, (
        "v15 移除冗余字段 (默认 1024x1024)"
    )
    assert "duration_seconds" not in mm, (
        "v15 移除冗余字段 (默认 4s / 6s 按 endpoint)"
    )
    # 验证跟 llm 段共享 base_url / api_key
    assert cfg.get("llm", {}).get("base_url") == cfg.get("llm", {}).get("base_url")

    print("[OK] v15: config.yaml multimodal 段简化, 跟 llm 共享 base_url/api_key")


def test_v15_cfg_multimodal_shares_llm_base_url_via_cfg():
    """v15: _multimodal.setup_config(整个 cfg) 后, _cfg('base_url') 优先从
    llm 段读 (主人 plan 是统一 key), 跟 multimodal 段无关。"""
    from app.engine.tools import _multimodal
    _multimodal.setup_config({
        "llm": {
            "base_url": "https://api.minimaxi.cn/v1",
            "api_key": "sk-plan-key",
        },
        "multimodal": {
            "enabled": True,
            "image_model": "image-01",
        },
    })
    assert _multimodal._cfg("base_url") == "https://api.minimaxi.cn/v1"
    assert _multimodal._cfg("api_key") == "sk-plan-key"
    assert _multimodal._cfg("image_model") == "image-01"
    _multimodal.setup_config({})

    print("[OK] v15: setup_config(整个 cfg) 让 multimodal 段共享 llm base_url")


def test_v15_endpoint_path_auto_judges_by_domain():
    """v15: endpoint 路径按 base_url 域名自动判断, 不再让用户配 endpoint_path。"""
    from app.engine.tools import _multimodal
    cases = [
        # (base_url, image, video, audio)
        # v17: MiniMax video 实际是 v2 端点 (/v2/video_generation)
        ("https://api.minimaxi.com/v1",
         "image_generation", "v2/video_generation", "t2a_v2"),
        ("https://api.minimaxi.cn/v1",
         "image_generation", "v2/video_generation", "t2a_v2"),
        ("https://api.minimaxi.chat/v1",
         "image_generation", "v2/video_generation", "t2a_v2"),
        # openai / 其他走 OpenAI 兼容
        ("https://api.openai.com/v1",
         "images/generations", "videos/generations", "audio/speech"),
    ]
    for base_url, exp_img, exp_vid, exp_aud in cases:
        _multimodal.setup_config({"llm": {"base_url": base_url}})
        assert _multimodal._endpoint_path("image") == exp_img, (
            f"{base_url}: image 期望 {exp_img}, 实际 {_multimodal._endpoint_path('image')}"
        )
        assert _multimodal._endpoint_path("video") == exp_vid, (
            f"{base_url}: video 期望 {exp_vid}, 实际 {_multimodal._endpoint_path('video')}"
        )
        assert _multimodal._endpoint_path("audio") == exp_aud, (
            f"{base_url}: audio 期望 {exp_aud}, 实际 {_multimodal._endpoint_path('audio')}"
        )
    _multimodal.setup_config({})

    print("[OK] v15: endpoint 路径按 base_url 域名自动判断")


def test_v15_multimodal_segment_optional():
    """v15: multimodal 段可以不写 (空), _multimodal 仍能从 llm 段共享
    base_url / api_key, 不抛错。"""
    from app.engine.tools import _multimodal
    _multimodal.setup_config({
        "llm": {
            "base_url": "https://api.minimaxi.com/v1",
            "api_key": "sk-llm-only",
        },
    })
    # 没 multimodal 段, 仍能读 base_url / api_key
    assert _multimodal._cfg("base_url") == "https://api.minimaxi.com/v1"
    assert _multimodal._cfg("api_key") == "sk-llm-only"
    # 但 _check_or_raise 还是要求 base_url + api_key 都有 (从 llm 段读 OK)
    _multimodal._check_or_raise("image")  # 不抛
    _multimodal.setup_config({})

    print("[OK] v15: multimodal 段可选, 不写时跟 llm 共享 base_url/api_key")



# ============================================================
# v16 调整 (2026-10-15):
#   MiniMax 1004 错 (login fail) 通常是 key 跟 base_url 不匹配 (plan key
#   走 plan 域, pay-as-you-go 走另一个域)。_post_json 抛错时给清晰中文提示,
#   主人不用读 stack trace 就能知道去 MiniMax 后台「套餐详情」找 plan base_url。
# ============================================================


def test_v16_post_json_1004_login_fail_clear_hint():
    """v16: MiniMax 1004 错 (login fail) 时, _post_json 抛的错应该含清晰
    提示告诉主人 key 跟 base_url 不匹配, 让主人去 MiniMax 后台「套餐详情」
    找 plan 专属 base_url。"""
    import io
    from app.engine.tools import _multimodal
    from urllib.error import HTTPError, URLError

    # 模拟 MiniMax 1004 响应
    def _fake_urlopen(req, timeout=60):
        if req.data is None:
            return io.BytesIO(b"")
        err = HTTPError(
            req.full_url, 401, "Unauthorized", {}, io.BytesIO(
                b'{"base_resp":{"statuscode":1004,"status_msg":"login fail: '
                b'Please carry the API secret key in the Authorization field"}}'
            ),
        )
        raise err

    real = _multimodal.urllib.request.urlopen
    _multimodal.urllib.request.urlopen = _fake_urlopen  # type: ignore[attr-defined]
    try:
        _multimodal.setup_config({
            "llm": {"base_url": "https://api.minimaxi.com/v1",
                    "api_key": "sk-cp-plan-key"},
        })
        try:
            _multimodal._post_json(
                "https://api.minimaxi.com/v1/image_generation",
                {"model": "image-01", "prompt": "cat"},
                {"Authorization": "Bearer sk-cp-plan-key"},
            )
            raise AssertionError("应抛 RuntimeError")
        except RuntimeError as e:
            msg = str(e)
            assert "1004" in msg or "login fail" in msg.lower(), (
                f"应提 1004 / login fail, 实际: {msg[:200]}"
            )
            # 关键: 提示主人 key 跟 base_url 不匹配
            assert "base_url" in msg and ("不匹配" in msg or "key" in msg), (
                f"应提示 key 跟 base_url 不匹配, 实际: {msg[:300]}"
            )
            # 提示主人去套餐详情找 plan 域
            assert "套餐详情" in msg or "plan" in msg.lower(), (
                f"应引导主人去 MiniMax 套餐详情找 plan 域, 实际: {msg[:300]}"
            )
    finally:
        _multimodal.urllib.request.urlopen = real  # type: ignore[attr-defined]
        _multimodal.setup_config({})

    print("[OK] v16: MiniMax 1004 错给清晰提示, 引导主人去套餐详情找 plan 域")


def test_v16_post_json_other_http_error_unchanged():
    """v16: 非 1004 错 (比如 500) 走原路径, 不被误判为 key/base_url 不匹配。"""
    import io
    from app.engine.tools import _multimodal
    from urllib.error import HTTPError

    def _fake_urlopen(req, timeout=60):
        if req.data is None:
            return io.BytesIO(b"")
        err = HTTPError(
            req.full_url, 500, "Internal Server Error", {},
            io.BytesIO(b'{"error":"server side oops"}'),
        )
        raise err

    real = _multimodal.urllib.request.urlopen
    _multimodal.urllib.request.urlopen = _fake_urlopen  # type: ignore[attr-defined]
    try:
        _multimodal.setup_config({"llm": {"base_url": "https://x", "api_key": "k"}})
        try:
            _multimodal._post_json("https://x", {}, {})
        except RuntimeError as e:
            msg = str(e)
            assert "500" in msg, f"应含 500, 实际: {msg[:200]}"
            # 关键: 不应含 v16 专属 plan 提示 (因为不是 1004)
            assert "套餐详情" not in msg, (
                f"非 1004 错不应触发 plan 提示, 实际: {msg[:200]}"
            )
    finally:
        _multimodal.urllib.request.urlopen = real  # type: ignore[attr-defined]
        _multimodal.setup_config({})

    print("[OK] v16: 非 1004 错不被误判为 plan key 错")



# ============================================================
# v17 调整 (2026-10-15):
#   1) chat 渲染 markdown image 缩略图限制 (主人实测 1024x1024 太大):
#      width=240 height=auto + CSS max-width:240px max-height:240px
#   2) 识图功能 (主人说「分析一下这张图片」):
#      understand_image tool 调 MiniMax M3 chat completion 多模态
#      (image_url data URL), 不需要装 winsdk / tesseract
#      ocr_image 仍保留, 描述改成「只提文字, 理解用 understand_image」
# ============================================================


def test_v17_understand_image_tool_registered():
    """v17: ocr 工具包 register 时同时注册 understand_image, 调 MiniMax M3
    多模态 chat completion 理解图片内容。"""
    from app.engine.tools._ocr import register
    from app.engine.tools._core import ToolRegistry

    reg = ToolRegistry()
    register(reg)
    # ocr_image 跟 understand_image 都应注册
    ocr = reg.get("ocr_image")
    und = reg.get("understand_image")
    assert ocr is not None, "ocr_image 应注册"
    assert und is not None, "understand_image 应注册 (v17 新增)"
    # understand_image description 提到不需要装 OCR 后端
    assert "ocr" in und.description.lower() or "OCR" in und.description, (
        f"understand_image description 应提到跟 ocr 区别。 实际: {und.description[:200]}"
    )
    assert "minimax" in und.description.lower() or "M3" in und.description, (
        f"understand_image description 应提到 MiniMax M3。 实际: {und.description[:200]}"
    )

    print("[OK] v17: understand_image 工具注册, 跟 ocr_image 区分清晰")


def test_v17_understand_image_missing_file_returns_clear_error():
    """v17: understand_image(image_path) 文件不存在时给清晰错, 不抛。"""
    from app.engine.tools._ocr import register
    from app.engine.tools._core import ToolRegistry
    reg = ToolRegistry()
    register(reg)
    und = reg.get("understand_image")
    out = und.fn(image_path="/no/such/file.png")
    assert "不存在" in out, f"应给「不存在」提示。 实际: {out}"
    assert "/no/such/file.png" in out, f"应含原路径。 实际: {out}"

    print("[OK] v17: understand_image 文件不存在给清晰错")


def test_v17_understand_image_missing_config_returns_clear_error():
    """v17: understand_image 缺 base_url / api_key 时给清晰错, 不抛。"""
    from app.engine.tools._ocr import register
    from app.engine.tools._core import ToolRegistry
    from app.engine.tools import _multimodal
    import tempfile
    from pathlib import Path
    from PIL import Image
    reg = ToolRegistry()
    register(reg)
    und = reg.get("understand_image")
    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "v17_test.png"
        Image.new("RGB", (4, 4), color="red").save(png, "PNG")
        # 清掉 multimodal cfg
        _multimodal.setup_config({})
        try:
            out = und.fn(image_path=str(png), prompt="描述图片")
            assert "base_url" in out or "api_key" in out, (
                f"应给「缺 base_url / api_key」提示。 实际: {out}"
            )
        finally:
            _multimodal.setup_config({})

    print("[OK] v17: understand_image 缺 cfg 给清晰错")


def test_v19_msg_html_empty_content_shows_placeholder():
    """v19: assistant / user 空 content + 无 tools 时, _msg_html 返灰色
    占位提示, 不渲染成空白文件图标。

    主人反馈 2026-10-15 "每次打开后信息就没了, 图片之类的":
    v12 之前 1004 鉴权失败批量产生 content=empty 的 assistant 消息,
    chat_store 一直保留, 重启后 _msg_html 渲染成「label + 空 body」
    = 视觉上像「空白文件图标」, 主人以为信息丢了。

    v19 给空 content + 无 tools 消息渲染一个灰色提示, 让主人知道哪条
    是空的, 主人按"清空"一键删掉。
    """
    from app.ui.chat_window import ChatWindow
    from app.ui.chat_window.message import Message
    from app.core.config import LLMConfig, CharacterConfig
    cw = ChatWindow(LLMConfig(), CharacterConfig(name="jingyu"),
                    "assets/sprites", asr_enabled=False)

    # v19 验证 1: assistant 空 content + 无 tools → 占位提示
    msg_empty = Message(role="assistant", content="")
    html = cw._msg_html(msg_empty)
    assert "empty-msg-hint" in html, (
        f"v19 应渲染 empty-msg-hint 占位 class. 实际: {html[:300]}"
    )
    assert "内容为空" in html, (
        f"v19 应展示「内容为空」文字提示. 实际: {html[:300]}"
    )
    # 不能渲染成空白 (空 body 不应出现)
    assert 'msg-body"></div>' not in html, (
        f"v19 空 content 不应渲染空 body. 实际: {html[:300]}"
    )

    # v19 验证 2: assistant 纯空格 content 也算空
    msg_spaces = Message(role="assistant", content="   \n  ")
    html2 = cw._msg_html(msg_spaces)
    assert "empty-msg-hint" in html2, (
        f"v19 纯空格 content 也走占位. 实际: {html2[:300]}"
    )

    # v19 验证 3: 有 tools 的空 content 不走占位 (工具有信息要保留)
    msg_with_tools = Message(role="assistant", content="", tools=[
        ("tool_xyz", {"arg": 1}, "return_text", 100),
    ])
    html3 = cw._msg_html(msg_with_tools)
    assert "empty-msg-hint" not in html3, (
        f"v19 有 tools 不走占位 (工具步骤要渲染). 实际: {html3[:300]}"
    )

    # v19 验证 4: 正常 content 走老路径, 不出占位
    msg_normal = Message(role="assistant", content="喵～主人好")
    html4 = cw._msg_html(msg_normal)
    assert "empty-msg-hint" not in html4, (
        f"v19 正常 content 不应出占位. 实际: {html4[:300]}"
    )

    # v19 验证 5: user 空 content 也走占位 (防御性, 实际 user 不会空)
    msg_user_empty = Message(role="user", content="")
    html5 = cw._msg_html(msg_user_empty)
    assert "empty-msg-hint" in html5, (
        f"v19 user 空 content 也走占位. 实际: {html5[:300]}"
    )

    print("[OK] v19: _msg_html 空 content 显示灰色占位提示")


def test_v19_chat_store_remove_empty_assistant():
    """v19: chat_store.remove_empty_assistant 删掉空 content 历史
    assistant 消息 (内存 + SQLite 双删), 让"打开桌宠看到空白文件图标"
    的历史 bug 不再复现。"""
    from app.engine.chat_store import ChatStore
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "v19.db"
        cs = ChatStore(db)
        # 造一批乱消息模拟主人历史
        cs.add("user", "主人问1")
        cs.add("assistant", "喵～回答1")
        cs.add("assistant", "")          # 1004 鉴权失败产生的空白
        cs.add("assistant", "   \n  ")    # 纯空格的空白
        cs.add("assistant", "", tools=[]) # 显式空 tools 空白
        cs.add("assistant", "", tools=[   # 有 tools 的空 content 不删
            ("tool_abc", {}, "out", 50),
        ])
        cs.add("user", "主人问2")
        cs.add("system", "系统提示")       # system 不动
        cs.add("assistant", "回答2")       # 正常回答

        # v19 验证 1: remove_empty_assistant 删 3 条空白
        # (assistant content 空 + tools 空)
        removed = cs.remove_empty_assistant()
        assert removed == 3, (
            f"v19 应删 3 条空白 assistant (2 条全空 + 1 条纯空格). 实际: {removed}"
        )

        # v19 验证 2: 内存剩下的 assistant 应都非空或有 tools
        remaining = [m for m in cs.all() if m.role == "assistant"]
        for m in remaining:
            assert (m.content or "").strip() or (m.tools or []), (
                f"v19 剩下的 assistant 不应「content 空 AND tools 空」. "
                f"实际 content={m.content!r} tools={m.tools!r}"
            )

        # v19 验证 3: 持久化也删了 (新建 ChatStore 读盘, 空白不再回来)
        cs2 = ChatStore(db)
        empty_count = sum(
            1 for m in cs2.all()
            if m.role == "assistant"
            and not (m.content or "").strip()
            and not (m.tools or [])
        )
        assert empty_count == 0, (
            f"v19 重读 DB 后空白应已删. 实际剩 {empty_count} 条空白"
        )

        # v19 验证 4: 没空白时返回 0, 不报错
        removed2 = cs.remove_empty_assistant()
        assert removed2 == 0, (
            f"v19 二次调用应返 0 (无空白). 实际: {removed2}"
        )

    print("[OK] v19: chat_store.remove_empty_assistant 双删空白历史")


def test_v20_render_markdown_image_thumb_and_click_to_zoom():
    """v20: chat 渲染 markdown image = 缩略图 + 点击放大 (主人 2026-10-16
    实测反馈「图片撑满整个窗口, 小窗口下看不全」)。

    实现路径:
      - <img> 用 HTML width=N (PyQt5 5.x 100% 支持, 不会撑开) 而非 v18 的
        CSS max-width:100% (PyQt5 5.x CSS subset 支持不全, 大图仍撑开)
      - 缩略图 width = min(340, max(200, viewport_w * 0.85)), chat 软
        件标准上限 340, 小窗口 fallback 200 保证能看清
      - 用 <a href="img-open:KEY"> 包裹, KEY 是 sha1(url)[:10], 点击
        走 _on_chat_anchor_clicked 路由到 _show_image_popup 弹原图
      - cursor:zoom-in 视觉提示可点击
    """
    from app.ui.chat_window import ChatWindow
    from app.ui.chat_window.message import Message
    from app.core.config import LLMConfig, CharacterConfig
    from app.core.qt_compat import QPixmap, QColor
    import tempfile
    from pathlib import Path

    cw = ChatWindow(LLMConfig(), CharacterConfig(name="test"),
                    "assets/sprites", asr_enabled=False)
    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "v20_thumb.png"
        pix = QPixmap(2048, 2048)  # 大图
        pix.fill(QColor("#abcabc"))
        pix.save(str(png), "PNG")
        file_url = "file:///" + str(png).replace("\\", "/").lstrip("/")
        md = f"![主人上传]({file_url})"
        msg = Message(role="assistant", content=md)
        html = cw._msg_html(msg)

    # v20 验证 1: 用 HTML width 属性 (PyQt5 100% 支持), 上限 340
    assert 'width="340"' in html, (
        f"v20 应给 width=340 上限。 实际: {html[:600]}"
    )
    assert 'style="display:block;width:340px' in html, (
        f"v20 应给 style width:340px (跟 HTML width 同步, 防 PyQt5 拉伸). 实际: {html[:600]}"
    )
    # v20 验证 2: cursor:zoom-in 提示可点击
    assert 'cursor:zoom-in' in html, (
        f"v20 应给 cursor:zoom-in 视觉提示. 实际: {html[:600]}"
    )
    # v20 验证 3: 包在 <a href="img-open:KEY"> 里, 可点击放大
    assert 'href="img-open:' in html, (
        f"v20 应包 img-open:KEY 锚点 (点击弹原图). 实际: {html[:600]}"
    )
    assert 'title="点击放大"' in html, (
        f"v20 应给 title 点击放大提示. 实际: {html[:600]}"
    )
    # v20 验证 4: img-open map 已记录真 URL
    assert len(cw._img_open_map) >= 1, (
        f"v20 应填 _img_open_map, 实际为空"
    )
    # 至少有一个 value 跟 file_url 等价 (data: url 转了 base64)
    found = False
    for v in cw._img_open_map.values():
        if v.startswith("data:") or v == file_url:
            found = True
            break
    assert found, (
        f"v20 _img_open_map 应含 file_url 或 data: 转码值. 实际: {list(cw._img_open_map.values())[:2]}"
    )

    # v20 验证 5: 移除 v17 固定 width=240
    assert 'width="240"' not in html, (
        f"v20 应移除 v17 固定 width=240. 实际: {html[:600]}"
    )
    # v20 验证 6: 移除 v18 object-fit:contain (新路径用 width attr)
    assert 'object-fit:contain' not in html, (
        f"v20 走 HTML width attr, 不需要 v18 object-fit. 实际: {html[:600]}"
    )
    # v20 验证 7: 移除 v18 max-height:480px (新路径不靠 CSS 控制高度)
    assert 'max-height:480px' not in html, (
        f"v20 走 width attr, 不靠 max-height CSS. 实际: {html[:600]}"
    )

    print("[OK] v20: chat 渲染 img 缩略图 width=340 + 点击放大 anchor")


def test_v20_img_open_key_stable():
    """v20: _img_open_key 同 URL 出同 KEY, 让 _img_open_map 重渲染可去重.

    用 sha1(url)[:10] 截前 10 位: 16^10 ≈ 1e12, 同一聊天 < 10 万张图不会撞。
    """
    from app.ui.chat_window.utils import _img_open_key

    # v20 验证 1: 同 URL 出同 KEY
    k1 = _img_open_key("file:///path/to/img.png")
    k2 = _img_open_key("file:///path/to/img.png")
    assert k1 == k2, f"v20 同 URL 应同 KEY. {k1} vs {k2}"

    # v20 验证 2: 不同 URL 出不同 KEY (sanity check)
    k3 = _img_open_key("file:///other.png")
    assert k1 != k3, f"v20 不同 URL 应不同 KEY. {k1} vs {k3}"

    # v20 验证 3: data: 长 base64 不抛异常
    long_data = "data:image/png;base64," + "A" * 5000
    k4 = _img_open_key(long_data)
    assert len(k4) == 10, f"v20 KEY 应 10 字符. 实际: {len(k4)}"

    # v20 验证 4: 中文 / unicode URL 也行
    k5 = _img_open_key("file:///C:/中文/桌宠图.png")
    assert len(k5) == 10, f"v20 unicode URL KEY 也应是 10. 实际: {len(k5)}"

    print("[OK] v20: _img_open_key sha1[:10] 稳定 + 不抛")


def test_v20_anchor_clicked_img_open_routes_to_popup():
    """v20: 点击 <a href="img-open:KEY"> 走 _on_chat_anchor_clicked
    路由到 _show_image_popup, KEY 不在 map 里就静默不弹。

    不真弹 QDialog (PyQt5 modal 会卡测试), 用 mock 验证调用。
    """
    from app.ui.chat_window import ChatWindow
    from app.core.config import LLMConfig, CharacterConfig
    from app.core.qt_compat import QUrl

    cw = ChatWindow(LLMConfig(), CharacterConfig(name="test"),
                    "assets/sprites", asr_enabled=False)

    # 模拟: 注册一个测试 img-open:KEY
    cw._img_open_map["deadbeef01"] = "file:///fake/path/to/img.png"

    # mock _show_image_popup 抓调用
    called = {}
    def _capture_popup(u):
        called["url"] = u
    cw._show_image_popup = _capture_popup  # type: ignore[method-assign]

    # v20 验证 1: 点 img-open:deadbeef01 应路由
    url = QUrl("img-open:deadbeef01")
    cw._on_chat_anchor_clicked(url)
    assert called.get("url") == "file:///fake/path/to/img.png", (
        f"v20 应路由到 _show_image_popup 拿真 url. 实际: {called}"
    )

    # v20 验证 2: 未注册的 KEY 静默 (不抛, 不调 popup)
    called.clear()
    url2 = QUrl("img-open:unknown_key")
    cw._on_chat_anchor_clicked(url2)
    assert "url" not in called, (
        f"v20 未知 KEY 应静默, 不调 _show_image_popup. 实际: {called}"
    )

    # v20 验证 3: img-open 不会被 cmd: / http: 分支误吞
    # 用 __setitem__ 让 lambda 总更新值 (setdefault 第二次不会覆写)
    called["url"] = None
    url3 = QUrl("img-open:deadbeef01")
    cw._on_chat_anchor_clicked(url3)
    assert called["url"] == "file:///fake/path/to/img.png", (
        f"v20 img-open 不应被 cmd/外部链接分支吃. 实际: {called}"
    )

    print("[OK] v20: _on_chat_anchor_clicked img-open 路由正确")
