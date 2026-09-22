# -*- coding: utf-8 -*-
"""桌宠小游戏：五子棋（Gomoku）。

- 纯逻辑，无 Qt 依赖，方便单元测试（棋盘、胜负、AI 评分）。
- 玩家执黑（BLACK=1）先手；AI 执白（WHITE=2）。
- 胜负：横 / 竖 / 两条斜线任一方连续 5 子；棋盘下满无胜负为和棋。

AI（三档难度，统一走"必胜/必堵 + 棋型评分"框架）：
  - easy  ：弱防守、在多个好点里随机（新手能赢）；
  - normal：会赢会堵、攻防均衡，选最高分；
  - hard  ：会赢会堵、防守权重更高。

棋型评分用"局部 5/6 格窗口枚举"，天然识别跳棋型（如 11011 跳四、1011 跳三），
且与棋盘规模无关，落子决策为 O(候选数)，很快。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

SIZE = 15
EMPTY = 0
BLACK = 1
WHITE = 2

S_FIVE = 1_000_000          # 五连（直接获胜）
S_LIVE_FOUR = 100_000       # 活四 / 双四（下一手必胜）
S_RUSH_FOUR = 10_000        # 冲四（一个必杀点）
S_LIVE_THREE = 8_000        # 活三（再补一手成活四）
S_SLEEP_THREE = 1_000       # 眠三
S_LIVE_TWO = 500            # 活二
S_SLEEP_TWO = 100           # 眠二
S_LIVE_ONE = 10             # 活一

# 四个方向：横、竖、主斜、副斜
_DIRECTIONS = ((0, 1), (1, 0), (1, 1), (1, -1))

DIFFICULTIES = ("easy", "normal", "hard")


def opponent(player: int) -> int:
    return 3 - player


@dataclass
class GomokuGame:
    """一局五子棋。"""
    board: List[List[int]] = field(default_factory=list)
    to_move: int = BLACK
    winner: int = 0          # 0 未分胜负 / 1 黑胜 / 2 白胜 / 3 和棋
    history: List[Tuple[int, int]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.board:
            self.reset()

    def reset(self) -> None:
        self.board = [[EMPTY] * SIZE for _ in range(SIZE)]
        self.to_move = BLACK
        self.winner = 0
        self.history = []

    def in_board(self, r: int, c: int) -> bool:
        return 0 <= r < SIZE and 0 <= c < SIZE

    def is_legal(self, r: int, c: int) -> bool:
        return self.winner == 0 and self.in_board(r, c) \
            and self.board[r][c] == EMPTY

    def last_move(self) -> Optional[Tuple[int, int]]:
        return self.history[-1] if self.history else None

    def play(self, r: int, c: int) -> bool:
        """落子；成功返回 True，并轮换/判定胜负。"""
        if not self.is_legal(r, c):
            return False
        player = self.to_move
        self.board[r][c] = player
        self.history.append((r, c))
        if self.five_at(r, c, player):
            self.winner = player
        elif len(self.history) >= SIZE * SIZE:
            self.winner = 3
        else:
            self.to_move = opponent(player)
        return True

    def five_at(self, r: int, c: int, player: int) -> bool:
        """以 (r,c) 为中心是否形成五连。"""
        for dr, dc in _DIRECTIONS:
            count = 1
            i = 1
            while self.in_board(r + dr * i, c + dc * i) \
                    and self.board[r + dr * i][c + dc * i] == player:
                count += 1
                i += 1
            i = 1
            while self.in_board(r - dr * i, c - dc * i) \
                    and self.board[r - dr * i][c - dc * i] == player:
                count += 1
                i += 1
            if count >= 5:
                return True
        return False

    def candidate_moves(self, radius: int = 1) -> List[Tuple[int, int]]:
        """已有棋子 radius 格内的空位（落子离战场太远没有意义）。"""
        if not self.history:
            return [(SIZE // 2, SIZE // 2)]
        marked = set()
        for (r, c) in self.history:
            for dr in range(-radius, radius + 1):
                for dc in range(-radius, radius + 1):
                    nr, nc = r + dr, c + dc
                    if self.in_board(nr, nc) and self.board[nr][nc] == EMPTY:
                        marked.add((nr, nc))
        return sorted(marked) or [self.history[-1]]


#  AI 评估
def _winning_moves(game: GomokuGame, player: int,
                   candidates: Optional[List[Tuple[int, int]]] = None
                   ) -> List[Tuple[int, int]]:
    """返回 player 落子即可直接五连的所有空位（必杀点）。"""
    out = []
    for (r, c) in (candidates if candidates is not None
                   else game.candidate_moves()):
        if game.board[r][c] != EMPTY:
            continue
        game.board[r][c] = player
        win = game.five_at(r, c, player)
        game.board[r][c] = EMPTY
        if win:
            out.append((r, c))
    return out


def _line_pattern_score(game: GomokuGame, r: int, c: int,
                        dr: int, dc: int, player: int) -> int:
    """沿 (dr,dc) 方向，评估假设 player 落子 (r,c) 后在该方向形成的棋型分。

    用"含中心的 5/6 格窗口枚举"：
      - 5 格窗口里 4 子 + 1 空 → 那个空是必杀点；
      - 该方向必杀点 ≥2 为活四、==1 为冲四；
      - 6 格窗口里 3 子，补一手能成活四为活三、成冲四为眠三；
      - 二/一退化用连续段判断。
    天然覆盖跳四（11011）、跳三（1011/1101）。
    """
    b = game.board

    def val(k: int) -> int:
        """沿方向 offset=k 的格子：1=己 0=空 2=敌/界。"""
        rr, cc = r + dr * k, c + dc * k
        if not game.in_board(rr, cc):
            return 2
        v = b[rr][cc]
        if v == player:
            return 1
        return 0 if v == EMPTY else 2

    def fives_after(extra_k: Optional[int] = None) -> Tuple[bool, set]:
        """返回（是否已成五连，沿该方向的必杀点 offset 集合）。

        extra_k 表示再假设在 offset=extra_k 补一手（用于活三前瞻）。
        """
        def cell(k: int) -> int:
            return 1 if k == extra_k else val(k)

        fives = set()
        five = False
        # 含中心 k=0 的长度 5 窗口：窗口起点 a ∈ [-4, 0]
        for a in range(-4, 1):
            ks = list(range(a, a + 5))
            vals = [cell(k) for k in ks]
            if all(x == 1 for x in vals):
                five = True
                continue
            if 2 not in vals:
                empties = [k for k, x in zip(ks, vals) if x == 0]
                ones = sum(1 for x in vals if x == 1)
                if ones == 4 and len(empties) == 1:
                    fives.add(empties[0])
        return five, fives

    five, fives = fives_after()
    if five:
        return S_FIVE
    if len(fives) >= 2:
        return S_LIVE_FOUR
    if len(fives) == 1:
        return S_RUSH_FOUR

    # 三：含中心的长度 6 窗口（起点 a ∈ [-5, 0]）
    best = 0
    for a in range(-5, 1):
        ks = list(range(a, a + 6))
        vals = [val(k) for k in ks]
        ones = [k for k, x in zip(ks, vals) if x == 1]
        empties = [k for k, x in zip(ks, vals) if x == 0]
        if 0 not in ones or len(ones) != 3 or len(empties) < 2:
            continue
        # 补任意一个空，看能否成活四 / 冲四
        for ek in empties:
            f5, fpts = fives_after(extra_k=ek)
            if f5 or len(fpts) >= 2:
                return S_LIVE_THREE
            if len(fpts) == 1:
                best = max(best, S_SLEEP_THREE)
    if best:
        return best

    # 二 / 一：连续段判断（连子 + 两端开放数）
    return _line_continuous(game, r, c, dr, dc, player)


def _line_continuous(game: GomokuGame, r: int, c: int,
                     dr: int, dc: int, player: int) -> int:
    """该方向的连续段（仅用于二、一的布局分）。"""
    b = game.board
    count = 1
    open_ends = 0

    i = 1
    while game.in_board(r + dr * i, c + dc * i) \
            and b[r + dr * i][c + dc * i] == player:
        count += 1
        i += 1
    if game.in_board(r + dr * i, c + dc * i) \
            and b[r + dr * i][c + dc * i] == EMPTY:
        open_ends += 1

    i = 1
    while game.in_board(r - dr * i, c - dc * i) \
            and b[r - dr * i][c - dc * i] == player:
        count += 1
        i += 1
    if game.in_board(r - dr * i, c - dc * i) \
            and b[r - dr * i][c - dc * i] == EMPTY:
        open_ends += 1

    if count >= 2:
        if open_ends == 2:
            return S_LIVE_TWO if count == 2 else S_SLEEP_THREE
        if open_ends == 1:
            return S_SLEEP_TWO
        return 0
    return S_LIVE_ONE if open_ends == 2 else 0


def evaluate_point(game: GomokuGame, r: int, c: int, player: int,
                   deep: bool = True) -> int:
    """假设 player 在 (r,c) 落子，对该点的进攻价值评分（越大越好）。

    聚合四个方向的棋型；deep=False 时忽略跳三/活三前瞻（简单档更弱）。
    """
    b = game.board
    if b[r][c] != EMPTY:
        return -1
    b[r][c] = player
    try:
        if game.five_at(r, c, player):
            return S_FIVE
        scores = [_line_pattern_score(game, r, c, dr, dc, player)
                  for dr, dc in _DIRECTIONS]
        # 五连 / 活四（任一方向必胜）
        if S_FIVE in scores:
            return S_FIVE
        if S_LIVE_FOUR in scores:
            return S_LIVE_FOUR
        if S_RUSH_FOUR in scores:
            # 冲四 + 活三（另一方向）= 必胜
            if deep and S_LIVE_THREE in scores:
                return S_LIVE_FOUR
            return S_RUSH_FOUR
        # 双活三 = 必胜
        if deep and sum(1 for x in scores if x == S_LIVE_THREE) >= 2:
            return S_LIVE_FOUR
        if not deep:
            # 简单档不把跳三/活三当作高优先（_line_pattern_score 已含，
            # 这里统一封顶到眠三档，使其更弱）
            scores = [min(x, S_SLEEP_THREE) for x in scores]
        return sum(scores)
    finally:
        b[r][c] = EMPTY


def analyze_move(game: GomokuGame, r: int, c: int, player: int) -> str:
    """player 在 (r,c) 已落子，判定这一步形成的最强威胁等级。

    复用 evaluate_point（临时撤子再评估），天然识别跳棋型。返回：
      "five"       五连（已获胜）
      "live_four"  活四 / 双活三 / 冲四+活三（下一手必胜）
      "rush_four"  冲四（一个必杀点，对手必须堵）
      "live_three" 活三（对手不堵就会成活四）
      "none"       其它（活二、眠型等，暂不构成威胁）
    """
    if not game.in_board(r, c) or game.board[r][c] != player:
        return "none"
    if game.winner == player:
        return "five"
    game.board[r][c] = EMPTY
    try:
        score = evaluate_point(game, r, c, player, deep=True)
    finally:
        game.board[r][c] = player
    if score >= S_FIVE:
        return "five"
    if score >= S_LIVE_FOUR:
        return "live_four"
    if score >= S_RUSH_FOUR:
        return "rush_four"
    if score >= S_LIVE_THREE:
        return "live_three"
    return "none"


@dataclass
class GomokuAI:
    """五子棋 AI。color 为 AI 执子（默认白）。"""
    difficulty: str = "normal"
    color: int = WHITE
    _rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self) -> None:
        if self.difficulty not in DIFFICULTIES:
            self.difficulty = "normal"

    @property
    def deep(self) -> bool:
        # 简单档不做活三 / 组合必胜判断（更弱）
        return self.difficulty != "easy"

    @property
    def defend_weight(self) -> float:
        return {"easy": 0.5, "normal": 1.0, "hard": 1.15}[self.difficulty]

    def choose_move(self, game: GomokuGame,
                    rng: Optional[random.Random] = None) -> Optional[Tuple[int, int]]:
        """返回 AI 选择的落子坐标；棋局已结束返回 None。"""
        rng = rng or self._rng
        if game.winner != 0:
            return None
        me = self.color
        opp = opponent(me)
        candidates = game.candidate_moves()
        if not candidates:
            return None

        # 1) 自己能直接赢 → 直接下
        my_wins = _winning_moves(game, me, candidates)
        if my_wins:
            return rng.choice(my_wins)

        # 2) 对手下一手能直接赢 → 必须堵
        opp_wins = _winning_moves(game, opp, candidates)
        if opp_wins:
            # 简单档偶尔漏堵（约 30%），其余/高难度必堵
            if self.difficulty == "easy" and rng.random() < 0.30:
                pass
            elif len(opp_wins) >= 2:
                # 对方已成双必杀点（理论上防不住），至少堵一个自己也能成势的点
                return max(opp_wins, key=lambda p: self._score(game, p, me))
            else:
                return opp_wins[0]

        # 3) 棋型评分
        scored = [(self._score(game, p, me), p) for p in candidates]
        scored.sort(key=lambda x: x[0], reverse=True)
        best_score = scored[0][0]

        if self.difficulty == "easy":
            # 弱：从前若干个好点里随机（容易走出非最优）
            pool = [p for s, p in scored[:6] if s >= 0]
            if pool:
                return rng.choice(pool)
        if self.difficulty == "normal":
            # 同分随机一个，避免每局完全一样
            top = [p for s, p in scored if s >= best_score]
            return rng.choice(top)
        return scored[0][1]  # hard：严格最高分

    def _score(self, game: GomokuGame, pos: Tuple[int, int], me: int) -> int:
        r, c = pos
        opp = opponent(me)
        attack = evaluate_point(game, r, c, me, deep=self.deep)
        defend = evaluate_point(game, r, c, opp, deep=self.deep)
        center_bonus = 2 - (abs(r - SIZE // 2) + abs(c - SIZE // 2)) // 6
        return int(attack + self.defend_weight * defend + center_bonus)
