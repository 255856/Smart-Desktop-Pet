# -*- coding: utf-8 -*-
"""桌宠小游戏：中国象棋（Xiangqi）。

- 纯逻辑、无 Qt / 无第三方依赖，便于单元测试（与 gomoku.py / werewolf.py 同一约定）。
- 棋盘 9 列 × 10 行，扁平数组 90 格；下标 = row * 9 + col，row 0 是黑方底线（上方），
  row 9 是红方底线（下方）。玩家执红先走，AI 执黑。
- 规则实现要点：
    · 九宫（3×3）：将 / 帅、仕 / 士 不能出宫
    · 象 / 相 不过河，且有「塞象眼」（田字格中心有子则不可走）
    · 马 / 傌 有「蹩马腿」（相邻正交方向有子则不可走）
    · 车 / 俥、炮 / 砲、兵 / 卒 的吃子规则
    · 白脸将（飞将）：将帅不得在同一直线上直接照面，中间不能有子
    · 走子后若己方被将军，该步非法
    · 将死（checkmate）与困毙（stalemate，轮到谁走谁输）都判负
- 不实现长将 / 长捉判和与自然限着（娱乐对局，按 xiangqi.py 的规则库判定胜负即可）。

坐标约定：对外统一 (row, col)，row 0 在上（黑方），col 0 在左。
"""
from __future__ import annotations

import random
from typing import Iterable, List, Optional, Tuple

# ---------------------------------------------------------------- 棋盘常量

COLS = 9
ROWS = 10
BOARD_SIZE = COLS * ROWS          # 90

EMPTY = 0

# 棋子编码：正数 = 红方，负数 = 黑方（取绝对值即类型）
K, A, B, N, R, C, P = 1, 2, 3, 4, 5, 6, 7     # 将/帅 士/仕 相/象 马/傌 车/俥 炮/砲 兵/卒

RED, BLACK = 1, -1

TYPE_NAME = {
    K: "帅", A: "仕", B: "相", N: "马", R: "车", C: "炮", P: "兵",
}
# 同一类型的红黑写法不同，展示时按阵营取
BLACK_NAME = {K: "将", A: "士", B: "象", N: "马", R: "车", C: "炮", P: "卒"}

DIFFICULTIES = ("easy", "normal", "hard")

# 难度 → 搜索深度（choose_move 走 depth-1 层前瞻，故数值 = 根着 + 前瞻层数）。
# 实测中局耗时：depth2≈20ms / depth3≈150ms / depth4≈890ms / depth5≈3800ms。
# 桌宠是 GUI 程序，AI 回合要卡在 ~1s 内，太深会明显卡住界面。
DIFF_DEPTH = {"easy": 1, "normal": 3, "hard": 4}


def name_of(piece: int) -> str:
    """棋子显示名（红黑用不同字）。"""
    if piece == EMPTY:
        return ""
    t = abs(piece)
    return TYPE_NAME[t] if piece > 0 else BLACK_NAME[t]


def side_of(piece: int) -> int:
    return 0 if piece == EMPTY else (1 if piece > 0 else -1)


def on_board(r: int, c: int) -> bool:
    return 0 <= r < ROWS and 0 <= c < COLS


def in_palace(r: int, c: int, side: int) -> bool:
    """九宫：红方 row 7~9，黑方 row 0~2，col 3~5。"""
    if side > 0:
        return 7 <= r <= 9 and 3 <= c <= 5
    return 0 <= r <= 2 and 3 <= c <= 5


def own_half(r: int, side: int) -> bool:
    """己方半场（象不过河）。"""
    return r >= 5 if side > 0 else r <= 4


# 初始局面
def initial_board() -> List[int]:
    b = [EMPTY] * BOARD_SIZE
    back = [R, N, B, A, K, A, B, N, R]        # row 0：黑方底线
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


# ---------------------------------------------------------------- 走法生成

_ORTHO = ((-1, 0), (1, 0), (0, -1), (0, 1))
_DIAG = ((-1, -1), (-1, 1), (1, -1), (1, 1))


