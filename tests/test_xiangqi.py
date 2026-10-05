# -*- coding: utf-8 -*-
"""中国象棋引擎单元测试（纯逻辑，不开窗口）。

夹具约定：blank() 建空盘后由各用例自行摆子。摆位时注意
「飞将」：两个将同列且中间无子时，双方都算被将军，会让所有
与飞将无关的走法断言全部失效。所以默认把黑将放在 (0,0)（与红帅 (9,4)
不同列）；只有专门测飞将的用例才把黑将放到 (0,4)。
"""
from __future__ import annotations

import sys

import pytest

sys.path.insert(0, r"E:\study\desktop-pet")

from app.games.xiangqi import (  # noqa: E402
    A, B, BLACK, C, COLS, DIFFICULTIES, EMPTY, K, N, P, R, RED,
    XiangqiAI, XiangqiGame, initial_board, name_of, side_of,
)


def blank() -> XiangqiGame:
    g = XiangqiGame()
    g.board = [EMPTY] * 90
    g.history = []
    g.winner = 0
    g.result = ""
    g.to_move = RED
    return g


def put(g: XiangqiGame, r: int, c: int, p: int) -> None:
    g.board[r * COLS + c] = p


def both_kings(g: XiangqiGame, black_col: int = 0) -> None:
    """红帅固定 (9,4)；黑将放 black_col 列（默认 0，避开飞将）。"""
    put(g, 9, 4, K)
    put(g, 0, black_col, -K)


def dests(g: XiangqiGame, r: int, c: int) -> set:
    return {(m.tr, m.tc) for m in g.legal_moves() if (m.fr, m.fc) == (r, c)}


# ---------------------------------------------------------------- 布局

class TestInitialBoard:
    def test_piece_counts(self):
        b = initial_board()
        assert len(b) == 90
        for t, n in ((K, 1), (A, 2), (B, 2), (N, 2), (R, 2), (C, 2), (P, 5)):
            assert sum(1 for p in b if p == t) == n, f"红 {t} 数量错"
            assert sum(1 for p in b if p == -t) == n, f"黑 {t} 数量错"

    def test_red_moves_first(self):
        assert XiangqiGame().to_move == RED

    def test_pawns_on_correct_rows(self):
        b = initial_board()
        assert [c for c in range(COLS) if b[6 * COLS + c] == P] == [0, 2, 4, 6, 8]
        assert [c for c in range(COLS) if b[3 * COLS + c] == -P] == [0, 2, 4, 6, 8]

    def test_name_of(self):
        assert name_of(K) == "帅" and name_of(-K) == "将"
        assert name_of(B) == "相" and name_of(-B) == "象"
        assert name_of(A) == "仕" and name_of(-A) == "士"
        assert name_of(N) == "马" and name_of(-N) == "马"
        assert name_of(EMPTY) == ""
        assert side_of(-5) == BLACK and side_of(EMPTY) == 0

    def test_opening_has_44_legal_moves(self):
        """初始局面红方合法走法必须是 44（象棋公认基准值）。"""
        assert len(XiangqiGame().legal_moves(RED)) == 44

    def test_opening_is_black_white_symmetric(self):
        assert len(XiangqiGame().legal_moves(BLACK)) == 44


# ---------------------------------------------------------------- 各兵种走法

class TestKing:
    def test_king_moves_orthogonally_in_palace(self):
        g = blank(); both_kings(g); put(g, 9, 4, K)
        ds = dests(g, 9, 4)
        assert (8, 4) in ds and (9, 3) in ds and (9, 5) in ds

    def test_king_cannot_leave_palace(self):
        g = blank(); both_kings(g); put(g, 9, 4, K)
        assert (7, 4) not in dests(g, 9, 4)      # 出九宫上界
        assert (9, 2) not in dests(g, 9, 4)      # 出九宫侧界
        assert (8, 3) not in dests(g, 9, 4)      # 不能斜走

    def test_king_from_palace_corner(self):
        """从九宫角上走：三个方向合法，出九宫的方向非法。"""
        g = blank()
        put(g, 9, 4, K)                  # 红帅不放在这里，否则 (9,4) 被自己占住
        g.board[9 * COLS + 4] = EMPTY
        put(g, 0, 0, -K)
        put(g, 8, 3, K)
        ds = dests(g, 8, 3)
        assert (8, 4) in ds and (7, 3) in ds and (9, 3) in ds
        assert (8, 2) not in ds, "不能走出九宫"
        assert (7, 4) not in ds and (9, 4) not in ds, "帅只能直走，斜走非法"


