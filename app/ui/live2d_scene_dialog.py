# -*- coding: utf-8 -*-
"""Live2D「触发场景动作」配置弹窗。

两类编辑共用同一个外观选择表单：
  - SceneEditDialog：编辑固定场景（开心/思考/摸头/深夜…）的外观组合；
  - ActionEditDialog：新增/编辑用户自定义动作（自命名 + 可选绑定工具动作键）。

外观组合（SceneBundle）：表情单选、发型单选（含「默认发型」）、配件/手势/特殊
多选，全部留空 = 触发时不改变该项。固定场景可「恢复默认」（回退到模型 YAML
triggers 推导的默认值）；保存直接落盘（按模型一份 JSON）。
"""
from __future__ import annotations

import logging
from typing import Optional

from app.core.qt_compat import (
    QCheckBox, QComboBox, QFrame, QGraphicsDropShadowEffect, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QColor, QObject, QEvent,
    QPushButton, QScrollArea, QSpinBox, Qt, QToolButton, QVBoxLayout,
    QWidget, QDialog, Signal,
)
from app.animation.live2d_scene import (
    DEFAULT_TRANSIENT_HOLD_MS, TOOL_ACTION_HOOKS, SceneBundle, SceneStore,
)
from app.ui import ui_style

log = logging.getLogger(__name__)

