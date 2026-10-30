"""统一 UI 皮肤：聊天面板 / 右键菜单 / 托盘菜单共用。"""
from __future__ import annotations

from pathlib import Path
import string

from app.core.qt_compat import Qt, QMenu

# 图标资源（统一用 PNG，SVG 在 QSS 里支持不稳定）
_ICON_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "icons"
_ARROW_URL = (_ICON_DIR / "arrow_down.png").as_posix()
_ARROW_UP_URL = (_ICON_DIR / "arrow_up.png").as_posix()
_CHECK_URL = (_ICON_DIR / "check.png").as_posix()

BG        = "#faf6f2"   # 窗口底色（暖米白）
CARD      = "#fffcf8"   # 卡片（暖白）
CARD_GRAD = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #fffcf8, stop:1 #fdf3ed)"
BORDER    = "#ebe3d6"   # 卡片描边（暖灰）
TEXT      = "#4a3d6e"   # 主文字（莓紫, 跟樱粉配）
TEXT_SUB  = "#8a7f8c"   # 次要文字
# v38: 主色从紫罗兰改为樱粉 — 摆脱通用 SaaS 感, 跟桌宠养成主题契合
ACCENT    = "#E89AAE"   # 主色（樱粉）
ACCENT_DK = "#D17F95"   # 主色 hover（深樱粉）
ACCENT_BG = "#fceef1"   # 主色浅底（选中/hover 背景）
ACCENT_LT = "#F5C9D3"   # 主色浅（focus 边框/发光）
DANGER    = "#ef5f7e"   # 停止/退出
FONT      = '"Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", sans-serif'

# v37: 升级解锁 / 状态色三件套 + 柔阴影 (轻抛光)
GOLD       = "#F59E0B"   # 当前等级 (橘金)
GOLD_BG    = "#FFF7E6"   # 当前等级浅底
GOLD_BORDER= "#FCD9A1"   # 当前等级描边
MINT       = "#10B981"   # 已完成 (薄荷绿)
MINT_BG    = "#ECFDF5"   # 已完成浅底
MINT_BORDER= "#A7E6C7"   # 已完成描边
LOCKED     = "#A0A0B0"   # 未解锁 (中性灰)
LOCKED_BG  = "#F4F4F8"   # 未解锁浅底
LOCKED_BORDER = "#E2E2EA" # 未解锁描边
SHADOW_RGBA = "rgba(232, 154, 174, 0.10)"  # 卡片柔阴影（粉调）

# v38: 桌宠心情光谱 (signature 元素 — 聊天气泡左侧色边)
# 跟创新点 2 主题契合: 桌宠心情 LOW/MID/HIGH → 聊天气泡左边色带不同颜色
# HIGH = 樱粉 (热情), MID = 蜜桃 (温和), LOW = 雾蓝 (低落)
MOOD_HIGH = "#E89AAE"   # 樱粉
MOOD_MID  = "#FFB88A"   # 蜜桃
MOOD_LOW  = "#B8C5D6"   # 雾蓝
MOOD_NONE = "#D8D2C8"   # 无数据 (暖灰)

# 4 个窗统一的 QGraphicsDropShadowEffect 参数 (QSS 不支持 box-shadow, 编程设)
# 3 个窗 (chat_window / memory_panel / tool_unlock) 都用这套, 看着一致
# v38: 阴影色跟主色一致 (樱粉, 不再是紫罗兰)
WINDOW_SHADOW_BLUR   = 44
WINDOW_SHADOW_OFFSET = 10
WINDOW_SHADOW_COLOR_R = 232
WINDOW_SHADOW_COLOR_G = 154
WINDOW_SHADOW_COLOR_B = 174
WINDOW_SHADOW_ALPHA   = 60


#  无边框窗口卡片容器（透明窗口内部的白色圆角卡片）
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

# QSpinBox/QDoubleSpinBox 上下箭头（用 PNG 图标）
_SPINBOX_ARROWS_QSS = f"""
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url("{_ARROW_UP_URL}"); width: 10px; height: 10px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url("{_ARROW_URL}"); width: 10px; height: 10px;
}}
QSpinBox::up-arrow:hover, QDoubleSpinBox::up-arrow:hover,
QSpinBox::down-arrow:hover, QDoubleSpinBox::down-arrow:hover {{
    background: {ACCENT_BG}; border-radius: 3px;
}}
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled,
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {{ opacity: 0.35; }}
"""

