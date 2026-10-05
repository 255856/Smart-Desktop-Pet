# -*- coding: utf-8 -*-
"""中国象棋小游戏窗口：玩家执红（先手）vs 桌宠 AI（黑）。

- 无边框圆角紫色主题弹窗，与设置 / 聊天 / 五子棋同一套皮肤。
- 自绘 9×10 棋盘（楚河漢界 + 九宫斜线）；先点己方棋子再点目标格落子，
  走法合法性实时高亮（绿点 = 可走、红圈 = 可吃）；桌宠 AI 用 QTimer 延迟应招。
- 三档难度（切换即重开一局）；胜负通过 game_finished 信号交控制器结算金币、
  触发桌宠表情 / 气泡；窗口本身不直接改 PetState。
"""
from __future__ import annotations

import logging
import random
from typing import List, Optional, Tuple

from app.core.qt_compat import (
    QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QComboBox,
    QColor, QObject, QEvent, QPushButton, Qt, QToolButton, QVBoxLayout,
    QWidget, QDialog, Signal, QPainter, QPen, QBrush,
    QSize, QTimer,
)
from app.ui import ui_style
from app.games.xiangqi import (
    XiangqiGame, XiangqiAI, RED, BLACK, EMPTY,
    DIFF_DEPTH, COLS, ROWS, Move, name_of,
)

log = logging.getLogger(__name__)

# 难度 → 中文 / 金币奖励（赢 / 和 / 输；认输 0）
DIFF_LABEL = {"easy": "简单", "normal": "普通", "hard": "困难"}
REWARDS = {
    "easy":   {"win": 10, "draw": 3, "lose": 0},
    "normal": {"win": 20, "draw": 5, "lose": 1},
    "hard":   {"win": 40, "draw": 10, "lose": 2},
}

# 桌宠局势解说（被将军 / 困毙 / 落子闲谈 / 结算，由控制器朗读 TTS）
COMMENTS = {
    "player_check": ["将！低级危险！", "哎呀，这下被将军了……"],
    "ai_check": ["将军！", "哼哼，你没看见我的杀招吧？", "将军！小心应对哦～"],
    "player_thinking": ["嗯……让我想想～", "这步棋可不能随便走～"],
    "ai_thinking": ["我可得好好想想～", "嗯……有点难度～"],
    "idle": ["开局慢慢下，不着急～", "让我看看怎么走～"],
}
SETTLE_COMMENTS = {
    "win": ["啊！怎么会……你居然赢了我！", "诶？！输了输了，你好厉害！"],
    "lose": ["哼哼，承让承让～这局是我赢啦～", "哈哈，赢了赢了！再来一局？"],
    "draw": ["和棋啦，势均力敌～", "平局平局，不分胜负！"],
    "stalemate": ["哎，车轮战也下不出胜负～", "这局谁都没招，和了和了～"],
    "giveup": ["诶，别灰心呀，再来一局嘛～", "这局先到这里，要再来一局吗？"],
    "resign": ["我认输啦，别太得意哦～", "今天就让让你～"],
}

# 玩家走出威胁 / 送将 / 闲谈时，桌宠也能插嘴（不再只让 AI 走完后单边说话）
PLAYER_COMMENTS = {
    # 玩家刚把军/将气了的瞬间
    "player_check": ["哎，小心你的将！", "哦豁，要被将军了哦～"],
    # 玩家走闲棋 / 一般推进
    "player_idle": ["嗯，这步我看看～", "哦，你走这边～", "嗯嗯，思考中～",
                   "好棋好棋，让我看看怎么应～"],
    # 玩家送将（自己被将军）——这种一般是失误，桌宠嘴炮一下
    "player_self_check": ["哎呀，你这步自己被将了呢～", "噢，你这一送……我可不客气啦！"],
}

# 棋盘配色（与五子棋接近的木纹暖色，但更浅更通透）
BOARD_BG_0 = "#f6e7c5"
BOARD_BG_1 = "#e9d4a3"
BOARD_LINE = "#7a5424"
BOARD_STAR = "#5b3f1e"
RIVER_BG_0 = "#f3e1b9"
RIVER_BG_1 = "#f1d9a6"
PALACE_LINE = "#9c6f3a"
RED_PIECE_BG_0 = "#ffe5d6"
RED_PIECE_BG_1 = "#f6a08e"
RED_PIECE_RIM = "#d04a3a"
BLACK_PIECE_BG_0 = "#e2e2e8"
BLACK_PIECE_BG_1 = "#6a6a78"
BLACK_PIECE_RIM = "#1c1c24"
MARK_LAST = "#7c6cf0"        # 最近一步的方框标记
SEL_RED = "#e74c3c"          # 已选中的己方棋子高亮
MOVE_DOT = "#3aaa55"          # 可走落点（空位）
MOVE_CAPTURE = "#e74c3c"      # 可吃（对方子）
MOVE_RING_WIDTH = 2.5