_SCENE_QSS = f"""
QWidget {{
    font-family: {ui_style.FONT};
    font-size: 10pt;
    color: {ui_style.TEXT};
}}
QDialog {{ background: transparent; }}
QLineEdit, QSpinBox, QComboBox {{
    background: #ffffff;
    border: 1.5px solid #e3e1f0;
    border-radius: 10px;
    padding: 6px 10px;
    font-size: 10pt;
    color: {ui_style.TEXT};
    selection-background-color: #c4b5fd;
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:hover {{ border-color: {ui_style.ACCENT_LT}; }}
QComboBox QAbstractItemView {{
    background: #fff; border: 1px solid #e3e1f0;
    selection-background-color: {ui_style.ACCENT_BG};
    selection-color: {ui_style.ACCENT_DK}; outline: none;
}}
QGroupBox {{
    background: #fbfbfe;
    border: 1px solid #eceaf5;
    border-radius: 12px;
    margin-top: 14px;
    padding: 10px 10px 8px 10px;
    font-weight: 700;
    font-size: 9pt;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px; padding: 0 6px;
    color: {ui_style.ACCENT_DK};
}}
QCheckBox {{ spacing: 6px; font-size: 9pt; padding: 3px 2px; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px;
    border: 1.5px solid #c9c5e0; background: #fff; }}
QCheckBox::indicator:checked {{ background: {ui_style.ACCENT}; border-color: {ui_style.ACCENT}; }}
QPushButton {{
    background: #ffffff;
    border: 1.5px solid #e0e0ee;
    border-radius: 10px;
    padding: 7px 16px;
    font-weight: 600;
    font-size: 9pt;
}}
QPushButton:hover {{ border-color: {ui_style.ACCENT_LT}; color: {ui_style.ACCENT_DK}; background: {ui_style.ACCENT_BG}; }}
QPushButton#primary_btn {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #a78bfa, stop:1 #7c6cf0);
    color: #fff; border: none; padding: 7px 22px;
}}
QPushButton#primary_btn:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #9a7ef8, stop:1 #6a58e0); }}
QPushButton#danger_btn {{ color: {ui_style.DANGER}; border-color: #f3c6cf; }}
QPushButton#danger_btn:hover {{ background: #fdeef1; border-color: {ui_style.DANGER}; }}
QScrollBar:vertical {{ background: transparent; width: 6px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #d5d2e6; border-radius: 3px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""


class _DragFilter(QObject):
    """按住标题栏拖动整个无边框对话框。"""

    def __init__(self, win):
        super().__init__(win)
        self._win = win
        self._drag_pos = None

    def eventFilter(self, obj, evt):
        if evt.type() == QEvent.Type.MouseButtonPress:
            self._drag_pos = evt.globalPosition().toPoint() - self._win.frameGeometry().topLeft() \
                if hasattr(evt, "globalPosition") else evt.globalPos() - self._win.pos()
        elif evt.type() == QEvent.Type.MouseMove and self._drag_pos is not None:
            if hasattr(evt, "globalPosition"):
                self._win.move(evt.globalPosition().toPoint() - self._drag_pos)
            else:
                self._win.move(evt.globalPos() - self._drag_pos)
        elif evt.type() == QEvent.Type.MouseButtonRelease:
            self._drag_pos = None
        return False


class _BaseAppearanceDialog(QDialog):
    """外观选择弹窗公共骨架（标题栏 + 卡片 + 滚动 body + 底部按钮）。"""

    saved = Signal()

    def __init__(self, renderer, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.renderer = renderer
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(_SCENE_QSS)
        self.resize(460, 600)
        self.setMinimumWidth(440)
        # 外观选择控件句柄
        self._emotion_combo: Optional[QComboBox] = None
        self._hairstyle_combo: Optional[QComboBox] = None
        self._toggle_checks: dict[str, QCheckBox] = {}
        self._hold_spin: Optional[QSpinBox] = None
        self._groups = []
        try:
            self._groups = renderer.get_menu_groups()
        except Exception:  # noqa: BLE001
            self._groups = []

    def _build_shell(self, title: str, subtitle: str) -> tuple[QVBoxLayout, QVBoxLayout]:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 14)
        root.setSpacing(0)
        card = QFrame(self)
        card.setObjectName("window_card")
        card.setStyleSheet(ui_style.WINDOW_CARD_QSS)
        shadow = QGraphicsDropShadowEffect(card)
        shadow.setBlurRadius(44)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(90, 78, 200, 50))
        card.setGraphicsEffect(shadow)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        root.addWidget(card)

        header = QFrame(card)
        header.setObjectName("titlebar")
        header.setStyleSheet(ui_style.TITLEBAR_QSS)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 10, 10, 10)
        hl.setSpacing(8)
        tb = QVBoxLayout()
        tb.setSpacing(1)
        t = QLabel(title)
        t.setObjectName("titlebar_title")
        sub = QLabel(subtitle)
        sub.setObjectName("titlebar_subtitle")
        tb.addWidget(t)
        tb.addWidget(sub)
        hl.addLayout(tb)
        hl.addStretch(1)
        btn_close = QToolButton()
        btn_close.setObjectName("win_btn_close")
        btn_close.setText("✕")
        btn_close.clicked.connect(self.close)
        hl.addWidget(btn_close)
        cl.addWidget(header)
        self._drag = _DragFilter(self)
        header.installEventFilter(self._drag)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        host.setObjectName("scene_host")
        body = QVBoxLayout(host)
        body.setContentsMargins(16, 14, 16, 10)
        body.setSpacing(10)
        scroll.setWidget(host)
        cl.addWidget(scroll, 1)
        return cl, body

    def _build_appearance_form(self, body: QVBoxLayout, bundle: SceneBundle,
                               with_hold: bool, hold_default: int) -> None:
        """构建外观选择表单（表情/发型单选 + 三类多选 + 时长）。"""
        # 收集各组
        emotion_items: list[tuple[str, str]] = []
        hair_items: list[tuple[str, str]] = []
        toggle_groups: list[dict] = []
        for g in self._groups:
            kind = g.get("kind", "category")
            if kind == "emotion":
                emotion_items = list(g.get("items", []))
            elif kind == "hairstyle":
                hair_items = list(g.get("items", []))
            else:
                toggle_groups.append(g)

        # 表情（单选）
        self._emotion_combo = QComboBox()
        self._emotion_combo.addItem("不改变（保持当前）", "")
        self._emotion_combo.addItem("恢复自然（普通眼）", "natural")
        for iid, label in emotion_items:
            if iid == "__default__":
                continue
            self._emotion_combo.addItem(label, iid)
        row = QHBoxLayout()
        cap = QLabel("表情")
        cap.setFixedWidth(64)
        row.addWidget(cap)
        row.addWidget(self._emotion_combo, 1)
        body.addLayout(row)

        # 发型（单选）
        self._hairstyle_combo = QComboBox()
        self._hairstyle_combo.addItem("不改变（保持当前）", "")
        for iid, label in hair_items:
            self._hairstyle_combo.addItem(
                ("默认发型" if iid == "__default__" else label), iid)
        row = QHBoxLayout()
        cap = QLabel("发型")
        cap.setFixedWidth(64)
        row.addWidget(cap)
        row.addWidget(self._hairstyle_combo, 1)
        body.addLayout(row)

        # toggle 三类（多选）
        for g in toggle_groups:
            box = QGroupBox(g["label"])
            grid = QGridLayout(box)
            grid.setHorizontalSpacing(8)
            grid.setVerticalSpacing(4)
            for col in range(3):
                grid.setColumnStretch(col, 1)
            items = list(g.get("items", []))
            for i, (iid, label) in enumerate(items):
                cb = QCheckBox(label)
                cb.setProperty("item_id", iid)
                self._toggle_checks[iid] = cb
                grid.addWidget(cb, i // 3, i % 3)
            body.addWidget(box)

        if with_hold:
            self._hold_spin = QSpinBox()
            self._hold_spin.setRange(400, 15000)
            self._hold_spin.setSingleStep(200)
            self._hold_spin.setSuffix(" 毫秒")
            self._hold_spin.setValue(int(hold_default or DEFAULT_TRANSIENT_HOLD_MS))
            row = QHBoxLayout()
            cap = QLabel("持续时长")
            cap.setFixedWidth(64)
            row.addWidget(cap)
            row.addWidget(self._hold_spin)
            row.addStretch(1)
            body.addLayout(row)

        self._load_bundle(bundle)

    def _load_bundle(self, b: SceneBundle) -> None:
        if self._emotion_combo is not None:
            idx = self._emotion_combo.findData(b.emotion or "")
            self._emotion_combo.setCurrentIndex(max(0, idx))
        if self._hairstyle_combo is not None:
            idx = self._hairstyle_combo.findData(b.hairstyle or "")
            self._hairstyle_combo.setCurrentIndex(max(0, idx))
        for iid, cb in self._toggle_checks.items():
            cb.setChecked(iid in (b.toggles or []))

    def _current_bundle(self) -> SceneBundle:
        emotion = self._emotion_combo.currentData() if self._emotion_combo else ""
        hairstyle = self._hairstyle_combo.currentData() if self._hairstyle_combo else ""
        toggles = [iid for iid, cb in self._toggle_checks.items() if cb.isChecked()]
        hold = self._hold_spin.value() if self._hold_spin else 0
        return SceneBundle(emotion=emotion or "", hairstyle=hairstyle or "",
                           toggles=toggles, hold_ms=int(hold or 0))


class SceneEditDialog(_BaseAppearanceDialog):
    """编辑一个固定触发场景的外观组合。"""

    def __init__(self, renderer, scene_id: str, title: str, kind: str,
                 parent: Optional[QWidget] = None):
        super().__init__(renderer, parent)
        self.scene_id = scene_id
        self.kind = kind
        transient = kind == "transient"
        bundle = renderer.get_scene_bundle(scene_id)
        hold_default = bundle.hold_ms or DEFAULT_TRANSIENT_HOLD_MS
        cl, body = self._build_shell(
            f"配置场景 · {title}",
            "一次性动作播放后自动恢复；持续场景会替换当前外观" if transient
            else "触发时切换到该外观，留空项保持不变")
        self._build_appearance_form(body, bundle, with_hold=transient,
                                    hold_default=hold_default)

        # 底部：试穿 / 恢复默认 / 保存 / 取消
        bar = QHBoxLayout()
        bar.setSpacing(8)
        btn_preview = QPushButton("试穿预览")
        btn_preview.clicked.connect(self._on_preview)
        bar.addWidget(btn_preview)
        btn_reset = QPushButton("恢复默认")
        btn_reset.clicked.connect(self._on_reset)
        bar.addWidget(btn_reset)
        bar.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.close)
        bar.addWidget(btn_cancel)
        btn_save = QPushButton("保存")
        btn_save.setObjectName("primary_btn")
        btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save.clicked.connect(self._on_save)
        bar.addWidget(btn_save)
        bar.setContentsMargins(16, 8, 16, 12)
        cl.addLayout(bar)

    def _on_preview(self) -> None:
        try:
            self.renderer.preview_bundle(self._current_bundle())
        except Exception:  # noqa: BLE001
            log.exception("场景试穿失败")

    def _on_save(self) -> None:
        self.renderer.set_scene_bundle(self.scene_id, self._current_bundle())
        self.saved.emit()
        self.close()

    def _on_reset(self) -> None:
        self.renderer.reset_scene_bundle(self.scene_id)
        self._load_bundle(self.renderer.get_scene_bundle(self.scene_id))
        self.saved.emit()


class ActionEditDialog(_BaseAppearanceDialog):
    """新增 / 编辑一个用户自定义动作。"""

    def __init__(self, renderer, action: Optional[dict] = None,
                 parent: Optional[QWidget] = None):
        super().__init__(renderer, parent)
        self.action = action  # None=新建；dict=编辑
        editing = action is not None
        bundle = (SceneStore.action_bundle(action) if editing
                  else SceneBundle())
        hold = (action.get("hold_ms") if editing else None) or DEFAULT_TRANSIENT_HOLD_MS
        cl, body = self._build_shell(
            "编辑自定义动作" if editing else "新建自定义动作",
            "命名一个动作并搭配外观；可绑定工具动作，也可仅手动播放")

        # 名称
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("动作名称，例如：打招呼、戴眼镜")
        if editing:
            self.name_edit.setText(action.get("name", ""))
        row = QHBoxLayout()
        cap = QLabel("名称")
        cap.setFixedWidth(64)
        row.addWidget(cap)
        row.addWidget(self.name_edit, 1)
        body.addLayout(row)

        # 绑定工具动作
        self.hook_combo = QComboBox()
        self.hook_combo.addItem("仅手动播放（不绑定）", "")
        for hook, label in TOOL_ACTION_HOOKS:
            self.hook_combo.addItem(f"工具动作 · {label}", hook)
        if editing:
            idx = self.hook_combo.findData(action.get("hook", ""))
            self.hook_combo.setCurrentIndex(max(0, idx))
        row = QHBoxLayout()
        cap = QLabel("触发方式")
        cap.setFixedWidth(64)
        row.addWidget(cap)
        row.addWidget(self.hook_combo, 1)
        body.addLayout(row)

        self._build_appearance_form(body, bundle, with_hold=True,
                                    hold_default=hold)

        # 底部
        bar = QHBoxLayout()
        bar.setSpacing(8)
        btn_preview = QPushButton("试穿预览")
        btn_preview.clicked.connect(self._on_preview)
        bar.addWidget(btn_preview)
        if editing:
            btn_del = QPushButton("删除")
            btn_del.setObjectName("danger_btn")
            btn_del.clicked.connect(self._on_delete)
            bar.addWidget(btn_del)
        bar.addStretch(1)
        btn_cancel = QPushButton("取消")
        btn_cancel.clicked.connect(self.close)
        bar.addWidget(btn_cancel)
        btn_save = QPushButton("保存")
        btn_save.setObjectName("primary_btn")
        btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_save.clicked.connect(self._on_save)
        bar.addWidget(btn_save)
        bar.setContentsMargins(16, 8, 16, 12)
        cl.addLayout(bar)

    def _on_preview(self) -> None:
        try:
            self.renderer.preview_bundle(self._current_bundle())
        except Exception:  # noqa: BLE001
            log.exception("动作试穿失败")

    def _on_save(self) -> None:
        name = self.name_edit.text().strip() or "未命名动作"
        hook = self.hook_combo.currentData() or ""
        b = self._current_bundle()
        hold = b.hold_ms or DEFAULT_TRANSIENT_HOLD_MS
        if self.action is not None:
            self.renderer.update_custom_action(
                self.action["id"], name=name, hook=hook, bundle=b, hold_ms=hold)
        else:
            self.renderer.add_custom_action(name, hook, b, hold)
        self.saved.emit()
        self.close()

    def _on_delete(self) -> None:
        if self.action is not None:
            self.renderer.remove_custom_action(self.action["id"])
        self.saved.emit()
        self.close()
