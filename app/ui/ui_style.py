"""统一 UI 皮肤：聊天面板 / 设置面板 / 右键菜单 / 托盘菜单共用。

设计语言：
    - 柔和浅色底 + 卡片式分组（白卡、圆角、细边框）
    - 主色：淡紫 #8b7cf6（hover 加深），辅助粉色
    - 圆角控件：输入框 / 按钮 / 滑块 / 进度条 / 菜单项全部统一圆角
    - 字体：Microsoft YaHei UI

用法：
    widget.setStyleSheet(ui_style.CHAT_QSS)      # 聊天窗
    widget.setStyleSheet(ui_style.SETTINGS_QSS)  # 设置面板
    ui_style.style_menu(menu)                    # 右键/托盘菜单（含子菜单）
"""
from __future__ import annotations

from app.core.qt_compat import Qt, QMenu

# ---- 调色板 ----
BG        = "#f4f4fa"   # 窗口底色
CARD      = "#ffffff"   # 卡片
BORDER    = "#e7e7f0"   # 卡片描边
TEXT      = "#2c2c38"   # 主文字
TEXT_SUB  = "#8a8a9c"   # 次要文字
ACCENT    = "#8b7cf6"   # 主色（淡紫）
ACCENT_DK = "#7466e8"   # 主色 hover
ACCENT_BG = "#efeaff"   # 主色浅底（选中/hover 背景）
DANGER    = "#ef5f7e"   # 停止/退出
FONT      = '"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif'

# ============================================================
#  聊天窗
# ============================================================
CHAT_QSS = f"""
QWidget {{
    font-family: {FONT};
    font-size: 10pt;
    color: {TEXT};
}}
QMainWindow {{ background: #f0f2f8; }}

/* 侧边栏 */
QListWidget {{
    background: #fafafe;
    border: 1px solid #e8e8f0;
    border-radius: 12px;
    padding: 6px;
}}
QListWidget::item {{
    border-radius: 10px;
    padding: 6px 10px;
    margin: 2px 2px;
    font-size: 9pt;
}}
QListWidget::item:hover {{ background: #f0f0fa; }}
QListWidget::item:selected {{ background: #e8e4ff; color: #6b5bd6; }}

/* 输入框 */
QLineEdit {{
    background: #ffffff;
    border: 2px solid #e0e0ee;
    border-radius: 14px;
    padding: 10px 16px;
    font-size: 10pt;
    selection-background-color: #c4b5fd;
}}
QLineEdit:focus {{ border-color: #a78bfa; background: #fefeff; }}

/* 按钮 */
QPushButton {{
    background: #ffffff;
    border: 1.5px solid #e0e0ee;
    border-radius: 12px;
    padding: 8px 18px;
    font-weight: 600;
    font-size: 9pt;
}}
QPushButton:hover {{ border-color: #a78bfa; color: #7c3aed; background: #f5f3ff; }}
QPushButton:pressed {{ background: #ede9fe; }}
QPushButton:disabled {{ color: #b0b0c0; background: #f5f5fa; border-color: #e8e8f0; }}

QPushButton#send_btn {{
    background: linear-gradient(135deg, #a78bfa, #8b5cf6); color: #fff;
    border: none; padding: 10px 26px; font-size: 10pt; font-weight: 700;
    border-radius: 14px;
}}
QPushButton#send_btn:hover {{ background: linear-gradient(135deg, #8b5cf6, #7c3aed); }}
QPushButton#send_btn:pressed {{ background: #6d28d9; }}
QPushButton#send_btn:disabled {{ background: #d4c8f7; color: #fff; }}

QPushButton#stop_btn {{
    color: #f43f5e; border-color: #fecdd3; background: #fff1f3;
}}
QPushButton#stop_btn:hover {{ background: #ffe4e6; border-color: #f43f5e; }}

/* 滚动条 */
QScrollBar:vertical {{
    background: transparent; width: 6px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #d5d2e6; border-radius: 3px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: #a78bfa; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ height: 0; }}
"""

