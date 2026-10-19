"""Qt 兼容垫片：让代码同时支持 PySide6 和 PyQt5。"""
from __future__ import annotations

try:
    from PyQt5 import QtCore
    from PyQt5.QtCore import (
        pyqtSignal as Signal, pyqtSlot as Slot, pyqtSlot, QObject, QThread, QTimer, QSize, QPoint, QRect, Qt,
        QEventLoop, QStringListModel, QEvent, QUrl, QBuffer, QByteArray, QSettings,
        QMimeData,
    )
    from PyQt5.QtCore import Qt as _Qt
    if not hasattr(Qt, "AlignmentFlag"):
        Qt.AlignmentFlag = _Qt
    if not hasattr(Qt, "AspectRatioMode"):
        Qt.AspectRatioMode = _Qt
    if not hasattr(Qt, "CursorShape"):
        Qt.CursorShape = _Qt
    if not hasattr(Qt, "MouseButton"):
        Qt.MouseButton = _Qt
    if not hasattr(Qt, "WidgetAttribute"):
        Qt.WidgetAttribute = _Qt
    if not hasattr(Qt, "WindowType"):
        Qt.WindowType = _Qt
    if not hasattr(Qt, "TransformationMode"):
        Qt.TransformationMode = _Qt
    if not hasattr(Qt, "Orientation"):
        Qt.Orientation = _Qt
    if not hasattr(Qt, "KeyboardModifier"):
        Qt.KeyboardModifier = _Qt
    if not hasattr(Qt, "Key"):
        Qt.Key = _Qt
    if not hasattr(Qt, "CaseSensitivity"):
        Qt.CaseSensitivity = _Qt
    if not hasattr(Qt, "MatchFlag"):
        Qt.MatchFlag = _Qt
    if not hasattr(Qt, "PenStyle"):
        Qt.PenStyle = _Qt
    if not hasattr(Qt, "BrushStyle"):
        Qt.BrushStyle = _Qt

    from PyQt5.QtGui import (
        QColor, QCursor, QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent,
        QDropEvent, QFont, QGuiApplication, QIcon, QImage, QKeyEvent, QKeySequence,
        QMouseEvent, QPainter, QPainterPath, QLinearGradient, QPaintEvent, QPixmap,
        QRegion, QTextCursor, QTextDocument, QTransform,
        QPen, QBrush, QRadialGradient, QWheelEvent, QFontMetrics,
    )
    from PyQt5.QtWidgets import QAction
    from PyQt5.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QCompleter, QDoubleSpinBox, QFormLayout, QFrame,
        QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QDialog, QDialogButtonBox,
        QFileDialog, QInputDialog, QLayout, QListWidget, QListWidgetItem, QMenu,
        QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSlider,
        QSpinBox, QSplitter, QStatusBar, QSystemTrayIcon, QTabWidget, QTextBrowser,
        QToolButton, QToolTip, QVBoxLayout, QWidget, QGraphicsDropShadowEffect, QScrollArea,
    )
    if not hasattr(QtCore, "QMimeData"):
        from PyQt5.QtCore import QMimeData as _QM
        QtCore.QMimeData = _QM
    _BACKEND = "PyQt5"

except ImportError:
    # PySide6 fallback（开发环境备用）
    from PySide6 import QtCore
    from PySide6.QtCore import Signal, Slot, QObject, QThread, QTimer, QSize, QPoint, QRect, Qt, QEventLoop, QEvent, QUrl, QBuffer, QByteArray, QSettings, QMimeData
    from PySide6.QtCore import Slot as pyqtSlot
    from PySide6.QtGui import (
        QAction, QColor, QCursor, QDragEnterEvent, QDragLeaveEvent,
        QDragMoveEvent, QDropEvent, QFont, QGuiApplication, QIcon, QImage,
        QKeyEvent, QKeySequence, QMouseEvent, QPainter, QPainterPath, QLinearGradient,
        QPaintEvent, QPixmap, QRegion, QTextCursor, QTextDocument, QTransform,
        QPen, QBrush, QRadialGradient, QWheelEvent, QFontMetrics,
    )
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QCompleter, QDoubleSpinBox, QFormLayout, QFrame,
        QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QDialog, QDialogButtonBox,
        QFileDialog, QInputDialog, QLayout, QListWidget, QListWidgetItem, QMenu,
        QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSlider,
        QSpinBox, QSplitter, QStatusBar, QStringListModel, QSystemTrayIcon, QTabWidget, QTextBrowser,
        QToolButton, QToolTip, QVBoxLayout, QWidget, QGraphicsDropShadowEffect, QScrollArea,
    )
    _BACKEND = "PySide6"

