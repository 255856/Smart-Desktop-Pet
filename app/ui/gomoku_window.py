# -*- coding: utf-8 -*-
"""五子棋小游戏窗口：玩家执黑（先手）vs 桌宠 AI（白棋）。

- 无边框圆角紫色主题弹窗，与设置 / 聊天窗同一套皮肤，可拖动标题栏。
- 自绘 15×15 木纹棋盘，鼠标落子；桌宠 AI 用 QTimer 延迟应招，避免界面卡顿。
- 三档难度（切换即重开一局）；胜负通过 game_finished 信号交控制器结算金币、
  触发桌宠表情/气泡；窗口本身不直接改 PetState。
"""
from __future__ import annotations

import logging
import random
from typing import Optional

from app.core.qt_compat import (
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QComboBox,
    QColor, QObject, QEvent, QPushButton, Qt, QToolButton, QVBoxLayout,
    QWidget, QDialog, Signal, QPainter, QPen, QBrush, QRadialGradient,
    QSize, QTimer,
)
from app.ui import ui_style
from app.games.gomoku import (
    GomokuGame, GomokuAI, BLACK, WHITE, EMPTY, SIZE, DIFFICULTIES,
    analyze_move,
)

log = logging.getLogger(__name__)

# 难度 → 中文 / 金币奖励（赢 / 和 / 输；认输 0）
DIFF_LABEL = {"easy": "简单", "normal": "普通", "hard": "困难"}
REWARDS = {
    "easy":   {"win": 10, "draw": 3, "lose": 0},
    "normal": {"win": 20, "draw": 5, "lose": 1},
    "hard":   {"win": 40, "draw": 10, "lose": 2},
}

# 桌宠局势解说（关键棋型 / 结算时触发，由控制器朗读 TTS）
COMMENTS = {
    # 玩家刚把棋走成对应威胁
    "player_live_four": ["不好！两头都堵不住了……", "完了完了，这要怎么防呀！"],
    "player_rush_four": ["好险！四连了，必须堵住！", "千钧一发，可不能让你连上！"],
    "player_live_three": ["哼，活三是吧，我这就堵上～", "想连成一片？没那么容易～",
                          "别以为我没看见你的活三哦～"],
    # AI 自己走成对应威胁
    "ai_live_four": ["哼哼，再一步我就赢咯～", "看好了，这是必杀的一手！", "嘿嘿，你挡不住啦～"],
    "ai_rush_four": ["哼哼，再一步怎么样？", "我可要冲了，小心哦～", "这一手，你打算怎么防？"],
    "ai_live_three": ["嘿嘿，我这边也有好棋了～", "我这一手也不错呢～"],
    # 开局偶尔闲聊
    "idle": ["嗯……让我想想下哪里～", "我可不会输给你哦～"],
}
SETTLE_COMMENTS = {
    "win": ["啊！怎么会……你居然赢了我！", "诶？！输了输了，你好厉害！"],
    "lose": ["哼哼，承让承让～这局是我赢啦～", "哈哈，赢了赢了！再来一局？"],
    "draw": ["棋盘下满啦，和棋，势均力敌～", "平局平局，不分胜负！"],
    "giveup": ["诶，别灰心呀，再来一局嘛～", "这局先到这里，要再来一局吗？"],
}

# 棋盘配色（木纹）
BOARD_BG_0 = "#f3d9a6"
BOARD_BG_1 = "#e9c585"
BOARD_LINE = "#9a6f3a"
BOARD_STAR = "#7a5424"
BLACK_STONE_0 = "#6b6b6b"
BLACK_STONE_1 = "#0c0c0c"
WHITE_STONE_0 = "#ffffff"
WHITE_STONE_1 = "#cfcfd9"
MARK = "#7c6cf0"

