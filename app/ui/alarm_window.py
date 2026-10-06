"""闹钟 / 倒计时到点的置顶提醒窗。

为什么需要它：桌宠本体只是屏幕角落一个小窗口，主人忙起来根本注意不到。
到点时必须有一个**抢注意力**的东西——置顶 + 循环提示音 + 语音播报，
并且在主人点「知道了」之前一直赖着不走。

同一时刻只显示一个：新的到点会顶掉旧的（多个提醒同时响只会变成噪音）。
"""
from __future__ import annotations

import logging

from app.core.qt_compat import (
    QFrame, QHBoxLayout, QLabel, QObject, QPushButton, QTimer, QVBoxLayout,
    QWidget, Qt, Signal,
)
from app.ui.ui_style import ACCENT, ACCENT_BG, BORDER, CARD, FONT, TEXT, TEXT_SUB
from app.voice.alarm_sound import AlarmSound, available as sound_available

log = logging.getLogger(__name__)

# 稍后提醒的默认延后（毫秒）
SNOOZE_MS = 5 * 60 * 1000

# 报警窗开着、主人还没确认时的再响间隔（毫秒）
RERING_INTERVAL_MS = 30 * 1000

# 主人一直不来点，自动收起前最多响多久（毫秒）
AUTO_CLOSE_MS = 5 * 60 * 1000

_QSS = f"""
QWidget#alarmRoot {{
    background: {CARD};
    border: 2px solid {ACCENT};
    border-radius: 16px;
}}
QLabel#alarmIcon {{ font-size: 46px; background: transparent; }}
QLabel#alarmTitle {{
    font-family: {FONT};
    font-size: 20px;
    font-weight: 600;
    color: {TEXT};
    background: transparent;
}}
QLabel#alarmText {{
    font-family: {FONT};
    font-size: 14px;
    color: {TEXT_SUB};
    background: transparent;
}}
QFrame#alarmSep {{ background: {BORDER}; border: none; }}
QPushButton#alarmPrimary {{
    font-family: {FONT};
    font-size: 14px;
    font-weight: 600;
    color: #ffffff;
    background: {ACCENT};
    border: none;
    border-radius: 9px;
    padding: 9px 22px;
}}
QPushButton#alarmPrimary:hover {{ background: #6a58e0; }}
QPushButton#alarmPrimary:pressed {{ background: #5a49c8; }}
QPushButton#alarmGhost {{
    font-family: {FONT};
    font-size: 13px;
    color: {TEXT_SUB};
    background: {ACCENT_BG};
    border: none;
    border-radius: 9px;
    padding: 9px 18px;
}}
QPushButton#alarmGhost:hover {{ color: {TEXT}; }}
"""


