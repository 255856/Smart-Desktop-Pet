# -*- coding: utf-8 -*-
"""表情包（send_sticker）专项测试。

锁住四件事：

1. **目录是索引出来的** —— 表情包只来自仓库里**已有的**
   assets/sprites/Emotion_* 帧（不下载、不新增素材文件）；
2. **工具便宜且诚实** —— 不联网、不调模型；发不出去就**如实报错**；
3. **气泡立刻出现** —— 工具结果一到就画成独立气泡，不等这一轮说完；
4. **图没了不画坏气泡** —— 解析不出来 / 文件不在就什么都不画。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".local-packages"))
sys.path.insert(0, str(ROOT))

from app.core.qt_compat import QApplication
app = QApplication.instance() or QApplication(sys.argv)

from app.core.config import CharacterConfig
from app.engine import stickers as st
from app.engine.stickers import (catalogue, catalogue_digest, get_sticker,
                                 normalize_emotion, parse_sticker_marker,
                                 pick_sticker, resolve_sticker_artifact,
                                 sticker_data_url)
from app.ui.chat_window import ChatWindow, Message


def _cw(name: str = "小汐") -> ChatWindow:
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    return ChatWindow(llm_cfg, CharacterConfig(name=name, persona=""),
                      "assets/sprites", asr_enabled=False)


def _registry():
    from app.engine.tools import build_default_tools

    class _S:
        def get(self, *a, **k):
            return None

        def stats_summary(self):
            return "stub"

    return build_default_tools(state=_S(), reminders=None, memory=None,
                               items=None)


# ---------------------------------------------------------------------------
# 1. 目录：只索引**已有**素材
# ---------------------------------------------------------------------------

def test_catalogue_is_built_from_existing_assets():
    """A1：目录来自仓库里已有的 Emotion_* 帧 —— 没有一个新素材文件。"""
    cat = catalogue()
    assert cat, "表情包目录是空的：assets/sprites/Emotion_* 没扫到"
    sprites = st.sprites_root().resolve()
    for sticker in cat.values():
        assert Path(sticker.file).is_file(), f"{sticker.id} 的图不在"
        assert Path(sticker.file).resolve().is_relative_to(sprites), (
            f"{sticker.id} 的图不在素材目录里")
        assert sticker.file.name.startswith("rmbg-"), sticker.file
    # 8 种情绪都在（happy/sad/anger/fear/doubt/pride/shy/surprise）
    emotions = {s.emotion for s in cat.values()}
    assert {"happy", "sad", "anger", "fear", "doubt", "pride", "shy",
            "surprise"} <= emotions, emotions


def test_catalogue_entry_shape():
    """目录形状：id -> {file, emotion, label, frames}。"""
    cat = catalogue()
    sid = sorted(cat)[0]
    data = cat[sid].to_dict()
    assert set(data) == {"id", "emotion", "label", "file", "frames"}
    assert data["id"] == sid
    assert data["file"]
    assert data["frames"], "每张表情包至少得有一帧"
    assert data["label"]


def test_catalogue_digest_lists_ids():
    """摘要里能看到每个情绪和它的 id（工具描述靠它）。"""
    digest = catalogue_digest()
    assert "happy" in digest and "happy_1" in digest


def test_pick_sticker_by_id_and_by_emotion():
    """按 id 挑 / 按情绪挑 / 都不给就随手一张。"""
    cat = catalogue()
    sid = sorted(cat)[0]
    assert pick_sticker(sticker_id=sid).id == sid
    assert pick_sticker(emotion="happy").emotion == "happy"
    assert pick_sticker(emotion="开心").emotion == "happy"
    assert pick_sticker(emotion="生气").emotion == "anger"
    assert pick_sticker() is not None


def test_normalize_emotion_aliases():
    """中文 / 别名都认；认不出来就返回空串（不瞎猜）。"""
    assert normalize_emotion("angry") == "anger"
    assert normalize_emotion("害怕") == "fear"
    assert normalize_emotion("") == ""
    assert normalize_emotion("confused") == ""


def test_get_sticker_unknown_returns_none():
    assert get_sticker("no_such_sticker") is None
    assert get_sticker("") is None


# ---------------------------------------------------------------------------
# 2. 产物解析：解析不出来 / 文件没了 -> 空（不画坏图）
# ---------------------------------------------------------------------------

def test_resolve_artifact_from_result_marker():
    art = resolve_sticker_artifact("send_sticker", "{}", "已发送。[sticker:happy_2]")
    assert art["id"] == "happy_2"
    assert art["emotion"] == "happy"
    assert art["exists"] is True
    assert Path(art["path"]).is_file()


def test_resolve_artifact_from_args():
    art = resolve_sticker_artifact("send_sticker", '{"sticker_id": "sad_1"}', "")
    assert art["id"] == "sad_1"


def test_resolve_artifact_other_tool_is_empty():
    assert resolve_sticker_artifact("open_app", "{}", "好了") == {}


def test_resolve_artifact_unknown_id_is_empty():
    """id 不认识 -> 解析不出来 -> 界面不画气泡。"""
    assert resolve_sticker_artifact("send_sticker", '{"sticker_id": "nope"}', "") == {}


def test_resolve_artifact_missing_file_reports_not_exists(tmp_path, monkeypatch):
    """图没了 -> exists=False（界面据此不画坏气泡）。"""
    fake = tmp_path / "sprites" / "Emotion_happy"
    fake.mkdir(parents=True)
    (fake / "rmbg-1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    cat = st.catalogue(tmp_path / "sprites")   # 缓存住（生产里就是这样）
    assert cat, "临时素材目录没建出目录"
    sid = sorted(cat)[0]
    (fake / "rmbg-1.png").unlink()          # 图没了
    art = st.resolve_sticker_artifact("send_sticker",
                                      '{"sticker_id": "%s"}' % sid, "",
                                      root=tmp_path / "sprites")
    assert art and art["exists"] is False


def test_sticker_data_url_missing_file_is_empty(tmp_path):
    assert sticker_data_url(tmp_path / "nope.png") == ""


def test_parse_sticker_marker():
    assert parse_sticker_marker("发了。[sticker:happy_3]") == "happy_3"
    assert parse_sticker_marker("没有标记") == ""


# ---------------------------------------------------------------------------
# 3. 工具：注册 / 便宜 / 诚实
# ---------------------------------------------------------------------------

def test_send_sticker_tool_is_registered():
    reg = _registry()
    assert "send_sticker" in reg.names()
    tool = reg.get("send_sticker")
    assert tool is not None
    assert "表情包" in tool.description


def test_send_sticker_tool_sends_existing_file():
    """成功时：结果里有标记，标记指向的图**真的存在**。"""
    tool = _registry().get("send_sticker")
    out = tool.fn(emotion="happy")
    assert "已发送" in out
    sid = parse_sticker_marker(out)
    assert sid
    stk = get_sticker(sid)
    assert stk is not None and Path(stk.file).is_file()


def test_send_sticker_tool_unknown_id_is_error():
    """不认识的 id -> **如实报错**（不糊弄，也不假装发了）。"""
    tool = _registry().get("send_sticker")
    out = tool.fn(sticker_id="no_such_sticker")
    assert out.startswith("错误：")
    assert parse_sticker_marker(out) == ""


def test_send_sticker_tool_unknown_emotion_is_error():
    tool = _registry().get("send_sticker")
    out = tool.fn(emotion="confused")
    assert out.startswith("错误：")


def test_send_sticker_tool_missing_file_is_error(tmp_path, monkeypatch):
    """图没了 -> 报错，**不发**（气泡端也就没有坏图）。"""
    tool = _registry().get("send_sticker")
    fake = tmp_path / "sprites" / "Emotion_happy"
    fake.mkdir(parents=True)
    (fake / "rmbg-1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    cat = st.build_catalogue(tmp_path / "sprites")
    sid = sorted(cat)[0]
    for f in cat[sid].frames:          # 图没了
        Path(f).unlink(missing_ok=True)
    monkeypatch.setattr(st, "catalogue", lambda root=None: cat)
    out = tool.fn(sticker_id=sid)
    assert out.startswith("错误：")
    assert parse_sticker_marker(out) == ""


def test_send_sticker_tool_does_not_touch_network(monkeypatch):
    """便宜：不联网（socket 一碰就炸，工具照样跑完）。"""
    import socket

    def _boom(*a, **k):
        raise AssertionError("表情包工具不该联网")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    tool = _registry().get("send_sticker")
    assert "已发送" in tool.fn(emotion="happy")


# ---------------------------------------------------------------------------
# 4. 界面：独立气泡 + 立刻出现 + 坏了不画
# ---------------------------------------------------------------------------

def test_sticker_bubble_html_contains_image():
    """A2：气泡里真的有图（data URL）+ 名字。"""
    cw = _cw()
    art = resolve_sticker_artifact("send_sticker", "", "[sticker:happy_1]")
    html = cw._sticker_bubble_html(art)
    assert "sticker-bubble" in html
    assert "data:image/png;base64," in html
    assert "开心" in html


def test_sticker_bubble_missing_file_is_empty():
    """A4：图没了 -> 不画（没有坏掉的空图）。"""
    cw = _cw()
    art = {"id": "x", "emotion": "happy", "label": "开心 1",
           "path": str(ROOT / "assets" / "sprites" / "Emotion_happy" / "rmbg-9999.png"),
           "exists": False}
    assert cw._sticker_bubble_html(art) == ""


def test_sticker_shows_up_immediately_as_its_own_bubble():
    """A3：工具结果一到就画成**独立气泡**，不等这一轮说完。"""
    cw = _cw()
    cw._handle_sticker_tool("send_sticker", '{"emotion": "happy"}',
                            "已发送表情包「开心 1」。[sticker:happy_1]", "ok")
    # 注意：Qt 的 toHtml() 会把 class 换成内联样式，所以查**气泡里的文字**
    # （表情包名字）和图片资源，而不是 class 名。
    assert "开心 1" in cw.chat_view.toPlainText(), "表情包没立刻出现在聊天里"
    assert "base64" in cw.chat_view.toHtml(), "气泡里没有图片"


def test_sticker_bubble_not_duplicated_inside_message():
    """已经单独画过的，同一条消息里**不再画一遍**。"""
    cw = _cw()
    result = "已发送表情包「开心 1」。[sticker:happy_1]"
    cw._handle_sticker_tool("send_sticker", '{"emotion": "happy"}', result, "ok")
    msg = Message(role="assistant", content="给你看~",
                  tools=[("send_sticker", '{"emotion": "happy"}', result, "ok")])
    assert "sticker-bubble" not in cw._msg_html(msg)


def test_sticker_bubble_in_history_message():
    """历史回放：老消息里的表情包照样画出来（落在消息里）。"""
    cw = _cw()
    msg = Message(role="assistant", content="给你看~",
                  tools=[("send_sticker", '{"emotion": "happy"}',
                          "已发送表情包「开心 1」。[sticker:happy_1]", "ok")])
    assert "sticker-bubble" in cw._msg_html(msg)


def test_sticker_bubble_survives_streaming_refresh():
    """流式占位气泡刷新时，表情包**不能被一起擦掉**。"""
    cw = _cw()
    cw._current_bot_msg = Message(role="assistant", content="正在说…")
    cw._streaming_anchor_pos = cw._insert_placeholder(cw._current_bot_msg)
    cw._handle_sticker_tool("send_sticker", '{"emotion": "happy"}',
                            "已发送表情包「开心 1」。[sticker:happy_1]", "ok")
    cw._refresh_streaming_message()
    assert "开心 1" in cw.chat_view.toPlainText(), "刷新占位气泡把表情包擦了"
    assert cw._streaming_anchor_pos is not None


def test_sticker_failed_tool_shows_no_bubble():
    """工具失败 -> 不画气泡（工具块里已经标红）。"""
    cw = _cw()
    cw._handle_sticker_tool("send_sticker", '{"sticker_id": "nope"}',
                            "错误：没有这种表情包", "error")
    assert "sticker-bubble" not in cw.chat_view.toHtml()
    assert "sticker-cap" not in cw.chat_view.toHtml()


def test_sticker_image_can_be_clicked_to_enlarge():
    """点图看大图：走聊天里普通图片那套 img-open 锚点。"""
    cw = _cw()
    art = resolve_sticker_artifact("send_sticker", "", "[sticker:happy_1]")
    html = cw._sticker_bubble_html(art)
    assert "#img-" in html, "表情包没有点开看大图的入口"
    key = html.split("#img-")[1].split('"')[0]
    assert key in cw._img_open_map
    url = cw._img_open_map[key]
    assert url.startswith("file:///")
    assert Path(url[len("file:///"):]).is_file()


def test_on_tool_draws_the_sticker_bubble_right_away():
    """走真实的 _on_tool 入口：气泡立刻出现，且消息里不再画第二遍。"""
    cw = _cw()
    cw._current_bot_msg = Message(role="assistant", content="")
    result = "已发送表情包「开心 1」。[sticker:happy_1]"
    cw._on_tool("send_sticker", '{"emotion": "happy"}', result, "ok")
    assert "开心 1" in cw.chat_view.toPlainText(), "表情包没立刻出现在聊天里"
    assert "sticker-bubble" not in cw._msg_html(cw._current_bot_msg), (
        "同一条消息里又画了一遍表情包")