# QMessageBox 皮肤（统一风格，避免弹出 Windows 原生灰框）
MESSAGE_BOX_QSS = f"""
QMessageBox {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 14px;
}}
QMessageBox QLabel {{
    color: {TEXT};
    font-family: {FONT};
    font-size: 10pt;
    background: transparent;
}}
QMessageBox QLabel#qt_msgbox_extradata {{ color: {TEXT_SUB}; font-size: 9pt; }}
QMessageBox QPushButton {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 10px;
    padding: 6px 20px;
    font-family: {FONT};
    font-weight: 600;
    font-size: 9pt;
    min-width: 68px;
}}
QMessageBox QPushButton:hover {{ border-color: {ACCENT_LT}; color: {ACCENT_DK}; background: {ACCENT_BG}; }}
QMessageBox QPushButton:pressed {{ background: #e6e0fd; }}
QMessageBox QPushButton[default="true"] {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #a78bfa, stop:1 {ACCENT});
    color: #fff; border: none;
}}
QMessageBox QPushButton[default="true"]:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #9a7ef8, stop:1 {ACCENT_DK});
}}
"""

# 两个面板共用的收尾规则：提示气泡、裸 QLabel 透明、通用工具按钮
COMMON_QSS = f"""
QLabel {{ background: transparent; }}

QToolTip {{
    background: #2f2b45;
    color: #ffffff;
    border: 1px solid #4a4370;
    border-radius: 8px;
    padding: 6px 10px;
    font-family: {FONT};
    font-size: 9pt;
}}

/* 通用工具按钮：#ghost_btn / #win_btn 之外的裸 QToolButton 也能看 */
QToolButton {{
    background: transparent;
    border: 1px solid {BORDER};
    border-radius: 9px;
    padding: 4px 10px;
    color: #5b4f9c;
    font-family: {FONT};
    font-size: 9pt;
}}
QToolButton:hover {{ background: {ACCENT_BG}; border-color: {ACCENT_LT}; }}
QToolButton:pressed {{ background: #e6e0fd; }}
QToolButton:disabled {{ color: #c3c1d4; border-color: #f0eff6; background: transparent; }}
"""

#  弹窗外壳（工具解锁 / 头像选择 …）：跟聊天窗同源的皮肤
DIALOG_QSS = f"""
QWidget {{
    font-family: {FONT};
    font-size: 10pt;
    color: {TEXT};
}}
QDialog {{ background: transparent; }}
QWidget#dialog_body {{ background: transparent; }}

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

QPushButton#accent_btn {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #a78bfa, stop:1 #7c6cf0);
    color: #fff; border: none; padding: 8px 20px;
}}
QPushButton#accent_btn:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
    stop:0 #9a7ef8, stop:1 #6a58e0); }}
QPushButton#accent_btn:pressed {{ background: #5d4bd6; }}

/* 危险动作（删除 / 移到回收站）—— 确认态用这个, 跟普通主按钮区分开 */
QPushButton#danger_btn {{
    background: {DANGER};
    color: #fff; border: none; padding: 8px 20px;
}}
QPushButton#danger_btn:hover {{ background: #d64d6b; }}
QPushButton#danger_btn:pressed {{ background: #c74a63; }}

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

QScrollBar:vertical {{
    background: transparent; width: 6px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: #d5d2e6; border-radius: 3px; min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{ background: {ACCENT_LT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ height: 0; }}

{COMMON_QSS}
"""


#  聊天窗
CHAT_QSS = f"""
QWidget {{
    font-family: {FONT};
    font-size: 10pt;
    color: {TEXT};
}}
QMainWindow {{ background: transparent; }}
QWidget#chat_root {{ background: transparent; }}

/* 聊天气泡区 + 输入容器 - 统一为同一套视觉风格 */
QTextBrowser#chat_view, QFrame#input_container {{
    background: #fbfbfd;
    border: 1px solid #e7e3f5;
    border-radius: 14px;
    selection-background-color: #dcd4ff;
}}
QTextBrowser#chat_view {{
    padding: 10px 8px;
}}
/* focus 不改边框颜色，永远中性灰 */
QTextBrowser#chat_view:focus {{ border-color: #e7e3f5; }}

/* 输入框 */
QLineEdit {{
    background: #fbfbfd;
    border: 1px solid #e3e1f0;
    border-radius: 14px;
    padding: 10px 16px;
    font-size: 10pt;
    selection-background-color: #c4b5fd;
}}
QLineEdit:focus {{ border-color: {ACCENT_LT}; background: #ffffff; }}
QPlainTextEdit#chat_input {{
    background: transparent;
    border: none;
    padding: 6px 10px;
    font-size: 11pt;
    line-height: 1.5;
}}
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

{COMMON_QSS}
"""