_GOMOKU_QSS = f"""
QWidget {{
    font-family: {ui_style.FONT};
    font-size: 10pt;
    color: {ui_style.TEXT};
}}
QDialog {{ background: transparent; }}
QComboBox {{
    background: #ffffff;
    border: 1.5px solid #e0e0ee;
    border-radius: 9px;
    padding: 4px 10px;
    font-size: 9pt;
    min-height: 20px;
}}
QComboBox:hover {{ border-color: {ui_style.ACCENT_LT}; }}
QComboBox QAbstractItemView {{
    background: #fff; border: 1px solid #e3e1f0;
    selection-background-color: {ui_style.ACCENT_BG};
    selection-color: {ui_style.ACCENT_DK}; outline: none;
}}
QPushButton {{
    background: #ffffff;
    border: 1.5px solid #e0e0ee;
    border-radius: 10px;
    padding: 7px 18px;
    font-weight: 600;
    font-size: 9pt;
}}
QPushButton:hover {{ border-color: {ui_style.ACCENT_LT}; color: {ui_style.ACCENT_DK}; background: {ui_style.ACCENT_BG}; }}
QPushButton:disabled {{ color: #b3b3c2; background: #f5f5fa; border-color: #e8e8f0; }}
QPushButton#primary_btn {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #a78bfa, stop:1 #7c6cf0);
    color: #fff; border: none; padding: 7px 22px;
}}
QPushButton#primary_btn:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #9a7ef8, stop:1 #6a58e0); }}
QPushButton#danger_btn {{ color: {ui_style.DANGER}; border-color: #f3c6cf; }}
QPushButton#danger_btn:hover {{ background: #fdeef1; border-color: {ui_style.DANGER}; }}
QPushButton#danger_btn:disabled {{ color: #c9b8bd; border-color: #efe4e7; background: #faf6f7; }}
QLabel#money_lbl {{ font-weight: 700; font-size: 11pt; color: #b8860b; }}
QLabel#remain_lbl {{ color: {ui_style.TEXT_SUB}; font-size: 9pt; }}
QLabel#turn_lbl {{ font-size: 10pt; font-weight: 600; color: {ui_style.ACCENT_DK}; }}
QLabel#result_win {{ color: #16a34a; font-size: 14pt; font-weight: 800; }}
QLabel#result_lose {{ color: {ui_style.DANGER}; font-size: 14pt; font-weight: 800; }}
QLabel#result_draw {{ color: #b45309; font-size: 14pt; font-weight: 800; }}
QLabel#reward_lbl {{ color: #b8860b; font-size: 10pt; font-weight: 700; }}
"""


def _antialias():
    """兼容 PyQt5 / PySide6 的抗锯齿枚举。"""
    return QPainter.RenderHint.Antialiasing if hasattr(QPainter, "RenderHint") \
        else QPainter.Antialiasing


class _DragFilter(QObject):
    """按住标题栏拖动整个无边框对话框。"""

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


