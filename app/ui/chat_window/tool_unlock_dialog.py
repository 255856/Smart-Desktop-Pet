"""工具解锁对话框：显示 10 级战令 + 当前等级高亮 + 工具明细。"""
from __future__ import annotations

from typing import Callable, Optional

from app.core.qt_compat import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QVBoxLayout, QWidget, Qt,
)


def _risk_color(risk: str) -> str:
    """工具风险等级 → 颜色名。"""
    return {
        "SAFE": "#9ec97c",
        "LOW": "#7eb6e8",
        "MEDIUM": "#e8c47e",
        "HIGH": "#e89870",
        "IRREVERSIBLE": "#e07a7a",
    }.get(risk, "#888")


def build_tool_unlock_dialog(
    *,
    level: int,
    unlocked: set[str],
    level_to_tools: dict[int, list[tuple[str, str]]],
    rewards: dict[int, str],
    celebrate: dict[int, str],
    risk_lookup: Callable[[str], str],
    parent: Optional[QWidget] = None,
) -> QDialog:
    """构造工具解锁对话框。level=当前等级;unlocked=可见工具集合;level_to_tools={level: [(name, risk)]};rewards={level: title};celebrate={level: msg};risk_lookup(name)=risk 等级名。"""
    dlg = QDialog(parent)
    dlg.setWindowTitle("工具解锁")
    dlg.setMinimumSize(720, 600)
    dlg.setStyleSheet("""
        QDialog { background: #faf8ff; }
        QLabel#header_title { color: #4a3d6e; font-size: 16pt; font-weight: 700; }
        QLabel#header_subtitle { color: #7a6d9e; font-size: 9pt; }
        QLabel#progress_text { color: #5b4d8e; font-size: 10pt; font-weight: 600; }
        QFrame#level_card_unlocked { background: #f5f3e8; border-left: 4px solid #c8b95e; border-radius: 8px; }
        QFrame#level_card_current { background: #fff8e0; border-left: 4px solid #f5a623; border-radius: 8px; }
        QFrame#level_card_locked { background: #f4f4f8; border-left: 4px solid #c8c8d0; border-radius: 8px; }
        QLabel#level_label { font-size: 11pt; font-weight: 700; }
        QLabel#level_title_unlocked { color: #6b5b1e; font-size: 10pt; }
        QLabel#level_title_current { color: #b25e00; font-size: 10pt; font-weight: 700; }
        QLabel#level_title_locked { color: #6b6b7d; font-size: 10pt; }
        QLabel#tool_chip { color: #3a3a4a; font-size: 9pt; padding: 2px 8px; border-radius: 8px; background: #ffffff; }
        QPushButton { padding: 6px 18px; border-radius: 6px; }
    """)

    root = QVBoxLayout(dlg)
    root.setContentsMargins(20, 18, 20, 18)
    root.setSpacing(8)

    # 标题区
    title = QLabel("工具解锁")
    title.setObjectName("header_title")
    root.addWidget(title)
    subtitle = QLabel("每升 1 级解锁新工具。低等级多解锁常用工具，高等级解锁高级/危险工具。")
    subtitle.setObjectName("header_subtitle")
    subtitle.setWordWrap(True)
    root.addWidget(subtitle)

    # 进度区
    progress_text = QLabel(
        f"当前等级  Lv.{level} / 10    "
        f"已解锁 {len(unlocked)} / {sum(len(v) for v in level_to_tools.values())} 个工具"
    )
    progress_text.setObjectName("progress_text")
    root.addWidget(progress_text)
    next_lv = level + 1 if level < 10 else None
    if next_lv is not None:
        nxt_tools = level_to_tools.get(next_lv, [])
        next_hint = QLabel(
            f"下一级 Lv.{next_lv} 解锁 {len(nxt_tools)} 个工具"
            + (f"（{', '.join(n for n, _ in nxt_tools[:3])}...）" if nxt_tools else "")
        )
        next_hint.setStyleSheet("color: #7a6d9e; font-size: 9pt;")
        root.addWidget(next_hint)

    # 滚动卡片列表
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
    scroll_inner = QWidget()
    scroll_layout = QVBoxLayout(scroll_inner)
    scroll_layout.setContentsMargins(0, 6, 0, 6)
    scroll_layout.setSpacing(8)

    for lvl in sorted(level_to_tools.keys()):
        tools = level_to_tools.get(lvl, [])
        unlocked_now = lvl <= level
        is_current = lvl == level
        if is_current:
            card = QFrame(); card.setObjectName("level_card_current")
        elif unlocked_now:
            card = QFrame(); card.setObjectName("level_card_unlocked")
        else:
            card = QFrame(); card.setObjectName("level_card_locked")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 10, 14, 12)
        card_layout.setSpacing(6)

        # 标题行
        head_row = QHBoxLayout()
        head_row.setSpacing(10)
        prefix = "当前  " if is_current else ("已解锁" if unlocked_now else "未解锁")
        lbl = QLabel(f"Lv.{lvl}  {prefix}")
        lbl.setObjectName("level_label")
        if is_current:
            lbl.setStyleSheet("color: #b25e00;")
        elif unlocked_now:
            lbl.setStyleSheet("color: #6b5b1e;")
        else:
            lbl.setStyleSheet("color: #6b6b7d;")
        head_row.addWidget(lbl)

        title_text = rewards.get(lvl, "")
        if title_text:
            t_lbl = QLabel(title_text)
            if is_current:
                t_lbl.setObjectName("level_title_current")
            elif unlocked_now:
                t_lbl.setObjectName("level_title_unlocked")
            else:
                t_lbl.setObjectName("level_title_locked")
            head_row.addWidget(t_lbl, 1)

        # 工具数
        cnt_lbl = QLabel(f"{len(tools)} 工具")
        cnt_lbl.setStyleSheet("color: #888; font-size: 9pt;")
        head_row.addWidget(cnt_lbl)
        card_layout.addLayout(head_row)

        # 工具 chip 行（用 flow layout 简化：直接 hbox 自动换）
        if tools:
            chip_row = QHBoxLayout()
            chip_row.setSpacing(6)
            chip_row.setContentsMargins(0, 0, 0, 0)
            # 限制每行 5 个,超出换行
            row_widget = QWidget()
            row_layout = QVBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(4)
            cur_row = QHBoxLayout(); cur_row.setSpacing(6); cur_row.setContentsMargins(0,0,0,0)
            cnt_in_row = 0
            for nm, _risk in tools:
                if cnt_in_row >= 5:
                    row_layout.addLayout(cur_row)
                    cur_row = QHBoxLayout(); cur_row.setSpacing(6); cur_row.setContentsMargins(0,0,0,0)
                    cnt_in_row = 0
                chip = QLabel(nm)
                chip.setObjectName("tool_chip")
                risk = risk_lookup(nm)
                chip_color = _risk_color(risk)
                chip.setStyleSheet(
                    f"QLabel#tool_chip {{ color: {chip_color}; "
                    "font-size: 9pt; padding: 2px 8px; border-radius: 8px;"
                    f" background: #ffffff; border: 1px solid {chip_color}; }}"
                )
                cur_row.addWidget(chip)
                cnt_in_row += 1
            if cnt_in_row:
                row_layout.addLayout(cur_row)
            row_widget.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
            card_layout.addWidget(row_widget)

        # 祝贺语（仅已解锁等级显示）
        if unlocked_now:
            msg = celebrate.get(lvl, "")
            if msg:
                m_lbl = QLabel(msg)
                m_lbl.setStyleSheet("color: #5b4d8e; font-size: 9pt; font-style: italic;")
                m_lbl.setWordWrap(True)
                card_layout.addWidget(m_lbl)

        scroll_layout.addWidget(card)

    scroll_layout.addStretch(1)
    scroll.setWidget(scroll_inner)
    root.addWidget(scroll, 1)

    # 关闭按钮
    btn_row = QHBoxLayout()
    btn_row.addStretch(1)
    close_btn = QPushButton("关闭")
    close_btn.clicked.connect(dlg.accept)
    btn_row.addWidget(close_btn)
    root.addLayout(btn_row)

    return dlg