# 聊天正文气泡（QTextBrowser 富文本子集）
#
# Qt 富文本（QTextDocument）只支持有限 CSS — 不支持 display:inline-block、
# max-width、box-shadow、span 上的 border-radius/width；td 上 border-radius 也不生效。
# 气泡布局完全依赖 <table>：外层两列（头像列 44px + 内容列），内容列再嵌一个
# shrink-to-fit 的 table 作为整块气泡背景（td 背景色）。头像由 QPainter 预先画成
# 圆形 PNG，转成自包含 data URI 以 <img> 注入。
#
# 类名必须保留（tests/test_chat_rendering.py 有断言）
CHAT_BUBBLE_CSS = (
    # 基础排版
    "body{margin:0;padding:4px 4px;background:#fbfbfd;}"
    "p{margin:5px 0;}"
    # 表格基础（气泡外层 / 内层都用）
    "table{border-collapse:collapse;}"
    "td{vertical-align:top;}"
    # 消息元信息（名字 + 时间），位于气泡上方小字
    "span.meta{font-size:8pt;color:#a0a0b4;}"
    "span.meta .name{font-weight:700;color:#d17f95;margin-right:6px;}"
    # v38: 气泡左边 3px 心情色带 (signature 元素)
    # 桌宠消息默认樱粉 (心情 HIGH), chat_window._msg_html 解析 message.mood
    # 字段后可内联覆盖为蜜桃/雾蓝/暖灰. 状态联动让 UI 有"桌宠心情" 的视觉表达.
    "td.bubble{line-height:1.55;font-size:10pt;border-left:3px solid #E89AAE;}"
    # 用户消息气泡: 右边 3px 主色 (跟主系统樱粉呼应)
    "td.user_bubble{border-left:none;border-right:3px solid #E89AAE;}"
    # 工具调用记录（安静的一行行痕迹，不抢主人回复的戏）
    "div.tools{font-size:8.5pt;color:#6b7280;background-color:transparent;"
    "border:none;border-left:2px solid #e2e8f0;border-radius:0;"
    "padding:1px 0 1px 8px;margin:2px 0 8px 0;}"
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
    # 压缩摘要条目（Stage 4.3）：**不是**主人说的话，用虚线边 + 淡底区分
    "div.msg-summary{margin:8px 0;}"
    "div.msg-summary .msg-label .who{color:#8b8ba3;"
    "font-weight:600;font-size:8.5pt;}"
    "div.msg-summary .msg-label .ts{color:#a0a0b4;font-size:8pt;}"
    "div.msg-summary .summary-body{border-left:3px dashed #c9c9de;"
    "background:#f6f6fb;padding:6px 10px;border-radius:0 4px 4px 0;"
    "color:#6b7280;font-size:9pt;line-height:1.5;}"
    # 工具步骤的状态 / 附注（跟 CHAT_MD_CSS 一一对应，两套样式不许漂移）
    "div.tools .tools-head-link{color:#9ca3af;text-decoration:none;font-size:8.5pt;}"
    "div.tools .tool-row .tool-num{color:#a0a0b4;margin-right:5px;}"
    "div.tools .tool-row .tool-cn{color:#a0a0b4;font-size:8pt;margin-left:5px;}"
    "div.tools .tool-row .tool-target{color:#9ca3af;margin:0 4px;}"
    "div.tools .tool-row .tool-mark-cell{margin-left:6px;}"
    "div.tools .tool-row.ok .tool-name{color:#475569;}"
    "div.tools .tool-row.failed .tool-name{color:#b91c1c;}"
    "div.tools .tool-row.cancelled .tool-name{color:#9ca3af;}"
    "div.tools .tool-row.rejected .tool-name{color:#a45b2e;}"
    "div.tools .tool-mark.tool-ok{color:#9ca3af;}"
    "div.tools .tool-mark.tool-failed{color:#dc2626;font-weight:600;}"
    "div.tools .tool-mark.tool-cancelled{color:#9ca3af;}"
    "div.tools .tool-mark.tool-rejected{color:#a45b2e;font-weight:600;}"
    "div.tools .tool-reason{color:#dc2626;font-size:8pt;margin:1px 0 2px 18px;}"
    "div.tools .tool-note{color:#9ca3af;font-size:8pt;margin:1px 0 2px 18px;}"
    "div.tools .tools-preview{color:#9ca3af;font-size:8.5pt;margin:1px 0 0 0;}"
    "div.tools .tools-tail{color:#dc2626;}"
    # 语音条（voice bubble）：语音是「一条消息」，独立于工具块
    "div.voice-bubble{font-size:9.5pt;color:#1f2937;background-color:#f5f3ef;"
    "border:1px solid #e4ded3;border-radius:8px;padding:6px 10px;margin:2px 0 8px 0;}"
    "div.voice-bubble .voice-play{color:#4f46e5;text-decoration:none;font-weight:600;}"
    "div.voice-bubble .voice-dur{color:#475569;margin-left:8px;}"
    "div.voice-bubble .voice-file{color:#a0a0b4;font-size:8pt;margin-left:8px;}"
    "div.voice-bubble .voice-text{color:#475569;font-size:9pt;margin-left:8px;}"
    "div.voice-bubble .voice-missing{color:#dc2626;font-size:9pt;margin-left:8px;}"
    "div.msg-notice{color:#6b7280;font-size:10pt;font-style:italic;"
    "border-left:3px solid #a45b2e;padding:2px 10px;margin:4px 0;}"
)


