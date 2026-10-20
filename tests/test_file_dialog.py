"""文件操作弹窗（拖文件进桌宠）UI 守卫。

主人要求: 美化 UI、不要表情包、去除 AI 味、完善功能。
本文件锁死这四点, 防止以后又退回 emoji + 渐变大按钮 + 让用户打字选格式。
"""
import re

import pytest

from app.core.qt_compat import QLabel, QPushButton

# 表情符号 / 装饰符号区间（U+1F300 起、U+2600 起的符号块等）
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F2FF]"
    "|[\u2190-\u21FF]"
    "|[\u2300-\u23FF]"
    "|[\u2B00-\u2BFF]"
    "|[\uFE0F]"
)


def _all_texts(dlg) -> list:
    """收集弹窗里所有可见文字（label + button）。"""
    out = []
    for w in dlg.findChildren(QLabel):
        out.append(w.text())
    for w in dlg.findChildren(QPushButton):
        out.append(w.text())
    return out


@pytest.fixture
def sample_file(tmp_path):
    f = tmp_path / "测试文件.txt"
    f.write_text("hello", encoding="utf-8")
    return f


def test_file_dialog_uses_shared_frameless_shell(qapp, sample_file):
    """走 FramelessDialog 外壳 —— 跟聊天窗/工具解锁窗是同一套外观。"""
    from app.ui.dialog_shell import FramelessDialog
    from app.ui.file_dialog import FileOperationDialog

    dlg = FileOperationDialog(sample_file)
    try:
        assert isinstance(dlg, FramelessDialog), (
            "文件弹窗没用统一外壳 —— 跟其他窗口长得不一样")
        # 标题栏是外壳给的, 标题就是文件名 (不再带 📄)
        assert dlg.title_label.text() == sample_file.name
    finally:
        dlg.close()
        dlg.deleteLater()


def test_file_dialog_has_no_emoji(qapp, sample_file):
    """界面上不许有表情包 / 装饰符号。"""
    from app.ui.file_dialog import FileOperationDialog

    dlg = FileOperationDialog(sample_file)
    try:
        for text in _all_texts(dlg):
            hit = _EMOJI_RE.search(text or "")
            assert hit is None, f"界面里还有表情符号: {text!r} ({hit.group(0)!r})"
    finally:
        dlg.close()
        dlg.deleteLater()


def test_file_dialog_lists_conversions_as_clickable_chips(qapp, sample_file):
    """可转换格式直接列成 chip 让人点 —— 不再让用户打字选格式。

    原来弹 QInputDialog.getText 让用户输入 "要转换为什么格式？"，
    打错字就静默转成第一个。
    """
    from app.ui.file_dialog import FileOperationDialog

    dlg = FileOperationDialog(sample_file)
    try:
        assert dlg._conversions, "txt 应该能转成 Markdown"
        chips = [w for w in dlg.findChildren(QPushButton)
                 if w.objectName() == "chip"]
        assert chips, "没找到格式 chip 按钮"
        # 点 chip = 直接转换, 不用再弹一次选择框
        dlg._on_convert_pick(dlg._conversions[0][1])
        assert dlg.result_action == "convert"
        assert dlg.convert_suffix == dlg._conversions[0][1]
    finally:
        dlg.close()
        dlg.deleteLater()


def test_file_dialog_eat_requires_confirmation(qapp, sample_file):
    """移到回收站是破坏性操作 —— 必须点两次, 第一次只切文案。"""
    from app.ui.file_dialog import FileOperationDialog

    dlg = FileOperationDialog(sample_file)
    try:
        dlg._on_eat()
        assert dlg.result_action == "", "第一次点就执行了 —— 没有二次确认"
        assert "确认" in dlg.btn_eat.text(), "第一次点应该把按钮改成确认态"
        dlg._on_eat()
        assert dlg.result_action == "eat"
    finally:
        dlg.close()
        dlg.deleteLater()


def test_file_dialog_shows_file_meta(qapp, sample_file):
    """文件信息卡: 大小 / 类型 / 修改时间, 路径过长要省略不撑破窗口。"""
    from app.ui.file_dialog import FileOperationDialog, _elide_middle

    dlg = FileOperationDialog(sample_file)
    try:
        meta = dlg._meta_text()
        assert "B" in meta or "KB" in meta, f"没显示大小: {meta}"
        assert "纯文本" in meta, f"没显示文件类型: {meta}"
        assert len(_elide_middle("C:/" + "x" * 200, 40)) <= 43
    finally:
        dlg.close()
        dlg.deleteLater()
