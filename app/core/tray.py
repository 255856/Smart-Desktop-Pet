"""系统托盘菜单：右键托盘图标 → 显示/隐藏桌宠 / 打开聊天 / 退出。"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.core.qt_compat import QAction, QApplication, QIcon, QMenu, QObject, QPixmap, QSystemTrayIcon
from app.ui import ui_style


class TrayController(QObject):
    """封装 QSystemTrayIcon，避免主程序被菜单代码淹没。"""

    def __init__(self, app_name: str, icon_path: Optional[Path] = None, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.app_name = app_name
        icon = self._make_icon(icon_path)
        self.tray = QSystemTrayIcon(icon)
        self.tray.setToolTip(app_name)

        menu = QMenu()

        self.act_show = QAction("🐾 显示桌宠", menu)
        self.act_hide = QAction("🙈 隐藏桌宠", menu)
        self.act_chat = QAction("💬 打开聊天", menu)
        self.act_settings = QAction("⚙️ 设置...", menu)
        self.act_dashboard = QAction("📊 调试面板 (Dashboard)", menu)
        self.act_dashboard.setToolTip("浏览器打开 http://127.0.0.1:8765")
        self.act_memory = QAction("🧠 查看长期记忆", menu)
        self.act_quit = QAction("❌ 退出", menu)
        menu.addAction(self.act_show)
        menu.addAction(self.act_hide)
        menu.addSeparator()
        menu.addAction(self.act_chat)
        menu.addAction(self.act_settings)
        menu.addAction(self.act_dashboard)
        menu.addAction(self.act_memory)
        menu.addSeparator()
        menu.addAction(self.act_quit)

        ui_style.style_menu(menu)   # 与右键菜单同款圆角卡片皮肤
        self.tray.setContextMenu(menu)
        self.tray.show()

    def _make_icon(self, path: Optional[Path]) -> QIcon:
        if path and path.is_file():
            return QIcon(str(path))
        # 退化：项目内的 fallback（路径参数相对 cwd）
        body = Path("assets/sprites/body_front.png")
        if body.is_file():
            return QIcon(str(body))
        # 退化 2：项目根下的默认第一帧
        try:
            from app.core.qt_compat import BACKEND as _BACKEND
            from pathlib import Path as _P
            here = _P(__file__).resolve().parent.parent
            first_idle = next(iter((here / "assets/sprites").glob("*/A_000_*.png")), None)
            if first_idle is not None:
                return QIcon(str(first_idle))
        except Exception:
            pass
        # 退化 3：直接返回空 QIcon（PyQt5 没有 ThemeIcon 枚举；
        # Qt6 路径都搞定后才会到这里）。托盘会显示一个空图标，不报错。
        return QIcon()

    def notify(self, title: str, msg: str, duration_ms: int = 4000) -> None:
        if self.tray.supportsMessages():
            self.tray.showMessage(title, msg, QSystemTrayIcon.MessageIcon.Information, duration_ms)