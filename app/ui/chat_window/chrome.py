"""chat_window 窗口装饰辅助类。"""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.qt_compat import (
    QCursor, QEvent, QObject, QPoint, Qt, QToolTip,
    event_global_pos,
)
from app.ui.window_drag import TitleBarDrag

if TYPE_CHECKING:
    from app.ui.chat_window import ChatWindow


# 标题栏拖动已抽到 app.ui.window_drag 供弹窗外壳共用，这里保留旧名
_TitleBarDrag = TitleBarDrag


class _AvatarHoverHook(QObject):
    """鼠标悬浮头像时弹 tooltip 显示桌宠状态（迷你进度条版）。

    之前是 stats_summary() 一行文字（"Lv.3 体力 80 饱食 60 ..."），密度低；
    现在每条状态都配一条迷你进度条（10 格 unicode 块字符），一眼能扫完。

    QToolTip 默认按 Qt::AutoText 渲染 HTML, 所以 <font color=...> 和
    <b>/<small> 都直接生效 —— 不用 QTextDocument。

    不吞事件 (返回 False) —— 让 _TitleBarDrag 仍然能拖动标题栏。
    """

    # 调色板: 跟 setting/chat 窗口的 mood 色带 (ui_style.MOOD_*) 保持一致
    _BAR_FULL = "#e89aae"   # 樱粉 (accent)
    _BAR_EMPTY = "#ebe3d6"   # 浅米 (border)
    _LABEL = "#5a4a36"
    _DIM = "#9a9aac"
    _GOOD = "#5aa845"
    _WARN = "#c2521a"

    def __init__(self, chat_window: "ChatWindow"):
        super().__init__(chat_window)
        self._cw = chat_window

    def eventFilter(self, obj, ev) -> bool:
        t = ev.type()
        if t == QEvent.Type.Enter:
            text = self._build_tip()
            if text:
                QToolTip.showText(QCursor.pos(), text, obj)
        elif t == QEvent.Type.Leave:
            QToolTip.hideText()
        return False

    def _build_tip(self) -> str:
        state = self._get_state()
        if state is None:
            return ""
        try:
            q = state.quantize()
        except Exception:  # noqa: BLE001
            q = {}
        mood = q.get("mood", "MID")
        mood_text = {"LOW": "低落", "MID": "一般", "HIGH": "开心"}.get(mood, "一般")
        mood_color = {"LOW": self._WARN, "MID": self._DIM, "HIGH": self._GOOD}.get(
            mood, self._DIM)
        name = self._get_name()
        try:
            lv = state.cal_level()
            money = int(state.money)
        except Exception:  # noqa: BLE001
            lv = 0
            money = 0

        # 迷你进度条: 10 格, ■ 已填 / □ 空。颜色按当前值着色 (低值偏红/警示色)
        def bar(pct: float, color: str = "") -> str:
            pct = max(0.0, min(100.0, float(pct)))
            n = 10
            filled = int(round(pct / 100.0 * n))
            full = "■" * filled
            empty = "□" * (n - filled)
            # 已填部分用 accent 粉, 空用浅米
            full_color = color or self._BAR_FULL
            return (
                f"<font color='{full_color}'>{full}</font>"
                f"<font color='{self._BAR_EMPTY}'>{empty}</font>")

        try:
            liking_max = int(state.likability_max)
            liking = int(state.likability)
        except Exception:  # noqa: BLE001
            liking_max = 100
            liking = 0

        # 用 <br> 换行; <small> 给副标题/副信息小字号
        rows = [
            f"<b style='color:#5a4a36'>{name}</b>"
            f"  <span style='color:{self._LABEL}'>Lv.{lv}</span>"
            f"  <span style='color:{self._LABEL}'>金币 {money}</span>",
            f"<small style='color:{mood_color}'>心情 {mood_text}</small>",
            f"体力 {bar(state.strength)} "
            f"<small style='color:{self._DIM}'>{int(state.strength)}/100</small>",
            f"饱食 {bar(state.strength_food)} "
            f"<small style='color:{self._DIM}'>{int(state.strength_food)}/100</small>",
            f"饮水 {bar(state.strength_drink)} "
            f"<small style='color:{self._DIM}'>{int(state.strength_drink)}/100</small>",
            f"健康 {bar(state.health)} "
            f"<small style='color:{self._DIM}'>{int(state.health)}/100</small>",
            f"好感 {bar(liking / max(1, liking_max) * 100)} "
            f"<small style='color:{self._DIM}'>{liking}/{liking_max}</small>",
        ]
        return "<br>".join(rows)

    def _get_state(self):
        try:
            sp = getattr(self._cw, "_state_provider", None)
            if not callable(sp):
                return None
            return sp()
        except Exception:  # noqa: BLE001
            return None

    def _get_name(self) -> str:
        try:
            char_cfg = getattr(self._cw, "char_cfg", None)
            if char_cfg is not None and getattr(char_cfg, "name", ""):
                return char_cfg.name
        except Exception:  # noqa: BLE001
            pass
        return "桌宠"


__all__ = ["_TitleBarDrag", "_AvatarHoverHook"]