_XQ_QSS = f"""
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
QLabel#turn_check {{ color: {ui_style.DANGER}; font-weight: 800; }}
QLabel#turn_thinking {{ color: {ui_style.ACCENT_DK}; }}
QLabel#result_win {{ color: #16a34a; font-size: 14pt; font-weight: 800; }}
QLabel#result_lose {{ color: {ui_style.DANGER}; font-size: 14pt; font-weight: 800; }}
QLabel#result_draw {{ color: #b45309; font-size: 14pt; font-weight: 800; }}
QLabel#reward_lbl {{ color: #b8860b; font-size: 10pt; font-weight: 700; }}
QLabel#history_lbl {{
    color: {ui_style.TEXT_SUB}; font-size: 9pt;
    background: #fbf7ee; border: 1px solid #ede3d0; border-radius: 8px;
    padding: 6px 9px;
}}
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


class XiangqiBoardWidget(QWidget):
    """9 列 × 10 行中国象棋自绘棋盘。"""

    clicked = Signal(int, int)  # 玩家点击 (row, col)

    def __init__(self, game: XiangqiGame, parent=None):
        super().__init__(parent)
        self.game = game
        # 格子边长与外缘留白（与五子棋接近但稍宽，让棋子有呼吸感）
        self.cell = 40
        self.margin_x = 28          # 左右多留些以放九宫外的车
        self.margin_y = 22            # 上下留出楚河汉字位置
        board_w = (COLS - 1) * self.cell + 2 * self.margin_x
        board_h = (ROWS - 1) * self.cell + 2 * self.margin_y
        self.setFixedSize(board_w, board_h)
        self.setMouseTracking(True)
        self.interactive = True
        self._hover: Optional[Tuple[int, int]] = None
        self._selected: Optional[Tuple[int, int]] = None
        # 缓存当前选中后能走到的落点（空位 = 显示绿点，对方子 = 显示红圈）
        self._legal_targets: List[Tuple[int, int]] = []
        # 被将军时，缓存将被的格子与攻击者位置（用于红框高亮）
        self._check_king_sq: Optional[int] = None
        self._check_attacker_sq: Optional[int] = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_check_highlight(self, king_sq: Optional[int],
                            attacker_sq: Optional[int]) -> None:
        """外部通知当前是否在被将军（用于红框高亮）。

        king_sq 为红/黑将的线性 index；任一为空则清除高亮。
        """
        self._check_king_sq = king_sq
        self._check_attacker_sq = attacker_sq
        self.update()

    def set_interactive(self, ok: bool) -> None:
        self.interactive = ok
        if not ok:
            self._hover = None
            self._selected = None
            self._legal_targets = []
        self.update()

    def clear_selection(self) -> None:
        self._selected = None
        self._legal_targets = []
        self.update()

    def _origin(self) -> Tuple[int, int]:
        return self.margin_x, self.margin_y

    def _pixel(self, r: int, c: int) -> Tuple[int, int]:
        ox, oy = self._origin()
        return ox + c * self.cell, oy + r * self.cell

    def _cell_at(self, x: int, y: int) -> Optional[Tuple[int, int]]:
        ox, oy = self._origin()
        if x < ox - self.cell // 2 or y < oy - self.cell // 2:
            return None
        c = int(round((x - ox) / float(self.cell)))
        r = int(round((y - oy) / float(self.cell)))
        if 0 <= r < ROWS and 0 <= c < COLS:
            return r, c
        return None

    # ------------------------------------------------------------------ 绘制
    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(_antialias(), True)

        # 棋盘木纹圆角背景
        from app.core.qt_compat import QLinearGradient
        grad = QLinearGradient(0, 0, self.width(), self.height())
        grad.setColorAt(0, QColor(BOARD_BG_0))
        grad.setColorAt(1, QColor(BOARD_BG_1))
        p.setPen(QPen(QColor("#d8b06f"), 1))
        p.setBrush(QBrush(grad))
        p.drawRoundedRect(0, 0, self.width() - 1, self.height() - 1, 12, 12)

        # 楚河漢界横带（row 4~5 之间的横向窄带）
        river_top = self._pixel(4, 0)[1] + self.cell // 2
        river_bot = self._pixel(5, 0)[1] - self.cell // 2
        if river_bot > river_top:
            p.setPen(QPen(QColor(RIVER_BG_0), 1))
            p.setBrush(QBrush(QColor(RIVER_BG_0)))
            # 横带只占内框宽度
            from app.core.qt_compat import QLinearGradient as _L
            river_grad = _L(0, river_top, 0, river_bot)
            river_grad.setColorAt(0, QColor(RIVER_BG_0))
            river_grad.setColorAt(1, QColor(RIVER_BG_1))
            p.setBrush(QBrush(river_grad))
            p.drawRect(self.margin_x - self.cell // 2 + 4, river_top,
                       (COLS - 1) * self.cell + self.cell - 8,
                       river_bot - river_top)

        # 网格（外框+内线）
        p.setPen(QPen(QColor(BOARD_LINE), 1.4))
        # 横线：row 0~4 与 5~9 各画一遍，中间是楚河
        for r in list(range(0, 5)) + list(range(5, ROWS)):
            x0, y0 = self._pixel(r, 0)
            x1, y1 = self._pixel(r, COLS - 1)
            p.drawLine(x0, y0, x1, y1)
        # 竖线：两端（row 0 / row 9）贯通
        x0, y0 = self._pixel(0, 0)
        x1, y1 = self._pixel(ROWS - 1, 0)
        p.drawLine(x0, y0, x1, y1)
        x0, y0 = self._pixel(0, COLS - 1)
        x1, y1 = self._pixel(ROWS - 1, COLS - 1)
        p.drawLine(x0, y0, x1, y1)
        # 内竖线：楚河断开（row 0~4 不连到 row 5~9）
        for c in range(1, COLS - 1):
            x_top, _ = self._pixel(0, c)
            _, y_top = self._pixel(4, c)
            p.drawLine(x_top, y_top, x_top, self._pixel(0, c)[1])  # 上半段
            _, y_bot = self._pixel(5, c)
            _, y_last = self._pixel(ROWS - 1, c)
            p.drawLine(x_top, y_bot, x_top, y_last)               # 下半段

        # 九宫斜线：黑方 (0~2, 3~5)，红方 (7~9, 3~5)
        p.setPen(QPen(QColor(PALACE_LINE), 1.2))
        for r0, r1 in ((0, 2), (7, 9)):
            xa, ya = self._pixel(r0, 3)
            xb, yb = self._pixel(r1, 5)
            p.drawLine(xa, ya, xb, yb)
            xa, ya = self._pixel(r0, 5)
            xb, yb = self._pixel(r1, 3)
            p.drawLine(xa, ya, xb, yb)

        # 兵 / 炮 位标记（小角标）：黑方 row 3 col 1,3,5,7,9(实际 c=0,2,4,6,8 不画,
        # 只在炮位与兵位画），红方 row 6 同列。这里画所有兵/炮位的角标更通用。
        p.setPen(QPen(QColor(BOARD_STAR), 1.2))
        marker_positions = [(3, c) for c in (1, 7)] + [(6, c) for c in (1, 7)] \
            + [(3, c) for c in (0, 2, 4, 6, 8)] + [(6, c) for c in (0, 2, 4, 6, 8)]
        # 简化：兵位/炮位用四角小 L 标
        for r, c in marker_positions:
            self._draw_corner_mark(p, r, c)

        # 楚河漢界文字（居中显示在河带里）
        if river_bot > river_top:
            mid_y = (river_top + river_bot) // 2
            from app.core.qt_compat import QFont
            f = QFont()
            f.setPointSize(13)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QPen(QColor("#a06b3a")))
            left_x = self.margin_x - self.cell // 2 + 4 + 18
            right_x = self.width() - (self.margin_x - self.cell // 2 + 4) - 18
            p.drawText(left_x - 70, mid_y - 16, 80, 32,
                       int(Qt.AlignmentFlag.AlignCenter), "楚 河")
            p.drawText(right_x - 10, mid_y - 16, 80, 32,
                       int(Qt.AlignmentFlag.AlignCenter), "漢 界")

        # 棋子
        for r in range(ROWS):
            for c in range(COLS):
                v = self.game.board[r * COLS + c]
                if v != EMPTY:
                    self._draw_piece(p, r, c, v)

        # 被将军时的红框高亮（在棋子之上、最后一步标记之下，避免覆盖棋子字）
        if self._check_king_sq is not None:
            self._draw_check_highlight(p)

        # 最后一步标记（最近一步的「方框角标」）
        last = self.game.last_move()
        if last is not None:
            self._draw_last_mark(p, last.tr, last.tc)

        # 已选中的己方棋子高亮（红方走时）
        if self._selected is not None and self.game.winner == 0 \
                and self.game.to_move == RED:
            sr, sc = self._selected
            self._draw_selected(p, sr, sc)
            # 合法落点标记
            for (tr, tc) in self._legal_targets:
                self._draw_target_mark(p, tr, tc)

        # 悬停预览（玩家回合，红方）
        if self.interactive and self.game.winner == 0 \
                and self.game.to_move == RED and self._hover is not None \
                and self._selected is None:
                # 仅当悬停在己方棋子上才显示半透明预览（不太必要，省略）
            pass

        p.end()

    def _draw_piece(self, p: QPainter, r: int, c: int, piece: int) -> None:
        x, y = self._pixel(r, c)
        rad = self.cell // 2 - 3
        # 阴影
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(QColor(0, 0, 0, 40)))
        p.drawEllipse(x - rad + 1, y - rad + 2, rad * 2, rad * 2)
        # 棋子主体（红/黑两种）
        from app.core.qt_compat import QRadialGradient
        grad = QRadialGradient(x - rad * 0.3, y - rad * 0.3, rad * 1.1)
        if piece > 0:           # 红
            grad.setColorAt(0, QColor(RED_PIECE_BG_0))
            grad.setColorAt(1, QColor(RED_PIECE_BG_1))
            rim = QColor(RED_PIECE_RIM)
        else:                   # 黑
            grad.setColorAt(0, QColor(BLACK_PIECE_BG_0))
            grad.setColorAt(1, QColor(BLACK_PIECE_BG_1))
            rim = QColor(BLACK_PIECE_RIM)
        p.setPen(QPen(rim, 1.4))
        p.setBrush(QBrush(grad))
        p.drawEllipse(x - rad, y - rad, rad * 2, rad * 2)
        # 文字
        from app.core.qt_compat import QFont
        f = QFont()
        f.setPointSize(12)
        f.setBold(True)
        p.setFont(f)
        text_color = QColor(RED_PIECE_RIM) if piece > 0 else QColor(BLACK_PIECE_RIM)
        p.setPen(QPen(text_color))
        p.drawText(x - rad, y - rad, rad * 2, rad * 2,
                   int(Qt.AlignmentFlag.AlignCenter), name_of(piece))

    def _draw_selected(self, p: QPainter, r: int, c: int) -> None:
        x, y = self._pixel(r, c)
        rad = self.cell // 2 - 1
        p.setPen(QPen(QColor(SEL_RED), 3))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(x - rad, y - rad, rad * 2, rad * 2)

    def _draw_target_mark(self, p: QPainter, r: int, c: int) -> None:
        x, y = self._pixel(r, c)
        # 对方子 = 红圈；空格 = 绿点
        piece = self.game.board[r * COLS + c]
        if piece == EMPTY:
            rad = 6
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(MOVE_DOT)))
            p.drawEllipse(x - rad, y - rad, rad * 2, rad * 2)
        else:
            rad = self.cell // 2
            p.setPen(QPen(QColor(MOVE_CAPTURE), MOVE_RING_WIDTH))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(x - rad + 2, y - rad + 2, (rad - 2) * 2, (rad - 2) * 2)

    def _draw_last_mark(self, p: QPainter, r: int, c: int) -> None:
        """最近一步的「方框角标」（与五子棋的圆环不同，更贴近象棋 UI 习惯）。"""
        x, y = self._pixel(r, c)
        rad = self.cell // 2 - 2
        L = 7
        p.setPen(QPen(QColor(MARK_LAST), 2.2))
        p.setBrush(Qt.BrushStyle.NoBrush)
        # 左上
        p.drawLine(x - rad, y - rad + L, x - rad, y - rad)
        p.drawLine(x - rad, y - rad, x - rad + L, y - rad)
        # 右上
        p.drawLine(x + rad, y - rad + L, x + rad, y - rad)
        p.drawLine(x + rad, y - rad, x + rad - L, y - rad)
        # 左下
        p.drawLine(x - rad, y + rad - L, x - rad, y + rad)
        p.drawLine(x - rad, y + rad, x - rad + L, y + rad)
        # 右下
        p.drawLine(x + rad, y + rad - L, x + rad, y + rad)
        p.drawLine(x + rad, y + rad, x + rad - L, y + rad)

    def _draw_check_highlight(self, p: QPainter) -> None:
        """被将军时的视觉提示：将帅格子外红框 + 攻击子格子内红点。

        仅在被将军时调用，确保玩家能直观看出「为什么是负」「下一步该怎么挡」。
        """
        king_sq = self._check_king_sq
        if king_sq is None:
            return
        kr, kc = divmod(king_sq, COLS)
        kx, ky = self._pixel(kr, kc)
        rad = self.cell // 2
        # 将帅格子外加粗红框（强调「这格被将」）
        p.setPen(QPen(QColor(MOVE_CAPTURE), 3.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(kx - rad + 2, ky - rad + 2, (rad - 2) * 2, (rad - 2) * 2)
        # 攻击子位置（若有）画一个红色实心圆点，便于指认「是哪个子在将」
        atk = self._check_attacker_sq
        if atk is not None and 0 <= atk < len(self.game.board) \
                and self.game.board[atk] != EMPTY:
            ar, ac = divmod(atk, COLS)
            ax, ay = self._pixel(ar, ac)
            p.setPen(QPen(QColor(MOVE_CAPTURE), 2))
            p.setBrush(QBrush(QColor(255, 80, 80)))
            rad2 = 5
            p.drawEllipse(ax - rad2, ay - rad2, rad2 * 2, rad2 * 2)

    def _draw_corner_mark(self, p: QPainter, r: int, c: int) -> None:
        """炮位/兵位的小角标（L 形短杠）。"""
        x, y = self._pixel(r, c)
        # 周边格坐标
        cands = ((r, c - 2), (r, c + 2), (r - 1, c - 1), (r - 1, c + 1),
                 (r + 1, c - 1), (r + 1, c + 1))
        # 该位置是否真的是炮位/兵位（row 3 or 6, col in 0,2,4,6,8 or 1,7）
        if r not in (3, 6):
            return
        if c not in (0, 1, 2, 4, 6, 7, 8):
            return
        # 内框边距（角标长度）
        L = 5
        gap = 3
        for dr, dc in ((-1, 0), (1, 0)):
            nr, nc = r + dr, c + dc
            if 0 <= nr < ROWS and 0 <= nc < COLS:
                nx, ny = self._pixel(nr, nc)
                # 上下小角
                p.drawLine(x - L, y + gap, x - gap, y + gap) if dr < 0 else \
                    p.drawLine(x - L, y - gap, x - gap, y - gap)
                p.drawLine(x + gap, y + gap, x + L, y + gap) if dr < 0 else \
                    p.drawLine(x + gap, y - gap, x + L, y - gap)
        # 实际上更标准的画法是只在上下左右某两个方向有边时才画，这里简化

    # ------------------------------------------------------------------ 交互
    def mousePressEvent(self, event):  # noqa: N802
        from app.core.qt_compat import event_local_pos
        if not self.interactive or self.game.winner != 0 \
                or self.game.to_move != RED:
            return
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event_local_pos(event)
        cell = self._cell_at(pos.x(), pos.y())
        if cell is None:
            return
        self.clicked.emit(cell[0], cell[1])

    def mouseMoveEvent(self, event):  # noqa: N802
        from app.core.qt_compat import event_local_pos
        pos = event_local_pos(event)
        cell = self._cell_at(pos.x(), pos.y())
        new_hover = cell
        if new_hover != self._hover:
            self._hover = new_hover
            self.update()

    def leaveEvent(self, event):  # noqa: N802
        self._hover = None
        self.update()


class XiangqiWindow(QDialog):
    """中国象棋弹窗。"""

    # 一局结束：result ∈ win / lose / draw / giveup / resign，第二个参数为难度
    game_finished = Signal(str, str)
    # 桌宠解说（控制器负责气泡 + TTS 朗读）
    comment = Signal(str)
    # 对局会话状态：True=进入一局（保持游戏动作），False=关闭退出（回默认）
    game_session_active = Signal(bool)

    def __init__(self, parent: Optional[QWidget] = None,
                 difficulty: str = "normal"):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(_XQ_QSS)
        self.difficulty = difficulty if difficulty in ("easy", "normal", "hard") else "normal"
        self.game = XiangqiGame()
        self.ai = XiangqiAI(self.difficulty, BLACK)
        self.rng = random.Random()
        self.over = False
        self._last_comment = ""
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
        t = QLabel("♟️ 中国象棋")
        t.setObjectName("titlebar_title")
        sub = QLabel("和桌宠对弈 · 你执红先手")
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
        for d in ("easy", "normal", "hard"):
            self.diff_combo.addItem(DIFF_LABEL[d], d)
        self.diff_combo.setCurrentIndex(("easy", "normal", "hard").index(self.difficulty))
        self.diff_combo.currentIndexChanged.connect(self._on_difficulty)
        info.addWidget(self.diff_combo)
        body.addLayout(info)

        # 回合状态（固定高度容器）
        turn_box = QWidget()
        turn_box.setFixedHeight(26)
        tb = QVBoxLayout(turn_box)
        tb.setContentsMargins(0, 0, 0, 0)
        tb.setSpacing(0)
        self.turn_lbl = QLabel("你的回合（红方先手）")
        self.turn_lbl.setObjectName("turn_lbl")
        self.turn_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tb.addWidget(self.turn_lbl)
        body.addWidget(turn_box)

        # 棋盘 + 右侧历史走法
        board_row = QHBoxLayout()
        board_row.setSpacing(10)
        self.board = XiangqiBoardWidget(self.game)
        self.board.clicked.connect(self._on_player_click)
        board_row.addStretch(1)
        board_row.addWidget(self.board)
        # 右侧走法记录（最近若干步）
        side = QVBoxLayout()
        side.setSpacing(4)
        side.setContentsMargins(0, 6, 0, 0)
        hist_title = QLabel("走法记录")
        hist_title.setObjectName("remain_lbl")
        hist_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        side.addWidget(hist_title)
        self.history_lbl = QLabel("")
        self.history_lbl.setObjectName("history_lbl")
        self.history_lbl.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignLeft)
        self.history_lbl.setWordWrap(True)
        self.history_lbl.setMinimumWidth(120)
        self.history_lbl.setMaximumWidth(150)
        self.history_lbl.setMinimumHeight(420)
        side.addWidget(self.history_lbl, 1)
        board_row.addLayout(side)
        body.addLayout(board_row)

        # 结算区（固定高度）
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
        self.btn_undo = QPushButton("悔棋")
        self.btn_undo.clicked.connect(self._undo)
        btns.addWidget(self.btn_undo)
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

        # AI 延迟落子定时器
        self._ai_timer = QTimer(self)
        self._ai_timer.setSingleShot(True)
        self._ai_timer.timeout.connect(self._ai_move)

        self.resize(620, 720)

    # ------------------------------------------------------------ 游戏流程
    def _new_game(self) -> None:
        self._ai_timer.stop()
        self.game_session_active.emit(True)
        self.game = XiangqiGame()
        self.ai = XiangqiAI(self.difficulty, BLACK)
        self.over = False
        self._last_comment = ""
        self.board.game = self.game
        self.board.set_interactive(True)
        self.board.clear_selection()
        self.board.update()
        self.diff_combo.setEnabled(True)
        self.result_lbl.hide()
        self.reward_lbl.hide()
        self.btn_giveup.setEnabled(True)
        self.btn_undo.setEnabled(True)
        self.turn_lbl.setObjectName("turn_lbl")
        self.turn_lbl.setStyleSheet("")
        self.turn_lbl.setText("你的回合（红方先手）")
        self.turn_lbl.show()
        self._update_history()

    def _on_difficulty(self, _idx: int) -> None:
        if not getattr(self, "_ready", True):
            return
        d = self.diff_combo.currentData()
        if d and d != self.difficulty:
            self.difficulty = d
            self._new_game()

    def _on_player_click(self, r: int, c: int) -> None:
        if self.over or self.game.winner != 0 or self.game.to_move != RED:
            return
        piece = self.game.piece_at(r, c)
        selected = self.board._selected
        # 情形 A：未选中 → 选己子
        if selected is None:
            if piece != EMPTY and piece > 0:        # 红方
                self.board._selected = (r, c)
                self.board._legal_targets = [
                    (m.tr, m.tc) for m in self.game.legal_moves(RED)
                    if m.fr == r and m.fc == c
                ]
                self.board.update()
            return
        # 情形 B：再次点击同一子 → 取消选择
        if selected == (r, c):
            self.board.clear_selection()
            return
        # 情形 C：点击己方另一子 → 切换选择
        if piece != EMPTY and piece > 0:
            self.board._selected = (r, c)
            self.board._legal_targets = [
                (m.tr, m.tc) for m in self.game.legal_moves(RED)
                if m.fr == r and m.fc == c
            ]
            self.board.update()
            return
        # 情形 D：尝试走子
        if (r, c) in self.board._legal_targets:
            self._play_move(selected[0], selected[1], r, c)
            self.board.clear_selection()
            return
        # 其他：空点击 → 取消选择
        self.board.clear_selection()

    def _play_move(self, fr: int, fc: int, tr: int, tc: int) -> None:
        ok = self.game.play(fr, fc, tr, tc)
        if not ok:
            self.board.clear_selection()
            return
        self.diff_combo.setEnabled(False)
        self.board.update()
        self._update_history()
        self._after_move()

    def _after_move(self) -> None:
        if self.game.result == "stalemate_draw":
            self._finish(force="stalemate")
            return
        if self.game.winner != 0:
            self._finish()
            return
        # 红方落子后：双向嘴炮——若走完当前棋路、对方被气到，桌宠点评一下；
        # 若这步自送将军，桌宠嘴炮一下；否则闲谈一句。
        self._comment_after_player()
        # 立即把"被将军"红框更新（如果 AI 当前就被将）
        self._update_check_highlight()
        self.board.set_interactive(False)
        self.turn_lbl.setObjectName("turn_lbl")
        self.turn_lbl.setStyleSheet("")
        self.turn_lbl.setText("桌宠思考中…")
        # AI 难度越大延迟越久（视觉上让玩家感知思考深度）
        delay = {"easy": 250, "normal": 420, "hard": 620}.get(self.difficulty, 420)
        self._ai_timer.start(delay)

    def _comment_after_player(self) -> None:
        """玩家走完一步 → 桌宠插嘴。基于局势（玩家送将 / AI 被气 / 闲棋）选话术。"""
        # AI 已被将军：嘴炮「小心你的将！」类（提醒玩家）
        if self.game.in_check(BLACK):
            self._say(self.rng.choice(PLAYER_COMMENTS["player_check"]))
            return
        # 玩家送将（自己这步让对方能直接攻将）：嘴炮一句
        last = self.game.last_move()
        if last is not None:
            # 撤销上一步看 AI 是否被将 → 若撤销后没将，说明这步送将了
            self.game.undo_one()
            ai_was_in_check_before = self.game.in_check(BLACK)
            self.game.play_move(last)
            self.game.to_move = BLACK     # undo_one 会翻转 to_move，需恢复
            if ai_was_in_check_before and not self.game.in_check(BLACK):
                self._say(self.rng.choice(PLAYER_COMMENTS["player_self_check"]))
                return
        # 闲棋闲谈
        if self.rng.random() < 0.45:
            self._say(self.rng.choice(PLAYER_COMMENTS["player_idle"]))

    def _update_check_highlight(self) -> None:
        """根据当前 to_move 方向决定是否高亮将帅格 / 攻击子。"""
        side_to_move = self.game.to_move
        # 当前轮到谁走，谁就可能是被将的受害方
        if self.game.winner != 0:
            self.board.set_check_highlight(None, None)
            return
        if not self.game.in_check(side_to_move):
            self.board.set_check_highlight(None, None)
            return
        king_sq = self.game.find_king(side_to_move)
        attacker_sq = self._find_attacker_square(king_sq, -side_to_move)
        self.board.set_check_highlight(king_sq, attacker_sq)

    def _find_attacker_square(self, king_sq: Optional[int],
                              by_side: int) -> Optional[int]:
        """找攻击将帅的那个敌子位置，用于红框 + 红点高亮。找不到返回 None。"""
        if king_sq is None or king_sq < 0:
            return None
        from app.games.xiangqi import _ORTHO as _ORTHO_, _DIAG as _DIAG_, \
            side_of as sf, R, N, A, B, P, K, on_board as ob, COLS as CL
        r, c = divmod(king_sq, CL)
        for dr, dc in _ORTHO_:
            nr, nc = r + dr, c + dc
            first = None
            while ob(nr, nc):
                p = self.game.board[nr * CL + nc]
                if p != 0:
                    if sf(p) == by_side and abs(p) in (R, K):
                        return nr * CL + nc
                    first = p
                    break
                nr, nc = nr + dr, nc + dc
            if first is not None and sf(first) == by_side and abs(first) == 6:   # C=6
                nr, nc = nr + dr, nc + dc
                while ob(nr, nc):
                    p = self.game.board[nr * CL + nc]
                    if p != 0:
                        if sf(p) == by_side:
                            return nr * CL + nc
                        break
                    nr, nc = nr + dr, nc + dc
        for dr, dc in _ORTHO_:
            lr, lc = r + dr, c + dc
            if not ob(lr, lc) or self.game.board[lr * CL + lc] != 0:
                continue
            if dr:
                cands = ((r + 2 * dr, c - 1), (r + 2 * dr, c + 1))
            else:
                cands = ((r - 1, c + 2 * dc), (r + 1, c + 2 * dc))
            for nr, nc in cands:
                if ob(nr, nc) and self.game.board[nr * CL + nc] == (N if by_side > 0 else -N):
                    return nr * CL + nc
        for dr, dc in _DIAG_:
            nr, nc = r + dr, c + dc
            if ob(nr, nc) and self.game.board[nr * CL + nc] == (A if by_side > 0 else -A):
                return nr * CL + nc
        for dr, dc in _DIAG_:
            tr, tc = r + 2 * dr, c + 2 * dc
            if ob(tr, tc) and self.game.board[tr * CL + tc] == (B if by_side > 0 else -B) \
                    and self.game.board[(r + dr) * CL + (c + dc)] == 0:
                return tr * CL + tc
        fwd = -1 if by_side > 0 else 1
        pr, pc = r - fwd, c
        if ob(pr, pc) and self.game.board[pr * CL + pc] == (P if by_side > 0 else -P):
            return pr * CL + pc
        for dc in (-1, 1):
            pr, pc = r - fwd, c + dc
            if ob(pr, pc) and self.game.board[pr * CL + pc] == (P if by_side > 0 else -P):
                if (by_side > 0 and pr <= 4) or (by_side < 0 and pr >= 5):
                    return pr * CL + pc
        return None

    def _ai_move(self) -> None:
        if self.over or self.game.winner != 0 or self.game.to_move != BLACK:
            return
        try:
            mv = self.ai.choose_move(self.game, self.rng)
        except Exception:  # noqa: BLE001
            log.exception("象棋 AI 落子失败")
            mv = None
        if mv is not None:
            self.game.play_move(mv)
        self.board.update()
        self._update_history()
        # 困毙：判和
        if self.game.result == "stalemate_draw":
            self._finish(force="stalemate")
            return
        if self.game.winner != 0:
            self._finish()
            return
        if mv is not None:
            self._comment_after_ai(mv)
        self.board.set_interactive(True)
        self.turn_lbl.setObjectName("turn_lbl")
        self.turn_lbl.setStyleSheet("")
        # 如果轮到红方且被将 → 红框高亮 + 标题提示
        self._update_check_highlight()
        if self.game.in_check(RED):
            self.turn_lbl.setObjectName("turn_check")
            self.turn_lbl.setText("将军！你的回合")
        else:
            self.turn_lbl.setText("你的回合（红方）")

    # ------------------------------------------------------------ 局势解说
    def _comment_after_ai(self, mv: Move) -> None:
        """AI 落子后选一句解说（被将军/闲谈）。"""
        in_check = self.game.in_check(RED)
        if in_check:
            self._say(self.rng.choice(COMMENTS["ai_check"]))
            return
        if len(self.game.history) <= 4 and self.rng.random() < 0.3:
            self._say(self.rng.choice(COMMENTS["ai_thinking"]))
        elif self.rng.random() < 0.12:
            self._say(self.rng.choice(COMMENTS["idle"]))

    def _say(self, text: str) -> None:
        if text and text != self._last_comment:
            self._last_comment = text
            self.comment.emit(text)

    # ------------------------------------------------------------ 操作按钮
    def _undo(self) -> None:
        """悔棋：撤销最近一着红 + 一着黑（必须轮到红方走）。"""
        if self.over or self.game.winner != 0 or self.game.to_move != RED:
            return
        ok = self.game.undo()
        if not ok:
            return
        self.board.update()
        self._update_history()
        self.turn_lbl.setObjectName("turn_lbl")
        self.turn_lbl.setStyleSheet("")
        self.turn_lbl.setText("你的回合（红方）")

    def _give_up(self) -> None:
        if self.over or self.game.winner != 0:
            return
        self._finish(force="giveup")

    def _finish(self, force: Optional[str] = None) -> None:
        self.over = True
        self._ai_timer.stop()
        self.board.set_interactive(False)
        self.board.clear_selection()
        self.btn_giveup.setEnabled(False)
        self.btn_undo.setEnabled(False)
        self.diff_combo.setEnabled(True)
        self.turn_lbl.hide()
        self.board.set_check_highlight(None, None)

        if force == "giveup":
            result, text, obj = "giveup", "你认输了，再来一局吧～", "result_lose"
        elif force == "stalemate":
            result, text, obj = "stalemate", "和棋，势均力敌～", "result_draw"
        elif self.game.winner == RED:
            result, text, obj = "win", "🎉 你赢了！", "result_win"
        elif self.game.winner == BLACK:
            result, text, obj = "lose", "桌宠赢啦～再接再厉", "result_lose"
        elif force == "resign":
            result, text, obj = "resign", "桌宠认输啦～", "result_win"
        else:
            result, text, obj = "draw", "和棋，势均力敌", "result_draw"
        self.result_lbl.setObjectName(obj)
        self.result_lbl.style().unpolish(self.result_lbl)
        self.result_lbl.style().polish(self.result_lbl)
        self.result_lbl.setText(text)
        self.result_lbl.show()
        self.reward_lbl.setText("")
        self.reward_lbl.hide()
        if result in SETTLE_COMMENTS:
            self.comment.emit(self.rng.choice(SETTLE_COMMENTS[result]))
        self.game_finished.emit(result, self.difficulty)

    # ------------------------------------------------------------ 控制器回调
    def set_wallet(self, money: float, remaining: float) -> None:
        self.money_lbl.setText(f"💰 {money:.0f}")
        self.remain_lbl.setText(f"今日可赚 {remaining:.0f}")

    def show_reward(self, amount: float, capped: bool) -> None:
        if amount > 0:
            self.reward_lbl.setText(f"金币 +{amount:.0f}")
        elif capped:
            self.reward_lbl.setText("今日小游戏奖励已达上限")
        else:
            self.reward_lbl.setText("本局没有金币奖励")
        self.reward_lbl.show()

    def _update_history(self) -> None:
        """右侧走法记录：每两步一组（红 → 黑），最近 12 组。"""
        hist = self.game.history
        if not hist:
            self.history_lbl.setText("")
            return
        lines = []
        for i in range(0, len(hist), 2):
            n = i // 2 + 1
            red_mv = hist[i]
            line = f"{n:>2}. {red_mv.text()}"
            if i + 1 < len(hist):
                black_mv = hist[i + 1]
                line += f"  {black_mv.text()}"
            lines.append(line)
        # 只显示最近 14 行
        if len(lines) > 14:
            lines = lines[-14:]
            lines.insert(0, "…（更早的略）")
        self.history_lbl.setText("\n".join(lines))

    def closeEvent(self, event):  # noqa: N802
        self._ai_timer.stop()
        self.game_session_active.emit(False)
        super().closeEvent(event)