# -*- coding: utf-8 -*-
"""聊天窗口的布局与步骤状态回归（owner 报的「名字错位」+「只显示调用工具」）。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from app.core.qt_compat import QApplication, QTextCursor


def _cw(tmp_path):
    from app.core.config import CharacterConfig
    from app.ui.chat_window import ChatWindow
    return ChatWindow(type('L', (), {'model': 't', 'api_key': 't'})(),
                      CharacterConfig(name='dfy', persona=''), 'assets/sprites',
                      asr_enabled=False)


def test_the_name_sits_on_the_same_side_as_its_bubble(qapp, tmp_path):
    """名字必须跟气泡同侧（主人的在右、她的在左）—— QQ 那样。

    踩过的坑：Qt 富文本里 CSS 的 text-align 盖不住「插入时继承来的块对齐」，
    上一条是右对齐（主人）时，下一条（她的）名字会跟着右对齐 → 名字和气泡分家。
    """
    from app.ui.chat_window import Message
    cw = _cw(tmp_path)
    cw.resize(760, 500)
    cw.show()
    cw.chat_view.clear()
    cw._render_message(Message(role='user', content='sing me a song'))
    cw._render_message(Message(role='assistant', content='好嘞，这就唱'))
    qapp.processEvents()
    qapp.processEvents()
    doc = cw.chat_view.document()
    xs = []
    texts = []
    b = doc.begin()
    while b.isValid():
        cur = QTextCursor(doc)
        cur.setPosition(b.position())
        xs.append(cw.chat_view.cursorRect(cur).x())
        texts.append(b.text())
        b = b.next()
    # 主人：名字和话都在右边；她：名字和话都在左边
    user_label = next(i for i, t in enumerate(texts) if '主人' in t)
    user_body = next(i for i, t in enumerate(texts) if 'sing me a song' in t)
    asst_label = next(i for i, t in enumerate(texts) if 'dfy' in t)
    asst_body = next(i for i, t in enumerate(texts) if '好嘞' in t)
    assert xs[user_label] > xs[user_body] - 400, (xs, texts)
    assert xs[asst_label] < 200, (xs, texts)
    assert xs[asst_body] < 200, (xs, texts)


def test_tool_steps_show_what_she_is_doing(qapp, tmp_path):
    """步骤行要写「她在做什么」（读取记忆 / 写入文件 / 查找资料），不只是工具名。"""
    from app.ui.chat_window.utils import tool_phase
    assert tool_phase('recall_memory') == '读取记忆'
    assert tool_phase('remember_fact') == '写入记忆'
    assert tool_phase('write_file') == '写入文件'
    assert tool_phase('web_search') == '查找资料'
    assert tool_phase('mcp_voice_sing') == '合成语音'
    assert tool_phase('open_app') == '打开应用'
    assert tool_phase('some_unknown_tool') == '执行中'

