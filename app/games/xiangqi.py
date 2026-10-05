# -*- coding: utf-8 -*-
"""桌宠小游戏:中国象棋(Xiangqi)。

- 规则与判定全部由 cchess 库负责(cchess>=1.20,MIT 协议)。本模块只是把 cchess
  的 (x,y) 坐标(列、行号,红方在 y=0 黑方在 y=9)包成与项目其他模块一致的
  (r,c) 坐标(行、列号,黑方在 r=0 红方在 r=9),并保留游戏流程所需的 API。
- 走法生成 / 合法性 / 将军 / 将死 / 困毙 / 飞将 / 长捉 长将 全部不在本模块
  实现,避免手写规则带来 bug。
- AI(估值 + alpha-beta 搜索)仍在本模块,因为 cchess 没有自带 AI;其调用走
  cchess 的合法走法接口,既可靠又方便。

坐标约定:对外统一 (row, col),row 0 在上(黑方),col 0 在左。
"""
from __future__ import annotations

import random
from typing import List, Optional, Tuple

import cchess
from cchess.board import ChessBoard, RED as C_RED, BLACK as C_BLACK
FULL_INIT_FEN = cchess.FULL_INIT_FEN

# ---------------------------------------------------------------- 棋盘常量

COLS = 9
ROWS = 10
BOARD_SIZE = COLS * ROWS          # 90

EMPTY = 0

# 棋子编码:正数 = 红方,负数 = 黑方(取绝对值即类型)
# 与项目其他模块(包括五子棋窗、xiangqi_window.py)保持一致。
K, _A, B, N, R, C, P = 1, 2, 3, 4, 5, 6, 7     # 帅 仕 相 马 车 炮 兵
A = _A                                          # 老代码 / 测试用别名

RED, BLACK = 1, -1

TYPE_NAME = {
    K: "帅", A: "仕", B: "相", N: "马", R: "车", C: "炮", P: "兵",
}
BLACK_NAME = {K: "将", A: "士", B: "象", N: "马", R: "车", C: "炮", P: "卒"}

DIFFICULTIES = ("easy", "normal", "hard")

# 难度 → 搜索深度
DIFF_DEPTH = {"easy": 1, "normal": 3, "hard": 4}

# cchess FEN 字符 → 我方编码。cchess 大写表示红方、小写表示黑方。
_FENCH_TO_INT = {
    "K": K, "A": A, "B": B, "N": N, "R": R, "C": C, "P": P,
    "k": -K, "a": -A, "b": -B, "n": -N, "r": -R, "c": -C, "p": -P,
}
# 反向:我方编码 → cchess FEN 字符(供 setup_board 用)
_FENCH_TO_INT_INV = {v: k for k, v in _FENCH_TO_INT.items()}


# ---------------------------------------------------------------- 坐标转换

def _to_cchess(r: int, c: int) -> Tuple[int, int]:
    """项目 (row, col) → cchess (x, y)。

    cchess y=0 在底部(红方),项目 r=0 在顶部(黑方),所以要翻转行号。
    """
    return (c, ROWS - 1 - r)


def _from_cchess(x: int, y: int) -> Tuple[int, int]:
    """cchess (x, y) → 项目 (row, col)。"""
    return (ROWS - 1 - y, x)


# ---------------------------------------------------------------- 走法对象


class Move:
    """一步棋(保留被吃的子,供悔棋 / 排序使用)。"""

    __slots__ = ("fr", "fc", "tr", "tc", "piece", "captured")

    def __init__(self, fr: int, fc: int, tr: int, tc: int,
                 piece: int, captured: int) -> None:
        self.fr, self.fc, self.tr, self.tc = fr, fc, tr, tc
        self.piece = piece
        self.captured = captured

    def uci(self) -> str:
        """棋谱坐标,如 b2e2(列字母 a~i + 行号 0~9)。"""
        return f"{chr(ord('a') + self.fc)}{self.fr}{chr(ord('a') + self.tc)}{self.tr}"

    def text(self) -> str:
        return f"{chr(ord('a') + self.fc)}{self.fr + 1}→{chr(ord('a') + self.tc)}{self.tr + 1}"

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Move({name_of(self.piece)} {self.text()})"


# ---------------------------------------------------------------- 对局状态