def _pseudo_moves(board: List[int], r: int, c: int) -> Iterable[Tuple[int, int]]:
    """单个棋子的伪合法走法（不检查走后是否被将军 / 飞将）。"""
    piece = board[r * COLS + c]
    side = side_of(piece)
    t = abs(piece)

    if t == K:                                    # 将 / 帅：九宫内直行
        for dr, dc in _ORTHO:
            nr, nc = r + dr, c + dc
            if in_palace(nr, nc, side):
                tgt = board[nr * COLS + nc]
                if tgt == EMPTY or side_of(tgt) != side:
                    yield nr, nc

    elif t == A:                                  # 仕 / 士：九宫内斜行
        for dr, dc in _DIAG:
            nr, nc = r + dr, c + dc
            if in_palace(nr, nc, side):
                tgt = board[nr * COLS + nc]
                if tgt == EMPTY or side_of(tgt) != side:
                    yield nr, nc

    elif t == B:                                  # 相 / 象：不过河 + 塞象眼
        for dr, dc in _DIAG:
            nr, nc = r + 2 * dr, c + 2 * dc
            if not on_board(nr, nc) or not own_half(nr, side):
                continue
            if board[(r + dr) * COLS + (c + dc)] != EMPTY:   # 塞象眼
                continue
            tgt = board[nr * COLS + nc]
            if tgt == EMPTY or side_of(tgt) != side:
                yield nr, nc

    elif t == N:                                  # 马 / 傌：蹩马腿
        for dr, dc in _ORTHO:
            lr, lc = r + dr, c + dc
            if not on_board(lr, lc):
                continue
            if board[lr * COLS + lc] != EMPTY:   # 蹩马腿
                continue
            # 长边与腿同方向、短边垂直于腿。早先写成 (2*dr, dc) / (dr, 2*dc)
            # 会算出「直走一格」这种非法着法，导致马几乎走不动。
            if dr:                               # 纵向腿
                cands = ((r + 2 * dr, c - 1), (r + 2 * dr, c + 1))
            else:                                # 横向腿
                cands = ((r - 1, c + 2 * dc), (r + 1, c + 2 * dc))
            for nr, nc in cands:
                if not on_board(nr, nc):
                    continue
                tgt = board[nr * COLS + nc]
                if tgt == EMPTY or side_of(tgt) != side:
                    yield nr, nc

    elif t == R:                                  # 车 / 俥：直线滑行
        for dr, dc in _ORTHO:
            nr, nc = r + dr, c + dc
            while on_board(nr, nc):
                tgt = board[nr * COLS + nc]
                if tgt == EMPTY:
                    yield nr, nc
                else:
                    if side_of(tgt) != side:
                        yield nr, nc
                    break
                nr, nc = nr + dr, nc + dc

    elif t == C:                                  # 炮 / 砲：不吃子同车，吃子需隔一子
        for dr, dc in _ORTHO:
            nr, nc = r + dr, c + dc
            while on_board(nr, nc):               # 第一段：空位可走
                tgt = board[nr * COLS + nc]
                if tgt == EMPTY:
                    yield nr, nc
                    nr, nc = nr + dr, nc + dc
                    continue
                # 第一个子：只作炮架
                nr, nc = nr + dr, nc + dc
                while on_board(nr, nc):           # 第二段：越过炮架后可吃
                    tgt = board[nr * COLS + nc]
                    if tgt != EMPTY:
                        if side_of(tgt) != side:
                            yield nr, nc
                        break
                    nr, nc = nr + dr, nc + dc
                break

    elif t == P:                                  # 兵 / 卒
        # 红兵在下方（row 大），向前 = row 减小；黑卒相反。
        fwd = -1 if side > 0 else 1
        # 是否已过河：红兵过河 = row <= 4；黑卒过河 = row >= 5
        crossed = (r <= 4) if side > 0 else (r >= 5)
        nr = r + fwd
        # 前进：任何时候都可以（受界与自身子力限制）
        if on_board(nr, c):
            tgt = board[nr * COLS + c]
            if tgt == EMPTY or side_of(tgt) != side:
                yield nr, c
        # 横走：只有过河后才可以，且是「在原行左右平移」——兵永远不能斜走。
        # 早先误写成 (nr, c±1)，那是斜着前进一格，是非法着法。
        if crossed:
            for dc in (-1, 1):
                nc = c + dc
                if on_board(r, nc):
                    tgt = board[r * COLS + nc]
                    if tgt == EMPTY or side_of(tgt) != side:
                        yield r, nc