class AlarmWindow(QWidget):
    """到点弹出的置顶提醒窗。"""

    dismissed = Signal()          # 主人点了「知道了」/ 关掉
    snoozed = Signal(int)         # 稍后提醒，参数是延后毫秒数

    def __init__(self, text: str = "", kind: str = "提醒", parent=None):
        super().__init__(parent)
        self.setObjectName("alarmRoot")
        self.setWindowFlags(
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(400, 236)

        self._sound = AlarmSound()
        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(self._on_auto_close)
        # 没被确认就每 30 秒再响一轮 —— 这才是闹钟该有的行为。
        # 只响一次的话，主人刚好在敲键盘/戴耳机就会彻底错过。
        self._rering = QTimer(self)
        self._rering.setInterval(RERING_INTERVAL_MS)
        self._rering.timeout.connect(self._on_rering)

        self._build(text, kind)

    # ---------------------------------------------------------------- UI
    def _build(self, text: str, kind: str) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(10)

        head = QHBoxLayout()
        head.setSpacing(12)
        icon = QLabel("⏰")
        icon.setObjectName("alarmIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        head.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(4)
        title = QLabel(kind)
        title.setObjectName("alarmTitle")
        col.addWidget(title)

        body = QLabel(text or "（没有内容）")
        body.setObjectName("alarmText")
        body.setWordWrap(True)
        col.addWidget(body)
        head.addLayout(col, 1)
        root.addLayout(head)

        sep = QFrame()
        sep.setObjectName("alarmSep")
        sep.setFixedHeight(1)
        root.addWidget(sep)

        btns = QHBoxLayout()
        btns.setSpacing(10)
        if sound_available():
            self.snooze_btn = QPushButton("稍后提醒 5 分钟")
            self.snooze_btn.setObjectName("alarmGhost")
            self.snooze_btn.clicked.connect(self._on_snooze)
            btns.addWidget(self.snooze_btn)
        else:
            self.snooze_btn = None
        btns.addStretch(1)
        ok = QPushButton("知道了")
        ok.setObjectName("alarmPrimary")
        ok.setDefault(True)
        ok.clicked.connect(self._on_ok)
        btns.addWidget(ok)
        root.addLayout(btns)

        self.setStyleSheet(_QSS)

    # ------------------------------------------------------------ 行为
    def show_alarm(self, text: str, kind: str = "提醒",
                   *, sound: bool = True) -> None:
        """（重新）显示一次到点提醒。"""
        title = self.findChild(QLabel, "alarmTitle")
        body = self.findChild(QLabel, "alarmText")
        if title is not None:
            title.setText(kind)
        if body is not None:
            body.setText(text or "（没有内容）")
        self._close_timer.stop()

        self.show()
        self.raise_()
        self.activateWindow()
        if sound:
            self._sound.start()
            # 开着窗就周期性再响，直到主人点掉
            self._rering.start()
        else:
            self._rering.stop()
        # 主人一直不来点也不能永远赖着不走：5 分钟后自动收，
        # 期间已经反复响过，不存在「完全没提醒到」的风险。
        self._close_timer.start(AUTO_CLOSE_MS)

    def _on_rering(self) -> None:
        if self.isVisible():
            self._sound.start()

    def _on_ok(self) -> None:
        self.dismissed.emit()
        self.close()

    def _on_snooze(self) -> None:
        self.snoozed.emit(SNOOZE_MS)
        self.close()

    def _on_auto_close(self) -> None:
        log.info("报警窗超时未确认，自动收起（期间已反复响铃）")
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._close_timer.stop()
        self._rering.stop()
        self._sound.stop()
        try:
            super().closeEvent(event)
        except Exception:  # noqa: BLE001
            event.accept()


class AlarmPresenter(QObject):
    """到点提醒的总入口：管窗口 + 提示音 + 语音播报。

    放在 UI 层，只暴露一个 `fire(text, kind)`。同刻到点会顶掉上一个。
    """

    def __init__(self, speak_fn=None, parent=None):
        super().__init__(parent)
        self._speak = speak_fn          # type: ignore[assignment]
        self._win: AlarmWindow | None = None
        # 「稍后提醒」的重响计划：(延后毫秒, 原文, 类型)
        self._snooze: QTimer | None = None
        self._pending: tuple[int, str, str] | None = None

    @property
    def window(self) -> AlarmWindow | None:
        return self._win

    def is_showing(self) -> bool:
        w = self._win
        return bool(w and w.isVisible())

    def has_pending_snooze(self) -> bool:
        return bool(self._snooze and self._snooze.isActive())

    def fire(self, text: str, kind: str = "提醒", *, sound: bool = True,
             speak: bool = True) -> None:
        """到点：置顶窗 + 提示音 + TTS 播报。

        speak 走注入的 `speak_fn`（UIController 里绑 VoiceManager），
        这样本类不依赖语音模块，测试也能直接跑。
        """
        if not (text or "").strip():
            text = "时间到啦~"
        # 新的到点顶掉旧的重响计划，别攒出一串连环提醒
        self._cancel_snooze()
        if self._win is None:
            self._win = AlarmWindow(parent=None)
            self._win.dismissed.connect(self._on_dismissed)
            self._win.snoozed.connect(self._on_snooze)
        # 记下本次内容，「稍后提醒」才知道要重放什么
        self._pending = (0, text, kind)
        self._win.show_alarm(text, kind, sound=sound)
        if speak and callable(self._speak):
            try:
                self._speak(f"{kind}啦~ {text}")
            except Exception:  # noqa: BLE001
                log.warning("闹钟 TTS 播报失败", exc_info=True)

    def _on_snooze(self, ms: int) -> None:
        """稍后提醒：原样再响一次。

        没有这一步的话，「稍后提醒」按钮就是个装饰——信号发出去没人收。
        """
        if not self._pending:
            return
        self._schedule_snooze(ms, self._pending[1], self._pending[2])

    def _schedule_snooze(self, ms: int, text: str, kind: str) -> None:
        self._cancel_snooze()
        self._pending = (ms, text, kind)
        t = QTimer(self)
        t.setSingleShot(True)
        t.setInterval(max(1000, int(ms)))
        t.timeout.connect(lambda: self._on_snooze_fire())
        t.start()
        self._snooze = t
        log.info("已安排 %.0f 秒后再提醒：%s", ms / 1000, text[:40])

    def _on_snooze_fire(self) -> None:
        _, text, kind = self._pending or (0, "", "提醒")
        log.info("稍后提醒到点：%s", text)
        self._snooze = None
        self._pending = None
        self.fire(text, kind=kind, sound=True, speak=True)

    def _cancel_snooze(self) -> None:
        if self._snooze is not None:
            self._snooze.stop()
            self._snooze = None
        self._pending = None

    def dismiss(self) -> None:
        self._cancel_snooze()
        if self._win is not None:
            self._win.close()

    def _on_dismissed(self) -> None:
        log.info("报警窗已被主人确认")

    def shutdown(self) -> None:
        self._cancel_snooze()
        if self._win is not None:
            self._win.close()
            self._win = None


__all__ = ["AlarmWindow", "AlarmPresenter", "SNOOZE_MS"]