# ---------------------------------------------------------------------------
# 聊天正文样式（MiniMax Code 风格的线性流 + 完整 markdown 渲染）
#
# **一份规则表 + 两套配色**（亮 / 暗）：
#   · _CHAT_MD_TEMPLATE —— 亮暗两套主题共用同一份规则（string.Template），
#     「加了一条规则却只加到一套主题上」在结构上就不可能发生；
#   · _CHAT_MD_LIGHT / _CHAT_MD_DARK —— 每套主题一份颜色 token。
#
# 类名必须保留（tests/test_chat_rendering.py 有断言）
# ---------------------------------------------------------------------------
_CHAT_MD_TEMPLATE = string.Template(
    """body{margin:0;padding:8px 14px;background:$bg_page;font-family:'Microsoft YaHei UI','Microsoft YaHei','PingFang SC','SF Pro Text','Segoe UI',sans-serif;font-size:11pt;line-height:1.65;color:$text_body;}
.msg{padding:14px 4px 12px 4px;border-bottom:1px solid $border_line;}
.msg:last-child{border-bottom:none;}
.msg-user{background:$bg_user;}
.msg-assistant{background:$bg_page;border-left:3px solid $accent;padding-left:10px;}
.msg-assistant:first-child{padding-top:14px;}
.msg-user .msg-label{text-align:right;color:$text_sub;}
.msg-user .msg-body{text-align:right;}
.msg-assistant .msg-label{text-align:left;}
.msg-assistant .msg-body{text-align:left;}
.msg-label{font-size:9pt;font-weight:600;color:$text_label;letter-spacing:0.5px;margin-bottom:5px;}
.msg-user .msg-label{color:$text_sub;}
.msg-label .ts{font-weight:400;color:$text_muted;margin-left:8px;font-size:8.5pt;letter-spacing:0;}
.msg-user .msg-label, .msg-user .msg-body{display:block;margin-left:auto;width:fit-content;max-width:75%;}
.msg-assistant .msg-label{display:block;margin-right:auto;width:fit-content;max-width:90%;text-align:left;}
.msg-assistant .msg-body{display:block;margin-right:auto;width:fit-content;max-width:90%;text-align:left;}
.msg-body{font-size:11pt;line-height:1.7;color:$text_msg;}
.msg-body p{margin:7px 0;}
.msg-body h1,.msg-body h2,.msg-body h3,.msg-body h4{font-weight:600;line-height:1.3;margin:14px 0 6px 0;color:$text_strong;}
.msg-body h1{font-size:18pt;border-bottom:1px solid $border_soft;padding-bottom:6px;}
.msg-body h2{font-size:14pt;color:$text_h2;}
.msg-body h3{font-size:12.5pt;color:$text_h3;}
.msg-body h4{font-size:11.5pt;color:$text_accent;}
.msg-body ul,.msg-body ol{padding-left:24px;margin:6px 0;}
.msg-body li{margin:3px 0;line-height:1.6;}
.msg-body li>p{margin:2px 0;}
.msg-body code{background:$bg_code_inline;padding:1px 6px;border-radius:4px;font-family:Consolas,Monaco,'Courier New',monospace;font-size:0.9em;color:$text_accent;}

.msg-body pre{background:$bg_terminal;color:$fg_terminal;padding:12px 14px;border-radius:8px;font-family:Consolas,Monaco,'Courier New',monospace;font-size:9.5pt;line-height:1.5;white-space:pre-wrap;overflow:auto;margin:8px 0;border:1px solid $border_terminal;}
.msg-body pre code{background:transparent;color:inherit;padding:0;font-size:inherit;}
.msg-body blockquote{border-left:3px solid $border_quote;background:$bg_quote;padding:6px 14px;margin:8px 0;color:$text_quiet;border-radius:0 4px 4px 0;}
.msg-body blockquote p{margin:4px 0;}
.msg-body table{border-collapse:collapse;margin:8px 0;}
.msg-body th,.msg-body td{border:1px solid $border_soft;padding:6px 12px;text-align:left;}
.msg-body th{background:$bg_soft;font-weight:600;color:$text_th;}
.msg-body tr:nth-child(even) td{background:$bg_row_alt;}
.msg-body hr{border:none;border-top:1px solid $border_soft;margin:12px 0;}
.msg-body a{color:$text_accent;text-decoration:none;}
.msg-body a:hover{text-decoration:underline;}
.msg-body strong{font-weight:600;color:$text_strong;}
.msg-body em{font-style:italic;color:$text_italic;}
div.tools{font-size:8.5pt;color:$text_sub;background-color:transparent;border:none;border-left:2px solid $border_panel;border-radius:0;padding:1px 0 1px 8px;margin:4px 0 8px 0;}
div.tools .tools-head-link{color:$text_muted;text-decoration:none;font-size:8.5pt;}
div.tools .tool-row{display:block;margin:1px 0;}
div.tools .tool-row .tool-num{color:$text_muted;margin-right:5px;}
div.tools .tool-row .tool-name{color:$text_quiet;font-weight:600;font-family:Consolas,Menlo,monospace;}
div.tools .tool-row .tool-cn{color:$text_muted;font-size:8pt;margin-left:5px;}
div.tools .tool-row .tool-target{color:$text_muted;margin:0 4px;}
div.tools .tool-row .tool-mark-cell{margin-left:6px;}
div.tools .tool-row.ok .tool-name{color:$text_quiet;}
div.tools .tool-row.failed .tool-name{color:$text_mark_fail;}
div.tools .tool-row.cancelled .tool-name{color:$text_muted;}
div.tools .tool-row.rejected .tool-name{color:$text_mark_rejected;}
div.tools .tool-mark.tool-ok{color:$text_muted;}
div.tools .tool-mark.tool-failed{color:$text_mark_fail;font-weight:600;}
div.tools .tool-mark.tool-cancelled{color:$text_muted;}
div.tools .tool-mark.tool-rejected{color:$text_mark_rejected;font-weight:600;}
div.tools .tool-reason{color:$text_mark_fail;font-size:8pt;margin:1px 0 2px 18px;}
div.tools .tool-note{color:$text_muted;font-size:8pt;margin:1px 0 2px 18px;}
div.tools .tools-preview{color:$text_muted;font-size:8.5pt;margin:1px 0 0 0;}
div.tools .tools-tail{color:$text_mark_fail;}
div.voice-bubble{font-size:9.5pt;color:$text_msg;background-color:$bg_voice;border:1px solid $border_voice;border-radius:8px;padding:6px 10px;margin:4px 0 8px 0;}
div.voice-bubble .voice-play{color:$text_accent;text-decoration:none;font-weight:600;}
div.voice-bubble .voice-dur{color:$text_quiet;margin-left:8px;}
div.voice-bubble .voice-file{color:$text_muted;font-size:8pt;margin-left:8px;font-family:Consolas,Menlo,monospace;}
div.voice-bubble .voice-text{color:$text_quiet;font-size:9pt;margin-left:8px;}
div.voice-bubble .voice-missing{color:$text_mark_fail;font-size:9pt;margin-left:8px;}

div.thinking{font-size:9.5pt;color:$text_quiet;background-color:$bg_soft;border-left:3px solid $text_terminal_head;padding:8px 12px;margin:4px 0 8px 0;border-radius:0 4px 4px 0;}
div.thinking .thinking-head{color:$text_think_head;font-weight:600;margin-bottom:2px;}
div.thinking .thinking-body{margin:4px 0 2px 0;white-space:pre-wrap;font-family:Consolas,Menlo,monospace;font-size:9pt;color:$text_think_pre;}
div.execs{background:$bg_terminal;color:$fg_terminal;padding:10px 14px;border-radius:6px;font-family:Consolas,Monaco,'Courier New',monospace;font-size:9.5pt;line-height:1.5;margin:6px 0;border:1px solid $border_terminal;}
div.execs .execs-head{color:$text_exec_head;font-size:8.5pt;margin-bottom:4px;}
div.execs .execs-cmd{color:$text_terminal_cmd;}
div.execs .execs-out{color:$text_terminal_out;white-space:pre-wrap;}
div.execs .execs-row{margin:3px 0;background:$bg_terminal;border-radius:6px;padding:6px 10px;font-family:Consolas,Menlo,monospace;font-size:9pt;color:$fg_terminal;}
div.execs .execs-cmd-line{color:$text_terminal_head;}
div.execs .execs-output{margin:3px 0 0 0;white-space:pre-wrap;color:$text_terminal_out;}
div.execs .execs-exit{margin-top:3px;font-size:8pt;color:$text_exec_fail;}
div.execs .execs-exit.ok{color:$text_exec_ok;}
div.file-edits{font-size:9.5pt;color:$text_file_edit;background-color:$bg_file_edit;border-left:3px solid $border_file_edit_block;padding:8px 12px;margin:6px 0;border-radius:0 4px 4px 0;}
div.file-edits .file-edits-head{color:$text_file_edit_head;font-weight:600;margin-bottom:2px;}
div.file-edits .file-edit-row{background:$bg_file_edit;border:1px solid $border_file_edit_row;border-radius:4px;padding:4px 8px;margin:2px 0;font-size:9pt;color:$text_file_edit;}
div.file-edits .file-edit-size{color:$text_file_edit_size;}
span.system-msg{display:inline-block;background:$bg_panel;color:$text_system;padding:4px 12px;border-radius:12px;font-size:9pt;margin:6px 0;}
div.msg-summary{margin:8px 0;}
div.msg-summary .msg-label .who{color:$text_summary_who;font-weight:600;font-size:8.5pt;}
div.msg-summary .msg-label .ts{color:$text_summary_ts;font-size:8pt;}
div.msg-summary .summary-body{border-left:3px dashed $border_summary;background:$bg_soft;padding:6px 10px;border-radius:0 4px 4px 0;color:$text_summary_body;font-size:9pt;line-height:1.5;}
div.msg-notice{color:$text_sub;font-size:10pt;font-style:italic;border-left:3px solid $text_mark_rejected;padding:2px 10px;margin:4px 0;}
div.plan-card{margin:8px 0 14px 0;padding:10px 12px;background:$bg_plan;border:1px solid $border_plan;border-radius:8px;}
div.plan-card .plan-card-head{color:$text_plan_head;font-weight:600;margin-bottom:6px;}
div.plan-card .plan-card-body{white-space:pre-wrap;color:$text_plan_body;line-height:1.55;}
td.bubble{line-height:1.55;font-size:10pt;}
.typing-dot{display:inline-block;width:6px;height:6px;border-radius:50%;margin:0 2px;background:$text_dot;vertical-align:middle;}
.typing-dot.active{background:$text_dot_active;}"""
)

