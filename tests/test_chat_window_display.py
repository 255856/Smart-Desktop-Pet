# -*- coding: utf-8 -*-
"""聊天窗**显示**专项测试（聊天窗显示评审）。

锁住这次评审发现并修掉的显示问题：

  1. 摘要条判定按**前缀**（主人自己写「【历史摘要】」不能被当成摘要条）
  2. 历史消息的时间戳用库里那条（不是「现在」）
  3. 桌宠消息显示**说话当时**的人设名（换人设后老消息不被改标）
  4. 工具步骤行带状态类名 + 失败原因（失败行不能和成功行长得一样）
  5. 语音工具在界面上留痕：中文名 + 真实状态 + 时长 + 音频文件
  6. 折叠态预览不能把后面的失败藏起来
  7. 代码块 / 表格的 markdown 不能把内容弄丢（转义 + 表格末行）
  8. 长代码行不再把整条消息撑到几千像素宽
  9. 亮 / 暗两套主题的规则必须同步
 10. 大肥鱼（非默认人设）渲染正确（名字 + 语气词不串人设）
 11. 界面补的那句「已经帮主人搞定啦~」在工具失败时不许说
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".local-packages"))
sys.path.insert(0, str(ROOT))

from app.core.qt_compat import QApplication
app = QApplication.instance() or QApplication(sys.argv)

from app.core.config import CharacterConfig, load_character_entry
from app.ui import ui_style as _us
from app.ui.chat_window import ChatWindow, Message


@pytest.fixture(autouse=True)
def _no_asr_native_load(monkeypatch):
    """挡住 ASR 预热真加载 CTranslate2（Windows 上会崩进程）。"""
    from app.voice import asr as asr_mod
    monkeypatch.setattr(asr_mod.SpeechRecognizer, "_get_model",
                        lambda self, *a, **k: None, raising=False)


def _cw(name: str = "小汐", persona: str = "") -> ChatWindow:
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    return ChatWindow(llm_cfg, CharacterConfig(name=name, persona=persona),
                      "assets/sprites", asr_enabled=False)


# ---------------------------------------------------------------------------
# 1. 摘要条：必须是**前缀**命中
# ---------------------------------------------------------------------------

def test_summary_entry_is_rendered_as_summary_block():
    """压缩摘要条目（内容以【历史摘要】开头）→ 独立摘要块，不是「主人」说的话。"""
    from app.brain.compaction import SUMMARY_MARKER
    cw = _cw()
    msg = Message(role="user", content=SUMMARY_MARKER + "（更早的对话已经压缩成下面这段）\n以前聊过天气")
    html = cw._msg_html(msg)
    assert "msg-summary" in html
    assert "历史摘要" in html
    assert ">主人<" not in html


def test_user_message_mentioning_summary_marker_is_not_a_summary():
    """主人自己写到「【历史摘要】」→ 仍然是主人的话，不能被改标成摘要条。"""
    from app.brain.compaction import SUMMARY_MARKER
    cw = _cw()
    msg = Message(role="user", content="刚才那个【历史摘要】是什么意思？")
    html = cw._msg_html(msg)
    assert "msg-summary" not in html
    assert ">主人<" in html
    assert "刚才那个" in html


# ---------------------------------------------------------------------------
# 2. 历史消息的时间戳
# ---------------------------------------------------------------------------

def test_history_message_keeps_its_own_timestamp(tmp_path):
    """从库里读出来的历史消息必须显示**当时**的时间，不是「现在」。"""
    from app.engine.chat_store import ChatStore, StoredMessage
    cw = _cw()
    store = ChatStore(data_file=tmp_path / "hist.db", legacy_json=None)
    old_ts = time.time() - 3600 * 5
    # 直接把「库里那条」放进 store（ts 是 5 小时前）
    store._messages = [StoredMessage(role="user", content="昨天说的话",
                                      ts=old_ts, character_name="")]
    cw.chat_store = store
    cw.history = []
    cw._load_history()
    assert cw.history, "历史没读进来"
    assert abs(cw.history[0].ts - old_ts) < 1, (
        f"历史消息的时间戳被换成现在了: {cw.history[0].ts} != {old_ts}"
    )


def test_message_label_shows_message_timestamp():
    """标签上的时间来自 Message.ts。"""
    cw = _cw()
    msg = Message(role="user", content="hi", ts=time.time() - 3600)
    html = cw._msg_html(msg)
    assert time.strftime("%H:%M", time.localtime(msg.ts)) in html


# ---------------------------------------------------------------------------
# 3. 桌宠自己的名字：说话当时的人设名
# ---------------------------------------------------------------------------

def test_assistant_message_keeps_its_own_speaker():
    """换人设后，老消息仍显示当时的名字（不被改标成新人设）。"""
    cw = _cw("大肥鱼")
    old = Message(role="assistant", content="哼，才不是特意帮你呢。", speaker="小汐")
    html = cw._msg_html(old)
    assert ">小汐<" in html
    assert "大肥鱼" not in html


def test_assistant_message_without_speaker_falls_back_to_current_character():
    """老记录没有 speaker 字段 → 退回当前人设名（行为不变）。"""
    cw = _cw("大肥鱼")
    html = cw._msg_html(Message(role="assistant", content="在的"))
    assert ">大肥鱼<" in html


# ---------------------------------------------------------------------------
# 4 + 5 + 6. 工具步骤卡片
# ---------------------------------------------------------------------------

def test_failed_tool_row_has_state_class_and_reason():
    """失败的工具行：状态类名 + 失败原因（不能只写一个「[失败]」）。"""
    cw = _cw()
    msg = Message(role="assistant", content="试试", tools=[
        ("open_app", '{"app_name":"记事本"}', "失败：没有找到该应用", "error"),
    ])
    cw._tools_collapsed_by_msg[id(msg)] = False
    html = cw._tool_steps_html(msg.tools, msg_id=id(msg))
    assert "tool-row failed" in html
    assert "[失败]" in html
    assert "没有找到该应用" in html, "失败原因没显示出来"


def test_ok_tool_row_has_ok_class():
    cw = _cw()
    msg = Message(role="assistant", content="好了", tools=[
        ("add_reminder", '{"content":"开会"}', "已设置提醒", "ok"),
    ])
    cw._tools_collapsed_by_msg[id(msg)] = False
    html = cw._tool_steps_html(msg.tools, msg_id=id(msg))
    assert "tool-row ok" in html
    assert "[完成]" in html


def test_voice_tool_step_shows_name_status_duration_and_file(tmp_path,
                                                                      monkeypatch):
    """语音工具在界面上留痕 —— 英文工具名（主）+ 中文小提示 + 真实状态 + 时长 + 体积。

    语音是**两段式**的：合成好了之后那条消息里的工具行才有时长 / 体积。
    """
    wav = _voice_artifact(tmp_path)
    _patch_voice_out(monkeypatch, tmp_path)
    cw = _cw()
    msg = Message(role="assistant", content="念给你听~", tools=[
        ("mcp_voice_speak", '{"text":"今天也要好好吃饭哦"}',
         "语音合成好了（3.4 秒，24.1 KB）。音频在 %s。" % wav, "ok"),
    ])
    cw._tools_collapsed_by_msg[id(msg)] = False
    html = cw._tool_steps_html(msg.tools, msg_id=id(msg))
    assert '<span class="tool-name">mcp_voice_speak</span>' in html, (
        "英文工具名不是主显示")
    assert '<span class="tool-cn">念一段话</span>' in html, "中文小提示没了"
    assert "[完成]" in html
    assert "3.4 秒" in html, "语音时长没显示"
    assert "24.1 KB" in html, "语音体积没显示"


def test_voice_sing_tool_step_shows_produced_file(tmp_path, monkeypatch):
    """唱歌：语音条上是 QQ 那样的语音条 —— 有时长，**没有文件名**。

    owner 原话：「不要显示『11.8 秒 sing_20261030-200825.wav』这种东西」。
    """
    wav = _voice_artifact(tmp_path, name="sing_20260101-120000.wav", kind="sing",
                          duration=5.0, size=12800)
    _patch_voice_out(monkeypatch, tmp_path)
    cw = _cw()
    msg = Message(role="assistant", content="唱一段", tools=[
        ("mcp_voice_sing", '{"lyrics":"啦啦啦"}',
         "语音合成好了（5.0 秒，12.5 KB）。音频在 %s。" % wav, "ok"),
    ])
    cw._tools_collapsed_by_msg[id(msg)] = False
    html = cw._tool_steps_html(msg.tools, msg_id=id(msg))
    assert '<span class="tool-name">mcp_voice_sing</span>' in html
    assert "5.0 秒" in html
    bubble = cw._voice_bubbles_html(msg.tools, msg_ts=msg.ts)
    assert "sing_20260101-120000.wav" not in bubble, (
        "语音条上不该再显示文件名（owner 要求）")
    import re as _re
    assert _re.search(r"\d+(\.\d+)? 秒", bubble), "语音条上没显示时长"
    assert "voice-wave" in bubble, "语音条上没画波形"


def test_voice_tool_failure_follows_envelope_status():
    """信封说失败就是失败 —— 不能因为文本里没前缀就显示成「完成」。"""
    cw = _cw()
    msg = Message(role="assistant", content="念一句", tools=[
        ("mcp_voice_speak", '{"text":"你好"}', "TTS 服务没响应", "error"),
    ])
    cw._tools_collapsed_by_msg[id(msg)] = False
    html = cw._tool_steps_html(msg.tools, msg_id=id(msg))
    assert "tool-row failed" in html
    assert "[失败]" in html
    assert "[完成]" not in html


def test_collapsed_preview_reports_later_failures():
    """折叠态：第一步成功、后面失败时，折叠行必须说出来（不能藏）。"""
    cw = _cw()
    msg = Message(role="assistant", content="好了", tools=[
        ("add_reminder", '{"content":"开会"}', "已设置提醒", "ok"),
        ("open_app", '{"app_name":"记事本"}', "失败：没有找到该应用", "error"),
    ])
    cw._tools_collapsed_by_msg[id(msg)] = True
    html = cw._tool_steps_html(msg.tools, msg_id=id(msg))
    assert "另有 1 步失败" in html, html


def test_tool_marks_have_no_inline_colour():
    """工具标记的颜色走 CSS 类（两套主题同步），不在渲染端写死。"""
    cw = _cw()
    msg = Message(role="assistant", content="好了", tools=[
        ("add_reminder", '{"content":"开会"}', "已设置提醒", "ok"),
    ])
    cw._tools_collapsed_by_msg[id(msg)] = False
    html = cw._tool_steps_html(msg.tools, msg_id=id(msg))
    assert "tool-mark tool-ok" in html
    assert "style=" not in html, "工具行里还有内联样式"


# ---------------------------------------------------------------------------
# 7 + 8. markdown 内容不许丢
# ---------------------------------------------------------------------------

def test_code_block_html_is_escaped_but_renders_verbatim():
    """代码块：HTML 里转义（否则 < b > 被当标签吞掉），显示出来一字不差。"""
    from PyQt5.QtGui import QTextDocument
    cw = _cw()
    fence = chr(96) * 3
    md = fence + "python\nif x < y and a > b:\n    print(\"<b>hi</b>\")\n" + fence
    html = cw._render_markdown(md)
    assert "<b>" not in html, "代码块里的标签被当成 HTML 了"
    doc = QTextDocument()
    doc.setHtml(html)
    shown = doc.toPlainText()
    assert "if x < y and a > b:" in shown, shown
    assert 'print("<b>hi</b>")' in shown, shown


def test_inline_markdown_inside_blocks_is_rendered():
    """列表 / 引用 / 表格 / 标题里的行内标记要渲染，不能显示成原样符号。"""
    cw = _cw()
    assert "<strong>粗</strong>" in cw._render_markdown("- **粗** 项")
    assert "<code>code</code>" in cw._render_markdown("- 用 \x60code\x60 写")
    assert "<strong>粗</strong>" in cw._render_markdown("> 引用 **粗**")
    assert "<strong>c</strong>" in cw._render_markdown("| **c** |\n|---|---|\n| d |")
    assert "<strong>粗</strong>" in cw._render_markdown("## 标题 **粗**")


def test_table_at_end_of_message_keeps_last_row():
    """表格正好在消息结尾时，最后一行不能丢（原来只剩表头 + 一串 | 原文）。"""
    cw = _cw()
    html = cw._render_markdown("| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<td>1</td>" in html and "<td>2</td>" in html, html
    assert "| 1 | 2 |" not in html, "表格数据行漏在表格外面"


def test_long_code_line_does_not_overflow_the_message():
    """长代码行不再把整条消息撑到几千像素宽（Qt 不支持 overflow:auto）。"""
    from PyQt5.QtGui import QTextDocument
    cw = _cw()
    fence = chr(96) * 3
    long_line = "x = some_very_long_variable_name " * 8
    html = cw._render_markdown(fence + "python\n" + long_line + "\n" + fence)
    doc = QTextDocument()
    doc.setDefaultStyleSheet(_us.CHAT_MD_CSS)
    doc.setTextWidth(400)
    doc.setHtml('<div class="msg msg-assistant"><div class="msg-body">' + html + "</div></div>")
    assert doc.idealWidth() <= 420, (
        f"消息被长代码行撑到 {doc.idealWidth()}px 宽（视口只有 400px）"
    )
    # 内容不能少
    assert "some_very_long_variable_name" in doc.toPlainText()


# ---------------------------------------------------------------------------
# 9. 亮 / 暗两套主题
# ---------------------------------------------------------------------------

def _selectors(css: str) -> list:
    out = []
    for chunk in css.split("}"):
        chunk = chunk.strip()
        if not chunk:
            continue
        out.append(chunk.split("{")[0].strip())
    return out


def test_two_themes_share_the_same_rules():
    """两套主题必须共用同一份规则表（结构上就不可能只改一套）。"""
    assert _us.CHAT_MD_CSS_DARK, "没有暗色主题"
    assert _selectors(_us.CHAT_MD_CSS) == _selectors(_us.CHAT_MD_CSS_DARK)
    assert _us.chat_md_css("dark") == _us.CHAT_MD_CSS_DARK
    assert _us.chat_md_css("light") == _us.CHAT_MD_CSS


def test_new_rules_exist_in_both_themes():
    """这次新增的规则在两套主题里都要有。"""
    for css in (_us.CHAT_MD_CSS, _us.CHAT_MD_CSS_DARK):
        for rule in (
            "div.tools .tool-row.ok",
            "div.tools .tool-row.failed",
            "div.tools .tool-mark.tool-failed",
            "div.tools .tool-reason",
            "div.tools .tool-note",
            "div.tools .tools-preview",
            "div.tools .tools-tail",
            "div.msg-notice",
            "div.plan-card",
            "div.thinking .thinking-body",
            "div.execs .execs-row",
            "div.file-edits .file-edit-row",
        ):
            assert rule in css, f"{rule} 不在某一套主题里"


def test_legacy_bubble_stylesheet_stays_in_sync():
    """旧气泡样式表也补上了同一批规则（两套样式不许漂移）。"""
    for rule in ("div.tools .tool-row.failed", "div.tools .tool-note",
                 "div.msg-notice"):
        assert rule in _us.CHAT_BUBBLE_CSS


# ---------------------------------------------------------------------------
# 10. 非默认人设：大肥鱼
# ---------------------------------------------------------------------------

def test_dfy_persona_renders_its_own_name():
    """A5：大肥鱼（characters/dfy.yaml）渲染出自己的名字。"""
    entry = load_character_entry(ROOT, "characters/dfy.yaml")
    assert entry.ok, entry.error
    cw = _cw(name=entry.name, persona=entry.persona)
    html = cw._msg_html(Message(role="assistant", content="哼，才不是特意帮你呢。"))
    assert ">大肥鱼<" in html
    assert "小汐" not in html


def test_dfy_greeting_does_not_meow():
    """大肥鱼是鲸鱼 —— 开场白不能对着主人「喵~」（那是小汐的口癖）。"""
    entry = load_character_entry(ROOT, "characters/dfy.yaml")
    assert entry.ok, entry.error
    cw = _cw(name=entry.name, persona=entry.persona)
    tone = cw._persona_tone()
    assert "喵" not in tone, f"大肥鱼的开场白语气词是 {tone!r}"
    assert tone, "大肥鱼的人设里明明有口癖，应该认得出来"


def test_xiaoxi_greeting_still_meows():
    """默认人设（小汐）的口癖不能被改掉。"""
    entry = load_character_entry(ROOT, "characters/xiaoxi.yaml")
    assert entry.ok, entry.error
    cw = _cw(name=entry.name, persona=entry.persona)
    assert cw._persona_tone() == "喵~"


# ---------------------------------------------------------------------------
# 11. 界面补的那句「已经帮主人搞定啦~」
# ---------------------------------------------------------------------------

def test_ui_fallback_claim_is_replaced_when_tool_failed():
    """工具失败时，界面补的那句「已经帮主人搞定啦~」不能显示（是假的）。"""
    from app.ui.chat_window import _UI_TOOL_DONE_TEXT
    cw = _cw()
    msg = Message(role="assistant", content=_UI_TOOL_DONE_TEXT, tools=[
        ("open_app", '{"app_name":"记事本"}', "失败：没有找到该应用", "error"),
    ])
    html = cw._msg_html(msg)
    assert _UI_TOOL_DONE_TEXT not in html, "界面还在说「已经帮主人搞定啦」"
    assert "msg-notice" in html


def test_ui_fallback_claim_is_kept_when_tool_succeeded():
    """工具真的成了 → 那句话成立，照旧显示。"""
    from app.ui.chat_window import _UI_TOOL_DONE_TEXT
    cw = _cw()
    msg = Message(role="assistant", content=_UI_TOOL_DONE_TEXT, tools=[
        ("add_reminder", '{"content":"开会"}', "已设置提醒", "ok"),
    ])
    html = cw._msg_html(msg)
    assert _UI_TOOL_DONE_TEXT in html
    assert "msg-notice" not in html


def test_streaming_shows_text_live_when_not_reading_aloud():
    """默认不朗读时，文字要**立刻**流式出。

    老设计：文字等 TTS 音频备好再一起出（音画同步）。但默认改成不朗读之后，
    那条链路不跑了 → 文字只剩「最后一次性蹦出来」一种表现。
    """
    char_cfg = CharacterConfig(name="小汐")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    # 有 TTS、但不朗读 → 立刻流式出
    cw.tts = object()
    cw._voice_auto_read_enabled = lambda: False
    assert cw._streaming_shows_live() is True

    # 有 TTS、而且要朗读 → 保持老行为（文字跟着音频一起出）
    cw._voice_auto_read_enabled = lambda: True
    assert cw._streaming_shows_live() is False

    # 根本没有 TTS → 立刻流式出
    cw.tts = None
    assert cw._streaming_shows_live() is True
    print("[OK] 流式文字与 TTS 已解耦：不朗读时立刻出字")


def test_rebuild_rerenders_history_so_tools_can_expand():
    """点开「已执行操作」要有内容 —— 这个重建函数以前是空的（只做滚动位置）。"""
    char_cfg = CharacterConfig(name="小汐")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites")

    msg = Message(role="assistant", content="查到了")
    msg.tools = [("web_search", '{"query":"天气"}', "晴", "")]
    cw.history = [msg]
    cw._tools_collapsed_by_msg[0] = False   # 展开态
    cw._rebuild_chat_view_from_history()
    text = cw.chat_view.toPlainText()
    assert "已执行操作" in text
    assert "web_search" in text or "联网搜索" in text
    # 折叠态也要能渲染出来（同一个函数，两种状态）
    cw._tools_collapsed_by_msg[0] = True
    cw._rebuild_chat_view_from_history()
    assert "已执行操作" in cw.chat_view.toPlainText()
    print("[OK] 重建真的重建了：展开/折叠两种状态都能渲染")

# ---------------------------------------------------------------------------
# 语音条（voice bubble）：语音是「一条消息」，不是工具块里的一行字
# ---------------------------------------------------------------------------

def _voice_artifact(tmp_path, *, name="speak_20260101-120000.wav", kind="speak",
                    duration=3.4, size=24647, make_wav=True):
    """造一份语音产物（wav + 旁路文件），返回 wav 路径。

    旁路文件 = 生产端（app/mcp/voice_server.py）落盘时写下的那份结构化信息
    （路径 / 时长 / 体积）—— 模型看不到它，界面靠它做语音条。
    """
    from app.mcp import voice_server
    out_dir = tmp_path / "voice_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    wav = out_dir / name
    if make_wav:
        wav.write_bytes(b"RIFF0000WAVEfmt ")
    meta = out_dir / (name + voice_server.VOICE_META_SUFFIX)
    meta.write_text(json.dumps({
        "kind": kind, "path": str(wav), "file_name": wav.name,
        "duration": duration, "size": size, "created": time.time(),
        "text": "",
    }, ensure_ascii=False), encoding="utf-8")
    return wav


def _patch_voice_out(monkeypatch, tmp_path):
    """让语音产物解析指向临时目录（不碰真的 data/voice_out）。"""
    from app.ui.chat_window import utils as _u
    monkeypatch.setattr(_u, "_voice_meta_settings",
                        lambda: (tmp_path / "voice_out", ".voice.json"))
    _u._VOICE_ARTIFACT_CACHE.clear()
    return _u


def test_voice_bubble_is_qq_style_no_file_name(tmp_path, monkeypatch):
    """A1：语音条 = QQ 语音条：播放按钮 + 波形 + 时长，**没有文件名**。

    owner 原话：「我要的播放标识指的是像QQ一样的语音条，不要显示
    『11.8 秒 sing_20261030-200825.wav』这种东西」。
    """
    import re as _re
    wav = _voice_artifact(tmp_path)
    _patch_voice_out(monkeypatch, tmp_path)
    cw = _cw()
    msg = Message(role="assistant", content="念给你听~", tools=[
        ("mcp_voice_speak", '{"text":"今天也要好好吃饭哦"}',
         "语音合成好了（3.4 秒，24.1 KB）。音频在 %s。" % wav, "ok"),
    ])
    html = cw._voice_bubbles_html(msg.tools, msg_ts=msg.ts)
    assert "voice-bubble" in html, html
    # QQ 语音条的三件套：播放按钮 + 波形 + 时长
    assert "voice-btn" in html, "语音条上没有播放按钮"
    assert "voice-wave" in html, "语音条上没有波形"
    assert "voice-bar" in html, "语音条上没有波形竖条"
    assert "3.4 秒" in html, "语音条上没有时长"
    # **没有文件名**、没有「秒 + 文件名」那一坨
    assert wav.name not in html, "语音条上还在显示文件名"
    assert "voice-file" not in html, "语音条上还有文件名那一块"
    assert "播放" in html, "播放入口没有中文文案"
    # 播放入口是**页内锚点**：点它不会把整个聊天区清空（空白）
    assert "#vp-" in html, "播放入口不是页内锚点"
    assert "voice-play:" not in html, "播放入口还在用自定义 scheme"
    # 整条消息里也有（语音条真的进了聊天窗，不只是孤立组件）
    full = cw._msg_html(msg)
    assert "voice-bubble" in full, full
    assert wav.name not in full, "整条消息里还漏着文件名"


def test_voice_bubble_play_click_keeps_chat_unchanged(tmp_path, monkeypatch):
    """A2：点播放 —— 声音响了，**聊天内容一个字都不许变**。

    owner 报过「点击播放后聊天窗又会空白跟之前展开操作一样」。
    这里锁住：点播放前后文本、消息条数、滚动位置全都不变。
    """
    from PyQt5.QtCore import QPoint
    from PyQt5.QtTest import QTest
    from PyQt5.QtCore import Qt
    wav = _voice_artifact(tmp_path)
    _patch_voice_out(monkeypatch, tmp_path)
    played = []
    from app.voice import voice as _voice
    monkeypatch.setattr(_voice, "play_audio_file",
                        lambda p, **kw: played.append(str(p)) or True)
    cw = _cw()
    cw.resize(900, 600)
    cw.show()
    cw.chat_view.clear()
    cw.history = []
    msg = Message(role="assistant", content="念给你听~", tools=[
        ("mcp_voice_speak", '{"text":"今天也要好好吃饭哦"}',
         "语音合成好了（3.4 秒，24.1 KB）。音频在 %s。" % wav, "ok"),
    ])
    cw._render_message(msg)
    cw.history.append(msg)
    app.processEvents()

    before_text = cw.chat_view.toPlainText()
    before_blocks = cw.chat_view.document().blockCount()
    before_chars = cw.chat_view.document().characterCount()
    before_history = len(cw.history)

    # 找到「播放」那个锚点，真的点一下
    point = None
    viewport = cw.chat_view.viewport()
    for y in range(0, viewport.height(), 2):
        for x in range(0, viewport.width(), 2):
            if str(cw.chat_view.anchorAt(QPoint(x, y))).startswith("#vp-"):
                point = QPoint(x, y)
                break
        if point is not None:
            break
    assert point is not None, "语音条上没找到播放锚点"
    QTest.mouseClick(viewport, Qt.LeftButton, Qt.NoModifier, point)
    app.processEvents()

    assert played == [str(wav)], played
    after_text = cw.chat_view.toPlainText()
    assert after_text == before_text, (
        "点播放之后聊天内容变了：\n前=%r\n后=%r" % (before_text, after_text))
    assert cw.chat_view.document().blockCount() == before_blocks, (
        "点播放之后消息条数变了")
    assert cw.chat_view.document().characterCount() == before_chars, (
        "点播放之后文档长度变了")
    assert len(cw.history) == before_history, "点播放之后历史被动过"


def test_voice_bubble_is_honest_when_file_is_gone(tmp_path, monkeypatch):
    """A2：音频文件没了就说没有 —— 不给点了没反应的死按钮。"""
    wav = _voice_artifact(tmp_path, make_wav=False)
    _patch_voice_out(monkeypatch, tmp_path)
    cw = _cw()
    msg = Message(role="assistant", content="念给你听~", tools=[
        ("mcp_voice_speak", '{"text":"今天也要好好吃饭哦"}',
         "语音合成好了（3.4 秒，24.1 KB）。音频在 %s。" % wav, "ok"),
    ])
    html = cw._voice_bubbles_html(msg.tools, msg_ts=msg.ts)
    assert "voice-bubble" in html, html
    assert "voice-play:" not in html, "文件没了还留着播放按钮（死按钮）"
    assert "音频文件已不在" in html, "文件没了没说清楚"


def test_voice_play_click_plays_that_wav(tmp_path, monkeypatch):
    """A1：点「播放」真的放那一条 wav（不弹窗、不抢焦点、不动滚动）。"""
    wav = _voice_artifact(tmp_path)
    _patch_voice_out(monkeypatch, tmp_path)
    played = []
    from app.voice import voice as _voice
    monkeypatch.setattr(_voice, "play_audio_file",
                        lambda p, **kw: played.append(str(p)) or True)
    cw = _cw()
    cw._play_voice_artifact(str(wav))
    assert played == [str(wav)], played
    # 走锚点点击（主人真的点那个链接）也是同一条路
    from PyQt5.QtCore import QUrl
    from urllib.parse import quote
    played.clear()
    cw._on_chat_anchor_clicked(QUrl("voice-play:" + quote(str(wav), safe="")))
    assert played == [str(wav)], played


def test_voice_bubble_not_shown_when_tool_failed(tmp_path, monkeypatch):
    """失败的语音不出语音条（失败那步在工具块里标红就够了）。"""
    _voice_artifact(tmp_path)
    _patch_voice_out(monkeypatch, tmp_path)
    cw = _cw()
    msg = Message(role="assistant", content="念一句", tools=[
        ("mcp_voice_speak", '{"text":"你好"}', "失败：TTS 服务没响应", "error"),
    ])
    assert cw._voice_bubbles_html(msg.tools, msg_ts=msg.ts) == ""


def test_tool_row_shows_english_tool_name_first():
    """A5：工具行主显示**英文工具名**，中文标签只当副的小提示。"""
    cw = _cw()
    msg = Message(role="assistant", content="查到了", tools=[
        ("web_search", '{"query":"今天天气"}', "晴", "ok"),
    ])
    cw._tools_collapsed_by_msg[id(msg)] = False
    html = cw._tool_steps_html(msg.tools, msg_id=id(msg))
    assert '<span class="tool-name">web_search</span>' in html, html
    assert '<span class="tool-cn">联网搜索</span>' in html, html
    # 折叠态预览也是英文名在前
    cw._tools_collapsed_by_msg[id(msg)] = True
    preview = cw._tool_steps_html(msg.tools, msg_id=id(msg))
    assert "▸ web_search" in preview, preview


def test_tool_block_is_quiet_but_failure_still_stands_out():
    """A6：工具块安静下来（更小 / 更淡 / 更少底色），失败那步仍然显眼。"""
    css = _us.CHAT_MD_CSS
    block = css.split("div.tools{")[1].split("}")[0]
    assert "background-color:transparent" in block, block
    assert "font-size:8.5pt" in block, block
    assert "border:none" in block, block
    assert "padding:1px 0 1px 8px" in block, block
    # 成功那步是低调的灰，失败那步是红色加粗
    muted = _us._CHAT_MD_LIGHT["text_muted"]
    fail_col = _us._CHAT_MD_LIGHT["text_mark_fail"]
    assert "div.tools .tool-mark.tool-ok{color:%s;}" % muted in css, css
    assert "div.tools .tool-mark.tool-failed{color:%s;font-weight:600;}" % fail_col in css, css
    assert "div.tools .tool-row.failed .tool-name{color:%s;}" % (
        _us._CHAT_MD_LIGHT["text_mark_fail"]) in css, css
    # 暗色主题同步（同一份规则表，只换颜色）
    dark = _us.CHAT_MD_CSS_DARK
    assert "div.tools .tool-mark.tool-failed{color:%s;font-weight:600;}" % (
        _us._CHAT_MD_DARK["text_mark_fail"]) in dark, dark


def test_voice_tool_result_text_unchanged_meta_goes_out_of_band(tmp_path, monkeypatch):
    """A3：模型看到的结果文本原样不变；结构化信息走旁路文件，不进文本。

    语音是**两段式**的：工具**立刻**返回「已经受理」（里面没有机器标记），
    合成好了之后结构化信息（路径 / 时长 / 体积）在旁路文件里。
    """
    from app.mcp import voice_server
    out_dir = tmp_path / "voice_out"
    monkeypatch.setattr(voice_server, "_cfg", lambda: {
        "output_dir": str(out_dir), "tts_url": "http://127.0.0.1:9880",
    })
    monkeypatch.setattr(voice_server, "_resolve_ref",
                        lambda cfg, emo: (Path("ref.wav"), "", "neutral", ""))
    monkeypatch.setattr(voice_server, "_wav_info", lambda p: (24647, 3.4))

    class _Resp:
        def read(self):
            return b"0" * 512

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(voice_server.urllib.request, "urlopen",
                        lambda *a, **k: _Resp())

    res = voice_server.handle_speak({"text": "今天也要好好吃饭哦"})
    text = res["content"][0]["text"]
    # 模型看到的那段文本里没有机器标记
    for bad in ("{", "}", ".voice.json", "<", ">"):
        assert bad not in text, f"模型看到的结果文本里有机器标记：{bad}"
    # 两段式：现在还没好（模型不能说「念完了」）
    assert "现在还没好" in text, text
    assert "念完" not in text, text
    # 合成好了 → 结构化信息在旁路文件里（不在文本里）
    job = voice_server.wait_voice_job(
        re.search(r"任务号[:：]\s*([A-Za-z0-9_\-]+)", text).group(1),
        voice_server._cfg(), timeout=30)
    assert job.get("state") == "done", job
    metas = list(out_dir.glob("*" + voice_server.VOICE_META_SUFFIX))
    assert metas, "旁路文件没写"
    data = json.loads(metas[0].read_text(encoding="utf-8"))
    assert data["duration"] == 3.4
    assert data["kind"] == "speak"
    assert data["path"].endswith(".wav")
    # 界面渲染也不会把机器标记漏进聊天窗
    _patch_voice_out(monkeypatch, tmp_path)
    cw = _cw()
    msg = Message(role="assistant", content="念给你听~", tools=[
        ("mcp_voice_speak", '{"text":"今天也要好好吃饭哦"}', text, "ok"),
    ])
    html = cw._msg_html(msg)
    assert ".voice.json" not in html
    assert "{" not in html and "}" not in html
    assert msg.tools[0][2] == text, "界面改了结果文本（模型看到的不一样）"


# ---------------------------------------------------------------------------
# 两段式：语音条**后到**（主人什么都不用做）
# ---------------------------------------------------------------------------

def _write_job(out_dir, job_id, payload):
    """写一个合成任务状态文件（界面端读的就是它）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / (job_id + ".voicejob.json")
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_voice_job_arrives_later_as_new_message(tmp_path, monkeypatch):
    """合成好了 → 聊天里**自己**多出一条带语音条的消息（不用主人点任何东西）。"""
    from app.ui.chat_window import utils as _u
    out_dir = tmp_path / "voice_out"
    _patch_voice_out(monkeypatch, tmp_path)
    wav = _voice_artifact(tmp_path, name="sing_20260101-120000.wav", kind="sing",
                          duration=5.0, size=12800)
    job_id = "sing_20260101-120000"
    _write_job(out_dir, job_id, {
        "id": job_id, "state": "done", "kind": "sing", "tool": "mcp_voice_sing",
        "text": "啦啦啦", "note": "", "error": "",
        "path": str(wav), "duration": 5.0, "size": 12800,
        "created": time.time(), "updated": time.time(),
    })
    cw = _cw()
    cw.chat_view.clear()
    # 先登记一个「在跑的合成」
    cw._track_voice_job("mcp_voice_sing", '{"lyrics":"啦啦啦"}',
                         "已经受理：开始按旋律唱这段词。 任务号：%s" % job_id)
    assert job_id in cw._voice_pending_jobs
    cw._check_voice_jobs()
    # 任务跑完了 → 聊天里多了一条带语音条的消息
    assert not cw._voice_pending_jobs, "跑完了还没从待办里去掉"
    assert cw._voice_arrived, "后到的语音消息没记下来"
    arrived = cw._voice_arrived[0]
    bubble = cw._voice_bubbles_html(arrived.tools, msg_ts=arrived.ts)
    assert "voice-bubble" in bubble, bubble
    assert "5.0 秒" in bubble, bubble
    # 聊天区里真的多了一条（语音条 + 时长）
    text = cw.chat_view.toPlainText()
    assert "5.0 秒" in text, text
    assert arrived.role == "assistant"