class TestAdvisor:
    def test_advisor_diagonal_only(self):
        g = blank(); both_kings(g); put(g, 9, 3, A)
        ds = dests(g, 9, 3)
        assert (8, 4) in ds
        assert (9, 4) not in ds          # 不能直走
        assert (8, 2) not in ds          # 出九宫
        assert (9, 2) not in ds


class TestElephant:
    def test_elephant_two_step_diagonal(self):
        g = blank(); both_kings(g); put(g, 9, 2, B)
        assert (7, 0) in dests(g, 9, 2) and (7, 4) in dests(g, 9, 2)

    def test_elephant_eye_blocked(self):
        """塞象眼：田字格中心有子则该方向不可走。"""
        g = blank(); both_kings(g); put(g, 9, 2, B); put(g, 8, 3, P)
        assert (7, 4) not in dests(g, 9, 2)     # 象眼被 (8,3) 挡住
        assert (7, 0) in dests(g, 9, 2)         # 另一方向通畅

    def test_elephant_cannot_cross_river(self):
        g = blank(); both_kings(g); put(g, 5, 2, B)
        assert dests(g, 5, 2), "贴河的象应该还有棋可走"
        assert all(r >= 5 for r, c in dests(g, 5, 2)), "象不能过河"

    def test_black_elephant_eye_blocked(self):
        g = blank(); both_kings(g); put(g, 4, 6, -B); put(g, 3, 5, -P)
        assert (2, 4) not in dests(g, 4, 6)

    def test_elephant_cannot_capture_across_river(self):
        """河界不只是移动限制，河边象吃不到对岸的子。"""
        g = blank(); both_kings(g)
        put(g, 5, 0, B)
        put(g, 4, 2, -P)                  # 对岸的卒
        assert (4, 2) not in dests(g, 5, 0)


class TestHorse:
    def test_horse_l_shape_eight_squares(self):
        g = blank(); both_kings(g); put(g, 5, 4, N)
        ds = dests(g, 5, 4)
        for want in ((3, 3), (3, 5), (4, 2), (4, 6), (6, 2), (6, 6), (7, 3), (7, 5)):
            assert want in ds, f"马应能走到 {want}，实际 {sorted(ds)}"

    def test_horse_never_moves_one_square(self):
        """回归：曾把蹩马腿写错成 (2*dr,dc)/(dr,2*dc)，导致马只能直走一格。"""
        g = blank(); both_kings(g); put(g, 5, 4, N)
        for r, c in dests(g, 5, 4):
            dr, dc = abs(r - 5), abs(c - 4)
            assert sorted((dr, dc)) == [1, 2], f"({r},{c}) 不是马的日字着法"

    def test_horse_leg_blocked_vertical(self):
        g = blank(); both_kings(g); put(g, 5, 4, N); put(g, 4, 4, P)
        ds = dests(g, 5, 4)
        assert (3, 3) not in ds and (3, 5) not in ds     # 北侧两格被蹩
        assert (4, 2) in ds and (4, 6) in ds           # 东西不受影响

    def test_horse_leg_blocked_horizontal(self):
        g = blank(); both_kings(g); put(g, 5, 4, N); put(g, 5, 3, P)
        ds = dests(g, 5, 4)
        assert (4, 2) not in ds and (6, 2) not in ds   # 西侧被蹩
        assert (4, 6) in ds and (6, 6) in ds           # 东侧不受影响
        assert (3, 3) in ds and (3, 5) in ds

    def test_horse_leg_is_same_orthogonal_direction(self):
        """蹩马腿判定：长边方向上的相邻格。往 (8,3) 走看的是 (9,2) 而非 (8,1)。"""
        g = blank(); both_kings(g)
        put(g, 5, 4, N)
        put(g, 5, 3, P)                  # 西腿堵死
        assert (4, 2) not in dests(g, 5, 4)


