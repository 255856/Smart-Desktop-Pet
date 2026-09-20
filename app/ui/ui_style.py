"""统一 UI 皮肤：聊天面板 / 设置面板 / 右键菜单 / 托盘菜单共用。

设计语言（v2 美化版）：
    - 柔和浅紫灰渐变底 + 卡片式分组（白卡、大圆角、细边框、轻阴影）
    - 主色：紫罗兰 #7c6cf0（hover 加深），辅助粉紫渐变
    - 全控件统一圆角：输入框 / 按钮 / 滑块 / 进度条 / 菜单项 / Tab
    - 无边框窗口：窗口透明，内部白色圆角卡片（16px）+ 自绘标题栏
    - 字体：Microsoft YaHei UI

用法：
    widget.setStyleSheet(ui_style.CHAT_QSS)      # 聊天窗
    widget.setStyleSheet(ui_style.SETTINGS_QSS)  # 设置面板
    ui_style.style_menu(menu)                    # 右键/托盘菜单（含子菜单）
    # 无边框圆角窗口：窗口透明（WA_TranslucentBackground），
    # 内部套一个 objectName="window_card" 的白色圆角卡片 + QGraphicsDropShadowEffect 阴影
"""
from __future__ import annotations

from pathlib import Path

from app.core.qt_compat import Qt, QMenu

# 图标资源（QSS 的 image: url 对 SVG 支持不稳定，统一用 PNG）
# ui_style.py 位于 app/ui/，项目根需再上一层
_ICON_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "icons"
_ARROW_URL = (_ICON_DIR / "arrow_down.png").as_posix()
_CHECK_URL = (_ICON_DIR / "check.png").as_posix()

# ---- 调色板 ----
BG        = "#f5f5fb"   # 窗口底色（浅紫灰）
CARD      = "#ffffff"   # 卡片
CARD_GRAD = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #ffffff, stop:1 #fbfaff)"
BORDER    = "#e7e4f2"   # 卡片描边
TEXT      = "#2c2c38"   # 主文字
TEXT_SUB  = "#8a8a9c"   # 次要文字
ACCENT    = "#7c6cf0"   # 主色（紫罗兰）
ACCENT_DK = "#6a58e0"   # 主色 hover
ACCENT_BG = "#efeaff"   # 主色浅底（选中/hover 背景）
ACCENT_LT = "#a99bfa"   # 主色浅（focus 边框/发光）
DANGER    = "#ef5f7e"   # 停止/退出
FONT      = '"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif'


# ============================================================
#  无边框窗口卡片容器（透明窗口内部的白色圆角卡片）
# ============================================================
WINDOW_CARD_QSS = f"""
QFrame#window_card {{
    background: {CARD_GRAD};
    border: 1px solid {BORDER};
    border-radius: 16px;
}}
"""

# 自绘标题栏（无边框窗口顶部）
TITLEBAR_QSS = f"""
QFrame#titlebar {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #f6f4ff, stop:0.5 #f8f6ff, stop:1 #fdf2f8);
    border: none;
    border-bottom: 1px solid {BORDER};
    border-top-left-radius: 16px;
    border-top-right-radius: 16px;
}}
QLabel#titlebar_title {{
    color: {TEXT};
    font-size: 13pt;
    font-weight: 700;
}}
QLabel#titlebar_subtitle {{
    color: {TEXT_SUB};
    font-size: 9pt;
}}
QToolButton#win_btn {{
    background: transparent;
    border: none;
    border-radius: 9px;
    font-size: 12pt;
    font-weight: 600;
    color: #6b6b80;
    padding: 3px 10px;
}}
QToolButton#win_btn:hover {{ background: #eceaff; color: {ACCENT_DK}; }}
QToolButton#win_btn:pressed {{ background: #e2dfff; }}
QToolButton#win_btn_close {{
    background: transparent;
    border: none;
    border-radius: 9px;
    font-size: 12pt;
    font-weight: 600;
    color: #6b6b80;
    padding: 3px 10px;
}}
QToolButton#win_btn_close:hover {{ background: #ef5f7e; color: #ffffff; }}
QToolButton#win_btn_close:pressed {{ background: #d64d6b; }}
"""