#: 亮色主题配色（默认）
_CHAT_MD_LIGHT = {
    "bg_page": "#fffcf8",
    "bg_user": "#faf6f2",
    "bg_panel": "#f1f5f9",
    "bg_soft": "#f8fafc",
    "bg_quote": "#f5f7ff",
    "bg_code_inline": "#eef2ff",
    "bg_row_alt": "#fafbfc",
    "bg_file_edit": "#fef3c7",
    "bg_plan": "#fbf7ff",
    "bg_voice": "#f5f3ef",
    "bg_terminal": "#0f172a",
    "border_line": "#ebe3d6",
    "border_soft": "#e5e7eb",
    "border_panel": "#e2e8f0",
    "border_terminal": "#1e293b",
    "border_quote": "#c7d2fe",
    "border_summary": "#cbd5e1",
    "border_file_edit_block": "#f59e0b",
    "border_file_edit_row": "#fbbf24",
    "border_plan": "#d9c9f5",
    "border_voice": "#e4ded3",
    "accent": "#E89AAE",
    "text_body": "#4a3d6e",
    "text_msg": "#1f2937",
    "text_strong": "#111827",
    "text_italic": "#374151",
    "text_sub": "#8a7f8c",
    "text_muted": "#a89eaa",
    "text_label": "#D17F95",
    "text_accent": "#4f46e5",
    "text_h2": "#3730a3",
    "text_h3": "#4338ca",
    "text_quiet": "#475569",
    "text_tool_name": "#0f172a",
    "text_th": "#1f2937",
    "text_think_head": "#64748b",
    "text_think_pre": "#374151",
    "text_terminal_head": "#94a3b8",
    "text_terminal_cmd": "#a5f3fc",
    "text_terminal_out": "#cbd5e1",
    "text_exec_head": "#0891b2",
    "text_exec_ok": "#16a34a",
    "text_exec_fail": "#dc2626",
    "fg_terminal": "#e2e8f0",
    "text_file_edit": "#78350f",
    "text_file_edit_head": "#b45309",
    "text_file_edit_size": "#92400e",
    "text_system": "#64748b",
    "text_summary_who": "#7c8aa0",
    "text_summary_ts": "#94a3b8",
    "text_summary_body": "#64748b",
    "text_dot": "#cbd5e1",
    "text_dot_active": "#6366f1",
    "text_preview": "#9ca3af",
    "text_mark_ok": "#16a34a",
    "text_mark_fail": "#dc2626",
    "text_mark_cancel": "#9ca3af",
    "text_mark_rejected": "#a45b2e",
    "text_row_ok": "#15803d",
    "text_row_fail": "#b91c1c",
    "text_plan_head": "#7c5cf0",
    "text_plan_body": "#2c2c38",
}

