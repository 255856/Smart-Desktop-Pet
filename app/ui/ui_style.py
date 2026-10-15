"""统一 UI 皮肤：聊天面板 / 设置面板 / 右键菜单 / 托盘菜单共用。"""
from __future__ import annotations

from pathlib import Path

from app.core.qt_compat import Qt, QMenu

# 图标资源（QSS 的 image: url 对 SVG 支持不稳定，统一用 PNG）
# ui_style.py 位于 app/ui/，项目根需再上一层
_ICON_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "icons"
_ARROW_URL = (_ICON_DIR / "arrow_down.png").as_posix()
_ARROW_UP_URL = (_ICON_DIR / "arrow_up.png").as_posix()
_CHECK_URL = (_ICON_DIR / "check.png").as_posix()

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

#  QSpinBox/QDoubleSpinBox 上下箭头：之前只给了 width + 透明背景，没给图标，
#  箭头位置是纯空白（看起来像坏了）。用与下拉箭头同源的 arrow_up/arrow_down。
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

#  QMessageBox：项目里 QMessageBox.warning 用在 file_dialog / pet_window 等处，
#  此前完全没套皮肤，深紫界面里弹出一个 Windows 原生灰框非常突兀。
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

#  两个面板共用的收尾规则：提示气泡、裸 QLabel 透明、通用工具按钮、禁用态描边。
#  这些控件在聊天页/设置页都在用，此前只有部分 objectName 变体有样式。
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