# ============================================================
#  聊天窗
# ============================================================
CHAT_QSS = f"""
QWidget {{
    font-family: {FONT};
    font-size: 10pt;
    color: {TEXT};
}}
QMainWindow {{ background: transparent; }}
QWidget#chat_root {{ background: transparent; }}

/* 聊天气泡区 */
QTextBrowser#chat_view {{
    background: #fbfbfd;
    border: 1px solid #eceaf5;
    border-radius: 14px;
    padding: 10px 8px;
    selection-background-color: #dcd4ff;
}}
QTextBrowser#chat_view:focus {{ border-color: {ACCENT_LT}; }}

/* 输入容器 */
QFrame#input_container {{
    background: #ffffff;
    border: 2px solid #e7e3f5;
    border-radius: 16px;
}}
QFrame#input_container:focus-within {{ border-color: {ACCENT_LT}; }}

/* 输入框 */
QLineEdit {{
    background: #ffffff;
    border: 2px solid #e3e1f0;
    border-radius: 14px;
    padding: 10px 16px;
    font-size: 10pt;
    selection-background-color: #c4b5fd;
}}
QLineEdit:focus {{ border-color: {ACCENT_LT}; background: #fefeff; }}
QPlainTextEdit#chat_input {{ background: transparent; border: none; }}
QPlainTextEdit#chat_input:focus {{ background: transparent; }}

/* 按钮 */
QPushButton {{
    background: #ffffff;
    border: 1.5px solid #e0e0ee;
    border-radius: 12px;
    padding: 8px 18px;
    font-weight: 600;
    font-size: 9pt;
}}
QPushButton:hover {{ border-color: {ACCENT_LT}; color: {ACCENT_DK}; background: {ACCENT_BG}; }}
QPushButton:pressed {{ background: #ede9fe; }}
QPushButton:disabled {{ color: #b0b0c0; background: #f5f5fa; border-color: #e8e8f0; }}

QPushButton#send_btn {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #a78bfa, stop:1 #7c6cf0);
    color: #fff;
    border: none;
    padding: 2px 28px;
    font-size: 10pt;
    font-weight: 700;
    border-radius: 14px;
}}
QPushButton#send_btn:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
    stop:0 #9a7ef8, stop:1 #6a58e0); }}
QPushButton#send_btn:pressed {{ background: #5d4bd6; }}
QPushButton#send_btn:disabled {{ background: #d4c8f7; color: #fff; }}

QPushButton#stop_btn {{
    color: #f43f5e; border-color: #fecdd3; background: #fff1f3;
    padding: 2px 18px;
}}
QPushButton#stop_btn:hover {{ background: #ffe4e6; border-color: #f43f5e; }}

/* 麦克风 / 侧栏按钮（轻量幽灵按钮） */
QPushButton#ghost_btn {{
    background: transparent;
    border: 1px solid #e3e1f0;
    border-radius: 9px;
    padding: 3px 12px;
    font-size: 9pt;
    color: #5b4f9c;
}}
QPushButton#ghost_btn:hover {{ background: {ACCENT_BG}; border-color: {ACCENT_LT}; }}
QPushButton#ghost_btn:disabled {{ color: #ccc; border-color: #f0f0f0; }}

/* 滚动条 */
QScrollBar:vertical {{
    background: transparent; width: 6px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #d5d2e6; border-radius: 3px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {ACCENT_LT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ height: 0; }}
"""

# 聊天正文气泡（QTextBrowser 富文本子集）
#
# 重要：Qt 富文本（QTextDocument）只支持有限 CSS —— 不支持 display:inline-block、
# max-width、box-shadow，也不支持 span 上的 border-radius/width，td 上 border-radius 也不生效。
# 气泡布局完全依赖 <table>：外层两列（头像列 44px + 内容列），内容列再嵌一个
# shrink-to-fit 的 table 作为整块气泡背景（td 背景色）。头像由 QPainter 预先画成
# 圆形 PNG，转成自包含 data URI 以 <img> 注入（document.addResource 在 chat_view.clear()
# 后会失效），保证圆形与稳定占位。
#
# 类名必须保留（tests/test_chat_rendering.py 有断言）：
#   avatar-col user / avatar-col bot / bubble user / bubble bot / meta right / meta left
CHAT_BUBBLE_CSS = (
    # 基础排版
    "body{margin:0;padding:4px 4px;background:#fbfbfd;}"
    "p{margin:5px 0;}"
    # 表格基础（气泡外层 / 内层都用）
    "table{border-collapse:collapse;}"
    "td{vertical-align:top;}"
    # 消息元信息（名字 + 时间），位于气泡上方小字
    "span.meta{font-size:8pt;color:#a0a0b4;}"
    "span.meta .name{font-weight:700;color:#7a6fe0;margin-right:6px;}"
    # 气泡单元格：背景/边框/padding 在 _msg_html 里内联给出（Qt 对多 class
    # 选择器和 td border-radius 支持不稳定，内联样式最可靠）。
    "td.bubble{line-height:1.55;font-size:10pt;}"
    # 工具调用记录（紧凑小卡片）
    "div.tools{font-size:9pt;color:#3f3f46;background-color:#f5f3ff;"
    "border:1px solid #e9e3ff;border-radius:8px;padding:6px 9px;"
    "margin:2px 0 8px 0;}"
    # 代码块 / 行内代码
    "pre{background:#232337;color:#e5e7eb;padding:10px 12px;"
    "border-radius:10px;font-family:Consolas,Monaco,monospace;"
    "font-size:9pt;overflow:auto;margin:6px 0;border:1px solid #2e2e46;}"
    "code{background:#f0f0f7;padding:1px 6px;border-radius:4px;"
    "font-family:Consolas,Monaco,monospace;font-size:9pt;color:#5b46e8;}"
    # 系统消息：居中浅灰条
    "span.system-msg{display:inline-block;background:#ececf4;"
    "color:#6b7280;padding:4px 12px;border-radius:10px;font-size:9pt;"
    "margin:6px 0;}"
)