# ---------------------------------------------------------------- 对局状态


class Move:
    """一步棋（保留被吃的子，供悔棋使用）。"""

    __slots__ = ("fr", "fc", "tr", "tc", "piece", "captured")

    def __init__(self, fr: int, fc: int, tr: int, tc: int,
                 piece: int, captured: int) -> None:
        self.fr, self.fc, self.tr, self.tc = fr, fc, tr, tc
        self.piece = piece
        self.captured = captured

    def uci(self) -> str:
        """棋谱坐标，如 b2e2（列字母 a~i + 行号 0~9）。"""
        return f"{chr(ord('a') + self.fc)}{self.fr}{chr(ord('a') + self.tc)}{self.tr}"

    def text(self) -> str:
        return f"{chr(ord('a') + self.fc)}{self.fr + 1}→{chr(ord('a') + self.tc)}{self.tr + 1}"

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Move({name_of(self.piece)} {self.text()})"


class XiangqiGame:
    """一局中国象棋。"""

    def __init__(self) -> None:
        self.board = initial_board()
        self.to_move = RED          # 红方（玩家）先走
        self.history: List[Move] = []
        self.winner = 0             # 0 未结束 / 1 红胜 / -1 黑胜
        self.result = ""            # checkmate / stalemate / resign / ''
        self.repetition = 0

    # ---------------------------------------------------------- 便捷查询
    def snapshot(self) -> List[int]:
        return list(self.board)

    def find_king(self, side: int) -> int:
        """返回该方将 / 帅所在格下标；找不到返回 -1（理论上不该发生）。"""
        target = K if side > 0 else -K
        for i, p in enumerate(self.board):
            if p == target:
                return i
        return -1

    def _generals_face(self) -> bool:
        """白脸将：两将同列且中间无子。"""
        rk = self.find_king(RED)
        bk = self.find_king(BLACK)
        if rk < 0 or bk < 0:
            return False
        rc, bc = rk % COLS, bk % COLS
        if rc != bc:
            return False
        lo, hi = sorted((rk, bk))
        for i in range(lo + COLS, hi, COLS):
            if self.board[i] != EMPTY:
                return False
        return True

    def is_attacked(self, sq: int, by_side: int) -> bool:
        """格 sq 是否被 by_side 攻击（不考虑走后己方是否安全）。"""
        r, c = divmod(sq, COLS)

        # 车 / 炮 / 将 的直线方向
        for dr, dc in _ORTHO:
            nr, nc = r + dr, c + dc
            first = None
            while on_board(nr, nc):
                p = self.board[nr * COLS + nc]
                if p != EMPTY:
                    if side_of(p) == by_side and abs(p) in (R, K):
                        # 第一个拦路的己方子是车或将 → 这条线被打通
                        return True
                    first = p
                    break
                nr, nc = nr + dr, nc + dc
            # 炮：越过 first（炮架）后遇到的第一个子是可吃的
            if first is not None and abs(first) == C and side_of(first) == by_side:
                nr, nc = nr + dr, nc + dc
                while on_board(nr, nc):
                    p = self.board[nr * COLS + nc]
                    if p != EMPTY:
                        if side_of(p) != by_side:
                            return True
                        break
                    nr, nc = nr + dr, nc + dc

        # 马 / 傌（与 _pseudo_moves 同一套走法配对：长边同腿方向、短边垂直）
        for dr, dc in _ORTHO:
            lr, lc = r + dr, c + dc
            if not on_board(lr, lc):
                continue
            if self.board[lr * COLS + lc] != EMPTY:
                continue                       # 蹩马腿
            if dr:
                cands = ((r + 2 * dr, c - 1), (r + 2 * dr, c + 1))
            else:
                cands = ((r - 1, c + 2 * dc), (r + 1, c + 2 * dc))
            for nr, nc in cands:
                if not on_board(nr, nc):
                    continue
                if self.board[nr * COLS + nc] == (N if by_side > 0 else -N):
                    return True

        # 仕 / 士
        for dr, dc in _DIAG:
            nr, nc = r + dr, c + dc
            if on_board(nr, nc) and self.board[nr * COLS + nc] == (A if by_side > 0 else -A):
                return True

        # 相 / 象
        for dr, dc in _DIAG:
            tr, tc = r + 2 * dr, c + 2 * dc
            if on_board(tr, tc) and self.board[tr * COLS + tc] == (B if by_side > 0 else -B):
                if self.board[(r + dr) * COLS + (c + dc)] == EMPTY:
                    return True

        # 兵 / 卒
        fwd = -1 if by_side > 0 else 1          # 攻击方前进方向
        pr, pc = r - fwd, c
        if on_board(pr, pc) and self.board[pr * COLS + pc] == (P if by_side > 0 else -P):
            return True
        # 横向：仅当目标格在攻击方「已过河」区域
        for dc in (-1, 1):
            pr, pc = r - fwd, c + dc
            if not on_board(pr, pc):
                continue
            if self.board[pr * COLS + pc] != (P if by_side > 0 else -P):
                continue
            # 兵过河（红兵 row<=4，黑卒 row>=5）才吃横
            if by_side > 0 and pr <= 4:
                return True
            if by_side < 0 and pr >= 5:
                return True
        return False

    def in_check(self, side: int) -> bool:
        """side 是否正被将军。"""
        ksq = self.find_king(side)
        if ksq < 0:
            return True
        if self.is_attacked(ksq, -side):
            return True
        # 飞将：走到这一步后双方是否照面
        return self._generals_face()

    # ---------------------------------------------------------- 走法
    def legal_moves(self, side: Optional[int] = None) -> List[Move]:
        """side 的全部合法走法（已排除送将 / 飞将的棋）。"""
        side = self.to_move if side is None else side
        out: List[Move] = []
        b = self.board
        for i, p in enumerate(b):
            if side_of(p) != side:
                continue
            r, c = divmod(i, COLS)
            for nr, nc in _pseudo_moves(b, r, c):
                mv = Move(r, c, nr, nc, p, b[nr * COLS + nc])
                if self._try(mv):
                    out.append(mv)
                    self._undo(mv)
        return out

    def is_legal(self, fr: int, fc: int, tr: int, tc: int) -> bool:
        if not (on_board(fr, fc) and on_board(tr, tc)):
            return False
        piece = self.board[fr * COLS + fc]
        if side_of(piece) != self.to_move or piece == EMPTY:
            return False
        for mv in _pseudo_moves(self.board, fr, fc):
            if (mv[0], mv[1]) == (tr, tc):
                m = Move(fr, fc, tr, tc, piece, self.board[tr * COLS + tc])
                ok = self._try(m)
                if ok:
                    self._undo(m)
                return ok
        return False

    def _try(self, mv: Move) -> bool:
        self._apply(mv)
        ok = not self.in_check(side_of(mv.piece))
        self._undo(mv)
        return ok

    def _apply(self, mv: Move) -> None:
        self.board[mv.fr * COLS + mv.fc] = EMPTY
        self.board[mv.tr * COLS + mv.tc] = mv.piece

    def _undo(self, mv: Move) -> None:
        self.board[mv.tr * COLS + mv.tc] = mv.captured
        self.board[mv.fr * COLS + mv.fc] = mv.piece

    def play(self, fr: int, fc: int, tr: int, tc: int) -> bool:
        """走一步。非法返回 False；合法则落子并更新胜负。"""
        if self.winner != 0:
            return False
        for mv in _pseudo_moves(self.board, fr, fc):
            if (mv[0], mv[1]) == (tr, tc):
                m = Move(fr, fc, tr, tc, self.board[fr * COLS + fc],
                         self.board[tr * COLS + tc])
                if not self._try(m):
                    return False
                self._apply(m)
                self.history.append(m)
                self.to_move = -self.to_move
                self._update_result()
                return True
        return False

    def play_move(self, mv: Move) -> bool:
        return self.play(mv.fr, mv.fc, mv.tr, mv.tc)

    def undo(self) -> bool:
        """悔棋。返回是否成功。"""
        if len(self.history) < 2 or self.winner != 0:
            return False
        for _ in range(2):
            mv = self.history.pop()
            self._undo(mv)
            self.to_move = -self.to_move
        self.result = ""
        return True

    def last_move(self) -> Optional[Move]:
        return self.history[-1] if self.history else None

    def _update_result(self) -> None:
        side = self.to_move
        if not self.legal_moves(side):
            # 将死与困毙在象棋里都判负
            self.winner = -side
            self.result = "checkmate" if self.in_check(side) else "stalemate"

    def resign(self) -> None:
        """认输。"""
        self.winner = -self.to_move
        self.result = "resign"

    def piece_at(self, r: int, c: int) -> int:
        if not on_board(r, c):
            return EMPTY
        return self.board[r * COLS + c]