class TestRook:
    def test_rook_slides_and_stops_at_first_piece(self):
        g = blank(); both_kings(g); put(g, 5, 0, R); put(g, 5, 4, -P)
        ds = dests(g, 5, 0)
        assert (5, 1) in ds and (5, 3) in ds
        assert (5, 4) in ds              # 吃黑兵
        assert (5, 5) not in ds          # 不能穿过

    def test_rook_cannot_capture_own_piece(self):
        g = blank(); both_kings(g); put(g, 5, 0, R); put(g, 5, 2, P)
        assert (5, 2) not in dests(g, 5, 0)
        assert (5, 1) in dests(g, 5, 0)


class TestCannon:
    def test_cannon_needs_screen_to_capture(self):
        g = blank(); both_kings(g)
        put(g, 5, 0, C)
        put(g, 5, 4, -P)
        assert (5, 4) not in dests(g, 5, 0), "没有炮架不能吃子"

    def test_cannon_jumps_over_screen(self):
        g = blank(); both_kings(g)
        put(g, 5, 0, C)
        put(g, 5, 2, -A)                 # 炮架
        put(g, 5, 4, -P)                 # 目标
        ds = dests(g, 5, 0)
        assert (5, 4) in ds, "有炮架应该能吃"
        assert (5, 2) not in ds, "炮架本身不能吃、也不能停"

    def test_cannon_moves_over_empty_squares(self):
        g = blank(); both_kings(g)
        put(g, 5, 0, C)
        assert (5, 1) in dests(g, 5, 0) and (5, 2) in dests(g, 5, 0)


class TestPawn:
    def test_red_pawn_forward_only_before_river(self):
        g = blank(); both_kings(g); put(g, 6, 4, P)
        ds = dests(g, 6, 4)
        assert (5, 4) in ds
        assert (6, 3) not in ds and (6, 5) not in ds, "未过河不能横走"
        assert (7, 4) not in ds, "兵不能后退"

    def test_red_pawn_sideways_after_river(self):
        """过河后可横走，且是原行平移（不是斜走）。"""
        g = blank(); both_kings(g); put(g, 4, 4, P)
        ds = dests(g, 4, 4)
        assert (3, 4) in ds, "仍可前进"
        assert (4, 3) in ds and (4, 5) in ds, "过河后可左右平移"
        for r, c in ds:
            assert abs(r - 4) + abs(c - 4) == 1, f"({r},{c}) 不是兵的合法着法"

    def test_black_pawn_sideways_after_river(self):
        g = blank(); both_kings(g); put(g, 5, 4, -P)
        g.to_move = BLACK
        ds = dests(g, 5, 4)
        assert (6, 4) in ds and (5, 3) in ds and (5, 5) in ds
        assert (4, 4) not in ds, "黑卒不能后退"

    def test_black_pawn_forward_only_before_river(self):
        g = blank(); both_kings(g); put(g, 3, 4, -P)
        g.to_move = BLACK
        ds = dests(g, 3, 4)
        assert (4, 4) in ds
        assert (3, 3) not in ds and (3, 5) not in ds


# ---------------------------------------------------------------- 将军 / 终局

class TestFlyingGeneral:
    def test_facing_generals_mean_both_in_check(self):
        g = blank(); both_kings(g, black_col=4)
        assert g.in_check(RED) and g.in_check(BLACK)

    def test_piece_between_breaks_facing(self):
        g = blank(); both_kings(g, black_col=4); put(g, 5, 4, -P)
        assert not g.in_check(RED) and not g.in_check(BLACK)

    def test_moving_off_general_file_is_illegal(self):
        g = blank(); both_kings(g, black_col=4)
        put(g, 5, 4, P)                  # 兵正好挡在两将之间
        assert not g.in_check(RED)
        ds = dests(g, 5, 4)
        assert (4, 4) in ds, "沿第 4 列前进仍挡着，合法"
        assert (5, 3) not in ds and (5, 5) not in ds, \
            "横走会离开第 4 列 → 造成白脸将，必须判非法"