# ============================================================
#  设置面板
# ============================================================
SETTINGS_QSS = f"""
QWidget {{
    font-family: {FONT};
    font-size: 10pt;
    color: {TEXT};
}}
QMainWindow, QDialog, QWidget#settings_root {{ background: transparent; }}

QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 14px;
    background: {BG};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_SUB};
    padding: 8px 18px;
    margin: 4px 3px 0 3px;
    border-radius: 10px;
    font-weight: 600;
}}
QTabBar::tab:selected {{ background: {ACCENT}; color: #fff; }}
QTabBar::tab:hover:!selected {{ background: {ACCENT_BG}; color: {ACCENT_DK}; }}

QGroupBox {{
    background: {CARD_GRAD};
    border: 1px solid {BORDER};
    border-radius: 14px;
    margin-top: 12px;
    padding: 16px 12px 12px 12px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 14px;
    padding: 0 8px;
    color: {ACCENT_DK};
    background: transparent;
}}

QPushButton {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 7px 16px;
    font-weight: 600;
}}
QPushButton:hover {{ border-color: {ACCENT_LT}; color: {ACCENT_DK}; background: {ACCENT_BG}; }}
QPushButton:checked {{ background: {ACCENT}; color: #fff; border-color: {ACCENT}; }}
QPushButton:disabled {{ color: {TEXT_SUB}; background: #f1f1f5; }}

/* —— Live2D 外观 chip 标签 —— */
QPushButton#chip {{
    background: #ffffff;
    border: 1.5px solid #e7e4f5;
    border-radius: 14px;
    padding: 7px 10px;
    font-weight: 500;
    color: #4b4b60;
}}
QPushButton#chip:hover {{
    border-color: {ACCENT_LT};
    color: {ACCENT_DK};
    background: #f4f1fe;
}}
QPushButton#chip:checked {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #a78bfa, stop:1 #7c6cf0);
    color: #fff;
    border-color: #7c6cf0;
    font-weight: 600;
}}
QPushButton#chip:checked:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #9a7ef8, stop:1 #6a58e0);
}}

/* —— Live2D 模型名卡片 —— */
QFrame#live2d_model_head {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #f3f0fe, stop:1 #e9e3fd);
    border: 1px solid #e0d9fb;
    border-radius: 14px;
}}
QLabel#model_caption {{
    color: #8a78f0; font-size: 9pt; font-weight: 700;
    letter-spacing: 2px; background: transparent;
}}
QLabel#model_name {{
    color: #2f2b45; font-size: 15pt; font-weight: 700; background: transparent;
}}
QWidget#live2d_page {{ background: transparent; }}

/* —— Live2D 分类「更多」展开按钮 —— */
QPushButton#more_btn {{
    background: transparent;
    border: 1px solid #e7e4f5;
    border-radius: 12px;
    color: #7c6cf0;
    font-weight: 600;
    padding: 6px 12px;
}}
QPushButton#more_btn:hover {{ background: #f4f1fe; border-color: {ACCENT_LT}; color: {ACCENT_DK}; }}
QPushButton#more_btn:pressed {{ background: #ece7fd; }}

QPushButton#accent_btn {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #a78bfa, stop:1 #7c6cf0);
    color: #fff; border: none; padding: 8px 20px;
}}
QPushButton#accent_btn:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
    stop:0 #9a7ef8, stop:1 #6a58e0); }}
QPushButton#accent_btn:pressed {{ background: #5d4bd6; }}
QPushButton#persist_btn {{
    background: #f59e0b; color: #fff; border: none; padding: 7px 16px;
}}
QPushButton#persist_btn:hover {{ background: #d97706; }}

QSlider::groove:horizontal {{
    height: 6px;
    background: #e8e6f3;
    border-radius: 3px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    width: 18px; height: 18px;
    margin: -6px 0;
    border-radius: 9px;
    background: #fff;
    border: 2px solid {ACCENT};
}}
QSlider::handle:horizontal:hover {{ background: {ACCENT_BG}; border-color: {ACCENT_LT}; }}

QCheckBox {{ spacing: 8px; }}
QCheckBox::indicator {{
    width: 18px; height: 18px;
    border: 1.5px solid #cfcde4;
    border-radius: 6px;
    background: {CARD};
}}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
    image: url("{_CHECK_URL}");
}}

QLabel {{ background: transparent; }}

/* 下拉框 / 数字框：统一圆角输入框样式，且重置内嵌 QLineEdit（避免双重边框） */
QComboBox {{
    background: #ffffff;
    border: 1.5px solid #e0e0ee;
    border-radius: 10px;
    padding: 6px 12px;
    min-height: 20px;
}}
QComboBox:hover {{ border-color: {ACCENT_LT}; }}
QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox:on {{ border-color: {ACCENT}; }}
QComboBox::drop-down {{
    border: none; width: 28px;
    border-top-right-radius: 10px; border-bottom-right-radius: 10px;
}}
QComboBox::down-arrow {{
    image: url("{_ARROW_URL}");
    width: 12px; height: 12px;
}}
QComboBox QAbstractItemView {{
    background: #ffffff;
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 4px;
    outline: none;
    selection-background-color: {ACCENT_BG};
    selection-color: {ACCENT_DK};
}}
QComboBox:editable QLineEdit {{
    background: transparent;
    border: none;
    padding: 0;
    margin: 0;
}}
QSpinBox, QDoubleSpinBox {{
    background: #ffffff;
    border: 1.5px solid #e0e0ee;
    border-radius: 10px;
    padding: 5px 10px;
    min-height: 20px;
}}
QSpinBox:hover, QDoubleSpinBox:hover {{ border-color: {ACCENT_LT}; }}
QSpinBox:focus, QDoubleSpinBox:focus {{ border-color: {ACCENT}; }}
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    width: 18px; border: none; background: transparent;
}}

QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #d5d2e6; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ height: 0; }}

/* 滚动区域与窗口卡片融为一体（模型配置 Tab 内容较多） */
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
"""