#  聊天窗
CHAT_QSS = f"""
QWidget {{
    font-family: {FONT};
    font-size: 10pt;
    color: {TEXT};
}}
QMainWindow {{ background: transparent; }}
QWidget#chat_root {{ background: transparent; }}

/* 聊天气泡区 + 输入容器 - 2026-10-15 v2:统一为同一套视觉风格,
   让输入框与显示框"搭得上"(用户实测:输入框与显示框不搭)。 */
QTextBrowser#chat_view, QFrame#input_container {{
    background: #fbfbfd;
    border: 1px solid #e7e3f5;
    border-radius: 14px;
    selection-background-color: #dcd4ff;
}}
QTextBrowser#chat_view {{
    padding: 10px 8px;
}}
QTextBrowser#chat_view:focus,
QFrame#input_container:focus-within {{
    border-color: {ACCENT_LT};
}}

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


#  MiniMax Code 风格的聊天窗样式（线性流 + 完整 markdown 渲染）
#  - 不再用 table + 气泡；每条消息是 div.msg 块，msg-label + msg-body 垂直堆叠
#  - msg-body 内支持标准 markdown 元素（h1-h4 / ul / ol / pre / blockquote / hr / table）
#  - 工具/思考/命令/文件编辑块用专用 class，整段使用浅灰/深色背景
#
# 类名约定（tests/test_chat_rendering.py 会断言）:
#   msg / msg-user / msg-assistant
#   msg-label / msg-body
#   tools / thinking / execs / file-edits
#   system-msg  (居中浅灰条)
CHAT_MD_CSS = (
    # 基础排版: 跟 MiniMax Code 类似的中性灰底
    "body{margin:0;padding:8px 14px;background:#ffffff;"
    "font-family:'Microsoft YaHei UI','Microsoft YaHei',"
    "'PingFang SC','SF Pro Text','Segoe UI',sans-serif;"
    "font-size:11pt;line-height:1.65;color:#1f2937;}"
    # 消息主块: 上下 padding, 底部 1px 分隔线 (MiniMax Code 风格)
    ".msg{padding:14px 4px 12px 4px;border-bottom:1px solid #eef0f4;}"
    ".msg:last-child{border-bottom:none;}"
    # 角色微调: user 有非常浅的灰底背景, assistant 纯白 (MiniMax Code 同款)
    ".msg-user{background:#fafbfc;}"
    ".msg-assistant{background:#ffffff;}"
    # 2026-10-15 v2: 用户消息整体靠右 (主人要求 "我发出的消息应该要保持在右侧")
    # 用 inline-block + max-width 让 user 块像气泡一样收在右半边
    ".msg-user .msg-label{text-align:right;color:#475569;}"
    ".msg-user .msg-body{text-align:right;}"
    ".msg-assistant .msg-label{text-align:left;}"
    ".msg-assistant .msg-body{text-align:left;}"
    # 角色标签 (You / 桌宠) + 时间
    ".msg-label{font-size:9pt;font-weight:600;color:#6366f1;"
    "letter-spacing:0.5px;margin-bottom:5px;}"
    ".msg-user .msg-label{color:#475569;}"
    ".msg-label .ts{font-weight:400;color:#94a3b8;margin-left:8px;"
    "font-size:8.5pt;letter-spacing:0;}"
    # 2026-10-15 v2: user 块右对齐 — 用 margin-left:auto 把内容挤到右边,
    # 但 msg-body 里的多行文本仍走 text-align:right, 这样图片/链接/代码块
    # 等会自然收在右半边
    ".msg-user .msg-label, .msg-user .msg-body{display:block;"
    "margin-left:auto;width:fit-content;max-width:75%;}"
    # 2026-10-15 v2: assistant 块左对齐 — 全宽, margin-right:auto 让它从左边起
    ".msg-assistant .msg-label, .msg-assistant .msg-body{display:block;"
    "margin-right:auto;width:fit-content;max-width:90%;}"
    # 消息正文容器
    ".msg-body{font-size:11pt;line-height:1.7;color:#1f2937;}"
    ".msg-body p{margin:7px 0;}"
    # markdown 元素
    ".msg-body h1,.msg-body h2,.msg-body h3,.msg-body h4"
    "{font-weight:600;line-height:1.3;margin:14px 0 6px 0;color:#111827;}"
    ".msg-body h1{font-size:18pt;border-bottom:1px solid #e5e7eb;"
    "padding-bottom:6px;}"
    ".msg-body h2{font-size:14pt;color:#3730a3;}"
    ".msg-body h3{font-size:12.5pt;color:#4338ca;}"
    ".msg-body h4{font-size:11.5pt;color:#4f46e5;}"
    # 列表 (MiniMax Code 缩进风格)
    ".msg-body ul,.msg-body ol{padding-left:24px;margin:6px 0;}"
    ".msg-body li{margin:3px 0;line-height:1.6;}"
    ".msg-body li>p{margin:2px 0;}"
    # 行内代码 (MiniMax Code 风格: 浅紫底色)
    ".msg-body code{background:#eef2ff;padding:1px 6px;border-radius:4px;"
    "font-family:Consolas,Monaco,'Courier New',monospace;"
    "font-size:0.9em;color:#4f46e5;}"
    # 代码块 (深色终端风格)
    ".msg-body pre{background:#0f172a;color:#e2e8f0;padding:12px 14px;"
    "border-radius:8px;font-family:Consolas,Monaco,'Courier New',monospace;"
    "font-size:9.5pt;line-height:1.5;overflow:auto;margin:8px 0;"
    "border:1px solid #1e293b;}"
    ".msg-body pre code{background:transparent;color:inherit;"
    "padding:0;font-size:inherit;}"
    # 引用块
    ".msg-body blockquote{border-left:3px solid #c7d2fe;"
    "background:#f5f7ff;padding:6px 14px;margin:8px 0;color:#475569;"
    "border-radius:0 4px 4px 0;}"
    ".msg-body blockquote p{margin:4px 0;}"
    # 表格 (兼容 GFM)
    ".msg-body table{border-collapse:collapse;margin:8px 0;}"
    ".msg-body th,.msg-body td{border:1px solid #e5e7eb;"
    "padding:6px 12px;text-align:left;}"
    ".msg-body th{background:#f8fafc;font-weight:600;color:#1f2937;}"
    ".msg-body tr:nth-child(even) td{background:#fafbfc;}"
    # 分隔线
    ".msg-body hr{border:none;border-top:1px solid #e5e7eb;margin:12px 0;}"
    # 链接
    ".msg-body a{color:#4f46e5;text-decoration:none;}"
    ".msg-body a:hover{text-decoration:underline;}"
    # 加粗 / 斜体
    ".msg-body strong{font-weight:600;color:#111827;}"
    ".msg-body em{font-style:italic;color:#374151;}"
    # 工具调用 (已执行操作) 卡片 - 仍类似 Claude Code 的紧凑小卡片
    "div.tools{font-size:9.5pt;color:#1f2937;background-color:#f1f5f9;"
    "border:1px solid #e2e8f0;border-radius:6px;padding:8px 12px;"
    "margin:6px 0 10px 0;}"
    "div.tools .tool-row{display:block;margin:2px 0;}"
    "div.tools .tool-row .tool-name{color:#0f172a;font-weight:600;}"
    "div.tools .tool-row .tool-target{color:#475569;}"
    "div.tools .tool-row.failed .tool-name{color:#b91c1c;}"
    "div.tools .tool-row.ok .tool-name{color:#15803d;}"
    # 思考过程卡片
    "div.thinking{font-size:9.5pt;color:#475569;background-color:#f8fafc;"
    "border-left:3px solid #94a3b8;padding:8px 12px;margin:4px 0 8px 0;"
    "border-radius:0 4px 4px 0;}"
    "div.thinking .thinking-head{color:#64748b;font-weight:600;"
    "margin-bottom:2px;}"
    # 命令执行块 (终端黑色风格)
    "div.execs{background:#0f172a;color:#e2e8f0;padding:10px 14px;"
    "border-radius:6px;font-family:Consolas,Monaco,'Courier New',monospace;"
    "font-size:9.5pt;line-height:1.5;margin:6px 0;border:1px solid #1e293b;}"
    "div.execs .execs-head{color:#94a3b8;font-size:8.5pt;margin-bottom:4px;}"
    "div.execs .execs-cmd{color:#a5f3fc;}"
    "div.execs .execs-out{color:#cbd5e1;white-space:pre-wrap;}"
    # 文件编辑块 (黄色提示风格)
    "div.file-edits{font-size:9.5pt;color:#78350f;background-color:#fef3c7;"
    "border-left:3px solid #f59e0b;padding:8px 12px;margin:6px 0;"
    "border-radius:0 4px 4px 0;}"
    "div.file-edits .file-edits-head{color:#b45309;font-weight:600;"
    "margin-bottom:2px;}"
    # 系统消息: 居中浅灰条
    "span.system-msg{display:inline-block;background:#f1f5f9;"
    "color:#64748b;padding:4px 12px;border-radius:12px;font-size:9pt;"
    "margin:6px 0;}"
    # 旧的"气泡"结构 (历史消息可能仍渲染): 保持视觉一致
    "td.bubble{line-height:1.55;font-size:10pt;}"
    # 三点跳动等待动画
    ".typing-dot{display:inline-block;width:6px;height:6px;"
    "border-radius:50%;margin:0 2px;background:#cbd5e1;vertical-align:middle;}"
    ".typing-dot.active{background:#6366f1;}"
)

#  设置面板
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
    margin-top: 20px;
    padding: 18px 14px 14px 14px;
    font-weight: 600;
}}
/* 标题给实底色，才能盖住边框线形成「开口」的分组感；
   透明背景时 1px 边框会从字上横穿过去，看着像被切掉半个字 */
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    padding: 2px 10px;
    color: {ACCENT_DK};
    background: {CARD};
    border-radius: 6px;
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
/* 文本输入框：设置页有 8 个 QLineEdit，此前完全没有规则 —— 在紫白卡片里
   夹着 Windows 原生方框，是「界面没做完」观感的主要来源 */
QLineEdit {{
    background: #ffffff;
    border: 1.5px solid #e0e0ee;
    border-radius: 10px;
    padding: 7px 12px;
    min-height: 20px;
    selection-background-color: #dcd4ff;
    selection-color: {TEXT};
}}
QLineEdit:hover {{ border-color: {ACCENT_LT}; }}
QLineEdit:focus {{ border-color: {ACCENT}; background: #fefeff; }}
QLineEdit:disabled {{ background: #f4f4f8; color: {TEXT_SUB}; border-color: #eceaf2; }}
QLineEdit[readOnly="true"] {{ background: #fafafd; color: {TEXT_SUB}; }}

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

/* 禁用态：Tab / 分组 / 复选框 / 进度条 —— 之前只有 QPushButton 有 :disabled */
QTabBar::tab:disabled {{ color: #cfcedd; }}
QTabBar::tab:disabled:selected {{ background: #e9e8f2; color: #b3b1c6; }}
QGroupBox:disabled {{ color: #b6b4c8; background: #f7f7fb; }}
QGroupBox::title:disabled {{ color: #c9c7da; }}
QCheckBox:disabled {{ color: #c3c1d4; }}
QCheckBox::indicator:disabled {{ background: #f2f2f7; border-color: #e2e0ec; }}
QCheckBox::indicator:checked:disabled {{ background: #d8d3f5; border-color: #d8d3f5; }}
QProgressBar {{
    background: #ecebf3; border: 1px solid #e4e3ee; border-radius: 8px;
    min-height: 14px; text-align: center; font-size: 8pt; color: {TEXT_SUB};
}}
QProgressBar::chunk {{ border-radius: 8px; background: {ACCENT_LT}; }}

/* 滚动区域与窗口卡片融为一体（模型配置 Tab 内容较多） */
QScrollArea {{ background: transparent; border: none; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}

{_SPINBOX_ARROWS_QSS}
{COMMON_QSS}
"""

