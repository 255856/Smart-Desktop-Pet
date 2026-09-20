# -*- coding: utf-8 -*-
"""桌宠内置小游戏（纯逻辑在各模块，UI 在 app/ui 下）。"""
from .gomoku import (
    BLACK, WHITE, EMPTY, SIZE, DIFFICULTIES,
    GomokuGame, GomokuAI, evaluate_point, analyze_move,
)

__all__ = [
    "BLACK", "WHITE", "EMPTY", "SIZE", "DIFFICULTIES",
    "GomokuGame", "GomokuAI", "evaluate_point", "analyze_move",
]