def test_voice_job_failure_is_reported_honestly(tmp_path, monkeypatch):
    """合成失败 → 聊天里**如实说**失败（不装没发生，也不给坏掉的语音条）。"""
    out_dir = tmp_path / "voice_out"
    _patch_voice_out(monkeypatch, tmp_path)
    job_id = "sing_20260101-120001"
    _write_job(out_dir, job_id, {
        "id": job_id, "state": "failed", "kind": "sing", "tool": "mcp_voice_sing",
        "text": "", "note": "", "error": "音转音失败：显存不够",
        "path": "", "duration": 0.0, "size": 0,
        "created": time.time(), "updated": time.time(),
    })
    cw = _cw()
    cw.chat_view.clear()
    cw._track_voice_job("mcp_voice_sing", '{"lyrics":"啦啦啦"}',
                         "已经受理：开始按旋律唱这段词。 任务号：%s" % job_id)
    cw._check_voice_jobs()
    text = cw.chat_view.toPlainText()
    assert "合成失败" in text, text
    assert "显存不够" in text, text
    assert cw._voice_arrived, "失败消息没记下来"
    assert cw._voice_arrived[0].role == "system"
    # 失败了**不给**语音条（不给一个坏掉的按钮）
    assert not cw._voice_arrived[0].tools
    assert cw._voice_bubbles_html(cw._voice_arrived[0].tools) == ""