# 状态条配色（体力/饱食/口渴/心情/健康/好感）
# 状态条渐变配色（左浅右深）：体力/饱食/口渴/心情/健康/好感
STAT_BAR_COLORS = {
    "strength":   ("#fbbf24", "#f59e0b"),
    "food":       ("#34d399", "#10b981"),
    "drink":      ("#60a5fa", "#3b82f6"),
    "feeling":    ("#f9a8d4", "#ec4899"),
    "health":     ("#f87171", "#ef4444"),
    "likability": ("#c4b5fd", "#8b5cf6"),
}


def stat_bar_qss(light: str, dark: str) -> str:
    return (
        "QProgressBar { background:#ecebf3; border:1px solid #e4e3ee; border-radius:8px; }"
        " QProgressBar::chunk { border-radius:8px; background:"
        "qlineargradient(x1:0,y1:0,x2:1,y2:0,"
        f" stop:0 {light}, stop:1 {dark}); }}"
    )


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
    """带皮肤的警告框。

    项目里 QMessageBox.warning 直接调用会弹出 Windows 原生灰框，在紫白
    界面里非常突兀。统一走这里，保证所有警告框外观一致。
    """
    from PyQt5.QtWidgets import QMessageBox

    box = QMessageBox(QMessageBox.Icon.Warning, title, text, QMessageBox.StandardButton.Ok, parent)
    box.setStyleSheet(MESSAGE_BOX_QSS)
    box.exec()


def style_menu(menu: QMenu) -> None:
    """给菜单（及其子菜单）套用圆角卡片皮肤。"""
    menu.setStyleSheet(MENU_QSS)
    # 圆角需要透明背景，否则 QSS 圆角外露出系统底色；
    # 同时去掉 Windows 系统矩形阴影（阴影层是矩形，会在圆角四个角露出黑边）。
    menu.setWindowFlags(
        menu.windowFlags()
        | Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.NoDropShadowWindowHint
    )
    menu.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    for child in menu.findChildren(QMenu):
        style_menu(child)