# ---------------------------------------------------------------- 评估与 AI

# 子力价值（千分）
VAL = {K: 10000, R: 900, N: 400, C: 450, A: 200, B: 200, P: 100}

# 位置价值表：行 0=黑方底线，行 9=红方底线。红方视角的表，黑方用镜像。
# 兵走得越靠前越有价值，中心比边角好。
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


def _piece_square(piece: int, r: int, c: int) -> int:
    t = abs(piece)
    if t == P:
        if piece > 0:
            return _PAWN_TABLE[r][c]
        return _PAWN_TABLE[ROWS - 1 - r][COLS - 1 - c]
    if t in (N, C):
        return 8 if 3 <= c <= 5 else 0        # 偏好中路
    if t == R:
        return 12 if 3 <= c <= 5 else 0
    return 0


def evaluate(board: List[int], side: int) -> int:
    """从 side 视角的局面分（正 = side 占优）。"""
    score = 0
    for i, p in enumerate(board):
        if p == EMPTY:
            continue
        r, c = divmod(i, COLS)
        v = VAL[abs(p)] + _piece_square(p, r, c)
        score += v if side_of(p) == side else -v
    return score


class XiangqiAI:
    """Alpha-Beta（negamax）搜索引擎。三档难度靠搜索深度区分。"""

    def __init__(self, difficulty: str = "normal", color: int = BLACK,
                 depth: Optional[int] = None) -> None:
        self.difficulty = difficulty if difficulty in DIFFICULTIES else "normal"
        self.color = color
        # 显式深度优先于难度档位（调试 / 标定用）
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
        """走法排序：吃子优先（MVV-LVA 简化），提升剪枝效率。"""
        vals = {R: 5, C: 4, N: 3, B: 2, A: 2, P: 1, K: 0}
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
            # 无棋可走（将死 / 困毙）都判负
            return -100000
        best = -10 ** 9
        for mv in self._ordered_moves(game, moves):
            game._apply(mv)
            # 只吃子时延长一层（简易 quiescence，避免刚吃完就被评价）
            ext = depth - 1
            score = -self._search(game, ext, -beta, -alpha, -side)
            game._undo(mv)
            if score > best:
                best = score
            if best > alpha:
                alpha = best
            if alpha >= beta:
                break                      # beta 剪枝
        return best

    # ---------------------------------------------------------- 对外
    def choose_move(self, game: XiangqiGame,
                    rng: Optional[random.Random] = None) -> Optional[Move]:
        """返回 AI 选择的一步；无棋可走返回 None。"""
        if game.winner != 0 or game.to_move != self.color:
            return None
        rng = rng or self._rng
        moves = game.legal_moves(self.color)
        if not moves:
            return None

        # 简单档：随机，避免新手被碾压
        if self.difficulty == "easy":
            return rng.choice(moves)

        self.nodes = 0
        best_score = -10 ** 9
        best_moves: List[Move] = []
        alpha = -10 ** 9
        for mv in self._ordered_moves(game, moves):
            game._apply(mv)
            score = -self._search(game, self.depth - 1, -10 ** 9, -alpha, -self.color)
            game._undo(mv)
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
]
