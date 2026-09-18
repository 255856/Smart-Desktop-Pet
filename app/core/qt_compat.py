"""Qt 兼容垫片：让代码同时支持 PySide6 和 PyQt5。

打包后（PyInstaller）使用 PyQt5（只有 PyQt5 插件被收集）。
开发环境下优先 PyQt5（因为 PySide6 的插件路径和 PyInstaller 不一致）。
"""
from __future__ import annotations

try:
    from PyQt5 import QtCore, QtGui, QtWidgets
    from PyQt5.QtCore import (
        pyqtSignal as Signal, pyqtSlot as Slot, QObject, QThread, QTimer, QSize, QPoint, QRect, Qt,
        QStringListModel, QEvent, QUrl, QBuffer, QByteArray,
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

    from PyQt5.QtGui import (
        QColor, QCursor, QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent,
        QDropEvent, QFont, QGuiApplication, QIcon, QImage, QKeyEvent, QKeySequence,
        QMouseEvent, QPainter, QPainterPath, QLinearGradient, QPaintEvent, QPixmap,
        QTextCursor, QTextDocument, QTransform,
    )
    from PyQt5.QtWidgets import QAction
    from PyQt5.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QCompleter, QDoubleSpinBox, QFormLayout, QFrame,
        QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QDialog, QDialogButtonBox,
        QFileDialog, QInputDialog, QListWidget, QListWidgetItem, QMenu,
        QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSlider,
        QSpinBox, QSplitter, QSystemTrayIcon, QTabWidget, QTextBrowser,
        QToolButton, QVBoxLayout, QWidget, QGraphicsDropShadowEffect, QScrollArea,
    )
    if not hasattr(QtCore, "QMimeData"):
        from PyQt5.QtCore import QMimeData as _QM
        QtCore.QMimeData = _QM
    _BACKEND = "PyQt5"

except ImportError:
    # PySide6 fallback（开发环境备用）
    from PySide6 import QtCore, QtGui, QtWidgets
    from PySide6.QtCore import Signal, Slot, QObject, QThread, QTimer, QSize, QPoint, QRect, Qt, QEvent, QUrl, QBuffer, QByteArray
    from PySide6.QtGui import (
        QAction, QColor, QCursor, QDragEnterEvent, QDragLeaveEvent,
        QDragMoveEvent, QDropEvent, QFont, QGuiApplication, QIcon, QImage,
        QKeyEvent, QKeySequence, QMouseEvent, QPainter, QPainterPath, QLinearGradient,
        QPaintEvent, QPixmap, QMimeData, QTextCursor, QTextDocument, QTransform,
    )
    from PySide6.QtWidgets import (
        QApplication, QCheckBox, QComboBox, QCompleter, QDoubleSpinBox, QFormLayout, QFrame,
        QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QDialog, QDialogButtonBox,
        QFileDialog, QInputDialog, QListWidget, QListWidgetItem, QMenu,
        QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QSlider,
        QSpinBox, QSplitter, QStringListModel, QSystemTrayIcon, QTabWidget, QTextBrowser,
        QToolButton, QVBoxLayout, QWidget, QGraphicsDropShadowEffect, QScrollArea,
    )
    _BACKEND = "PySide6"

BACKEND = _BACKEND


# ---------------- 鼠标事件坐标兼容垫 ----------------
# Qt 6 (PySide6 / PyQt6):  QMouseEvent 有 .position() / .globalPosition()，返回 QPointF
# Qt 5 (PyQt5):           仅有 .pos() / .globalPos()，返回 QPoint
# 业务代码里需要的是「event 里的位置」、规范到 QPoint —— 跨版本稳定的 helper：

def event_global_pos(evt) -> "QPoint":
    """跨 Qt5/Qt6 取鼠标事件的全局位置，统一返回 QPoint。

    调用：
        drag_start = event_global_pos(evt) - win.frameGeometry().topLeft()
    """
    # 优先 Qt6 API
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


__all__ = [
    "BACKEND", "Signal", "Slot", "QObject", "QThread", "QTimer", "QSize", "QPoint", "QRect", "Qt",
    "QEvent", "QUrl", "QBuffer", "QByteArray",
    "QAction", "QColor", "QCursor", "QFont", "QGuiApplication", "QIcon", "QImage",
    "QKeyEvent", "QKeySequence", "QMouseEvent", "QPainter", "QPainterPath", "QLinearGradient",
    "QPaintEvent", "QPixmap", "QTextCursor", "QTextDocument", "QTransform",
    "QApplication", "QCheckBox", "QComboBox", "QCompleter", "QDoubleSpinBox", "QFormLayout",
    "QFrame", "QGridLayout", "QGroupBox",
    "QHBoxLayout", "QLabel", "QLineEdit", "QDialog", "QDialogButtonBox", "QFileDialog",
    "QInputDialog", "QListWidget", "QListWidgetItem",
    "QMenu", "QMessageBox", "QPlainTextEdit", "QProgressBar", "QPushButton", "QSizePolicy",
    "QSlider", "QSpinBox", "QSplitter", "QStringListModel", "QSystemTrayIcon", "QTabWidget",
    "QTextBrowser", "QToolButton", "QVBoxLayout", "QWidget", "QGraphicsDropShadowEffect",
    "QScrollArea",
    "QDragEnterEvent", "QDragLeaveEvent", "QDragMoveEvent", "QDropEvent",
    "event_global_pos", "event_local_pos",
]