class GomokuBoardWidget(QWidget):
    """15×15 自绘棋盘。"""

    clicked = Signal(int, int)

    def __init__(self, game: GomokuGame, parent=None):
        super().__init__(parent)
        self.game = game
        self.cell = 28
        self.margin = 22
        self.radius = self.cell // 2 - 3
        self.interactive = True
        self._hover: Optional[tuple[int, int]] = None
        board_px = (SIZE - 1) * self.cell + 2 * self.margin
        self.setFixedSize(board_px, board_px)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def _origin(self) -> tuple[int, int]:
        return self.margin, self.margin

    def set_interactive(self, ok: bool) -> None:
        self.interactive = ok
        if not ok:
            self._hover = None
        self.update()

    def _pixel(self, r: int, c: int) -> tuple[int, int]:
        ox, oy = self._origin()
        return ox + c * self.cell, oy + r * self.cell

    def _cell_at(self, x: int, y: int) -> tuple[int, int]:
        ox, oy = self._origin()
        c = int(round((x - ox) / float(self.cell)))
        r = int(round((y - oy) / float(self.cell)))
        return r, c

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(_antialias(), True)
        # 木纹圆角背景
        from app.core.qt_compat import QLinearGradient
        grad = QLinearGradient(0, 0, self.width(), self.height())
        grad.setColorAt(0, QColor(BOARD_BG_0))
        grad.setColorAt(1, QColor(BOARD_BG_1))
        p.setPen(QPen(QColor("#d8b06f"), 1))
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(0, 0, self.width() - 1, self.height() - 1, 12, 12)

        # 网格线
        p.setPen(QPen(QColor(BOARD_LINE), 1.4))
        for i in range(SIZE):
            x0, y0 = self._pixel(i, 0)
            x1, y1 = self._pixel(i, SIZE - 1)
            p.drawLine(x0, y0, x1, y1)          # 横线
            xa, ya = self._pixel(0, i)
            xb, yb = self._pixel(SIZE - 1, i)
            p.drawLine(xa, ya, xb, yb)          # 竖线

        # 星位（天元 + 四方位）
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(BOARD_STAR)))
        for sr, sc in ((3, 3), (3, 11), (7, 7), (11, 3), (11, 11)):
            x, y = self._pixel(sr, sc)
            p.drawEllipse(x - 4, y - 4, 8, 8)

        # 棋子
        for r in range(SIZE):
            for c in range(SIZE):
                v = self.game.board[r][c]
                if v != EMPTY:
                    self._draw_stone(p, r, c, v)

        # 最后一步标记
        last = self.game.last_move()
        if last is not None:
            x, y = self._pixel(*last)
            p.setPen(QPen(QColor(MARK), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(x - 5, y - 5, 10, 10)

        # 悬停预览（玩家回合、黑棋）
        if self.interactive and self._hover is not None \
                and self.game.to_move == BLACK and self.game.winner == 0:
            r, c = self._hover
            if self.game.is_legal(r, c):
                self._draw_stone(p, r, c, BLACK, alpha=110)
        p.end()

    def _draw_stone(self, p: QPainter, r: int, c: int, color: int,
                    alpha: int = 255) -> None:
        x, y = self._pixel(r, c)
        rad = self.radius
        cx, cy = x - rad * 0.35, y - rad * 0.35
        g = QRadialGradient(cx, cy, rad * 1.05)
        if color == BLACK:
            g.setColorAt(0, QColor(107, 107, 107, alpha))
            g.setColorAt(1, QColor(12, 12, 12, alpha))
            p.setPen(Qt.PenStyle.NoPen)
        else:
            g.setColorAt(0, QColor(255, 255, 255, alpha))
            g.setColorAt(1, QColor(207, 207, 217, alpha))
            pen = QPen(QColor(176, 176, 190, alpha), 1)
            p.setPen(pen)
        p.setBrush(QBrush(g))
        p.drawEllipse(x - rad, y - rad, rad * 2, rad * 2)

    def mousePressEvent(self, event):  # noqa: N802
        from app.core.qt_compat import event_local_pos
        if not self.interactive or self.game.winner != 0 \
                or self.game.to_move != BLACK:
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event_local_pos(event)
        r, c = self._cell_at(pos.x(), pos.y())
        if self.game.is_legal(r, c):
            self.clicked.emit(r, c)

    def mouseMoveEvent(self, event):  # noqa: N802
        from app.core.qt_compat import event_local_pos
        pos = event_local_pos(event)
        r, c = self._cell_at(pos.x(), pos.y())
        if self.game.in_board(r, c) and (r, c) != self._hover:
            self._hover = (r, c)
            self.update()

    def leaveEvent(self, event):  # noqa: N802
        self._hover = None
        self.update()


class GomokuWindow(QDialog):
    """五子棋弹窗。"""

    # 一局结束：result ∈ win / lose / draw / giveup，第二个参数为难度
    game_finished = Signal(str, str)
    # 桌宠局势解说（控制器负责气泡 + TTS 朗读）
    comment = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None,
                 difficulty: str = "normal"):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(_GOMOKU_QSS)
        self.difficulty = difficulty if difficulty in DIFFICULTIES else "normal"
        self.game = GomokuGame()
        self.ai = GomokuAI(self.difficulty, WHITE)
        self.rng = random.Random()
        self.over = False
        self._player_threat = "none"     # 玩家最近一步的威胁等级
        self._last_comment = ""          # 避免连续重复同一句
        self._ready = False
        self._build_ui()
        self._new_game()
        self._ready = True

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
        t = QLabel("🎮 五子棋")
        t.setObjectName("titlebar_title")
        sub = QLabel("和桌宠对弈 · 你执黑先手")
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

        # 信息行：金币 / 今日剩余 / 难度
        info = QHBoxLayout()
        info.setSpacing(10)
        self.money_lbl = QLabel("💰 0")
        self.money_lbl.setObjectName("money_lbl")
        info.addWidget(self.money_lbl)
        self.remain_lbl = QLabel("今日可赚 100")
        self.remain_lbl.setObjectName("remain_lbl")
        info.addWidget(self.remain_lbl)
        info.addStretch(1)
        info.addWidget(QLabel("难度"))
        self.diff_combo = QComboBox()
        for d in DIFFICULTIES:
            self.diff_combo.addItem(DIFF_LABEL[d], d)
        self.diff_combo.setCurrentIndex(DIFFICULTIES.index(self.difficulty))
        self.diff_combo.currentIndexChanged.connect(self._on_difficulty)
        info.addWidget(self.diff_combo)
        body.addLayout(info)

        # 回合状态（固定高度容器，避免对局/结算切换时棋盘被挤压）
        turn_box = QWidget()
        turn_box.setFixedHeight(26)
        tb = QVBoxLayout(turn_box)
        tb.setContentsMargins(0, 0, 0, 0)
        tb.setSpacing(0)
        self.turn_lbl = QLabel("你的回合（黑棋先手）")
        self.turn_lbl.setObjectName("turn_lbl")
        self.turn_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tb.addWidget(self.turn_lbl)
        body.addWidget(turn_box)

        # 棋盘
        self.board = GomokuBoardWidget(self.game)
        self.board.clicked.connect(self._on_player_move)
        board_wrap = QHBoxLayout()
        board_wrap.addStretch(1)
        board_wrap.addWidget(self.board)
        board_wrap.addStretch(1)
        body.addLayout(board_wrap)

        # 结算区（固定高度，对局中为空也占位，防止棋盘/按钮位移或重影）
        res_box = QWidget()
        res_box.setFixedHeight(50)
        rb = QVBoxLayout(res_box)
        rb.setContentsMargins(0, 2, 0, 0)
        rb.setSpacing(2)
        self.result_lbl = QLabel("")
        self.result_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.result_lbl.hide()
        rb.addWidget(self.result_lbl)
        self.reward_lbl = QLabel("")
        self.reward_lbl.setObjectName("reward_lbl")
        self.reward_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.reward_lbl.hide()
        rb.addWidget(self.reward_lbl)
        body.addWidget(res_box)

        # 底部按钮
        btns = QHBoxLayout()
        btns.setContentsMargins(0, 4, 0, 0)
        btns.setSpacing(10)
        self.btn_giveup = QPushButton("认输")
        self.btn_giveup.setObjectName("danger_btn")
        self.btn_giveup.clicked.connect(self._give_up)
        btns.addWidget(self.btn_giveup)
        btns.addStretch(1)
        self.btn_again = QPushButton("再来一局")
        self.btn_again.setObjectName("primary_btn")
        self.btn_again.clicked.connect(self._new_game)
        btns.addWidget(self.btn_again)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.close)
        btns.addWidget(self.btn_close)
        body.addLayout(btns)

        # AI 延迟落子定时器（成员化，便于重开/关窗时取消）
        self._ai_timer = QTimer(self)
        self._ai_timer.setSingleShot(True)
        self._ai_timer.timeout.connect(self._ai_move)

        self.resize(520, 720)

    # ------------------------------------------------------------ 游戏流程
    def _new_game(self) -> None:
        self._ai_timer.stop()
        self.game = GomokuGame()
        self.ai = GomokuAI(self.difficulty, WHITE)
        self.over = False
        self._player_threat = "none"
        self._last_comment = ""
        self.board.game = self.game
        self.board.set_interactive(True)
        self.board.update()
        self.diff_combo.setEnabled(True)
        self.result_lbl.hide()
        self.reward_lbl.hide()
        self.btn_giveup.setEnabled(True)
        self.turn_lbl.setObjectName("turn_lbl")
        self.turn_lbl.setStyleSheet("")
        self.turn_lbl.setText("你的回合（黑棋先手）")
        self.turn_lbl.show()

    def _on_difficulty(self, _idx: int) -> None:
        if not getattr(self, "_ready", True):
            return
        d = self.diff_combo.currentData()
        if d and d != self.difficulty:
            self.difficulty = d
            self._new_game()

    def _on_player_move(self, r: int, c: int) -> None:
        if self.over or self.game.to_move != BLACK or not self.game.is_legal(r, c):
            return
        self.game.play(r, c)
        self.diff_combo.setEnabled(False)   # 对局中锁定难度
        self.board.update()
        self._after_move()

    def _after_move(self) -> None:
        if self.game.winner != 0:
            self._finish()
            return
        last = self.game.last_move()
        if last is not None:
            self._player_threat = analyze_move(self.game, last[0], last[1], BLACK)
        self.board.set_interactive(False)
        self.turn_lbl.setText("桌宠思考中…")
        self._ai_timer.start(380)

    def _ai_move(self) -> None:
        if self.over or self.game.winner != 0 or self.game.to_move != WHITE:
            return
        try:
            mv = self.ai.choose_move(self.game, self.rng)
        except Exception:  # noqa: BLE001
            log.exception("五子棋 AI 落子失败")
            mv = None
        if mv is not None and self.game.is_legal(*mv):
            self.game.play(*mv)
        self.board.update()
        if self.game.winner != 0:
            self._finish()
            return
        if mv is not None:
            self._comment_after_ai(mv)
        self.board.set_interactive(True)
        self.turn_lbl.setText("你的回合（黑棋）")

    # ------------------------------------------------------------ 局势解说
    def _comment_after_ai(self, ai_pos: tuple[int, int]) -> None:
        """AI 落子后，依据双方刚刚形成的威胁选一句解说。"""
        ai_t = analyze_move(self.game, ai_pos[0], ai_pos[1], WHITE)
        p_t = self._player_threat
        key = None
        if ai_t == "live_four":
            key = "ai_live_four"
        elif ai_t == "rush_four":
            key = "ai_rush_four"
        elif ai_t == "live_three" and self.rng.random() < 0.5:
            key = "ai_live_three"
        elif p_t == "live_four":
            key = "player_live_four"
        elif p_t == "rush_four":
            key = "player_rush_four"
        elif p_t == "live_three":
            key = "player_live_three"
        elif len(self.game.history) <= 4 and self.rng.random() < 0.25:
            key = "idle"
        if key:
            self._say(self.rng.choice(COMMENTS[key]))

    def _say(self, text: str) -> None:
        if text and text != self._last_comment:
            self._last_comment = text
            self.comment.emit(text)

    def _give_up(self) -> None:
        if self.over or self.game.winner != 0:
            return
        self._finish(force="giveup")

    def _finish(self, force: Optional[str] = None) -> None:
        self.over = True
        self._ai_timer.stop()
        self.board.set_interactive(False)
        self.btn_giveup.setEnabled(False)
        self.diff_combo.setEnabled(True)
        self.turn_lbl.hide()

        if force == "giveup":
            result, text, obj = "giveup", "你认输了，再来一局吧～", "result_lose"
        elif self.game.winner == BLACK:
            result, text, obj = "win", "🎉 你赢了！", "result_win"
        elif self.game.winner == WHITE:
            result, text, obj = "lose", "桌宠赢啦～再接再厉", "result_lose"
        else:
            result, text, obj = "draw", "和棋，势均力敌", "result_draw"
        self.result_lbl.setObjectName(obj)
        # 强制刷新 objectName 对应的 QSS
        self.result_lbl.style().unpolish(self.result_lbl)
        self.result_lbl.style().polish(self.result_lbl)
        self.result_lbl.setText(text)
        self.result_lbl.show()
        self.reward_lbl.setText("")
        self.reward_lbl.hide()
        # 结算台词（TTS 朗读）；随后才发结算信号给控制器发金币
        if result in SETTLE_COMMENTS:
            self.comment.emit(self.rng.choice(SETTLE_COMMENTS[result]))
        self.game_finished.emit(result, self.difficulty)

    # ------------------------------------------------------------ 控制器回调
    def set_wallet(self, money: float, remaining: float) -> None:
        """更新顶部金币与今日可赚额度。"""
        self.money_lbl.setText(f"💰 {money:.0f}")
        self.remain_lbl.setText(f"今日可赚 {remaining:.0f}")

    def show_reward(self, amount: float, capped: bool) -> None:
        """结算后展示获得的金币（控制器在 add_game_reward 后调用）。"""
        if amount > 0:
            self.reward_lbl.setText(f"金币 +{amount:.0f}")
        elif capped:
            self.reward_lbl.setText("今日小游戏奖励已达上限")
        else:
            self.reward_lbl.setText("本局没有金币奖励")
        self.reward_lbl.show()

    def closeEvent(self, event):  # noqa: N802
        self._ai_timer.stop()
        super().closeEvent(event)