class XiangqiGame:
    """一局中国象棋。

    内部用 cchess.board.ChessBoard 存棋盘;所有走法合法性、将军、将死、困毙
    判定都走 cchess。悔棋靠 FEN 快照栈(简单可靠)。
    """

    def __init__(self) -> None:
        self._board = ChessBoard(FULL_INIT_FEN)
        # FEN 快照栈:每次 play() 推入「走之前」的 FEN,以便 undo 回退
        self._fen_stack: List[str] = [self._board.to_fen()]
        self.history: List[Move] = []
        self.winner = 0             # 0 未结束 / 1 红胜 / -1 黑胜
        self.result = ""            # checkmate / stalemate_draw / resign / ''
        self.repetition = 0

    # ---------------------------------------------------------- 便捷查询
    def _read_board(self) -> List[int]:
        """把 cchess 棋盘读成 90 长度的扁平数组(项目编码)。"""
        out: List[int] = []
        for r in range(ROWS):
            for c in range(COLS):
                f = self._board.get_fench(_to_cchess(r, c))
                out.append(_FENCH_TO_INT.get(f, EMPTY) if f else EMPTY)
        return out

    @property
    def board(self) -> List[int]:
        """90 格扁平数组(项目编码)。读访问;不要直接修改返回值。"""
        return self._read_board()

    @property
    def to_move(self) -> int:
        return RED if self._board.move_player.color == C_RED else BLACK

    @to_move.setter
    def to_move(self, side: int) -> None:  # pragma: no cover - 测试摆残局用
        """强制设置待走方(主要用于测试摆残局后让对局继续)。"""
        if side not in (RED, BLACK):
            raise ValueError(f"side must be RED or BLACK, got {side}")
        self._board.set_move_color(C_RED if side == RED else C_BLACK)

    def find_king(self, side: int) -> int:
        """返回该方将 / 帅所在格下标(项目编码);找不到返回 -1。"""
        cchess_side = C_RED if side == RED else C_BLACK
        king = self._board.get_king(cchess_side)
        if king is None:
            return -1
        r, c = _from_cchess(king.x, king.y)
        return r * COLS + c

    def piece_at(self, r: int, c: int) -> int:
        if not (0 <= r < ROWS and 0 <= c < COLS):
            return EMPTY
        f = self._board.get_fench(_to_cchess(r, c))
        return _FENCH_TO_INT.get(f, EMPTY) if f else EMPTY

    def snapshot(self) -> List[int]:
        """返回棋盘快照(90 长度扁平数组)。"""
        return self._read_board()

    def setup_board(self, flat: List[int], to_move: int = RED) -> None:
        """从项目编码的 90 长扁平数组直接摆棋(测试 / UI 构造特定局面用)。

        项目编码:正数=红方,负数=黑方,绝对值即类型(K=1,A=2,B=3,N=4,
        R=5,C=6,P=7),0=空。会清掉原 history 并强制 to_move(不影响其他规则)。
        """
        if len(flat) != BOARD_SIZE:
            raise ValueError(f"flat board must have {BOARD_SIZE} cells")
        self._board = ChessBoard()
        self._board.clear()
        for r in range(ROWS):
            for c in range(COLS):
                v = flat[r * COLS + c]
                if v == EMPTY:
                    continue
                fench = _FENCH_TO_INT_INV.get(v)
                if fench is None:
                    raise ValueError(f"unknown piece code {v} at ({r},{c})")
                self._board.put_fench(fench, _to_cchess(r, c))
        c_side = C_RED if to_move == RED else C_BLACK
        self._board.set_move_color(c_side)
        self._fen_stack = [self._board.to_fen()]
        self.history = []
        self.winner = 0
        self.result = ""

    # ---------------------------------------------------------- 攻击判定
    def is_attacked(self, sq: int, by_side: int) -> bool:
        """sq 是否被 by_side 攻击(直接借 cchess 逐子 is_valid_move 判定)。

        备注:King 的 is_valid_move 内部会调 get_king(对手),若对方将不在宫里会
        返回 None 然后 AttributeError。这里手动处理 King:只算「相邻 1 格」
        (棋规如此);飞将由 detect_facing_general 单独算。
        """
        if by_side not in (RED, BLACK):
            return False
        cchess_side = C_RED if by_side == RED else C_BLACK
        target = _to_cchess(sq // COLS, sq % COLS)
        tx, ty = target
        for piece in self._board.get_pieces(cchess_side):
            if piece.species == "k":
                if abs(piece.x - tx) + abs(piece.y - ty) == 1:
                    return True
                continue
            if piece.is_valid_move(target):
                return True
        return False

    def _generals_face(self) -> bool:
        """飞将(白脸将):双方将帅同列无子。"""
        red = self._board.get_king(C_RED)
        black = self._board.get_king(C_BLACK)
        if red is None or black is None:
            return False
        if red.x != black.x:
            return False
        lo, hi = sorted((red.y, black.y))
        # 中间不能有任何子
        for y in range(lo + 1, hi):
            if self._board.get_fench((red.x, y)):
                return False
        return True

    def _would_fly_general(self, fr: int, fc: int, tr: int, tc: int) -> bool:
        """走完这一步后是否会产生飞将(若此前没有,这就是非法走子)。"""
        # 模拟走子:取出 red/black king 的位置,临时移动己方 king 到 (tc, tc)
        c_from = _to_cchess(fr, fc)
        c_to = _to_cchess(tr, tc)
        # 当前 cchess king 位置
        opp = C_BLACK if self.to_move == RED else C_RED
        my_c = C_RED if self.to_move == RED else C_BLACK
        my_king = self._board.get_king(my_c)
        opp_king = self._board.get_king(opp)
        if opp_king is None:
            return False
        # 走完后的 my king 位置
        new_my_x = c_to[0] if c_from == (my_king.x, my_king.y) else my_king.x
        new_my_y = c_to[1] if c_from == (my_king.x, my_king.y) else my_king.y
        # 同列
        if new_my_x != opp_king.x:
            return False
        # 中间无子
        lo, hi = sorted((new_my_y, opp_king.y))
        for y in range(lo + 1, hi):
            if self._board.get_fench((new_my_x, y)):
                # 排除被移动的 king 自己(在 lo 那行)
                if y == new_my_y:
                    continue
                return False
        return True

    def in_check(self, side: int) -> bool:
        """side 是否正被将军(含飞将)。"""
        if side not in (RED, BLACK):
            return False
        ksq = self.find_king(side)
        if ksq < 0:
            return True
        if self.is_attacked(ksq, -side):
            return True
        return self._generals_face()

    # ---------------------------------------------------------- 走法
    def legal_moves(self, side: Optional[int] = None) -> List[Move]:
        """side 的全部合法走法(已排除送将 / 飞将的棋)。

        不传 side 默认查当前 to_move 方。切换 side 临时改 cchess 的 move_player,
        走完恢复(避免污染 board 状态)。
        """
        target_side = self.to_move if side is None else side
        if target_side not in (RED, BLACK):
            return []
        if target_side != self.to_move:
            saved_fen = self._board.to_fen()
            self._board.set_move_color(
                C_RED if target_side == RED else C_BLACK)
            try:
                return self._collect_moves()
            finally:
                self._board.from_fen(saved_fen)
        return self._collect_moves()

    def _collect_moves(self) -> List[Move]:
        out: List[Move] = []
        # cchess.create_moves() 每次都生成全量(含送将、含飞将攻击),
        # 用 is_legal 过滤一遍 → 留下的就是项目意义上的合法走法。
        for move_tuple in self._board.create_moves():
            (cfx, cfy), (ctx, cty) = move_tuple
            fr, fc = _from_cchess(cfx, cfy)
            tr, tc = _from_cchess(ctx, cty)
            if self.is_legal(fr, fc, tr, tc):
                piece = self._fench_at(fr, fc)
                captured = self._fench_at(tr, tc)
                out.append(Move(fr, fc, tr, tc, piece, captured))
        return out

    def _fench_at(self, r: int, c: int) -> int:
        f = self._board.get_fench(_to_cchess(r, c))
        return _FENCH_TO_INT.get(f, EMPTY) if f else EMPTY

    def is_legal(self, fr: int, fc: int, tr: int, tc: int) -> bool:
        """走法合法(规则 + 不会送将)。

        关键:cchess 的 King 白脸将规则让它把"走一步到对方将所在列且中间无子"
        当作合法 King 攻击,这会污染 cchess.is_checked_move / is_checking 的判定
        (把同列无子的两个 King 视为互相攻击),导致很多显然合法的走子被误判为
        送将。我们完全绕过它:King 走法用手写规则,送将检测用我们自己的
        is_attacked + _generals_face。
        """
        if not (0 <= fr < ROWS and 0 <= fc < COLS
                and 0 <= tr < ROWS and 0 <= tc < COLS):
            return False
        if (fr, fc) == (tr, tc):
            return False
        piece = self.piece_at(fr, fc)
        if piece == EMPTY or side_of(piece) != self.to_move:
            return False
        target = self.piece_at(tr, tc)
        if target != EMPTY and side_of(target) == self.to_move:
            return False
        c_from = _to_cchess(fr, fc)
        c_to = _to_cchess(tr, tc)
        # King 走法:手写 1 步 + 在宫 + 落点不被攻击 + 不产生飞将
        if abs(piece) == K:
            if abs(c_to[0] - c_from[0]) + abs(c_to[1] - c_from[1]) != 1:
                return False
            if not in_palace(tr, tc, self.to_move):
                return False
            target_sq = tr * COLS + tc
            if self.is_attacked(target_sq, -self.to_move):
                return False
            if self._would_fly_general(fr, fc, tr, tc):
                return False
            return True
        # 非 King:规则走 cchess.is_valid_move,送将检测走我们自己
        if not self._board.is_valid_move(c_from, c_to):
            return False
        return not self._move_leaves_king_in_check(c_from, c_to)

    def _move_leaves_king_in_check(self, c_from: Tuple[int, int],
                                   c_to: Tuple[int, int]) -> bool:
        """模拟走子后,to_move 方的将是否被将(含飞将)。

        不走 cchess.is_checked_move — 它的 King 白脸将规则会让显然合法的走
        子被判为送将。

        快路径:直接修改 cchess 的内部 2D 数组,避免 from_fen / to_fen 的字符
        串解析(那是 AI 搜索的瓶颈)。cchess 的 piece 对象只在 get_pieces /
        get_king 时按需读 self._board,所以数组改完后再调用 is_attacked /
        _generals_face 就能反映「走完之后」的状态。
        """
        grid = self._board._board                     # cchess 2D 数组[y][x]
        src_piece = grid[c_from[1]][c_from[0]]
        dst_piece = grid[c_to[1]][c_to[0]]
        if src_piece is None:
            # 起点没子,理论上 is_legal 已经挡过 → 保守判为送将
            return True
        try:
            grid[c_to[1]][c_to[0]] = src_piece
            grid[c_from[1]][c_from[0]] = None
            king_sq = self.find_king(self.to_move)
            if king_sq < 0:
                return True
            if self.is_attacked(king_sq, -self.to_move):
                return True
            if self._generals_face():
                return True
            return False
        finally:
            grid[c_from[1]][c_from[0]] = src_piece
            grid[c_to[1]][c_to[0]] = dst_piece

    def play(self, fr: int, fc: int, tr: int, tc: int) -> bool:
        """走一步。非法返回 False;合法则落子并更新胜负。"""
        if self.winner != 0:
            return False
        if not self.is_legal(fr, fc, tr, tc):
            return False
        # 记录历史(走前快照查询,避免 cchess 应用后位置变了)
        piece = self.piece_at(fr, fc)
        captured = self.piece_at(tr, tc)
        # 推入「走之前」的 FEN,以便 undo 回退
        self._fen_stack.append(self._board.to_fen())
        # 应用走子(注意:cchess.move 不会自动切换 turn,需要手动 next_turn)
        ret = self._board.move(_to_cchess(fr, fc), _to_cchess(tr, tc))
        if ret is None:
            # 走不进去,弹出多余快照
            self._fen_stack.pop()
            return False
        self._board.next_turn()
        self.history.append(Move(fr, fc, tr, tc, piece, captured))
        self._update_result()
        return True

    def play_move(self, mv: Move) -> bool:
        return self.play(mv.fr, mv.fc, mv.tr, mv.tc)

    def undo(self) -> bool:
        """悔棋:撤销最近两步(红 + 黑),即整个回合。返回是否成功。"""
        if len(self._fen_stack) < 2 or self.winner != 0:
            return False
        # fen_stack[0] 是开局 FEN。走完 1 步后 fen_stack=[FEN0, FEN1]。
        # 想撤销整回合(红+黑)需要 ≥3 步:fen_stack=[FEN0, FEN1, FEN2, FEN3]。
        # 退到 FEN1(开局 + 红方走完 1 步),用 FEN1 覆盖当前,弹出 FEN3、FEN2。
        if len(self._fen_stack) < 3:
            return False
        prev_fen = self._fen_stack[-3]
        self._board.from_fen(prev_fen)
        for _ in range(2):
            if self.history:
                self.history.pop()
        self._fen_stack.pop()
        self._fen_stack.pop()
        self.winner = 0
        self.result = ""
        return True

    def undo_one(self) -> bool:
        """撤销最近一着(单步悔棋,不影响对手着法)。

        UI 想看「玩家走完一步之前 AI 是否被将」时用:undo_one → 读 AI 状态 →
        再 play_move 还原。
        """
        if len(self._fen_stack) < 2 or self.winner != 0:
            return False
        prev_fen = self._fen_stack[-2]
        self._board.from_fen(prev_fen)
        self._fen_stack.pop()
        if self.history:
            self.history.pop()
        self.winner = 0
        self.result = ""
        return True

    def last_move(self) -> Optional[Move]:
        return self.history[-1] if self.history else None

    def _update_result(self) -> None:
        """走完一步后,基于 cchess 判定胜负 / 平局。"""
        # 当前轮到 to_move。判断:to_move 是否无棋可走 + 是否被将。
        # cchess 的 no_moves() 检查的是当前 move_player 的所有子,无合法走子即 True。
        if not self._board.no_moves():
            return
        side = self.to_move
        if self.is_attacked(self.find_king(side), -side):
            # 被将军 + 无子可走 → 将死(to_move 输)
            self.winner = -side
            self.result = "checkmate"
        else:
            # 未被将 + 无子可走 → 困毙(本引擎按和棋)
            self.winner = 0
            self.result = "stalemate_draw"

    def resign(self) -> None:
        """认输。"""
        self.winner = -self.to_move
        self.result = "resign"


# ---------------------------------------------------------------- 工具函数(向后兼容)

def name_of(piece: int) -> str:
    """棋子显示名(红黑用不同字)。"""
    if piece == EMPTY:
        return ""
    t = abs(piece)
    return TYPE_NAME[t] if piece > 0 else BLACK_NAME[t]


def side_of(piece: int) -> int:
    return 0 if piece == EMPTY else (1 if piece > 0 else -1)


def on_board(r: int, c: int) -> bool:
    return 0 <= r < ROWS and 0 <= c < COLS


def in_palace(r: int, c: int, side: int) -> bool:
    """九宫:红方 row 7~9,黑方 row 0~2,col 3~5。"""
    if side > 0:
        return 7 <= r <= 9 and 3 <= c <= 5
    return 0 <= r <= 2 and 3 <= c <= 5


def own_half(r: int, side: int) -> bool:
    """己方半场(象不过河)。"""
    return r >= 5 if side > 0 else r <= 4


# 方向向量:正交 4 向(车/炮/将扫描用) + 对角 4 向(象/仕扫描用)
_ORTHO = ((-1, 0), (1, 0), (0, -1), (0, 1))
_DIAG = ((-1, -1), (-1, 1), (1, -1), (1, 1))


def initial_board() -> List[int]:
    b = [EMPTY] * BOARD_SIZE
    back = [R, N, B, A, K, A, B, N, R]
    for c, p in enumerate(back):
        b[0 * COLS + c] = -p
    b[2 * COLS + 1] = -C
    b[2 * COLS + 7] = -C
    for c in (0, 2, 4, 6, 8):
        b[3 * COLS + c] = -P

    for c, p in enumerate(back):
        b[9 * COLS + c] = p
    b[7 * COLS + 1] = C
    b[7 * COLS + 7] = C
    for c in (0, 2, 4, 6, 8):
        b[6 * COLS + c] = P
    return b


# ---------------------------------------------------------------- 评估与 AI

# 子力价值(千分)
VAL = {K: 10000, R: 900, N: 400, C: 450, A: 200, B: 200, P: 100}

# 位置价值表(只对兵)
_PAWN_TABLE = (
    (0,  3,  6,  9, 12,  9,  6,  3,  0),
    (18, 36, 56, 80, 120, 80, 56, 36, 18),
    (14, 26, 42, 60, 80, 60, 42, 26, 14),
    (10, 20, 30, 34, 40, 34, 30, 20, 10),
    (6, 12, 18, 18, 20, 18, 18, 12, 6),
    (2, 0, 8, 0, 8, 0, 8, 0, 2),
    (0, 0, -2, 0, 4, 0, -2, 0, 0),
    (0, 0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 0, 0, 0, 0),
    (0, 0, 0, 0, 0, 0, 0, 0, 0),
)


def evaluate(board: List[int], side: int) -> int:
    """从 side 视角的局面分(正 = side 占优)。"""
    score = 0
    for i, p in enumerate(board):
        if p == EMPTY:
            continue
        r, c = divmod(i, COLS)
        v = VAL[abs(p)]
        if abs(p) == P:
            v += _PAWN_TABLE[r if p > 0 else ROWS - 1 - r][c if p > 0 else COLS - 1 - c]
        elif abs(p) in (N, C):
            v += 8 if 3 <= c <= 5 else 0        # 偏好中路
        elif abs(p) == R:
            v += 12 if 3 <= c <= 5 else 0
        score += v if side_of(p) == side else -v
    return score


class XiangqiAI:
    """Alpha-Beta(negamax)搜索引擎。

    三档难度靠搜索深度区分。底层走法生成与合法性全部委托 cchess。
    """

    def __init__(self, difficulty: str = "normal", color: int = BLACK,
                 depth: Optional[int] = None) -> None:
        self.difficulty = difficulty if difficulty in DIFFICULTIES else "normal"
        self.color = color
        self._depth_override = depth
        self._rng = random.Random()
        self.nodes = 0

    @property
    def depth(self) -> int:
        if self._depth_override is not None:
            return max(1, int(self._depth_override))
        return DIFF_DEPTH[self.difficulty]

    # ---------------------------------------------------------- 内部
    def _ordered_moves(self, game: XiangqiGame, moves: List[Move]) -> List[Move]:
        """走法排序:吃子优先(MVV-LVA 简化),提升剪枝效率。"""
        return sorted(
            moves,
            key=lambda m: (VAL.get(abs(m.captured), 0) if m.captured else 0,
                           -abs(m.piece)),
            reverse=True,
        )

    def _search(self, game: XiangqiGame, depth: int, alpha: int, beta: int,
                side: int) -> int:
        self.nodes += 1
        if depth <= 0:
            return evaluate(game.board, side)
        moves = game.legal_moves(side)
        if not moves:
            return -100000
        best = -10 ** 9
        for mv in self._ordered_moves(game, moves):
            game.play_move(mv)
            score = -self._search(game, depth - 1, -beta, -alpha, -side)
            game.undo()
            if score > best:
                best = score
            if best > alpha:
                alpha = best
            if alpha >= beta:
                break
        return best

    # ---------------------------------------------------------- 对外
    def choose_move(self, game: XiangqiGame,
                    rng: Optional[random.Random] = None) -> Optional[Move]:
        """返回 AI 选择的一步;无棋可走返回 None。"""
        if game.winner != 0 or game.to_move != self.color:
            return None
        rng = rng or self._rng
        moves = game.legal_moves(self.color)
        if not moves:
            return None

        # 简单档:随机,避免新手被碾压
        if self.difficulty == "easy":
            return rng.choice(moves)

        self.nodes = 0
        best_score = -10 ** 9
        best_moves: List[Move] = []
        alpha = -10 ** 9
        for mv in self._ordered_moves(game, moves):
            game.play_move(mv)
            score = -self._search(game, self.depth - 1, -10 ** 9, -alpha,
                                 -self.color)
            game.undo()
            if score > best_score:
                best_score, best_moves = score, [mv]
            elif score == best_score:
                best_moves.append(mv)
            if score > alpha:
                alpha = score
        return rng.choice(best_moves) if best_moves else moves[0]


__all__ = [
    "COLS", "ROWS", "BOARD_SIZE", "EMPTY",
    "K", "A", "B", "N", "R", "C", "P",
    "RED", "BLACK", "DIFFICULTIES", "DIFF_DEPTH", "VAL",
    "Move", "XiangqiGame", "XiangqiAI",
    "name_of", "side_of", "on_board", "in_palace", "own_half",
    "initial_board", "evaluate",
    "_ORTHO", "_DIAG",                          # 供 UI 找攻击子用
]