# 状态条配色（体力/饱食/口渴/心情/健康/好感）
STAT_BAR_COLORS = {
    "strength":  "#f59e0b",
    "food":      "#10b981",
    "drink":     "#3b82f6",
    "feeling":   "#f472b6",
    "health":    "#ef4444",
    "likability": "#a78bfa",
}


def stat_bar_qss(color: str) -> str:
    return (
        f"QProgressBar {{ background: #ecebf3; border: none; border-radius: 8px; }}"
        f" QProgressBar::chunk {{ background: {color}; border-radius: 8px; }}"
    )


# ============================================================
#  菜单（右键 / 托盘）
# ============================================================
MENU_QSS = f"""
QMenu {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 12px;
    padding: 6px;
    font-family: {FONT};
    font-size: 10pt;
    color: {TEXT};
}}
QMenu::item {{
    padding: 8px 28px 8px 16px;
    border-radius: 8px;
    background: transparent;
}}
QMenu::item:selected {{ background: {ACCENT_BG}; color: {ACCENT_DK}; }}
QMenu::item:disabled {{ color: {TEXT_SUB}; }}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 5px 10px;
}}
QMenu::right-arrow {{
    width: 8px; height: 8px;
}}
"""


def style_menu(menu: QMenu) -> None:
    """给菜单（及其子菜单）套用圆角卡片皮肤。"""
    menu.setStyleSheet(MENU_QSS)
    # 圆角需要透明背景，否则 QSS 圆角外露出系统底色
    menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    for child in menu.findChildren(QMenu):
        style_menu(child)