#: 暗色主题配色（与亮色**同一份规则表**，只换颜色）
_CHAT_MD_DARK = {
    "bg_page": "#1b1720",
    "bg_user": "#241f2b",
    "bg_panel": "#2b2536",
    "bg_soft": "#2a2433",
    "bg_quote": "#262238",
    "bg_code_inline": "#2f2b48",
    "bg_row_alt": "#262130",
    "bg_file_edit": "#3a2f18",
    "bg_plan": "#262036",
    "bg_voice": "#2a2533",
    "bg_terminal": "#0b1220",
    "border_line": "#3a3346",
    "border_soft": "#3b3547",
    "border_panel": "#423a52",
    "border_terminal": "#1f2a3d",
    "border_quote": "#4b4683",
    "border_summary": "#4a4a5e",
    "border_file_edit_block": "#b45309",
    "border_file_edit_row": "#b45309",
    "border_plan": "#4a3f66",
    "border_voice": "#3d3750",
    "accent": "#f0a8bd",
    "text_body": "#e8e4f2",
    "text_msg": "#e6e2f0",
    "text_strong": "#f6f3fc",
    "text_italic": "#d7d1e6",
    "text_sub": "#b6acc8",
    "text_muted": "#8f86a3",
    "text_label": "#f0a8bd",
    "text_accent": "#a5b4fc",
    "text_h2": "#c4b5fd",
    "text_h3": "#b7a6fb",
    "text_quiet": "#c3bcd6",
    "text_tool_name": "#f1eef8",
    "text_th": "#ece8f6",
    "text_think_head": "#a49bba",
    "text_think_pre": "#d7d1e6",
    "text_terminal_head": "#94a3b8",
    "text_terminal_cmd": "#7dd3fc",
    "text_terminal_out": "#cbd5e1",
    "text_exec_head": "#7dd3fc",
    "text_exec_ok": "#4ade80",
    "text_exec_fail": "#f87171",
    "fg_terminal": "#e2e8f0",
    "text_file_edit": "#fcd34d",
    "text_file_edit_head": "#fbbf24",
    "text_file_edit_size": "#d9a441",
    "text_system": "#a9a1bb",
    "text_summary_who": "#9fb0c4",
    "text_summary_ts": "#8d97a8",
    "text_summary_body": "#a6aec0",
    "text_dot": "#4a4358",
    "text_dot_active": "#f0a8bd",
    "text_preview": "#9a92ab",
    "text_mark_ok": "#4ade80",
    "text_mark_fail": "#f87171",
    "text_mark_cancel": "#9ca3af",
    "text_mark_rejected": "#e0a06a",
    "text_row_ok": "#4ade80",
    "text_row_fail": "#f87171",
    "text_plan_head": "#c4b5fd",
    "text_plan_body": "#e2ddf0",
}

