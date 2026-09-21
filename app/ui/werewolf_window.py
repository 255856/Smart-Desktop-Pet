# -*- coding: utf-8 -*-
"""桌宠狼人杀窗口：标准 9 人局，桌宠扮演主持人上帝（唯一 TTS）。

- 紫色无边框圆角弹窗（与五子棋 / 设置 / 聊天同一套皮肤），标题栏可拖动。
- 上方 3×3 座位（名字 / 状态 / 玩家自己与已知身份）；中间发言流（主持人、
  玩家发言、夜晚频道、遗言）；底部为随阶段变化的玩家操作区。
- 后台由 WerewolfDirector（QThread + 多 Agent）驱动；窗口只负责显示与收集操作，
  金币与 TTS 通过 game_finished / host_spoke 信号交给控制器。
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

from app.core.qt_compat import (
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QLineEdit,
    QColor, QObject, QEvent, QPushButton, Qt, QToolButton, QVBoxLayout,
    QGridLayout, QWidget, QDialog, Signal, QScrollArea, QSizePolicy,
    QTimer,
)
from app.ui import ui_style
from app.games.werewolf import ROLE_LABEL
from app.games.werewolf_director import WerewolfDirector

log = logging.getLogger(__name__)

ROLE_EMOJI = {"wolf": "🐺", "seer": "🔮", "witch": "🧪", "hunter": "🏹",
              "villager": "👤"}

_WW_QSS = f"""
QWidget {{
    font-family: {ui_style.FONT};
    font-size: 10pt;
    color: {ui_style.TEXT};
}}
QDialog {{ background: transparent; }}
QPushButton {{
    background: #ffffff;
    border: 1.5px solid #e0e0ee;
    border-radius: 10px;
    padding: 6px 14px;
    font-weight: 600;
    font-size: 9pt;
}}
QPushButton:hover {{ border-color: {ui_style.ACCENT_LT}; color: {ui_style.ACCENT_DK}; background: {ui_style.ACCENT_BG}; }}
QPushButton:disabled {{ color: #b3b3c2; background: #f5f5fa; border-color: #e8e8f0; }}
QPushButton#primary_btn {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #a78bfa, stop:1 #7c6cf0);
    color: #fff; border: none; padding: 8px 24px;
}}
QPushButton#primary_btn:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #9a7ef8, stop:1 #6a58e0); }}
QPushButton#danger_btn {{ color: {ui_style.DANGER}; border-color: #f3c6cf; }}
QPushButton#danger_btn:hover {{ background: #fdeef1; border-color: {ui_style.DANGER}; }}
QLineEdit {{
    background: #fff; border: 1.5px solid #e0e0ee; border-radius: 10px;
    padding: 8px 12px; font-size: 10pt;
}}
QLineEdit:focus {{ border-color: {ui_style.ACCENT_LT}; }}
QFrame#seat {{
    background: #fbfbff; border: 1.5px solid #e6e4f5; border-radius: 10px;
}}
QFrame#seat_me {{
    background: {ui_style.ACCENT_BG}; border: 1.5px solid {ui_style.ACCENT_LT};
    border-radius: 10px;
}}
QFrame#seat_dead {{
    background: #f2f2f5; border: 1.5px solid #e4e4ea; border-radius: 10px;
}}
QFrame#seat_wolf {{
    background: #fff1f2; border: 1.5px solid #f3b6bd; border-radius: 10px;
}}
QScrollArea#feed {{ border: 1px solid #ececf3; background: #f7f6fc; border-radius: 10px; }}
QWidget#feed_host {{ background: #f7f6fc; }}
QWidget#msg_host {{ background: #f1ecff; border: 1.5px solid #d8ccfa; border-radius: 12px; }}
QWidget#msg_private {{ background: #fbf7ea; border: 1.2px dashed #e2cf8d; border-radius: 10px; }}
QWidget#msg_wolf {{ background: #fdecec; border: 1.5px solid #f1b4b4; border-radius: 10px; }}
QWidget#msg_plain {{ background: #f7f7fb; border: 1px solid #ececf3; border-radius: 10px; }}
QLabel#day_lbl {{ font-weight: 700; font-size: 11pt; color: {ui_style.ACCENT_DK}; }}
QLabel#role_lbl {{ font-weight: 800; font-size: 11pt; }}
QLabel#phase_lbl {{ color: {ui_style.TEXT_SUB}; font-size: 9pt; }}
"""


class _DragFilter(QObject):
    def __init__(self, win):
        super().__init__(win)
        self._win = win
        self._drag_pos = None

    def eventFilter(self, obj, evt):
        from app.core.qt_compat import event_global_pos
        if evt.type() == QEvent.Type.MouseButtonPress:
            self._drag_pos = event_global_pos(evt) - self._win.frameGeometry().topLeft()
        elif evt.type() == QEvent.Type.MouseMove and self._drag_pos is not None:
            self._win.move(event_global_pos(evt) - self._drag_pos)
        elif evt.type() == QEvent.Type.MouseButtonRelease:
            self._drag_pos = None
        return False


class WerewolfWindow(QDialog):
    """狼人杀弹窗。"""

    # 主持人台词（控制器负责气泡 + TTS）
    host_spoke = Signal(str)
    # 一局结束：result dict（winner / player_won / player_role / days）
    game_finished = Signal(dict)

    def __init__(self, parent: Optional[QWidget] = None,
                 llm_cfg=None, enable_llm: bool = False,
                 player_name: str = "你", pet_name: str = "桌宠"):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(_WW_QSS)
        self.llm_cfg = llm_cfg
        self.enable_llm = enable_llm
        self.player_name = player_name
        self.pet_name = pet_name

        self.director: Optional[WerewolfDirector] = None
        self._seats: Dict[int, QFrame] = {}
        self._seat_role: Dict[int, QLabel] = {}
        self._started = False

        self._build_ui()
        self._show_intro()

    # ------------------------------------------------------------ UI
    def _build_ui(self) -> None:
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
        cl.setContentsMargins(0, 0, 0, 12)
        cl.setSpacing(0)
        root.addWidget(card)

        # 标题栏
        header = QFrame(card)
        header.setObjectName("titlebar")
        header.setStyleSheet(ui_style.TITLEBAR_QSS)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 10, 10, 10)
        hl.setSpacing(8)
        tb = QVBoxLayout()
        tb.setSpacing(1)
        t = QLabel("🎭 狼人杀")
        t.setObjectName("titlebar_title")
        sub = QLabel(f"{self.pet_name} 主持 · 标准 9 人局 · 3狼/预言家/女巫/猎人/3平民")
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

        body = QVBoxLayout()
        body.setContentsMargins(16, 12, 16, 0)
        body.setSpacing(8)
        cl.addLayout(body)

        # 信息行：天数 / 你的身份 / 阶段
        info = QHBoxLayout()
        info.setSpacing(10)
        self.day_lbl = QLabel("准备开始")
        self.day_lbl.setObjectName("day_lbl")
        info.addWidget(self.day_lbl)
        info.addStretch(1)
        self.timer_lbl = QLabel("")
        self.timer_lbl.setStyleSheet(
            "font-weight: 700; font-size: 10pt; color: #d97706;")
        info.addWidget(self.timer_lbl)
        self.role_lbl = QLabel("你的身份：？")
        self.role_lbl.setObjectName("role_lbl")
        info.addWidget(self.role_lbl)
        body.addLayout(info)

        # 座位区（3×3）
        seats_box = QFrame()
        sg = QGridLayout(seats_box)
        sg.setContentsMargins(0, 0, 0, 0)
        sg.setSpacing(6)
        for seat in range(9):
            sf = self._make_seat(seat)
            r, c = divmod(seat, 3)
            sg.addWidget(sf, r, c)
        body.addWidget(seats_box)

        self.phase_lbl = QLabel("")
        self.phase_lbl.setObjectName("phase_lbl")
        self.phase_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        body.addWidget(self.phase_lbl)

        # 发言流（滚动）
        self.feed = QScrollArea()
        self.feed.setObjectName("feed")
        self.feed.setWidgetResizable(True)
        self.feed.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.feed.setFixedHeight(250)
        feed_host = QWidget()
        feed_host.setObjectName("feed_host")
        self.feed_layout = QVBoxLayout(feed_host)
        self.feed_layout.setContentsMargins(4, 4, 4, 4)
        self.feed_layout.setSpacing(6)
        self.feed_layout.addStretch(1)
        self.feed.setWidget(feed_host)
        self.feed.viewport().installEventFilter(self)
        self.feed.installEventFilter(self)
        body.addWidget(self.feed)

        # 操作区（固定高度，随阶段切换）
        self.action_box = QFrame()
        self.action_box.setFixedHeight(86)
        self.action_layout = QVBoxLayout(self.action_box)
        self.action_layout.setContentsMargins(2, 2, 2, 2)
        self.action_layout.setSpacing(6)
        body.addWidget(self.action_box)

        self.resize(560, 812)

    def _make_seat(self, seat: int) -> QFrame:
        f = QFrame()
        f.setObjectName("seat")
        v = QVBoxLayout(f)
        v.setContentsMargins(6, 5, 6, 5)
        v.setSpacing(0)
        top = QLabel(f"{seat}号")
        top.setStyleSheet("font-size: 8pt; color: #9a97b8;")
        name = QLabel("—")
        name.setStyleSheet("font-weight: 700; font-size: 10pt;")
        role = QLabel("")
        role.setStyleSheet("font-size: 8pt; color: #7c6cf0;")
        role.setAlignment(Qt.AlignmentFlag.AlignCenter)
        name.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.setAlignment(Qt.AlignmentFlag.AlignCenter)
        v.addWidget(top)
        v.addWidget(name)
        v.addWidget(role)
        self._seats[seat] = f
        self._seat_role[seat] = (name, role, top)
        return f

    # ------------------------------------------------------------ 消息渲染
    def _append_message(self, kind: str, who: str, text: str,
                        color: str = "") -> None:
        bar = self.feed.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 40
        box = QFrame()
        box.setObjectName({"host": "msg_host", "private": "msg_private",
                           "wolf": "msg_wolf"}.get(kind, "msg_plain"))
        h = QVBoxLayout(box)
        h.setContentsMargins(10, 6, 10, 6)
        h.setSpacing(1)
        head = QLabel(who)
        head.setStyleSheet(
            "font-weight: 700; font-size: 9pt; color: "
            + ("#7c6cf0" if kind == "host"
               else "#b8860b" if kind == "private"
               else "#c0392b" if kind == "wolf"
               else "#55507a") + ";")
        body = QLabel(text)
        body.setWordWrap(True)
        if kind == "private":
            body.setStyleSheet("font-size: 9pt; color: #8a7431; font-style: italic;")
        elif kind == "wolf":
            body.setStyleSheet("font-size: 10pt; color: #b03a3a;")
        else:
            body.setStyleSheet("font-size: 10pt; color: "
                               + (color or ui_style.TEXT) + ";")
        h.addWidget(head)
        h.addWidget(body)
        # 插到 stretch 之前
        self.feed_layout.insertWidget(self.feed_layout.count() - 1, box)
        if at_bottom:
            self._scroll_bottom()

    def _scroll_bottom(self) -> None:
        bar = self.feed.verticalScrollBar()
        bar.setValue(bar.maximum())

        def _do() -> None:
            # 新消息插入后布局高度在下一帧才更新，先刷新内部尺寸再滚到底
            self.feed.widget().adjustSize()
            self.feed.widget().updateGeometry()
            bar.setValue(bar.maximum())

        QTimer.singleShot(0, _do)

    def eventFilter(self, obj, evt):
        # 消息区滚轮滚动（在 viewport 与滚动区上都生效）
        if evt.type() == QEvent.Type.Wheel and obj in (
                self.feed, self.feed.viewport()):
            bar = self.feed.verticalScrollBar()
            delta = evt.angleDelta().y()
            if delta:
                steps = delta / 120.0
                bar.setValue(bar.value() - int(steps * 90))
                return True
        return super().eventFilter(obj, evt)

    # ------------------------------------------------------------ 开始 / 介绍
    def _show_intro(self) -> None:
        self._clear_action()
        self.action_box.setFixedHeight(86)
        self._append_message(
            "host", f"🎤 {self.pet_name}（上帝）",
            "欢迎来到狼人杀！天黑闭眼、白天发言投票，准备好了就点「开始游戏」吧～")
        hint = QLabel("标准 9 人局：3 狼人 · 预言家 · 女巫 · 猎人 · 3 平民。\n"
                      "你是其中一名玩家，其余 8 位由 AI 扮演，桌宠当上帝主持。")
        hint.setWordWrap(True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet(f"color: {ui_style.TEXT_SUB}; font-size: 9pt;")
        self.action_layout.addWidget(hint)
        row = QHBoxLayout()
        row.addStretch(1)
        btn = QPushButton("开始游戏")
        btn.setObjectName("primary_btn")
        btn.clicked.connect(self._start)
        row.addWidget(btn)
        row.addStretch(1)
        self.action_layout.addLayout(row)

    def _start(self) -> None:
        if self._started:
            return
        self._started = True
        self.director = WerewolfDirector(
            llm_cfg=self.llm_cfg, enable_llm=self.enable_llm,
            player_name=self.player_name, pet_name=self.pet_name, parent=self)
        d = self.director
        d.your_role.connect(self._on_your_role)
        d.host_message.connect(self._on_host)
        d.speech.connect(self._on_speech)
        d.wolf_chat.connect(self._on_wolf_chat)
        d.private_channel.connect(self._on_private)
        d.countdown.connect(self._on_countdown)
        d.npc_phase.connect(self._on_npc_phase)
        d.state_changed.connect(self._on_state)
        d.request_action.connect(self._on_request_action)
        d.game_over.connect(self._on_game_over)
        d.failed.connect(self._on_failed)
        d.start()
        self._set_wait("游戏开始…")

    # ------------------------------------------------------------ 导演信号
    def _on_your_role(self, role: str) -> None:
        emoji = ROLE_EMOJI.get(role, "")
        self.role_lbl.setText(f"你的身份：{emoji} {ROLE_LABEL.get(role, role)}")
        self.role_lbl.setStyleSheet(
            "font-weight: 800; font-size: 11pt; color: "
            + ("#d6455d" if role == "wolf" else "#7c6cf0") + ";")

    def _on_host(self, text: str) -> None:
        self._append_message("host", f"🎤 {self.pet_name}（上帝）", text)
        self.host_spoke.emit(text)

    def _on_speech(self, seat: int, name: str, text: str, kind: str) -> None:
        if kind == "last_words":
            self._append_message("plain", f"🕯️ {name}（{seat}号）遗言", text,
                                 color="#8a8a99")
        else:
            tag = "（你）" if seat == 0 else f"（{seat}号）"
            color = "#7c6cf0" if seat == 0 else ui_style.TEXT
            self._append_message("plain", f"💬 {name}{tag}", text, color=color)

    def _on_private(self, text: str) -> None:
        self._append_message("private", "🌙 夜晚（仅你可见）", text)

    def _on_wolf_chat(self, seat: int, name: str, text: str) -> None:
        tag = "（你）" if seat == 0 else f"（{seat}号）"
        self._append_message("wolf", f"🐺 {name}{tag} · 狼频道", text,
                             color="#b03a3a")

    @staticmethod
    def _fmt_clock(seconds: int) -> str:
        seconds = max(0, int(seconds))
        return f"{seconds // 60}:{seconds % 60:02d}"

    def _on_countdown(self, seconds: int) -> None:
        # 玩家操作倒计时（橙色）
        if seconds and seconds > 0:
            self.timer_lbl.setStyleSheet(
                "font-weight: 700; font-size: 10pt; color: #d97706;")
            self.timer_lbl.setText(f"⏱ {self._fmt_clock(seconds)}")
        else:
            self.timer_lbl.setText("")

    def _on_npc_phase(self, label: str, seconds: int) -> None:
        # 等待 NPC 集体行动：灰色倒计时，提示仍在进行而非卡死
        if label:
            self.phase_lbl.setText(label)
        self.timer_lbl.setStyleSheet(
            "font-weight: 700; font-size: 10pt; color: #8a8f99;")
        if seconds and seconds > 0:
            self.timer_lbl.setText(f"⏳ {self._fmt_clock(seconds)}")
        else:
            self.timer_lbl.setText("⏳ 请稍候…")

    def _on_state(self, view: dict) -> None:
        self._last_view = view
        self.day_lbl.setText(f"第 {view['day']} 天")
        # 座位
        seats = {s["seat"]: s for s in view["seats"]}
        for seat in range(9):
            s = seats[seat]
            frame = self._seats[seat]
            name_lbl, role_lbl, top_lbl = self._seat_role[seat]
            sheriff = view.get("sheriff")
            name_lbl.setText(("👑 " if seat == sheriff else "") + s["name"])
            obj = "seat"
            if not s["alive"]:
                obj = "seat_dead"
                top_lbl.setText(f"{seat}号 · 💀")
                name_lbl.setStyleSheet("font-weight: 700; font-size: 10pt; color: #a9a9b5;")
            else:
                top_lbl.setText(f"{seat}号")
                name_lbl.setStyleSheet("font-weight: 700; font-size: 10pt;")
            if seat == view["self_seat"]:
                obj = "seat_dead" if not s["alive"] else "seat_me"
            role_txt = ""
            if s["role"]:
                role_txt = f"{ROLE_EMOJI.get(s['role'], '')}{ROLE_LABEL[s['role']]}"
            role_lbl.setText(role_txt)
            frame.setObjectName(obj)
            frame.style().unpolish(frame)
            frame.style().polish(frame)

    def _on_request_action(self, kind: str, options, context: dict) -> None:
        input_kinds = {
            "speech": ("发表你的发言…", "轮到你发言"),
            "last_words": ("留一句遗言…", "请留一句遗言"),
            "campaign_speech": ("发表你的竞选演说…", "发表竞选演说"),
            "pk_speech": ("发表 PK 演说…", "发表 PK 演说"),
            "wolf_chat": ("在狼频道和队友讨论…", "🐺 狼频道（仅狼可见）"),
        }
        if kind in input_kinds:
            ph, default_hint = input_kinds[kind]
            self._set_input(context.get("hint", default_hint), placeholder=ph)
        elif kind == "witch":
            self._set_witch(options)
        elif kind == "run_sheriff":
            self._set_run_sheriff()
        elif kind == "transfer_badge":
            seats = [x for x in (options or []) if isinstance(x, int)]
            self._set_targets("👑 警徽移交给谁？", seats,
                              allow_skip=True, skip_text="撕掉警徽",
                              skip_payload="tear")
        else:
            labels = {
                "wolf_kill": "🌙 你是狼人，选择今晚击杀目标",
                "seer_check": "🔮 你是预言家，选择查验目标",
                "vote": "🗳️ 选择你的投票对象",
                "vote_sheriff": "👑 把警徽投给哪位参选者？",
                "hunter": "🏹 你是猎人，选择开枪带走的目标（可放弃）",
            }
            skip_text = {"vote": "弃票", "vote_sheriff": "弃票",
                         "hunter": "放弃开枪"}
            self._set_targets(labels.get(kind, "选择目标"), options or [],
                              allow_skip=(kind in skip_text),
                              skip_text=skip_text.get(kind, "跳过"))

    def _set_run_sheriff(self) -> None:
        self._clear_action()
        self.action_box.setFixedHeight(48)
        self.phase_lbl.setText("👑 是否竞选警长？")
        row = QHBoxLayout()
        b1 = QPushButton("🙋 举手竞选")
        b1.setObjectName("primary_btn")
        b1.clicked.connect(lambda: self._submit({"run": True}))
        b2 = QPushButton("不参选")
        b2.clicked.connect(lambda: self._submit({"run": False}))
        row.addStretch(1)
        row.addWidget(b1)
        row.addWidget(b2)
        row.addStretch(1)
        self.action_layout.addLayout(row)

    def _on_failed(self, err: str) -> None:
        self._append_message("host", "⚠️ 出错了", err)
        self._set_wait("对局中断，可关闭后重新开始。")

    def _on_game_over(self, result: dict) -> None:
        winner = result.get("winner")
        won = result.get("player_won")
        role = result.get("player_role", "")
        title = "🎉 你的阵营获胜！" if won else "😿 你的阵营失败了"
        camp = "狼人" if winner == "wolves" else "好人"
        text = (f"{title}\n你是{ROLE_LABEL.get(role, role)}，"
                f"本局共 {result.get('days', 0)} 天，{camp}阵营获胜。")
        self._append_message("host", "🏁 游戏结束", text)
        view = getattr(self, "_last_view", None)
        if view:
            parts = []
            for s in view["seats"]:
                r = s.get("role")
                if r:
                    parts.append(f"{s['seat']}号{s['name']}"
                                 f"（{ROLE_EMOJI.get(r, '')}{ROLE_LABEL.get(r, r)}）")
            if parts:
                self._append_message("host", "🔎 身份揭晓", "、".join(parts))
        self.game_finished.emit(result)
        self._clear_action()
        self.action_box.setFixedHeight(88)
        lbl = QLabel(title)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet("font-size: 13pt; font-weight: 800; color: "
                          + ("#16a34a" if won else "#d6455d") + ";")
        self.action_layout.addWidget(lbl)
        row = QHBoxLayout()
        btn_again = QPushButton("再来一局")
        btn_again.setObjectName("primary_btn")
        btn_again.clicked.connect(self._restart)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        row.addStretch(1)
        row.addWidget(btn_again)
        row.addWidget(btn_close)
        row.addStretch(1)
        self.action_layout.addLayout(row)

    # ------------------------------------------------------------ 操作区
    def _clear_action(self) -> None:
        while self.action_layout.count():
            item = self.action_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            else:
                sub = item.layout()
                if sub is not None:
                    self._clear_layout(sub)
        self.phase_lbl.setText("")

    def _clear_layout(self, layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())

    def _set_wait(self, text: str) -> None:
        self._clear_action()
        self.action_box.setFixedHeight(48)
        lbl = QLabel("⏳ " + text)
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(f"color: {ui_style.TEXT_SUB}; font-size: 10pt;")
        self.action_layout.addWidget(lbl)

    def _set_input(self, hint: str, placeholder: str = "") -> None:
        self._clear_action()
        self.action_box.setFixedHeight(52)
        self.phase_lbl.setText(hint)
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        row = QHBoxLayout()
        btn = QPushButton("发送")
        btn.setObjectName("primary_btn")

        def submit():
            text = edit.text().strip()
            if not text:
                return
            self._submit(text)
        btn.clicked.connect(submit)
        edit.returnPressed.connect(submit)
        row.addWidget(edit)
        row.addWidget(btn)
        self.action_layout.addLayout(row)
        edit.setFocus()

    def _set_targets(self, hint: str, candidates: List[int],
                     allow_skip: bool = False, skip_text: str = "弃票") -> None:
        self._clear_action()
        self.phase_lbl.setText(hint)
        # 高度随候选行数自适应（每行 4 个），避免按钮被裁掉
        rows = max(1, (len(candidates) + 3) // 4)
        self.action_box.setFixedHeight(rows * 34 + (38 if allow_skip else 0) + 8)
        grid = QGridLayout()
        grid.setSpacing(6)
        view = self.director.game.perspective(0) if self.director else None
        names = {s["seat"]: s["name"] for s in view["seats"]} if view else {}
        for i, seat in enumerate(candidates):
            b = QPushButton(f"{seat}号 {names.get(seat, '')}")
            b.clicked.connect(lambda _=False, s=seat: self._submit(s))
            r, c = divmod(i, 4)
            grid.addWidget(b, r, c)
        wrap = QHBoxLayout()
        wrap.addStretch(1)
        wrap.addLayout(grid)
        wrap.addStretch(1)
        self.action_layout.addLayout(wrap)
        if allow_skip:
            sk = QHBoxLayout()
            skip = QPushButton(skip_text)
            skip.setObjectName("danger_btn")
            skip.clicked.connect(lambda: self._submit(None))
            sk.addStretch(1)
            sk.addWidget(skip)
            sk.addStretch(1)
            self.action_layout.addLayout(sk)

    def _set_witch(self, opt: dict) -> None:
        self._clear_action()
        self.action_box.setFixedHeight(50)
        self.phase_lbl.setText("🧪 你是女巫（一夜最多用一瓶药）")
        row = QHBoxLayout()
        killed = opt.get("killed")
        if opt.get("can_save") and killed is not None:
            bsave = QPushButton(f"💊 用解药救 {killed}号")
            bsave.setObjectName("primary_btn")
            bsave.clicked.connect(
                lambda: self._submit({"action": "save"}))
            row.addWidget(bsave)
        if opt.get("can_poison"):
            bpoison = QPushButton("☠️ 使用毒药")
            bpoison.setObjectName("danger_btn")
            bpoison.clicked.connect(
                lambda: self._set_targets("选择毒药目标（不能毒自己）",
                                          opt.get("poison_candidates", [])))
            row.addWidget(bpoison)
        bnone = QPushButton("不用药")
        bnone.clicked.connect(lambda: self._submit({"action": "none"}))
        row.addWidget(bnone)
        self.action_layout.addLayout(row)

    def _submit(self, payload) -> None:
        if self.director is not None:
            self.director.submit_action(payload)
        self._set_wait("等待其他玩家行动…")

    # ------------------------------------------------------------ 重开 / 关闭
    def _restart(self) -> None:
        self._stop_director()
        # 清空发言流与座位
        while self.feed_layout.count() > 1:
            item = self.feed_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for seat in range(9):
            _, role_lbl, _ = self._seat_role[seat]
            role_lbl.setText("")
        self.role_lbl.setText("你的身份：？")
        self.role_lbl.setStyleSheet("font-weight: 800; font-size: 11pt;")
        self.day_lbl.setText("准备开始")
        self._started = False
        self._start()

    def _stop_director(self) -> None:
        d = self.director
        if d is None:
            return
        self.director = None
        try:
            d.request_stop()          # 取消 asyncio 任务（含进行中的网络请求）
            if d.isRunning():
                d.wait(5000)          # 取消后通常 1-2 秒结束
            if d.isRunning():
                # 极端情况仍未结束：交给 finished 信号延迟清理，
                # 绝不在线程运行时 deleteLater（会原生崩溃）
                d.finished.connect(
                    lambda: (d.setParent(None), d.deleteLater()))
            else:
                d.setParent(None)
                d.deleteLater()
        except Exception:
            log.exception("停止狼人杀导演失败")

    def closeEvent(self, event):  # noqa: N802
        self._stop_director()
        super().closeEvent(event)
