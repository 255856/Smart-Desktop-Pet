# -*- coding: utf-8 -*-
"""长期记忆管理面板单元测试：增删 / 去重 / 搜索 / 类别筛选 / 信号 / ChatWindow 接入。"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".local-packages"))
sys.path.insert(0, str(ROOT))

from app.core.qt_compat import QApplication
app = QApplication.instance() or QApplication(sys.argv)

from app.brain.memory import MemoryStore
from app.core.config import CharacterConfig
from app.ui.memory_panel import MemoryDialog, CATEGORY_LABELS
from app.ui.chat_window import ChatWindow


def _store():
    p = Path(tempfile.mkdtemp()) / "m.json"
    m = MemoryStore(str(p))
    m.add("主人不喜欢吃香菜", "preference", 0.9)
    m.add("主人是大三学生", "fact", 0.5)
    m.add("主人的室友叫阿杰", "person", 0.6)
    return m


def test_dialog_add():
    m = _store()
    dlg = MemoryDialog(m)
    n = m.count()
    dlg.new_edit.setText("主人每周五晚上打球")
    dlg.new_cat.setCurrentIndex(dlg.new_cat.findData("event"))
    dlg._on_add()
    assert m.count() == n + 1
    assert any("打球" in i.content and i.category == "event" for i in m.all())
    # 新增后输入框清空
    assert dlg.new_edit.text() == ""


def test_dialog_add_dedup():
    m = _store()
    dlg = MemoryDialog(m)
    n = m.count()
    dlg.new_edit.setText("主人不喜欢吃香菜")
    dlg._on_add()
    assert m.count() == n  # 完全相同内容不重复


def test_dialog_delete():
    m = _store()
    dlg = MemoryDialog(m)
    n = m.count()
    mid = m.all()[0].id
    dlg._on_delete(mid)
    assert m.count() == n - 1
    assert m.get(mid) is None


def test_dialog_search():
    m = _store()
    dlg = MemoryDialog(m)
    dlg.search_edit.setText("香菜")
    items = dlg._filtered_items()
    assert items and all("香菜" in i.content for i in items)
    dlg.search_edit.setText("不存在的关键字xyz")
    assert dlg._filtered_items() == []


def test_dialog_category_filter():
    m = _store()
    dlg = MemoryDialog(m)
    dlg.cat_combo.setCurrentIndex(dlg.cat_combo.findData("preference"))
    items = dlg._filtered_items()
    assert items and all(i.category == "preference" for i in items)
    # 「全部类别」时数量 = 全量
    dlg.cat_combo.setCurrentIndex(dlg.cat_combo.findData(""))
    assert len(dlg._filtered_items()) == m.count()


def test_changed_signal(qtbot=None):
    m = _store()
    dlg = MemoryDialog(m)
    fired = []
    dlg.changed.connect(lambda: fired.append(1))
    dlg.new_edit.setText("又一条新记忆")
    dlg._on_add()
    mid = m.all()[0].id
    dlg._on_delete(mid)
    assert len(fired) == 2  # 新增、删除各发一次


def test_chat_window_has_memory_button_and_panel():
    char_cfg = CharacterConfig(name="鲸鱼娘")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    m = _store()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites",
                    asr_enabled=False, memory_store=m)
    assert cw.memory_store is m
    assert hasattr(cw, "btn_memory")
    # 副标题记忆数直接来自 store
    assert f"{m.count()} 记忆" in cw._status_text()
    # 打开面板
    cw._open_memory()
    assert cw._memory_dlg is not None
    # 再次打开应复用同一实例
    first = cw._memory_dlg
    cw._open_memory()
    assert cw._memory_dlg is first


def test_chat_window_without_memory_store():
    char_cfg = CharacterConfig(name="鲸鱼娘")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites",
                    asr_enabled=False, memory_store=None)
    assert cw.memory_store is None
    # 无存储时不应崩溃（追加系统提示）
    cw._open_memory()
    assert cw._memory_dlg is None


def test_category_labels_cover_all():
    # 面板覆盖 MemoryStore 的全部 6 类
    from app.brain.memory import CATEGORY_CHOICES
    assert set(CATEGORY_LABELS.keys()) == set(CATEGORY_CHOICES)
