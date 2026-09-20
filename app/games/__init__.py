# -*- coding: utf-8 -*-
"""桌宠内置小游戏（纯逻辑在各模块，UI 在 app/ui 下）。"""
from .gomoku import (
    BLACK, WHITE, EMPTY, SIZE, DIFFICULTIES,
    GomokuGame, GomokuAI, evaluate_point, analyze_move,
)
from .werewolf import (
    WerewolfGame, WOLF, SEER, WITCH, HUNTER, VILLAGER,
    CAMP_WOLF, CAMP_GOOD, ROLESET_9, ROLE_LABEL,
)

__all__ = [
    "BLACK", "WHITE", "EMPTY", "SIZE", "DIFFICULTIES",
    "GomokuGame", "GomokuAI", "evaluate_point", "analyze_move",
    # 狼人杀（纯逻辑；多 Agent / 窗口在 app.games.werewolf_director、app.ui.werewolf_window）
    "WerewolfGame", "WOLF", "SEER", "WITCH", "HUNTER", "VILLAGER",
    "CAMP_WOLF", "CAMP_GOOD", "ROLESET_9", "ROLE_LABEL",
]