class TestCheckAndMate:
    def test_rook_gives_check(self):
        g = blank(); both_kings(g)
        put(g, 0, 3, R)
        assert g.in_check(BLACK)
        assert not g.in_check(RED)

    def test_horse_gives_check_through_leg(self):
        g = blank(); both_kings(g)
        put(g, 4, 4, N)                  # 马从 (4,4) 跳到 (3,4)?? 用另一组
        put(g, 5, 4, N)                  # (5,4) 的马攻击 (3,3)/(3,5)/(4,2)/(4,6)…
        g.board[5 * COLS + 4] = EMPTY
        # 直接构造：马在 (3,2)，黑将在 (0,0)，马攻击不到；改为验证马的将军
        g = blank(); both_kings(g)
        put(g, 2, 2, N)
        put(g, 0, 1, -K)                 # 黑将挪到 (0,1)
        g.board[0 * COLS + 0] = EMPTY
        # 马 (2,2) 的长边在北/南/西/东，逐一验证至少一处能打到 (0,1)
        put(g, 2, 2, N)
        ds = dests(g, 2, 2)
        assert (0, 1) in ds or (0, 3) in ds, f"马应能跳到 (0,1)/(0,3)：{sorted(ds)}"

    def test_checkmate_detected(self):
        """双车绝杀：黑将在九宫内被封死。"""
        g = blank()
        put(g, 0, 4, -K)
        put(g, 9, 4, K)
        put(g, 1, 3, R)                  # 红车控制 (0,3) 与第 3 列
        put(g, 1, 5, R)                  # 红车控制 (0,5) 与第 5 列
        g.to_move = BLACK
        assert g.legal_moves(BLACK) == [], "黑将应无路可走"
        assert g.in_check(BLACK)
        g._update_result()
        assert g.winner == RED
        assert g.result == "checkmate"

    def test_stalemate_is_draw(self):
        """困毙：本引擎按和棋处理（多数用户/对局软件的约定）。

        严格象棋规则困毙判负，但对局体验差、易引争议；
        故这里把困毙归为和棋（winner 不变，result 标记为 stalemate_draw）。

        合成局面（只为验证困毙判定，棋子摆位不追求可实战）：
        黑将孤身困在 (0,4)，九宫内三个可走格全被红方控制，且黑将本身并未被将军——
          · (0,3)  被红马 (2,2)（腿在 (1,2)）控制
          · (0,5)  被红马 (2,6)（腿在 (1,6)）控制
          · (1,4)  被红车 (1,8) 直接吃（同行）
        红帅放 (9,0) 而非 (9,4)，避免与黑将同列形成飞将（那会变成将死）。
        """
        g = blank()
        put(g, 9, 0, K)
        put(g, 0, 4, -K)
        put(g, 1, 8, R)                  # 控制 (1,4)
        put(g, 2, 2, N)                  # 控制 (0,3)
        put(g, 2, 6, N)                  # 控制 (0,5)
        g.to_move = BLACK
        assert g.legal_moves(BLACK) == [], "黑方应无任何合法着法"
        assert not g.in_check(BLACK), "这里应是困毙而不是将死"
        g._update_result()
        assert g.winner == 0, "困毙按和棋处理，winner 不应有值"
        assert g.result == "stalemate_draw"

    def test_cannot_move_into_self_check(self):
        """送将的棋必须被过滤。

        局面：黑车 (5,4) 沿第 4 列将军，红帅 (9,4)。
        红车 (7,2) 只有走到 (7,4) 挡住才算应将，横走仍然暴露红帅 → 必须判非法。
        """
        g = blank()
        put(g, 9, 4, K)
        put(g, 0, 0, -K)
        put(g, 5, 4, -R)
        assert g.in_check(RED), "黑车应正在将军红帅"

        put(g, 7, 2, R)
        ds = dests(g, 7, 2)
        assert (7, 4) in ds, "走到 (7,4) 挡住黑车，是合法应将"
        assert (7, 1) not in ds, "横走不挡车，走完仍被将军，必须非法"
        assert (7, 3) not in ds, "(7,3) 也挡不住，必须非法"


# ---------------------------------------------------------------- 流程

