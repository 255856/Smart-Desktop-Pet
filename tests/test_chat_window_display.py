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


def test_voice_tool_step_shows_name_status_duration_and_file():
    """A2：语音工具在界面上留痕 —— 中文名 + 真实状态 + 时长 + 音频文件。"""
    cw = _cw()
    msg = Message(role="assistant", content="念给你听~", tools=[
        ("mcp_voice_speak", '{"text":"今天也要好好吃饭哦"}',
         "念完了：今天也要好好吃饭哦（24.1 KB，3.4 秒）。 音色：中性。 "
         "已经放给主人听了。", "ok"),
    ])
    cw._tools_collapsed_by_msg[id(msg)] = False
    html = cw._tool_steps_html(msg.tools, msg_id=id(msg))
    assert "念一段话" in html, "语音工具没有中文名"
    assert "[完成]" in html
    assert "3.4 秒" in html, "语音时长没显示"
    assert "24.1 KB" in html, "语音体积没显示"


def test_voice_sing_tool_step_shows_produced_file():
    """唱歌：产物路径要能看到（音频落在哪）。"""
    cw = _cw()
    msg = Message(role="assistant", content="唱一段", tools=[
        ("mcp_voice_sing", '{"lyrics":"啦啦啦"}',
         "唱完了（12.5 KB，5.0 秒）。 旋律是按这段词现编的调（seed=7，基准 330 Hz）。 "
         "音频在 data\\voice_out\\sing_20260101-120000.wav。 已经放给主人听了。", "ok"),
    ])
    cw._tools_collapsed_by_msg[id(msg)] = False
    html = cw._tool_steps_html(msg.tools, msg_id=id(msg))
    assert "唱几句" in html
    assert "5.0 秒" in html
    assert "sing_20260101-120000.wav" in html, "音频文件没显示"


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