#: 聊天正文样式表（亮色，默认）
CHAT_MD_CSS = _CHAT_MD_TEMPLATE.safe_substitute(**_CHAT_MD_LIGHT)

#: 聊天正文样式表（暗色）—— 与亮色共用同一份规则表
CHAT_MD_CSS_DARK = _CHAT_MD_TEMPLATE.safe_substitute(**_CHAT_MD_DARK)


def chat_md_css(theme: str = "light") -> str:
    """按主题取聊天正文样式表（"light" / "dark"）。"""
    return CHAT_MD_CSS_DARK if str(theme).lower() == "dark" else CHAT_MD_CSS




# v37: 工具解锁窗
# 三态用 3 种颜色 (GOLD 当前 / MINT 已完成 / LOCKED 未解锁) — 视觉冲击力强,
# 让主人一眼看出"我现在到哪级 / 还差什么没解锁"
UNLOCK_QSS = f"""
QDialog {{
    background: transparent;
    font-family: {FONT};
    font-size: 10pt;
    color: {TEXT};
}}
QWidget#dialog_body {{
    background: transparent;
    font-family: {FONT};
    font-size: 10pt;
    color: {TEXT};
}}
QLabel#header_title {{
    color: #3f3560; font-size: 17pt; font-weight: 700;
    background: transparent; border: none;
}}
QLabel#header_subtitle {{
    color: #6f6690; font-size: 9pt; font-weight: 500;
    background: transparent; border: none;
}}
QLabel#progress_text {{
    color: #4a3d6e; font-size: 11pt; font-weight: 700;
    background: transparent; border: none;
}}
QLabel#progress_bar {{
    background: #e4e1ee; border: none; border-radius: 4px;
    height: 8px;
}}
QLabel#next_hint {{
    color: #6f6690; font-size: 9pt;
    background: transparent; border: none;
}}

/* 三态卡片 — 颜色 token 化, 跟主系统一致 */
QFrame#card_done {{
    background: {MINT_BG};
    border: 1.5px solid {MINT_BORDER};
    border-radius: 12px;
}}
QFrame#card_current {{
    background: {GOLD_BG};
    border: 2px solid {GOLD_BORDER};
    border-radius: 12px;
}}
QFrame#card_locked {{
    background: {LOCKED_BG};
    border: 1.5px solid {LOCKED_BORDER};
    border-radius: 12px;
}}

QLabel#lv_done    {{ color: #2f7a44; font-size: 12pt; font-weight: 700; background: transparent; border: none; }}
QLabel#lv_current {{ color: #8a5a08; font-size: 12pt; font-weight: 700; background: transparent; border: none; }}
QLabel#lv_locked  {{ color: {LOCKED}; font-size: 12pt; font-weight: 700; background: transparent; border: none; }}

QLabel#reward_done    {{ color: #2f6b34; font-size: 10pt; background: transparent; border: none; }}
QLabel#reward_current {{ color: #8a5a08; font-size: 10pt; font-weight: 700; background: transparent; border: none; }}
QLabel#reward_locked  {{ color: {LOCKED}; font-size: 10pt; background: transparent; border: none; }}

QLabel#cnt {{
    color: {TEXT_SUB}; font-size: 9pt; background: transparent; border: none;
}}
QLabel#celebrate {{
    color: #4a3d6e; font-size: 9pt; font-style: italic;
    background: #f7f4ff; border-radius: 6px; padding: 5px 8px;
    border: 1px solid {BORDER};
}}
QLabel#chip {{
    font-size: 9pt; padding: 3px 8px; border-radius: 9px;
    background: #ffffff; border: 1px solid {BORDER};
    color: {TEXT};
}}
QPushButton#close_btn {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #a78bfa, stop:1 {ACCENT});
    color: #ffffff; border: none;
    border-radius: 8px; padding: 7px 26px; font-weight: 600;
}}
QPushButton#close_btn:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 #9a7ef8, stop:1 {ACCENT_DK});
}}
QPushButton#close_btn:pressed {{ background: {ACCENT_DK}; }}
"""


#  菜单（右键 / 托盘）
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


def warn(parent, title: str, text: str) -> None:
    """带皮肤的警告框。"""
    from PyQt5.QtWidgets import QMessageBox

    box = QMessageBox(QMessageBox.Icon.Warning, title, text, QMessageBox.StandardButton.Ok, parent)
    box.setStyleSheet(MESSAGE_BOX_QSS)
    box.exec()


def style_menu(menu: QMenu) -> None:
    """给菜单（及其子菜单）套用圆角卡片皮肤。"""
    menu.setStyleSheet(MENU_QSS)
    # 圆角需要透明背景；去掉 Windows 系统矩形阴影避免圆角四角露出黑边
    menu.setWindowFlags(
        menu.windowFlags()
        | Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.NoDropShadowWindowHint
    )
    menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    for child in menu.findChildren(QMenu):
        style_menu(child)