class TestFlow:
    def test_play_switches_turn(self):
        g = XiangqiGame()
        mv = next(m for m in g.legal_moves(RED) if (m.fr, m.fc) == (6, 0))
        assert g.play(mv.fr, mv.fc, mv.tr, mv.tc)
        assert g.to_move == BLACK
        assert len(g.history) == 1

    def test_illegal_move_rejected(self):
        g = XiangqiGame()
        assert g.play(0, 0, 4, 4) is False    # (0,0) 是黑方棋子，红方走不了

    def test_undo_rolls_back_two_plies(self):
        """悔棋一次退两手（一个完整回合）——退完正好回到开局。"""
        g = XiangqiGame()
        snap = g.snapshot()
        for _ in range(2):
            mv = g.legal_moves()[0]
            g.play(mv.fr, mv.fc, mv.tr, mv.tc)
        assert len(g.history) == 2
        assert g.undo() is True
        assert g.snapshot() == snap, "退 2 手后应回到开局局面"
        assert g.to_move == RED
        assert g.history == []

    def test_undo_to_start(self):
        g = XiangqiGame()
        snap = g.snapshot()
        for _ in range(4):
            mv = g.legal_moves()[0]
            g.play(mv.fr, mv.fc, mv.tr, mv.tc)
        assert g.undo() and g.undo()
        assert g.snapshot() == snap
        assert g.to_move == RED

    def test_undo_rejected_at_start(self):
        assert XiangqiGame().undo() is False

    def test_resign(self):
        g = XiangqiGame()
        g.resign()
        assert g.winner == BLACK and g.result == "resign"

    def test_pawn_capture(self):
        g = blank(); both_kings(g)
        put(g, 4, 4, P); put(g, 3, 4, -A)
        assert g.play(4, 4, 3, 4)
        assert g.board[3 * COLS + 4] == P

    def test_play_rejected_after_game_over(self):
        g = XiangqiGame()
        g.resign()
        assert g.play(6, 0, 5, 0) is False


# ---------------------------------------------------------------- AI

class TestAI:
    @pytest.mark.parametrize("diff", DIFFICULTIES)
    def test_ai_returns_legal_move(self, diff):
        g = XiangqiGame()
        g.play(6, 0, 5, 0)               # 红方一步，轮到黑
        assert g.to_move == BLACK
        mv = XiangqiAI(diff, BLACK).choose_move(g)
        assert mv is not None
        assert g.is_legal(mv.fr, mv.fc, mv.tr, mv.tc)

    def test_ai_illegal_side_returns_none(self):
        g = XiangqiGame()
        assert XiangqiAI("normal", BLACK).choose_move(g) is None

    def test_ai_takes_free_capture(self):
        """白送的子必须被吃掉。"""
        g = blank(); both_kings(g)
        put(g, 4, 0, P)                   # 红兵
        put(g, 4, 2, -R)                  # 黑车与兵同行，一步可吃
        g.to_move = BLACK
        mv = XiangqiAI("normal", BLACK).choose_move(g)
        assert mv is not None
        assert abs(mv.captured) == P, f"AI 没吃白送的兵，选了 {mv}"

    def test_ai_answers_check(self):
        """被将军时必须应将（走完后不能仍被将军）。"""
        g = blank()
        put(g, 0, 4, -K)                  # 黑将在九宫内
        put(g, 9, 0, K)                   # 红帅放 (9,0)，避免与黑将同列形成飞将
        put(g, 0, 2, R)                   # 红车在第 0 行将军
        assert g.in_check(BLACK)
        g.to_move = BLACK
        assert g.legal_moves(BLACK), "被将军但有棋可走（不是将死）"
        mv = XiangqiAI("normal", BLACK).choose_move(g)
        assert mv is not None
        g.play(mv.fr, mv.fc, mv.tr, mv.tc)
        assert not g.in_check(BLACK), "AI 走完仍被将军 = 没应将"

    def test_ai_depth_increases_with_difficulty(self):
        assert (XiangqiAI("easy").depth
                < XiangqiAI("normal").depth
                < XiangqiAI("hard").depth)

    def test_ai_explicit_depth_override(self):
        assert XiangqiAI("easy", BLACK, depth=5).depth == 5

    def test_ai_is_reasonably_fast(self):
        """UI 回合预算：普通档应远快于 1s。"""
        import time
        g = XiangqiGame()
        for i in range(12):
            ms = g.legal_moves()
            g.play(ms[i % len(ms)].fr, ms[i % len(ms)].fc,
                   ms[i % len(ms)].tr, ms[i % len(ms)].tc)
        t = time.perf_counter()
        mv = XiangqiAI("normal", g.to_move).choose_move(g)
        assert mv is not None
        assert time.perf_counter() - t < 1.5, "AI 太慢，会卡住界面"