def test_voice_watch_only_polls_pending_jobs(tmp_path, monkeypatch):
    """没有任务在跑时**不轮询**（不扫产物目录）；有任务才开表。"""
    from app.ui.chat_window import utils as _u
    out_dir = tmp_path / "voice_out"
    _patch_voice_out(monkeypatch, tmp_path)
    cw = _cw()
    assert not cw._voice_job_timer.isActive(), "没有任务却一直在轮询"
    # 没有任务号 → 不登记
    cw._track_voice_job("mcp_voice_sing", "{}", "已经受理：开始唱。")
    assert not cw._voice_pending_jobs
    assert not cw._voice_job_timer.isActive()
    # 不是语音工具 → 不登记
    cw._track_voice_job("web_search", "{}", "任务号：abc")
    assert not cw._voice_pending_jobs
    # 有任务号 → 登记并开表
    cw._track_voice_job("mcp_voice_sing", "{}", "任务号：sing_1")
    assert cw._voice_pending_jobs
    assert cw._voice_job_timer.isActive()
    # 任务跑完 → 表停掉
    _write_job(out_dir, "sing_1", {
        "id": "sing_1", "state": "done", "kind": "sing", "tool": "mcp_voice_sing",
        "text": "", "note": "", "error": "", "path": "", "duration": 0.0,
        "size": 0, "created": time.time(), "updated": time.time(),
    })
    cw._check_voice_jobs()
    assert not cw._voice_job_timer.isActive(), "任务跑完了还在轮询"

