# -*- coding: utf-8 -*-
"""长期记忆管理面板单元测试：增删 / 去重 / 搜索 / 类别筛选 / 信号 / ChatWindow 接入。

后端是 **记忆 sidecar 的 HTTP 客户端**（进程内的老记忆库已经删掉了）：
这里用 tests/fake_sidecar.py 的假实现顶替网络，不要求真 sidecar。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".local-packages"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/fake_sidecar.py

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.qt_compat import QApplication  # noqa: E402
app = QApplication.instance() or QApplication(sys.argv)

from app.brain.memory_client import (  # noqa: E402
    CATEGORY_TO_KIND, MemoryClient, category_to_kind,
)
from app.core.config import CharacterConfig  # noqa: E402
from app.ui.chat_window import ChatWindow  # noqa: E402
from app.ui.memory_panel import MemoryDialog, CATEGORY_LABELS  # noqa: E402
from fake_sidecar import install  # noqa: E402


def _store():
    """一个装了假 sidecar 的客户端，预置 3 条记忆。"""
    client = MemoryClient(cooldown=0.0)
    fake = install(client)
    for text, cat, imp in (
        ("主人不喜欢吃香菜", "preference", 0.9),
        ("主人是大三学生", "fact", 0.5),
        ("主人的室友叫阿杰", "person", 0.6),
    ):
        client.add(text, kind=category_to_kind(cat), importance=imp, category=cat)
    # 更新时间拉开，保证「按时间倒序」可断言
    for n, card in enumerate(sorted(fake.cards.values(), key=lambda c: c["text"])):
        card["updatedAt"] = 1000 + n
    return client


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


def test_dialog_add_duplicate_keeps_count():
    m = _store()
    dlg = MemoryDialog(m)
    n = m.count()
    # 面板默认类别是「事实」，预置里的「主人是大三学生」也是 fact
    dlg.new_edit.setText("主人是大三学生")
    dlg._on_add()
    # 内容派生的卡片 id → 同一类别里同一条事实写两次是幂等的
    assert m.count() == n
    assert "已经记住" in dlg.new_edit.placeholderText()


def test_dialog_delete():
    m = _store()
    dlg = MemoryDialog(m)
    items = m.all()
    n = len(items)
    dlg._on_delete(items[0].id)
    assert len(m.all()) == n - 1


def test_dialog_search_filter():
    m = _store()
    dlg = MemoryDialog(m)
    dlg.search_edit.setText("香菜")
    filtered = dlg._filtered_items()
    assert len(filtered) == 1
    assert "香菜" in filtered[0].content


def test_dialog_category_filter():
    m = _store()
    dlg = MemoryDialog(m)
    dlg.cat_combo.setCurrentIndex(dlg.cat_combo.findData("person"))
    filtered = dlg._filtered_items()
    assert [i.content for i in filtered] == ["主人的室友叫阿杰"]


def test_dialog_changed_signal():
    m = _store()
    dlg = MemoryDialog(m)
    seen = []
    dlg.changed.connect(lambda: seen.append(1))
    dlg.new_edit.setText("主人喜欢在雨天散步")
    dlg._on_add()
    assert seen


def test_chat_window_uses_client():
    m = _store()
    char_cfg = CharacterConfig(name="鲸鱼娘")
    llm_cfg = type("L", (), {"model": "test", "api_key": "test"})()
    cw = ChatWindow(llm_cfg, char_cfg, "assets/sprites",
                    asr_enabled=False, memory_store=m)
    assert cw.memory_store is m
    assert hasattr(cw, "btn_memory")
    # 副标题记忆数直接来自客户端（走 /health）
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
    """面板的类别标签要覆盖客户端认的全部类别（否则有的卡筛不出来）。"""
    assert set(CATEGORY_LABELS.keys()) == set(CATEGORY_TO_KIND.keys())


def test_category_is_recovered_when_list_strips_it(monkeypatch):
    """真 sidecar 的 /list 只回固定字段（不含写卡时塞的 category）。

    面板要按「偏好 / 共同经历」这类老类别筛选，就得从 sidecar 的真值文件
    cards.json 里按 id 补回来 —— 这条钉住那个补齐逻辑。
    """
    client = MemoryClient(cooldown=0.0)
    fake = install(client)
    client.add("一起下棋你输了", kind=category_to_kind("shared"),
               importance=0.7, category="shared")
    cid = next(iter(fake.cards))
    fake.cards[cid].pop("category")            # 模拟 _public_card 的字段投影
    monkeypatch.setattr(client, "_cards_from_file",
                        lambda: [{"id": cid, "category": "shared"}])

    items = client.all()
    assert items and items[0].category == "shared"
    assert [v.content for v in client.recent_shared(3)] == ["一起下棋你输了"]

    # 不要求类别时不读文件（proactive 的 recent() 不该多一次 IO）
    called = []
    monkeypatch.setattr(client, "_cards_from_file",
                        lambda: called.append(1) or [])
    fake.cards[cid].pop("category", None)
    client.recent(1)
    assert called == []


def test_panel_lists_cards_from_sidecar():
    """面板的「全部」列表来自 sidecar（/list），不是本地记忆库。"""
    m = _store()
    fake = getattr(m._http, "__self__", None)
    items = m.all()
    assert len(items) == 3
    assert all(i.id and i.content for i in items)
    # 更新时间倒序
    assert items[0].updated_at >= items[-1].updated_at
    assert fake is not None, "必须真的问过假 sidecar 的 /list"
    assert fake.count("/list") >= 1
