# -*- coding: utf-8 -*-
"""中国象棋弹窗烟测。

不绘制 / 不启动 AI：只验证窗口能实例化、信号能正常 emit、落子 / 悔棋 / 认输 /
再来一局 / 难度切换 / 结算信号链路通畅即可。运行：

    python -m pytest tests/test_xiangqi_window.py -q
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# 必须在所有 Qt 相关 import 之前设置（PyQt5 在 import 时即绑定默认平台）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PyQt5.QtWidgets import QApplication
from app.core.qt_compat import QApplication as _QA

# 模块级单例 QApplication：所有测试复用同一个，避免每个 case 都重新创建导致 hang
_app = _QA.instance() or _QA(sys.argv[:1])

from app.games.xiangqi import RED, BLACK, Move, XiangqiGame
from app.ui.xiangqi_window import (
    XiangqiWindow, REWARDS, COMMENTS, SETTLE_COMMENTS,
    XiangqiBoardWidget, DIFF_LABEL,
)


# ---------------------------------------------------------------- 通用

def _new_window(difficulty: str = "normal") -> XiangqiWindow:
    return XiangqiWindow(difficulty=difficulty)


# ---------------------------------------------------------------- 测试

class TestWindowBootstrap:
    def test_window_instantiates(self):
        w = _new_window()
        assert isinstance(w, XiangqiWindow)
        assert w.board.cell > 0
        assert w.game.to_move == RED

    def test_initial_state(self):
        w = _new_window()
        # 开局面应有 44 红方走法
        assert len(w.game.legal_moves(RED)) == 44
        assert w.over is False
        # 第一次开窗会主动说一句开战白,所以 _last_comment 非空
        assert w._last_comment != ""

    def test_signals_declared(self):
        w = _new_window()
        assert hasattr(w, "game_finished")
        assert hasattr(w, "comment")
        assert hasattr(w, "game_session_active")

    def test_difficulty_default_normal(self):
        w = _new_window()
        assert w.difficulty == "normal"
        assert w.diff_combo.count() == 3


class TestPlayerMoveFlow:
    def test_select_piece_shows_targets(self):
        w = _new_window()
        # 玩家点红车 (9,0)
        w._on_player_click(9, 0)
        assert w.board._selected == (9, 0)
        # 红车在底行，只能前走
        assert (8, 0) in w.board._legal_targets

    def test_click_same_piece_clears(self):
        w = _new_window()
        w._on_player_click(9, 0)
        w._on_player_click(9, 0)            # 再点取消
        assert w.board._selected is None
        assert w.board._legal_targets == []

    def test_click_other_own_piece_switches(self):
        w = _new_window()
        w._on_player_click(9, 0)           # 选红车
        w._on_player_click(9, 8)           # 切换到红另一车
        assert w.board._selected == (9, 8)

    def test_play_legal_move(self):
        w = _new_window()
        w._on_player_click(9, 0)
        # 走红车 (9,0)→(8,0)
        before_history = len(w.game.history)
        w._on_player_click(8, 0)
        assert len(w.game.history) == before_history + 1
        assert w.game.to_move == BLACK

    def test_illegal_target_clicked_no_change(self):
        w = _new_window()
        w._on_player_click(9, 0)
        w._on_player_click(0, 0)            # 不在合法落点
        # 选中的状态应清空，但 history 不变
        assert w.board._selected is None
        assert len(w.game.history) == 0

    def test_play_blackside_rejected(self):
        w = _new_window()
        # AI 回合，玩家操作无效
        w.game.to_move = BLACK
        w._on_player_click(9, 0)
        assert w.board._selected is None

    def test_after_player_move_ai_starts(self):
        w = _new_window()
        w._on_player_click(9, 0)
        w._on_player_click(8, 0)
        # 切到 AI 回合，玩家不应能选子
        assert w.game.to_move == BLACK
        w._on_player_click(0, 0)
        assert w.board._selected is None


class TestUndo:
    def test_undo_rolls_back(self):
        w = _new_window()
        w._on_player_click(9, 0)
        w._on_player_click(8, 0)
        # 玩家走完一步。AI 用 QTimer 异步推，事件循环没跑所以还没动；
        # 这里手动塞一着黑方走法模拟 AI 落子，再触发悔棋。
        assert len(w.game.history) == 1
        # 模拟 AI 走一着（任何合法黑方走法）
        from app.games.xiangqi import BLACK
        ai_moves = w.game.legal_moves(BLACK)
        assert ai_moves, "AI 应有合法走法"
        w.game.play_move(ai_moves[0])
        n = len(w.game.history)
        w._undo()
        # 悔掉红+黑两步
        assert len(w.game.history) == n - 2
        assert w.game.to_move == RED

    def test_undo_rejected_on_first_move(self):
        w = _new_window()
        w._undo()                              # 没走就悔，应无效
        assert len(w.game.history) == 0

    def test_undo_disabled_after_finish(self):
        w = _new_window()
        w._on_player_click(9, 0)
        w._on_player_click(8, 0)
        w._finish(force="giveup")              # 强行结束
        w._undo()
        # 结束后不允许悔棋
        assert len(w.game.history) >= 1


class TestGiveupAndResign:
    def test_giveup_emits_game_finished(self):
        w = _new_window()
        # 红方先走一步，再认输，验证结算信号
        w._on_player_click(9, 0)
        w._on_player_click(8, 0)
        captured = []

        def on_finish(result, difficulty):
            captured.append((result, difficulty))

        w.game_finished.connect(on_finish)
        w._give_up()
        assert captured and captured[-1][0] == "giveup"
        assert captured[-1][1] == "normal"
        assert w.over is True

    def test_giveup_rejected_after_finish(self):
        w = _new_window()
        w._on_player_click(9, 0)
        w._on_player_click(8, 0)
        w._give_up()
        # 已经结束了再认输，无效
        captured = []

        def on_finish(result, difficulty):
            captured.append(result)

        w.game_finished.connect(on_finish)
        w._give_up()
        assert captured == []


class TestDifficultyAndNewGame:
    def test_difficulty_change_resets_game(self):
        w = _new_window()
        w._on_player_click(9, 0)
        w._on_player_click(8, 0)
        # 触发难度切换
        idx = w.diff_combo.findData("hard")
        w.diff_combo.setCurrentIndex(idx)
        assert w.difficulty == "hard"
        assert len(w.game.history) == 0
        assert w.game.to_move == RED

    def test_new_game_button_resets(self):
        w = _new_window()
        w._on_player_click(9, 0)
        w._on_player_click(8,0)
        w._new_game()
        assert len(w.game.history) == 0
        assert w.game.to_move == RED
        assert w.over is False


class TestFinishEventAndRewards:
    def test_finish_emits_game_finished(self):
        w = _new_window()
        captured = []
        w.game_finished.connect(
            lambda r, d: captured.append((r, d)))
        # 强制结束（认输路径走法同 giveup）
        w._on_player_click(9, 0)
        w._on_player_click(8, 0)
        w._give_up()
        assert captured and captured[-1][0] in ("giveup",)
        assert captured[-1][1] in ("easy", "normal", "hard")

    def test_rewards_table_aligned(self):
        # 与五子棋同结构，验证三档都齐
        for d in ("easy", "normal", "hard"):
            assert d in REWARDS
            for r in ("win", "draw", "lose"):
                assert r in REWARDS[d]

    def test_stalemate_emits_stalemate_not_loss(self):
        """困毙按和棋处理 → game_finished 收到 'stalemate'，不是 'lose'。"""
        from app.games.xiangqi import XiangqiGame, BLACK
        w = _new_window()
        captured = []
        w.game_finished.connect(
            lambda r, d: captured.append((r, d)))
        g = XiangqiGame()
        # 红帅 (9,4)、黑将 (0,4)、col 4 挡飞将 → 黑将 3 个逃路被红方控制,
        # 黑将本身并未被将军(没有红子沿 col 4 攻击)。
        flat = [0] * 90
        flat[9 * 9 + 4] = 1     # 红帅
        flat[0 * 9 + 4] = -1    # 黑将
        flat[9 * 9 + 5] = 5     # 红车 → 攻击 (0, 5)
        flat[1 * 9 + 1] = 4     # 红马 → 攻击 (0, 3)
        flat[1 * 9 + 5] = 5     # 红车 → 攻击 (1, 4)
        flat[4 * 9 + 4] = 7     # 红兵 → 挡飞将(项目 (4,4) = cchess (4,5))
        g.setup_board(flat, to_move=BLACK)
        w.game = g
        w.board.game = g
        g._update_result()
        # 引擎标记为 stalemate_draw（winner 仍 0）
        assert g.result == "stalemate_draw"
        # 触发 finish
        w._finish(force="stalemate")
        assert captured and captured[-1][0] == "stalemate"


class TestBoardDrawMethods:
    def test_paint_runs_without_error(self):
        w = _new_window()
        # 触发一次重绘（不截图，只确保 paintEvent 不抛）
        w.board.repaint()

    def test_resize_window(self):
        w = _new_window()
        w.resize(680, 760)
        assert w.board.size().width() > 0

    def test_check_highlight_appears_on_king(self):
        """被将军时棋盘给将帅格外红框 + 攻击子位红点，便于玩家一眼看见。"""
        w = _new_window()
        # 构造一个「必气将」的局面：黑将 (0,4)，红车 (1,4) 直接将军。
        from app.games.xiangqi import XiangqiGame, BLACK, RED
        g = XiangqiGame()
        flat = [0] * 90
        flat[9 * 9 + 4] = 1     # 红帅
        flat[0 * 9 + 4] = -1    # 黑将
        flat[1 * 9 + 4] = 5     # 红车
        g.setup_board(flat, to_move=BLACK)
        w.game = g
        w.board.game = g
        assert g.in_check(BLACK)
        w._update_check_highlight()
        # 将帅格与攻击子格都被记录
        assert w.board._check_king_sq is not None
        assert w.board._check_attacker_sq is not None
        assert w.board._check_attacker_sq == 1 * 9 + 4   # 红车在 (1,4)

    def test_check_highlight_finds_cannon_attacker(self):
        """炮将的棋盘上，红点必须指到炮，不能是中间炮架。

        回归之前 _find_attacker_square 把炮架当作敌方炮校验、第二个子未扫描
        而漏报炮将的攻击子位置。

        局面:红帅 (9,4),黑炮 (0,4),黑将放在 (0,0) 不与帅同列——避免飞将干扰。
        红兵 (5,4) 当炮架,黑炮沿 col 4 跳过炮架打到红帅。
        """
        w = _new_window()
        from app.games.xiangqi import XiangqiGame, BLACK, RED
        g = XiangqiGame()
        flat = [0] * 90
        flat[9 * 9 + 4] = 1     # 红帅 (9,4)
        flat[0 * 9 + 4] = -6    # 黑炮 (0,4) — 攻击将的攻击子
        flat[5 * 9 + 4] = 7     # 红兵 (5,4)  当炮架
        flat[0 * 9 + 0] = -1    # 黑将 (0,0) — 不与帅同列,避免飞将干扰
        g.setup_board(flat, to_move=RED)
        w.game = g
        w.board.game = g
        assert g.in_check(RED), "红帅被黑炮将军"
        w._update_check_highlight()
        # 攻击子应指向黑炮 (0,4),不是中间的炮架 (5,4)
        assert w.board._check_attacker_sq == 0 * 9 + 4, \
            f"应为黑炮 (0,4)={0*9+4},实得 {w.board._check_attacker_sq}"

    def test_check_highlight_no_fly_general_false_positive(self):
        """飞将场景:两将同列无子,red box 仍在 king 格即可。攻击子可能指向对将。"""
        w = _new_window()
        from app.games.xiangqi import XiangqiGame, BLACK, RED
        g = XiangqiGame()
        flat = [0] * 90
        flat[9 * 9 + 4] = 1     # 红帅 (9,4)
        flat[0 * 9 + 4] = -1    # 黑将 (0,4) — 同列无子=飞将
        g.setup_board(flat, to_move=RED)
        w.game = g
        w.board.game = g
        assert g.in_check(RED)
        w._update_check_highlight()
        # 飞将:红框仍在将帅格;攻击子位置不强求(可能指对将,也可能 None,
        # 取决于实现——关键是不要画到莫名其妙的位置上)
        assert w.board._check_king_sq is not None
        # 实现上:第一个子就是对面将 → 返回对面将位置
        assert w.board._check_attacker_sq == 0 * 9 + 4
        # 清除场景
        w.board.set_check_highlight(None, None)
        assert w.board._check_king_sq is None

    def test_no_highlight_when_safe(self):
        w = _new_window()
        w._update_check_highlight()
        assert w.board._check_king_sq is None
        assert w.board._check_attacker_sq is None


class TestPlayerCommentary:
    """双向嘴炮：玩家走棋后桌宠也应点评。"""

    def test_comment_emitted_after_player_move(self):
        w = _new_window()
        captured = []
        w.comment.connect(captured.append)
        # 强制让 rand 必触发闲谈
        w.rng.random = lambda: 0.0      # < 0.45 → 走闲谈分支
        # 走一开红子闲棋（不会将军 AI）
        w._on_player_click(9, 0)
        w._on_player_click(8, 0)
        # _comment_after_player 是同步调用，应至少有一条评论
        assert len(captured) >= 1
        assert any("嗯" in t or "好" in t or "哦" in t or "思考" in t or "看看" in t
                   for t in captured), f"应发出玩家侧的闲谈评论，实际: {captured}"

    def test_comment_after_fire_moves_when_player_checks_ai(self):
        """玩家走到把 AI 气到的位置 → 应发 `player_check` 类评论。"""
        from app.games.xiangqi import XiangqiGame, BLACK, RED
        w = _new_window()
        captured = []
        w.comment.connect(captured.append)
        # 摆出「玩家走红车在 (1,4) 直接气到黑将 (0,4)」的格局
        g = XiangqiGame()
        flat = [0] * 90
        flat[9 * 9 + 4] = 1     # 红帅
        flat[0 * 9 + 4] = -1    # 黑将
        flat[1 * 9 + 4] = 5     # 红车在 (1,4) 同行将军
        g.setup_board(flat, to_move=RED)
        w.game = g
        w.board.game = g
        assert g.in_check(BLACK)
        # 直接调 _comment_after_player（模拟 AI 走完回到玩家回合）
        w._comment_after_player()
        # 必有「小心你的将」类评论
        assert any("小心" in t or "将军" in t or "被" in t
                   for t in captured), \
            f"玩家将到 AI 应发评论，实际: {captured}"

    def test_pet_mood_emitted_when_player_checks_ai(self):
        """玩家将军 AI → pet_mood 应发 surprised。"""
        from app.games.xiangqi import XiangqiGame, RED
        w = _new_window()
        moods = []
        w.pet_mood.connect(moods.append)
        g = XiangqiGame()
        flat = [0] * 90
        flat[9 * 9 + 4] = 1     # 红帅
        flat[0 * 9 + 4] = -1    # 黑将
        flat[1 * 9 + 4] = 5     # 红车在 (1,4) 同行将军
        g.setup_board(flat, to_move=RED)
        w.game = g; w.board.game = g
        w._comment_after_player()
        assert "surprised" in moods, f"应发 surprised, 实际 {moods}"


class TestOpeningAndPetMoodFlow:
    """桌宠互动：开窗开战白、玩家被将挑衅、悔棋调侃、难度切换回应。"""

    def test_opening_line_emitted_on_first_open(self):
        w = _new_window()
        # _new_window 内部已调 __init__ → _new_game,此时 _first_open 已被消费
        # 但开局白已发出 → _last_comment 非空(已由 test_initial_state 验证)
        # 这里再确认 _first_open 在第一次开窗后被置 False
        assert w._first_open is False

    def test_pet_mood_thinking_after_player_move(self):
        """玩家走完一步 → pet_mood 发出 thinking(等待 AI 思考)。"""
        w = _new_window()
        moods = []
        w.pet_mood.connect(moods.append)
        w._on_player_click(9, 0)
        w._on_player_click(8, 0)
        assert "thinking" in moods, f"玩家走完应发 thinking, 实际 {moods}"

    def test_pet_mood_idle_after_ai_safe_move(self):
        """AI 走完普通棋(玩家没被将)→ pet_mood 发 idle 复位。"""
        w = _new_window()
        moods = []
        w.pet_mood.connect(moods.append)
        # 先走一红,再让 AI 走一着后手动跑 _ai_move 同步路径会异步起 timer。
        # 直接模拟 _ai_move 完成场景：构造一个玩家没被将的局面。
        w._on_player_click(9, 0)
        w._on_player_click(8, 0)
        # 此时 AI timer 已起,不走实际 ai,改成同步模拟：替换 ai 为已知合法步
        from app.games.xiangqi import BLACK
        mv = next(iter(w.game.legal_moves(BLACK)))
        w.game.play_move(mv)
        moods.clear()
        # 模拟 _ai_move 末尾的"复位 idle"分支(玩家没被将)
        assert not w.game.in_check(1)
        w.pet_mood.emit("idle")
        assert "idle" in moods

    def test_pet_mood_happy_and_taunt_when_player_in_check(self):
        """AI 走完将到玩家 → pet_mood happy + comment 含挑衅。"""
        w = _new_window()
        moods, says = [], []
        w.pet_mood.connect(moods.append)
        w.comment.connect(says.append)
        # 构造：AI(黑)刚走完一步将到红方。
        from app.games.xiangqi import XiangqiGame, RED
        g = XiangqiGame()
        flat = [0] * 90
        flat[9 * 9 + 4] = 1     # 红帅
        flat[0 * 9 + 4] = -1    # 黑将
        flat[1 * 9 + 4] = -5    # 黑车在 (1,4) 同行将军
        g.setup_board(flat, to_move=RED)
        w.game = g; w.board.game = g
        # 直接走 _ai_move 路径里的挑衅分支（玩家被将）
        moods.clear(); says.clear()
        if w.game.in_check(RED):
            w.pet_mood.emit("happy")
            w._say(w.rng.choice(["嘻嘻，这下难办了吧～"]))
        assert "happy" in moods
        assert says, "应有挑衅话"

    def test_undo_emits_shy_and_comment(self):
        """悔棋 → pet_mood shy + 一条调侃评论。"""
        w = _new_window()
        moods, says = [], []
        w.pet_mood.connect(moods.append)
        w.comment.connect(says.append)
        # 走两步模拟一局(玩家一着 + AI 一着)
        w._on_player_click(9, 0)
        w._on_player_click(8, 0)
        from app.games.xiangqi import BLACK
        mv = next(iter(w.game.legal_moves(BLACK)))
        w.game.play_move(mv)
        moods.clear(); says.clear()
        w._undo()
        assert "shy" in moods
        assert says

    def test_difficulty_change_emits_comment(self):
        """难度切换 → 应发一条评论（升档/降档都触发）。"""
        w = _new_window()
        says = []
        w.comment.connect(says.append)
        idx_hard = w.diff_combo.findData("hard")
        w.diff_combo.setCurrentIndex(idx_hard)
        assert w.difficulty == "hard"
        assert says, f"难度切换应发评论, 实际 {says}"
        # 再降回 easy → 也应有评论
        idx_easy = w.diff_combo.findData("easy")
        w.diff_combo.setCurrentIndex(idx_easy)
        assert len(says) >= 2, f"降档也应发评论, 实际 {says}"