BACKEND = _BACKEND


# Qt 6: QMouseEvent 有 .position() / .globalPosition()，返回 QPointF
# Qt 5: 仅有 .pos() / .globalPos()，返回 QPoint
# 业务代码需要跨版本稳定地取 event 里的位置、规范到 QPoint

def event_global_pos(evt) -> "QPoint":
    """跨 Qt5/Qt6 取鼠标事件的全局位置，统一返回 QPoint。"""
    try:
        return evt.globalPosition().toPoint()
    except AttributeError:
        return evt.globalPos()


def event_local_pos(evt) -> "QPoint":
    """跨 Qt5/Qt6 取鼠标事件的 widget-local 位置，返回 QPoint。"""
    try:
        return evt.position().toPoint()
    except AttributeError:
        return evt.pos()


def is_qobject_alive(obj) -> bool:
    """QObject 的 C++ 侧是否还活着。

    后台线程（如倒计时提醒线程、agent 的 to_thread 工具线程）可能在 QObject
    析构之后才回来调它的信号——再 emit 就是访问已释放内存，Windows 上会
    access violation 打崩整个进程。

    PyQt5 走 sip.isdeleted，PySide6 走 shiboken6.isValid；非 QObject 或
    后端没提供 API 时保守返回 True。
    """
    if obj is None:
        return False
    try:
        from PyQt5 import sip  # type: ignore
        if isinstance(obj, QObject):
            return not sip.isdeleted(obj)
        return True
    except ImportError:
        pass
    try:
        import shiboken6  # type: ignore
        return bool(shiboken6.isValid(obj))
    except ImportError:
        pass
    return True


__all__ = [
    "BACKEND", "Signal", "Slot", "pyqtSlot", "QObject", "QThread", "QTimer", "QSize", "QPoint", "QRect", "Qt",
    "QEventLoop", "QEvent", "QUrl", "QBuffer", "QByteArray", "QSettings",
    "QMimeData",
    "QAction", "QColor", "QCursor", "QFont", "QGuiApplication", "QIcon", "QImage",
    "QKeyEvent", "QKeySequence", "QMouseEvent", "QPainter", "QPainterPath", "QLinearGradient",
    "QPaintEvent", "QPixmap", "QTextCursor", "QTextDocument", "QTransform",
    "QPen", "QBrush", "QRadialGradient", "QWheelEvent", "QFontMetrics",
    "QApplication", "QCheckBox", "QComboBox", "QCompleter", "QDoubleSpinBox", "QFormLayout",
    "QFrame", "QGridLayout", "QGroupBox",
    "QHBoxLayout", "QLabel", "QLineEdit", "QDialog", "QDialogButtonBox", "QFileDialog",
    "QInputDialog", "QLayout", "QListWidget", "QListWidgetItem",
    "QMenu", "QMessageBox", "QPlainTextEdit", "QProgressBar", "QPushButton", "QRegion", "QSizePolicy",
    "QSlider", "QSpinBox", "QSplitter", "QStringListModel", "QSystemTrayIcon", "QTabWidget",
    "QTextBrowser", "QToolButton", "QToolTip", "QVBoxLayout", "QWidget", "QGraphicsDropShadowEffect",
    "QScrollArea",
    "QDragEnterEvent", "QDragLeaveEvent", "QDragMoveEvent", "QDropEvent",
    "event_global_pos", "event_local_pos", "is_qobject_alive",
]