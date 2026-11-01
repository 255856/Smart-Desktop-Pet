# -*- coding: utf-8 -*-
"""输入框高度回归：两行起步、随内容长高。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from app.core.qt_compat import QApplication


def test_input_box_starts_at_two_lines_and_grows(qapp, tmp_path):
    """输入框：两行高度起步，输入多了才长高（底部不留大片空白）。"""
    from app.core.config import CharacterConfig
    from app.ui.chat_window import ChatWindow
    cw = ChatWindow(type('L', (), {'model': 't', 'api_key': 't'})(),
                    CharacterConfig(name='dfy', persona=''), 'assets/sprites',
                    asr_enabled=False)
    cw.resize(760, 500)
    cw.show()
    qapp.processEvents()
    edit = cw.input_edit
    two_lines = cw._input_min_height
    assert edit.minimumHeight() == two_lines, edit.minimumHeight()
    assert edit.height() <= two_lines * 1.6, edit.height()
    # 塞 6 行 → 应该长高，但不超过上限
    edit.setPlainText(NL.join(['第 %d 行' % i for i in range(6)]))
    qapp.processEvents()
    grew = edit.height()
    assert grew > two_lines, (grew, two_lines)
    assert grew <= two_lines * 3 + 2, grew

NL = chr(10)