# 聊天正文气泡（QTextBrowser 富文本子集）
# 设计参考微信聊天：每条消息 = 头像（外侧）+ 气泡（内侧），
# 用户消息靠右、桌宠消息靠左；气泡最大宽度不超过 75%，长文本自动换行。
# 微信尖角：用「重叠的小三角形」+ border 拼出来（Qt 不支持 ::before/::after 伪元素，
# 所以尖角用纯 CSS 三角形 + 同色 border 模拟，HTML 层搭一行 + 偏移几像素）。
CHAT_BUBBLE_CSS = (
    # 基础排版
    "body{margin:0;padding:4px 6px;background:#f5f5f7;}"
    "p{margin:6px 0;}"
    # 头像列：固定 36x36 圆形，与气泡顶部对齐
    "span.avatar-col{display:inline-block;width:36px;height:36px;"
    "border-radius:18px;text-align:center;line-height:36px;"
    "font-size:13pt;font-weight:700;color:#fff;vertical-align:top;}"
    "span.avatar-col.user{background:linear-gradient(135deg,#60a5fa,#2563eb);"
    "box-shadow:0 1px 2px rgba(37,99,235,0.25);}"
    "span.avatar-col.bot{background:linear-gradient(135deg,#f9a8d4,#ec4899);"
    "box-shadow:0 1px 2px rgba(236,72,153,0.25);}"
    # 气泡主体：白底 + 圆角 + 阴影 + 最大宽度 75%
    "span.bubble{display:inline-block;max-width:75%;padding:9px 12px;"
    "border-radius:10px;line-height:1.55;word-wrap:break-word;"
    "word-break:break-word;font-size:10pt;text-align:left;"
    "box-shadow:0 1px 2px rgba(0,0,0,0.06);}"
    # 用户气泡（微信风格：浅蓝绿白色）
    "span.bubble.user{background:#95ec69;color:#1f2937;"
    "border:1px solid #7ed463;}"
    # 桌宠气泡（白色）
    "span.bubble.bot{background:#ffffff;color:#2c2c38;"
    "border:1px solid #e5e7eb;}"
    # 消息元信息（名字 + 时间），位于气泡上方小字
    "span.meta{font-size:8pt;color:#8a8a9c;display:block;margin:0 4px 2px;}"
    "span.meta.right{text-align:right;}"
    "span.meta.left{text-align:left;}"
    "span.meta .name{font-weight:600;color:#6b7280;margin-right:6px;}"
    # 工具调用记录（紧凑小卡片）
    "div.tools{font-size:8pt;color:#6b5bd6;background-color:#f3eeff;"
    "border:1px solid #e9e3ff;border-radius:6px;padding:3px 8px;"
    "margin:3px 0 6px;}"
    # 代码块 / 行内代码
    "pre{background:#1f2937;color:#e5e7eb;padding:8px 10px;"
    "border-radius:6px;font-family:Consolas,Monaco,monospace;"
    "font-size:9pt;overflow:auto;margin:6px 0;}"
    "code{background:#f0f0f5;padding:1px 5px;border-radius:3px;"
    "font-family:Consolas,Monaco,monospace;font-size:9pt;color:#4338ca;}"
    # 系统消息：居中浅灰条
    "span.system-msg{display:inline-block;background:#e5e7eb;"
    "color:#6b7280;padding:3px 10px;border-radius:8px;font-size:9pt;"
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
QMainWindow, QDialog, QWidget#settings_root {{ background: {BG}; }}

QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: 12px;
    background: {BG};
    top: -1px;
}}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_SUB};
    padding: 7px 16px;
    margin: 4px 3px 0 3px;
    border-radius: 9px;
    font-weight: 600;
}}
QTabBar::tab:selected {{ background: {ACCENT}; color: #fff; }}
QTabBar::tab:hover:!selected {{ background: {ACCENT_BG}; color: {ACCENT_DK}; }}

QGroupBox {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 12px;
    margin-top: 12px;
    padding: 14px 10px 10px 10px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: {ACCENT_DK};
}}

QPushButton {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 9px;
    padding: 7px 14px;
    font-weight: 600;
}}
QPushButton:hover {{ border-color: {ACCENT}; color: {ACCENT_DK}; background: {ACCENT_BG}; }}
QPushButton:checked {{ background: {ACCENT}; color: #fff; border-color: {ACCENT}; }}
QPushButton:disabled {{ color: {TEXT_SUB}; background: #f1f1f5; }}

QPushButton#accent_btn {{
    background: {ACCENT}; color: #fff; border: none; padding: 8px 18px;
}}
QPushButton#accent_btn:hover {{ background: {ACCENT_DK}; }}
QPushButton#accent_btn:pressed {{ background: #6554d6; }}
QPushButton#persist_btn {{
    background: #f59e0b; color: #fff; border: none; padding: 7px 14px;
}}
QPushButton#persist_btn:hover {{ background: #d97706; }}

QSlider::groove:horizontal {{
    height: 6px;
    background: #e4e2ef;
    border-radius: 3px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    width: 16px; height: 16px;
    margin: -6px 0;
    border-radius: 8px;
    background: #fff;
    border: 2px solid {ACCENT};
}}
QSlider::handle:horizontal:hover {{ background: {ACCENT_BG}; }}

QCheckBox {{ spacing: 7px; }}
QCheckBox::indicator {{
    width: 17px; height: 17px;
    border: 1.5px solid #cfcde4;
    border-radius: 5px;
    background: {CARD};
}}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{
    background: {ACCENT};
    border-color: {ACCENT};
    image: url(none);
}}

QLabel {{ background: transparent; }}

QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: #d5d2e6; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ height: 0; }}
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
        f"QProgressBar {{ background: #ecebf3; border: none; border-radius: 7px; }}"
        f" QProgressBar::chunk {{ background: {color}; border-radius: 7px; }}"
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
    padding: 7px 26px 7px 14px;
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
