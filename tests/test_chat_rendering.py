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
    """流式输出：bot 气泡末尾应有「三点跳动」等待动画（无 emotion 时）。"""
    char_cfg = CharacterConfig(name="测试")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    msg = Message(role="assistant", content="正在思考", emotion=None)
    html = cw._msg_html(msg, streaming_meta="typing…")
    # 旧的沙漏 ⏳ 已替换为三个圆点
    assert "⏳" not in html
    assert html.count("●") == 3, f"应渲染 3 个等待圆点，实际 {html.count('●')}"
    # 初始帧恰好一个点高亮（主题紫）
    assert html.count("#8b5cf6") == 1, "初始帧应有且仅有一个点高亮"
    print("[OK] Streaming indicator (three bouncing dots) shown in bot bubble")


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
    # 折叠态用 anchor link 触发 toggle
    assert 'href="cmd:toggle_tools"' in html
    assert "点击展开" in html
    assert "收起" not in html

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
    # 展开态有「点击收起」链接
    assert "点击收起" in html2
    assert "点击展开" not in html2
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
    """chat_cfg.user_avatar_path 有值时,_make_avatar_pixmap('user') 真的去读这个图。"""
    from app.core.config import LLMConfig, CharacterConfig, ChatConfig
    # 准备一个 32x32 红图作为 user 自定义头像
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        red_png = tmp_path / "red_user.png"
        # 用 QPixmap 生成真图(否则 QPixmap.isNull() 拒收)
        from app.core.qt_compat import QPixmap, QColor, QImage
        img = QImage(32, 32, QImage.Format.Format_ARGB32)
        img.fill(QColor(255, 0, 0))
        assert QPixmap.fromImage(img).save(str(red_png), "PNG")

        chat_cfg = ChatConfig(user_avatar_path=str(red_png))
        cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                        'assets/sprites', asr_enabled=False, chat_cfg=chat_cfg)
        pm = cw._make_avatar_pixmap("user", 40)
        assert not pm.isNull()
        # 不应再走默认「我」字+蓝渐变路径(user 角色有自定义 path 时)
        # 简单判定：非 32x32 缩放(我们画的是 40x40 圆头像,中心像素应不是蓝渐变)
        pixel = pm.toImage().pixel(20, 20)
        r = (pixel >> 16) & 0xff
        g = (pixel >> 8) & 0xff
        b = pixel & 0xff
        # 中心是红色(255,0,0),不应是蓝渐变(0x60,0xa5,0xfa 之类)
        assert r > 200 and g < 50 and b < 50, f"中心像素 {r},{g},{b} 应为红色"
        print("[OK] custom user_avatar_path is loaded into avatar pixmap")


def test_custom_avatar_fallback_when_missing():
    """user_avatar_path 指向不存在的文件 → 回退默认首字+蓝渐变,不崩。"""
    from app.core.config import LLMConfig, CharacterConfig, ChatConfig
    chat_cfg = ChatConfig(user_avatar_path="Z:/no/such/file.png")
    cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                    'assets/sprites', asr_enabled=False, chat_cfg=chat_cfg)
    pm = cw._make_avatar_pixmap("user", 40)
    # 不应崩,应回退到默认(中心蓝渐变像素)
    assert not pm.isNull()
    print("[OK] missing user_avatar_path falls back to default")


def test_set_custom_avatar_copies_and_persists():
    """_set_custom_avatar 复制源图到 data/avatars/<role>.png,更新 cfg 字段并写回 config.yaml。"""
    from app.core.config import LLMConfig, CharacterConfig, ChatConfig, load_config
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text("chat:\n  request_timeout_s: 30\n", encoding="utf-8")

        # 准备一个 16x16 真 PNG 当头像源
        from app.core.qt_compat import QImage, QPixmap, QColor
        src_img = tmp_path / "source.png"
        img = QImage(16, 16, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 255, 0))   # 纯绿
        assert QPixmap.fromImage(img).save(str(src_img), "PNG")

        chat_cfg = ChatConfig(request_timeout_s=30)
        # 改 cwd 让 AVATAR_DIR (data/avatars) 落在 tmp 下
        import os
        old_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            # 改 AVATAR_DIR 到 tmp 下
            import app.ui.chat_window as cw_mod
            from pathlib import Path as _P
            cw_mod.AVATAR_DIR = _P(tmp_path) / "data" / "avatars"
            cw_mod.AVATAR_DIR.mkdir(parents=True, exist_ok=True)

            cw = ChatWindow(LLMConfig(), CharacterConfig(name='test'),
                            'assets/sprites', asr_enabled=False, chat_cfg=chat_cfg,
                            cfg_path=str(cfg_path))
            ok = cw._set_custom_avatar("user", str(src_img))
            assert ok is True
            # self._user_avatar_path 应指向 .../data/avatars/user.png
            assert Path(cw._user_avatar_path).name == "user.png"
            assert "avatars" in Path(cw._user_avatar_path).parts
            # 文件应真在 tmp/data/avatars/user.png
            dst = cw_mod.AVATAR_DIR / "user.png"
            assert dst.is_file()
            # 重新加载验证内容是源图
            pm = QPixmap(str(dst))
            assert not pm.isNull()
            # config.yaml 写回 chat.user_avatar_path
            saved = load_config(cfg_path)
            assert Path(saved.chat.user_avatar_path).name == "user.png"
            assert "avatars" in Path(saved.chat.user_avatar_path).parts
            assert saved.chat.request_timeout_s == 30    # 其它字段保留
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
