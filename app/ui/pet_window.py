"""桌宠主窗口（重写）—— VPet 风格

特性：
    - 透明无边框置顶
    - VPet 同款帧动画（QTimer.singleShot 按帧持续时间驱动）
    - 触摸热区（head/body/drag）
    - 拖动 = raise（抓住角色整只移动）
    - 单击身体不同部位触发不同反应
    - 头顶气泡
    - 移动动画（由 motion.py 驱动）
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from app.core.qt_compat import (
    QAction, QApplication, QColor, QCursor, QDragEnterEvent, QDragLeaveEvent,
    QDragMoveEvent, QDropEvent, QEvent, QFont, QHBoxLayout, QImage, QLabel, QMenu,
    QMouseEvent, QPainter, QPainterPath, QPixmap, QPoint, QProgressBar,
    QRegion, QSize, QSizePolicy, Qt, QTimer, QWidget, QVBoxLayout, Signal, QLineEdit,
    QGraphicsDropShadowEffect,
    event_global_pos, event_local_pos,
)
from app.animation.animations import Animation, Frame
from app.ui import ui_style

log = logging.getLogger(__name__)


# 缓存：{(pixmap_id, width, height): scaled_pixmap}
# pixmap_id 用 id(pixmap) 或帧内容 hash 标识，避免每帧重新缩放
_scaled_pixmap_cache: dict[tuple[int, int, int], QPixmap] = {}
_CACHE_MAX_SIZE = 8  # 最多缓存 8 个缩放后的 pixmap


def _current_anim_ms(player) -> int:
    """取当前 player 动画的 frame 时长（用于右键菜单标当前帧率）。"""
    anim = player.current_animation()
    if not anim or not anim.frames:
        return 0
    idx = player._frame_idx
    if idx < 0 or idx >= len(anim.frames):
        return 0
    return anim.frames[idx].duration_ms


def _scale_pixmap_keep_alpha(pix: QPixmap, size: QSize, cache_key: tuple = None) -> QPixmap:
    """等比缩放 QPixmap 同时**保留 alpha 并消除白点**。

    PyQt5 下 ``QPixmap.scaled(Qt.SmoothTransformation)`` 会丢 alpha，导致
    透明 PNG 缩放后背景变白。这里先转 QImage（保留 alpha），QImage 缩放
    （保 alpha），再扫一遍"a != 0 但 RGB 全接近 255"的边缘像素，把它们
    的 a 强制设为 0 —— 否则在透明窗口底色上看着就是"白点闪烁"。

    使用 cache_key 可以缓存缩放结果，避免每帧重复计算。
    """
    if pix.isNull():
        return pix

    # 如果有 cache_key 且缓存命中，直接返回
    if cache_key is not None and cache_key in _scaled_pixmap_cache:
        return _scaled_pixmap_cache[cache_key]

    try:
        import numpy as np
        img = pix.toImage().convertToFormat(QImage.Format.Format_ARGB32)
        scaled = img.scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
        # 扫白边像素 → 强制 a=0
        arr = np.array(scaled, copy=True)
        rgb = arr[..., :3]
        a = arr[..., 3]
        # "近白": RGB 全 >= 245 (留一些余地防止误伤真正浅色角色)
        white_mask = (
            (rgb[..., 0] >= 245) &
            (rgb[..., 1] >= 245) &
            (rgb[..., 2] >= 245)
        ) & (a > 0)
        arr[white_mask, 3] = 0
        from PIL import Image as _PI
        cleaned = _PI.fromarray(arr, "RGBA")
        result = QPixmap.fromImage(cleaned.toImage())
    except Exception:
        result = pix.scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)

    # 缓存结果
    if cache_key is not None:
        if len(_scaled_pixmap_cache) >= _CACHE_MAX_SIZE:
            # 简单 LRU：删除最旧的一个
            oldest_key = next(iter(_scaled_pixmap_cache))
            del _scaled_pixmap_cache[oldest_key]
        _scaled_pixmap_cache[cache_key] = result

    return result


def _clear_pixmap_cache() -> None:
    """清空缩放缓存（在切换角色或窗口尺寸变化时调用）。"""
    _scaled_pixmap_cache.clear()


# 触摸热区（在 sprite 坐标系内的相对比例，0.0-1.0）
# VPet 同款约定（GameCore.cs TouchArea 重构）
@dataclass
class TouchArea:
    """VPet 同款触摸热区（src: GameCore.cs TouchArea）。

    美术改 sprite 尺寸后，只要调整这里的比例即可，**不用改业务逻辑**。
    locate 和 size 都是 0.0-1.0 的相对比例，基于 SPRITE_SIZE 转换。
    """
    name: str                                  # 'head'/ 'body'/ 'raise'/ 自定义
    locate: tuple[float, float] = (0.0, 0.0)   # (x, y) 左上角，相对比例
    size: tuple[float, float] = (1.0, 1.0)     # (w, h) 矩形，相对比例
    on_click: Optional[Callable[[], None]] = None    # 单击 / 双击 触发的回调
    on_press: Optional[Callable[[], None]] = None    # 长按才触发（VPet IsPress）
    is_press: bool = False          # VPet 同款：True 表示要长按才触发 on_press
    priority: int = 0              # 同坐标命中时高 priority 优先

    def hit(self, sx: int, sy: int, sprite_size: tuple[int, int]) -> bool:
        """(sx, sy) 是 SPRITE_SIZE 坐标系下的点。

        把相对比例转为像素坐标后做矩形命中检测。
        """
        lx = self.locate[0] * sprite_size[0]
        ly = self.locate[1] * sprite_size[1]
        lw = self.size[0] * sprite_size[0]
        lh = self.size[1] * sprite_size[1]
        return lx <= sx <= lx + lw and ly <= sy <= ly + lh


# 兼容老代码（仍用字符串 'head' / 'body' / 'raise' 标识）
class HitZone:
    HEAD = "head"
    BODY = "body"
    RAISE = "raise"
class PetWindow(QWidget):
    """桌宠本体。"""
    chat_requested = Signal()
    quit_requested = Signal()
    reaction_requested = Signal(str)   # 触摸了 head / body
    # PR-right-click-settings: 让 main.py 弹出 SettingsWindow
    open_settings_requested = Signal()
    # PR-right-click-fps: 让 main.py 跑 retune_ms + reload atlas
    retune_ms_requested = Signal(int)
    # PR-right-click-settings: 动态改缩放 / crossfade / lock_idle
    scale_changed = Signal(float)
    crossfade_toggled = Signal(bool, int)
    lock_first_idle_toggled = Signal(bool)

    # 鼠标进入/离开桌宠窗（PR-mute-motion：进入后不再自主运动）
    mouse_entered = Signal()
    mouse_left = Signal()

    # 状态栏显示/隐藏（PR-status-overlay：VPet 同款状态条）
    status_bar_toggled = Signal(bool)  # True = 显示，False = 隐藏

    # 吃饭（右键菜单触发：播放吃饭动画 + 主程序涨饱食/体力）
    eat_requested = Signal()

    # 快捷聊天输入（底部输入框发送消息）
    chat_input_sent = Signal(str)

    # sprite 设计尺寸（与生成器一致）
    SPRITE_SIZE = QSize(1000, 1000)

    def __init__(self, sprite_dir: str | Path,
                 fallback_image: str | Path | None = None,
                 scale: float = 0.4,
                 always_on_top: bool = True,
                 renderer_type: str = "sprite",
                 live2d_model_dir: str | Path | None = None,
                 live2d_hide_watermark: bool = True,
                 live2d_random_exp_cfg: dict | None = None,
                 live2d_max_fps: int = 30):
        super().__init__()
        # 计算窗口尺寸（基于 sprite 设计尺寸 × 缩放）
        self._window_size = QSize(
            int(self.SPRITE_SIZE.width() * scale),
            int(self.SPRITE_SIZE.height() * scale),
        )
        self._scale = scale

        # 创建渲染器（sprite / live2d 由 cfg 决定，live2d 失败 fallback sprite）
        from app.animation.renderer_factory import create_renderer
        self.renderer = create_renderer(
            renderer_type=renderer_type,
            sprite_dir=Path(sprite_dir),
            fallback_image=Path(fallback_image) if fallback_image else None,
            window_size=self._window_size,
            scale=scale,
            live2d_model_dir=Path(live2d_model_dir) if live2d_model_dir else None,
            live2d_hide_watermark=live2d_hide_watermark,
            live2d_random_exp_cfg=live2d_random_exp_cfg,
            live2d_max_fps=live2d_max_fps,
        )
        self.animator = self.renderer  # 旧代码兼容：self.animator.xxx() 仍可用

        # 表情包贴纸（live2d 模型自带表情包时，随机弹在右上角）
        self._sticker_label: QLabel | None = None
        self._sticker_files: list[str] = []
        self._sticker_timer: QTimer | None = None
        self._sticker_cache: dict[str, QPixmap] = {}
        self._stickers_enabled = True
        self._setup_sticker_overlay()

        # live2d：头部/眼睛跟随鼠标（驱动物理链，角色才"活"）
        self._setup_look_at()

        # live2d：透明区域点击穿透（按渲染 alpha 定时生成窗口遮罩）
        self._setup_click_mask()
        self._mask_timer = None

        # 把渲染器的 widget 嵌入到 PetWindow
        display_widget = self.renderer.get_widget()
        if display_widget.parent() is None:
            display_widget.setParent(self)
        self._sprite_label = display_widget
        self._sprite_label.setGeometry(0, 0, self._window_size.width(), self._window_size.height())
        if hasattr(self._sprite_label, 'setAlignment'):
            self._sprite_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if hasattr(self._sprite_label, 'setStyleSheet'):
            self._sprite_label.setStyleSheet("background-color: transparent;")

        # PR-fix-drag-menu (live2d): QWebEngineView 在 PetWindow 内会拦截鼠标事件。
        # 实测真实鼠标事件落在 QWebEngineView 内部一个全屏覆盖的 Chromium delegate
        # 子 QWidget 上（不冒泡到 view），只给 view 本身装过滤器会漏掉拖动、点击、
        # 右键菜单和文件拖拽。这里递归覆盖 view 及其所有后代；eventFilter 里还会
        # 通过 ChildAdded 为页面加载后动态创建的子控件补装。
        self._install_display_input_filters(self._sprite_label)
        # sprite 模式保存旧引用以便像素路径不破
        if hasattr(self, 'atlas'):
            _clear_pixmap_cache()

        # 气泡 label（替代 _draw_bubble）—— v2 美化：白底大圆角 + 淡紫描边 + 柔和阴影 + 尖角
        self._bubble_label = QLabel(self)
        self._bubble_label.setStyleSheet(
            "QLabel { background-color: rgba(255,255,255,242); color: #28283c; "
            "border: 1px solid #e4dff5; "
            "border-radius: 12px; padding: 6px 12px; font-weight: bold; "
            "font-family: 'Microsoft YaHei', sans-serif; font-size: 11px; }")
        self._bubble_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._bubble_shadow = QGraphicsDropShadowEffect(self._bubble_label)
        self._bubble_shadow.setBlurRadius(18)
        self._bubble_shadow.setOffset(0, 3)
        self._bubble_shadow.setColor(QColor(70, 60, 140, 50))
        self._bubble_label.setGraphicsEffect(self._bubble_shadow)
        self._bubble_label.hide()

        # 气泡尖角（小三角，与气泡同色，贴合气泡底部）
        self._bubble_tail = QLabel(self)
        _tail = QPixmap(18, 12)
        _tail.fill(Qt.GlobalColor.transparent)
        _tp = QPainter(_tail)
        _tp.setRenderHint(QPainter.RenderHint.Antialiasing)
        _tp.setPen(Qt.PenStyle.NoPen)
        _tp.setBrush(QColor(255, 255, 255, 242))
        _path = QPainterPath()
        _path.moveTo(2, 1)
        _path.lineTo(16, 1)
        _path.lineTo(9, 11)
        _path.closeSubpath()
        _tp.drawPath(_path)
        _tp.end()
        self._bubble_tail.setPixmap(_tail)
        self._bubble_tail.hide()

        # 气泡
        self._bubble_text: str = ""
        self._bubble_until: float = 0.0
        self._streaming_bubble: bool = False

        # VPet 同款状态栏（透明背景，小进度条显示关键指标）
        self._status_bar: Optional[QWidget] = None
        self._status_bars: dict[str, QProgressBar] = {}
        self._attached_state = None
        self._status_visible = False

        # 快捷聊天输入框（底部简单输入框）
        self._chat_input: Optional[QLineEdit] = None

        # 拖动
        self._dragging = False
        self._system_moving = False
        self._drag_start = QPoint()
        # 用户交互时间戳（PR-mute-motion：press / drag / hover 都算）
        self._last_user_interaction_ts = 0.0
        self._user_inside = False
        # 帧计时器（用 singleShot 按帧持续时间驱动，VPet 同款方式）
        self._frame_timer: Optional[QTimer] = None

        self._build_window(always_on_top)
        # 默认播放待机
        self.animator.set_idle()
        # 立即显示第一帧并启动帧计时器
        self._start_frame_timer()

    # ---------------- 窗口初始化 ----------------
    def _build_window(self, always_on_top: bool) -> None:
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        # PR-fix-window-invisible:
        # 之前用 WA_NoSystemBackground + WA_TranslucentBackground + Frameless 三件套让桌宠
        # 在某些 Windows 系统（DWM 未启用 / 多显示器 / 远程桌面 / 高 DPI 缩放）下渲染成
        # 100% 透明完全看不到。这里：
        #   - 去掉 WA_NoSystemBackground（让 Qt 自己绘制窗口背景）
        #   - 保留 WA_TranslucentBackground（让 PNG / WebView 的透明区域能透出桌面）
        #   - 配合 setStyleSheet 设背景色：sprite 用 windowBackground role；live2d 用纯白
        # 这样 PetWindow 永远有一层可见背景，绝不会再"看不见"。
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFixedSize(self._window_size)
        self.setMouseTracking(True)
        # PR-drag-drop: 让 widget 接收系统拖拽
        self.setAcceptDrops(True)

        # PR-V2: VPet 同款 TouchArea（src: GameCore.cs TouchArea）
        # 每个区域带 on_click / on_press 回调，加 sprite 改尺寸不用改业务代码。
        self._init_touch_areas()

    def _init_touch_areas(self) -> None:
        """配置触摸热区（VPet 同款 TouchArea 数据驱动）。

        美术改了 sprite 尺寸后，只要调整这里的坐标 + size 即可——业务逻辑
        （mouseReleaseEvent 等）不用动。
        """
        self.touch_areas: list[TouchArea] = [
            TouchArea(
                name=HitZone.HEAD,
                locate=(0.28, 0.06), size=(0.44, 0.36),  # 头部区域
                on_click=self._on_touch_head_click,
                on_press=None, is_press=False,
            ),
            TouchArea(
                name=HitZone.BODY,
                locate=(0.22, 0.42), size=(0.56, 0.30),  # 身体区域
                on_click=self._on_touch_body_click,
                on_press=None, is_press=False,
            ),
            TouchArea(
                name=HitZone.RAISE,
                locate=(0.18, 0.72), size=(0.64, 0.28),  # 整只脚 / 拖动区
                on_click=self._on_touch_raise_click,
                on_press=None, is_press=False,
            ),
        ]

    # ---- TouchArea 回调 ----
    def _on_touch_head_click(self) -> None:
        self.animator.play_reaction('head')
        self.reaction_requested.emit('head')

    def _on_touch_body_click(self) -> None:
        self.animator.play_reaction('body')
        self.reaction_requested.emit('body')

    def _on_touch_raise_click(self) -> None:
        self.chat_requested.emit()

    def _on_chat_input_sent(self) -> None:
        """底部输入框回车发送消息到聊天。"""
        if self._chat_input is None:
            return
        text = self._chat_input.text().strip()
        if text:
            self.chat_input_sent.emit(text)
            self._chat_input.clear()
            self._chat_input.hide()

    # ---------------- 公开 API ----------------
    def play_reaction(self, where: str) -> None:
        """由外部调用（chat reply / mood trigger）：play reaction。"""
        self.animator.play_reaction(where)

    def play_emotion(self, name: str) -> None:
        self.animator.set_emotion(name)

    def set_idle(self) -> None:
        self.animator.set_idle()

    def set_drag_image(self, pixmap: QPixmap, duration_ms: int = 200) -> None:
        """播放用户拖进窗口的「拖拽图」作为一次性动画。"""
        if pixmap is None or pixmap.isNull():
            return
        anim = Animation(
            name="drag_dropped",
            frames=[Frame(pixmap=pixmap, duration_ms=duration_ms)],
            mode=Animation.ONCE,
        )
        player = getattr(self.renderer, 'player', None)
        if player is not None:
            player.play_one_shot(anim, on_finished=self.animator.set_idle)
        self._start_frame_timer()  # 重启帧计时器
        log.info("拖拽图已切换显示")

    def _place_bubble(self) -> None:
        """把气泡 + 尖角一起居中定位在窗口顶部。"""
        self._bubble_label.adjustSize()
        bw = self._bubble_label.width()
        bx = (self._window_size.width() - bw) // 2
        self._bubble_label.move(bx, 10)
        tw = self._bubble_tail.width()
        self._bubble_tail.move(bx + (bw - tw) // 2,
                               10 + self._bubble_label.height() - 4)
        self._bubble_tail.show()

    def show_bubble(self, text: str, duration_ms: int = 4000) -> None:
        self._bubble_text = text if len(text) <= 60 else text[:57] + "…"
        self._bubble_until = time.time() + duration_ms / 1000.0
        self._bubble_label.setText(self._bubble_text)
        self._place_bubble()
        self._bubble_label.show()

    def show_streaming_bubble(self, text: str) -> None:
        """流式输出气泡：展示模型正在生成的文本（最多 120 字），不自动隐藏。"""
        self._streaming_bubble = True
        # 折叠换行和多余空白，避免气泡里出现大段空白
        display = re.sub(r'\s+', '', text).strip()
        if len(display) > 120:
            display = display[:117] + "…"
        self._bubble_text = display
        self._bubble_until = time.time() + 9999.0  # 不自动隐藏
        self._bubble_label.setText(display)
        self._place_bubble()
        self._bubble_label.show()

    def stop_streaming_bubble(self) -> None:
        """流式输出结束：隐藏气泡，或保留 3 秒后自动消失。"""
        self._streaming_bubble = False
        if self._bubble_text:
            # 保留最后内容 3 秒
            self._bubble_until = time.time() + 3.0

    def _build_status_bar(self) -> None:
        """构建状态栏：4 行，每行文字+进度条，左下角显示。"""
        if self._status_bar is not None:
            return
        self._status_bar = QWidget(self)
        self._status_bar.setStyleSheet(
            "QWidget { background-color: rgba(58,46,110,160); border: 1px solid "
            "rgba(255,255,255,45); border-radius: 10px; }")
        layout = QVBoxLayout(self._status_bar)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(3)

        bar_style = (
            "QProgressBar {{ background: rgba(255,255,255,26); border: none; "
            "border-radius: 3px; height: 6px; }}"
            "QProgressBar::chunk {{ border-radius: 3px; background: {color}; }}")
        label_style = "color: rgba(255,255,255,190); font-size: 10px; font-family: 'Microsoft YaHei', sans-serif;"
        rows = [
            ("体力", "strength", "rgba(100,200,150,160)"),
            ("饱食", "food",     "rgba(200,180,80,160)"),
            ("口渴", "drink",    "rgba(80,160,220,160)"),
            ("心情", "feeling",  "rgba(200,120,80,160)"),
        ]
        for text, key, color in rows:
            row = QHBoxLayout()
            row.setSpacing(6)
            lbl = QLabel(text)
            lbl.setStyleSheet(label_style)
            lbl.setFixedWidth(48)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(100)
            bar.setFormat("")
            bar.setStyleSheet(bar_style.format(color=color))
            bar.setFixedHeight(6)
            self._status_bars[key] = bar
            row.addWidget(lbl)
            row.addWidget(bar, 1)
            layout.addLayout(row)

        # 左下角
        self._status_bar.adjustSize()
        self._status_bar.hide()

    def toggle_status_bar(self, visible: bool) -> None:
        """显示/隐藏状态栏。"""
        if self._status_bar is None:
            self._build_status_bar()
        self._status_visible = visible
        if visible:
            # 每次显示时重新定位到左下角（窗口尺寸可能变化）
            self._status_bar.adjustSize()
            bw = self._status_bar.width()
            bh = self._status_bar.height()
            self._status_bar.move(2, self._window_size.height() - bh - 2)
            self._status_bar.show()
        else:
            self._status_bar.hide()
        self.status_bar_toggled.emit(visible)

    def attach_state(self, state) -> None:
        """挂载 PetState，状态变化自动刷新状态栏。"""
        if self._attached_state is state:
            return
        self._attached_state = state
        if state is not None:
            # 在原有 on_change 上叠加刷新 UI，不覆盖其他回调
            original_on_change = getattr(state, 'on_change', None)
            def _combined():
                self._refresh_status_bar()
                if original_on_change is not None:
                    try:
                        original_on_change()
                    except Exception:  # noqa: BLE001
                        pass
            state.on_change = _combined
            self._refresh_status_bar()

    def _refresh_status_bar(self) -> None:
        """刷新状态栏进度条。"""
        if self._attached_state is None or not self._status_visible:
            return
        s = self._attached_state
        self._status_bars["strength"].setValue(int(s.strength))
        self._status_bars["food"].setValue(int(s.strength_food))
        self._status_bars["drink"].setValue(int(s.strength_drink))
        self._status_bars["feeling"].setValue(int(s.feeling))

    def _build_chat_input(self) -> None:
        """构建底部快捷聊天输入框。"""
        if self._chat_input is not None:
            return
        self._chat_input = QLineEdit(self)
        self._chat_input.setPlaceholderText("输入消息...")
        self._chat_input.setStyleSheet(
            "QLineEdit { background-color: rgba(255,255,255,200); border: 1px solid ""rgba(0,0,0,50); border-radius: 4px; padding: 4px 8px; font-size: 11px; }")
        self._chat_input.returnPressed.connect(self._on_chat_input_sent)
        # 定位到窗口底部
        self._chat_input.setFixedHeight(28)
        self._chat_input.adjustSize()
        self._chat_input.hide()

    def toggle_chat_input(self, visible: bool) -> None:
        """显示/隐藏底部聊天输入框。"""
        if self._chat_input is None:
            self._build_chat_input()
        if visible:
            self._chat_input.setFixedWidth(self._window_size.width() - 4)
            self._chat_input.move(2, self._window_size.height() - self._chat_input.height() - 2)
            self._chat_input.show()
            self._chat_input.setFocus()
        else:
            self._chat_input.hide()

    # ---------------- 内部：帧推进 ----------------
    # PR-right-click-fps: 帧率统计（右键「显示 FPS」勾选时启用）
    _fps_enabled: bool = False
    _fps_count: int = 0
    _fps_window_start: float = 0.0
    _last_fps_report: float = 0.0

    def _start_frame_timer(self) -> None:
        """显示当前帧并按其 duration_ms 调度 advance（VPet 同款方式）。

        PR-bugfix：
            1. advance 在 setPixmap 之后立即调，**第 0 帧永远只显示一帧**——已
               把 advance 挪到 _on_frame_timeout 回调里。
            2. **「每次点击加速」真因**：之前每次 set_idle/set_walk/... 都新建一个
               QTimer 实例，**旧 timer 没被 stop**。多次点击 → 多个 timer
               同时 pending → _on_frame_timeout 被多次触发 → advance 频率
               倍增 → 动画加速 N 倍。
               现在先 stop 旧 timer 再 start 新 timer，保证同时只有 1 个 timer。

        Live2D 模式下不做任何事（由 JS 内部 requestAnimationFrame 驱动）。
        """
        player = getattr(self.renderer, 'player', None)
        if player is None:
            return
        anim = player.current_animation()
        if not anim or not anim.frames:
            return
        # 显示当前帧
        pix = player.current_pixmap()
        if pix and not pix.isNull():
            self._sprite_label.setPixmap(pix)
        # 当前帧停留时长（advance 前的帧）
        frame_idx = player._frame_idx
        if frame_idx < 0 or frame_idx >= len(anim.frames):
            return
        duration_ms = max(1, anim.frames[frame_idx].duration_ms)

        # 自动隐藏气泡（流式气泡在结束前不自动隐藏）
        if self._bubble_text and not self._streaming_bubble and time.time() >= self._bubble_until:
            self._bubble_text = ""
            self._bubble_label.hide()
            self._bubble_tail.hide()

        # PR-bugfix: 重用同一个 QTimer 实例。先 stop 旧 timer 避免多 timer 并发。
        if self._frame_timer is None:
            self._frame_timer = QTimer(self)
            self._frame_timer.setSingleShot(True)
            self._frame_timer.timeout.connect(self._on_frame_timeout)
        else:
            self._frame_timer.stop()
        self._frame_timer.start(duration_ms)

    def _on_frame_timeout(self) -> None:
        """单触发：advance 到下一帧 + 重新调度显示。"""
        player = getattr(self.renderer, 'player', None)
        if player is None:
            return
        self._fps_count += 1
        player._advance()
        if self._fps_enabled:
            now = time.monotonic()
            if self._fps_window_start == 0.0:
                self._fps_window_start = now
            elif now - self._fps_window_start >= 1.0:
                fps = self._fps_count / (now - self._fps_window_start)
                self._fps_count = 0
                self._fps_window_start = now
                # 每秒刷一次气泡显示
                if now - self._last_fps_report >= 1.0:
                    self.show_bubble(f"{fps:.1f} FPS")
                    self._last_fps_report = now
        self._start_frame_timer()

    # ---------------- 绘制（由 QLabel 完成，无需自定义 paintEvent）---------------
    # 但保留一个简单的 paintEvent 用于调试
    def paintEvent(self, evt) -> None:
        # QLabel 会处理精灵绘制，这里什么都不做
        pass

    # ---------------- 触摸热区（PR-V2: VPet TouchArea 数据驱动） ----------------
    def _hit_zone(self, pos: QPoint) -> Optional[TouchArea]:
        """把窗口坐标归一化到 sprite 1000x1000，按 priority 找命中的 TouchArea。

        返回 TouchArea 对象（None = 未命中）。优先级：priority 高者胜出。
        """
        if not self.touch_areas:
            return None
        sx = pos.x() / max(1, self.width()) * self.SPRITE_SIZE.width()
        sy = pos.y() / max(1, self.height()) * self.SPRITE_SIZE.height()
        # 按 priority 倒序排，重叠时高 priority 胜出
        sprite_size_tuple = (self.SPRITE_SIZE.width(), self.SPRITE_SIZE.height())
        for area in sorted(self.touch_areas,
                           key=lambda a: -a.priority):
            if area.hit(int(sx), int(sy), sprite_size_tuple):
                return area
        return None

    # ---------------- 鼠标事件 ----------------
    def mousePressEvent(self, evt: QMouseEvent) -> None:
        self._last_user_interaction_ts = time.time()
        self._drag_animation_started = False  # 重置拖动动画标记
        if evt.button() == Qt.MouseButton.LeftButton:
            self._dragging = True
            self._drag_start = event_global_pos(evt) - self.frameGeometry().topLeft()
            self._press_pos = event_global_pos(evt)  # 保存按下位置用于判断是否拖动
            self.setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            # 不接受事件，让 Qt 能正常识别双击
        elif evt.button() == Qt.MouseButton.RightButton:
            self._show_context_menu(event_global_pos(evt))
            evt.accept()

    def mouseMoveEvent(self, evt: QMouseEvent) -> None:
        if self._dragging:
            self._last_user_interaction_ts = time.time()
            # 真正移动时才开始播放拖动动画（移动距离 ≥5 像素才算拖动）
            if not self._drag_animation_started:
                dist = (event_global_pos(evt) - self._press_pos).manhattanLength()
                if dist >= 5:
                    self._drag_animation_started = True
                    self.animator.start_drag()
                    # 交给系统拖动窗口：逐事件手动 move() 在带 WebGL 的透明窗口上
                    # 会一卡一卡；startSystemMove 由 OS 完成窗口移动，全程平滑。
                    # Qt 会在松开鼠标时结束系统拖动并回发 release 事件。
                    handle = self.windowHandle()
                    if handle is not None and hasattr(handle, "startSystemMove"):
                        try:
                            if handle.startSystemMove():
                                self._system_moving = True
                                evt.accept()
                                return
                        except Exception:  # noqa: BLE001
                            pass
            elif not getattr(self, "_system_moving", False):
                # 系统拖动不可用时的兜底：手动移动
                self.move(event_global_pos(evt) - self._drag_start)
            evt.accept()

    def hideEvent(self, evt) -> None:
        """隐藏（托盘）时暂停 live2d 渲染循环：零 GPU/CPU 开销。"""
        pause = getattr(self.renderer, "pause", None)
        if callable(pause):
            pause()
        super().hideEvent(evt)

    def showEvent(self, evt) -> None:
        """重新显示时恢复渲染循环。"""
        resume = getattr(self.renderer, "resume", None)
        if callable(resume):
            resume()
        super().showEvent(evt)

    def enterEvent(self, evt) -> None:
        """鼠标进入桌宠窗：进入「在看」模式（PR-mute-motion 配合用）。"""
        self._user_inside = True
        self._last_user_interaction_ts = time.time()
        self.mouse_entered.emit()
        super().enterEvent(evt)

    def leaveEvent(self, evt) -> None:
        self._user_inside = False
        self.mouse_left.emit()
        super().leaveEvent(evt)

    def _install_display_input_filters(self, widget: QWidget) -> None:
        """给显示控件本身及其所有后代控件装上事件过滤器。

        QWebEngineView 内有一个全屏覆盖的 Chromium delegate 子 QWidget，
        真实鼠标/拖拽事件直接发给它且不冒泡；只给 view 装过滤器会漏掉。
        配合 eventFilter 里的 ChildAdded 分支，delegate 在页面加载后才
        创建也能被补上，sprite 的 QLabel 走同一条路径，无副作用。
        """
        widget.installEventFilter(self)
        if hasattr(widget, "setAcceptDrops"):
            widget.setAcceptDrops(True)
        for child in widget.findChildren(QWidget):
            child.installEventFilter(self)
            if hasattr(child, "setAcceptDrops"):
                child.setAcceptDrops(True)

    def _is_display_widget(self, obj) -> bool:
        """obj 是否是显示控件（view/label）或其内部后代（Chromium delegate）。"""
        display = getattr(self, "_sprite_label", None)
        if display is None or not isinstance(obj, QWidget):
            return False
        return obj is display or display.isAncestorOf(obj)

    def eventFilter(self, obj, evt):
        """PR-fix-drag-menu (live2d): 把显示控件（含 QWebEngineView 内部 delegate）
        的鼠标/拖拽事件转发给 PetWindow 自己处理。

        默认 Qt 先把事件发给最内层 child，QWebEngineView 的内部 delegate 会吞掉
        事件且不冒泡。安装过滤器后，press/move/release/dblclick 先到本方法，
        我们用 mapToGlobal + self.mapFromGlobal 转成 PetWindow 坐标，再调自己的
        handler；拖拽文件（dragEnter/dragMove/drop）也一并转发，否则拖到网页
        上会被 Chromium 拦截而不是「喂文件」。
        """
        if not self._is_display_widget(obj):
            return super().eventFilter(obj, evt)

        # 内部 delegate 是页面加载后动态创建的：一旦出现就给它（及其子）补装
        if evt.type() == QEvent.Type.ChildAdded:
            child = getattr(evt, "child", None)
            if isinstance(child, QWidget):
                child.installEventFilter(self)
                for sub in child.findChildren(QWidget):
                    sub.installEventFilter(self)
            return False

        # 鼠标进入/离开：delegate 全屏覆盖，顶层 enterEvent/leaveEvent 收不到，
        # 这里代为维护 _user_inside（自主运动据此暂停）。
        if evt.type() == QEvent.Type.Enter:
            self._user_inside = True
            self._last_user_interaction_ts = time.time()
            self.mouse_entered.emit()
            return False
        if evt.type() == QEvent.Type.Leave:
            self._user_inside = False
            self.mouse_left.emit()
            return False

        if isinstance(evt, QMouseEvent):
            # 把 child 局部坐标转成 PetWindow 坐标（delegate/view/PetWindow 都从
            # (0,0) 起、同尺寸，但保险起见统一走全局坐标转换）。
            # 注意：必须同时带上全局坐标（QMouseEvent 只传局部坐标时 globalPos
            # 会被 Qt 置成局部值），否则右键菜单的 event_global_pos() 拿到
            # 窗口局部坐标 → 菜单永远弹在屏幕同一个固定位置。
            local_pt = QPoint(evt.pos())
            global_pt = obj.mapToGlobal(local_pt)
            mapped = QMouseEvent(
                evt.type(),
                self.mapFromGlobal(global_pt),
                global_pt,
                evt.button(),
                evt.buttons(),
                evt.modifiers(),
            )
            if evt.type() == QEvent.Type.MouseButtonPress:
                self.mousePressEvent(mapped)
                return True
            if evt.type() == QEvent.Type.MouseMove:
                self.mouseMoveEvent(mapped)
                return True
            if evt.type() == QEvent.Type.MouseButtonRelease:
                self.mouseReleaseEvent(mapped)
                return True
            if evt.type() == QEvent.Type.MouseButtonDblClick:
                self.mouseDoubleClickEvent(mapped)
                return True
            return False

        # 文件拖拽：dragEnter/drop 只用 mimeData；dragMove 用坐标决定光标，
        # delegate/view/PetWindow 坐标完全重合，可直接转发原事件。
        if isinstance(evt, QDragEnterEvent):
            self.dragEnterEvent(evt)
            return True
        if isinstance(evt, QDragMoveEvent):
            self.dragMoveEvent(evt)
            return True
        if isinstance(evt, QDragLeaveEvent):
            self.dragLeaveEvent(evt)
            return True
        if isinstance(evt, QDropEvent):
            self.dropEvent(evt)
            return True

        return super().eventFilter(obj, evt)

    def mouseReleaseEvent(self, evt: QMouseEvent) -> None:
        if evt.button() == Qt.MouseButton.LeftButton:
            was_dragging = self._drag_animation_started
            self._dragging = False
            self._drag_animation_started = False
            self._system_moving = False
            self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
            if was_dragging:
                # 拖动结束，回到待机
                self.animator.end_drag()
            else:
                # 没拖动：算一次 click → 走 TouchArea.on_click 回调
                # 注意：双击的第二次 release 也会进入这里，但不会触发双击（因为
                # mouseDoubleClickEvent 已经处理了双击逻辑）
                area = self._hit_zone(event_local_pos(evt))
                if area is not None and area.on_click is not None:
                    area.on_click()
            evt.accept()

    def mouseDoubleClickEvent(self, evt: QMouseEvent) -> None:
        # 双击：sprite 播转圈圈；live2d 模型无转圈 motion，改做模型原生吐舌表情
        if evt.button() == Qt.MouseButton.LeftButton:
            rtype = self.renderer.get_renderer_type() if self.renderer else "sprite"
            if rtype == "live2d":
                self.animator.play_animation('tongue')
            else:
                self.animator.play_spin()
            evt.accept()

    # ---------------- 拖拽文件接收 ----------------
    _IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}

    @classmethod
    def _is_image_url(cls, url) -> bool:
        local = url.toLocalFile() if hasattr(url, "toLocalFile") else ""
        if not local:
            return False
        return Path(local).suffix.lower() in cls._IMAGE_SUFFIXES

    @staticmethod
    def _is_file_url(url) -> bool:
        """判断 URL 是否是本地文件。"""
        local = url.toLocalFile() if hasattr(url, "toLocalFile") else ""
        return bool(local)

    def dragEnterEvent(self, evt: QDragEnterEvent) -> None:
        md = evt.mimeData()
        if md.hasUrls() and any(self._is_file_url(u) for u in md.urls()):
            evt.acceptProposedAction()
            self.show_bubble("放下文件试试~", duration_ms=2000)
        else:
            evt.ignore()

    def dragMoveEvent(self, evt: QDragMoveEvent) -> None:
        if evt.mimeData().hasUrls():
            evt.acceptProposedAction()
        else:
            evt.ignore()

    def dropEvent(self, evt: QDropEvent) -> None:
        md = evt.mimeData()
        if not md.hasUrls():
            evt.ignore()
            return
        for url in md.urls():
            local = url.toLocalFile()
            if not local:
                continue
            p = Path(local)
            if not p.is_file():
                continue

            # 图片：直接设置桌宠图片（旧行为）
            if p.suffix.lower() in self._IMAGE_SUFFIXES:
                pix = QPixmap(str(p))
                if not pix.isNull():
                    self.set_drag_image(pix)
                    self.show_bubble(f"收到图：{p.name}", duration_ms=2500)
                    evt.acceptProposedAction()
                    log.info("用户拖入图片：%s", p)
                    return

            # 其他文件：弹出操作弹窗（吃掉 / 转换）
            self._on_file_dropped(p)
            evt.acceptProposedAction()
            return
        evt.ignore()

    def _on_file_dropped(self, path: Path) -> None:
        """文件拖入弹窗：吃掉或转换。"""
        from app.ui.file_dialog import (
            show_file_dialog, send_to_recycle_bin, convert_file,
        )
        from app.core.qt_compat import QMessageBox

        try:
            action, convert_suffix = show_file_dialog(path, None)
        except Exception as e:  # noqa: BLE001
            log.error("文件弹窗出错：%s", e)
            self.show_bubble("出错了…", duration_ms=2000)
            return

        if action == "eat":
            # 吃掉：移到回收站 + 播放 file 动画
            if send_to_recycle_bin(path):
                self.animator.play_file()
                self.show_bubble(f"吃掉了 {path.name}！", duration_ms=3000)
                self.eat_requested.emit()
                log.info("文件被吃掉（已回收站）：%s", path)
            else:
                self.show_bubble("吃不了这个…", duration_ms=2000)
                QMessageBox.warning(self, "错误", "无法将文件移到回收站")

        elif action == "convert"and convert_suffix:
            # 转换格式
            dest = convert_file(path, convert_suffix)
            if dest:
                self.animator.play_file()
                self.show_bubble(f"转换成功：{dest.name}", duration_ms=3000)
                log.info("文件转换成功：%s -> %s", path, dest)
            else:
                self.show_bubble("转换失败了…", duration_ms=2000)
                QMessageBox.warning(self, "转换失败", f"无法将 {path.name} 转换为 {convert_suffix}")

    # ---------------- 右键菜单信号包装器 ----------------
    def _emit_open_settings(self) -> None:
        self.open_settings_requested.emit()

    def _on_eat_menu(self) -> None:
        """右键菜单「吃饭」：播放吃饭动画 + 通知主程序涨状态。"""
        self.animator.play_eat()
        self.eat_requested.emit()

    def _on_swim_menu(self) -> None:
        """右键菜单「游泳」：播放游泳动画（once）→ 回到 idle。"""
        # live2d 不支持 atlas，sprite 模式才走老路径
        atlas = getattr(self.renderer, 'atlas', None)
        player = getattr(self.renderer, 'player', None)
        if atlas is None or player is None:
            self.animator.play_swim()
            return
        from app.animation.animations import Animation as AnimCls
        atlas.ensure_loaded('playing_water')
        grp = atlas.playing_water
        if not grp:
            return
        from random import choice
        a = choice(grp)
        if a:
            once_anim = AnimCls(a.name, a.frames, AnimCls.ONCE)
            player.play_one_shot(once_anim, on_finished=self.animator.set_idle)

    def _toggle_quick_chat_menu(self) -> None:
        """切换底部快捷输入框显示/隐藏。"""
        self._chat_input_visible = not getattr(self, '_chat_input_visible', False)
        self.toggle_chat_input(self._chat_input_visible)

    def _toggle_random_expressions(self) -> None:
        """开关挂机随机表情（保留给程序化调用；菜单项已移入设置面板）。"""
        if self.renderer is None or not hasattr(self.renderer, "set_random_expressions"):
            return
        self.renderer.set_random_expressions(
            not self.renderer.is_random_expressions_enabled())

    def apply_display_size(self, scale: float) -> None:
        """按缩放系数调整窗口与显示控件尺寸（sprite / live2d 通用）。

        live2d 的显示控件是固定尺寸的 QWebEngineView，只改窗口大小没用，
        必须同步调 renderer.set_size 让模型按新画布重新 fit。
        """
        self._scale = float(scale)
        self._window_size = QSize(
            int(self.SPRITE_SIZE.width() * self._scale),
            int(self.SPRITE_SIZE.height() * self._scale),
        )
        self.setFixedSize(self._window_size)
        if self._sprite_label is not None:
            self._sprite_label.setGeometry(
                0, 0, self._window_size.width(), self._window_size.height())
        resize = getattr(self.renderer, "set_size", None)
        if callable(resize):
            resize(self._window_size.width(), self._window_size.height())
        # 尺寸变化后旧遮罩坐标失效：清掉重建（模型重新 fit 需要一点时间）
        self._mask_base = None
        self._mask_prev_region = None
        self.clearMask()
        QTimer.singleShot(800, self._sample_base_mask)

    # ---------------- 点击穿透（live2d：透明区域不让它挡住后面的窗口） ----------------
    def _setup_click_mask(self) -> None:
        """live2d 模式下窗口是方形画布，模型四周大量透明像素会挡住点击。
        周期性从渲染帧的 alpha 通道生成窗口遮罩（setMask）：遮罩外既不显示
        也不接收鼠标 → 透明处直接点到后面的窗口。

        遮罩宁大勿小（偏大只是少一块穿透区，偏小会当场裁掉角色）：
            * 就绪后采样「头转到左右极限」的剪影并集作为下限（头发甩出的范围）；
            * 与上一帧遮罩求并（物理甩动是连续的，相邻两帧不会跳很远）。
        """
        rtype = self.renderer.get_renderer_type() if self.renderer else "sprite"
        if rtype != "live2d":
            return
        self._mask_base = None        # 极限姿势剪影并集（bool 数组）
        self._mask_prev_region: QRegion | None = None
        self._mask_timer = QTimer(self)
        self._mask_timer.timeout.connect(self._update_click_mask)
        self._mask_timer.start(1000)
        # 模型 ready 后（约 2s）采样极限姿势
        QTimer.singleShot(2000, self._sample_base_mask)

    def _grab_opaque(self):
        """抓当前显示帧的 alpha>16 布尔阵列；失败返回 None。"""
        import numpy as np
        from PyQt5.QtGui import QImage
        w = self._sprite_label
        if w is None:
            return None
        img = w.grab().toImage().convertToFormat(QImage.Format_ARGB32)
        h, line = img.height(), img.bytesPerLine() // 4
        # PyQt5 的 constBits() 是无尺寸 sip 指针，需 asstring 显式取字节
        buf = img.constBits().asstring(img.sizeInBytes())
        alpha = np.frombuffer(buf, dtype=np.uint8).reshape(h, line, 4)[:, :, 3]
        return alpha > 16

    def _region_from_mask(self, m) -> QRegion:
        """bool 剪影阵列 → QRegion（膨胀 12px，细微摆动不被裁）。"""
        import numpy as np
        m = m.copy()
        for _ in range(3):
            m = (m | np.roll(m, 4, 0) | np.roll(m, -4, 0)
                 | np.roll(m, 4, 1) | np.roll(m, -4, 1))
        region = QRegion()
        edges = np.diff(np.pad(m.astype(np.int8), ((0, 0), (1, 1))))
        for y in range(m.shape[0]):
            row = edges[y]
            starts = np.flatnonzero(row == 1)
            ends = np.flatnonzero(row == -1)
            for s, e in zip(starts, ends):
                region += QRegion(int(s), y, int(e - s), 1)
        return region

    def _sample_base_mask(self) -> None:
        """采样头转向左右极限（+俯仰）时的剪影并集 → 遮罩下限。"""
        if self.renderer is None or self.renderer.get_renderer_type() != "live2d":
            return
        self._base_poses = [(-0.95, 0.0), (0.95, 0.0),
                            (-0.9, 0.7), (0.9, 0.7), (0.0, 0.0)]
        self._base_samples: list = []
        self._sample_base_step()

    def _sample_base_step(self) -> None:
        if not getattr(self, "_base_poses", None):
            if self._base_samples:
                acc = self._base_samples[0]
                for s in self._base_samples[1:]:
                    acc = acc | s
                self._mask_base = acc
                self._update_click_mask()
            return
        dx, dy = self._base_poses.pop(0)
        look = getattr(self.renderer, "look_at", None)
        if callable(look):
            look(dx, dy)

        def grab():
            try:
                if self.isVisible() and self.renderer is not None \
                        and getattr(self.renderer, "is_ready", lambda: False)():
                    cur = self._grab_opaque()
                    if cur is not None:
                        self._base_samples.append(cur)
            except Exception:  # noqa: BLE001
                pass
            self._sample_base_step()

        QTimer.singleShot(450, grab)

    def _update_click_mask(self) -> None:
        if not self.isVisible():
            return
        try:
            w = self._sprite_label
            if w is None or self.renderer is None \
                    or not getattr(self.renderer, "is_ready", lambda: False)():
                return
            cur = self._grab_opaque()
            if cur is None:
                return
            if not cur.any() and self._mask_base is None:
                self.clearMask()
                return
            # 当前帧 ∪ 极限姿势下限
            m = cur if self._mask_base is None else (cur | self._mask_base)
            region = self._region_from_mask(m)
            # 与上一帧遮罩求并：视线甩动/物理摆动期间不会被当场裁掉
            if self._mask_prev_region is not None:
                region = region.united(self._mask_prev_region)
            # 可见子控件（状态栏/输入框/气泡/贴纸）不被遮罩裁掉
            for child in self.children():
                if isinstance(child, QWidget) and child is not w \
                        and child.isVisible() and not child.isHidden():
                    region += QRegion(child.geometry())
            self.setMask(region)
            self._mask_prev_region = QRegion(region)
        except Exception:  # noqa: BLE001
            pass

    def set_always_on_top(self, on: bool) -> None:
        """运行时切换窗口置顶（设置面板「视觉」页）。"""
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, bool(on))
        self.show()   # setWindowFlag 会把可见窗口隐藏，需要重新 show

    # ---------------- 视线跟随（live2d：头/眼追鼠标，驱动物理） ----------------
    def _setup_look_at(self) -> None:
        rtype = self.renderer.get_renderer_type() if self.renderer else "sprite"
        if rtype != "live2d":
            return
        self._look_timer = QTimer(self)
        self._look_timer.timeout.connect(self._look_at_cursor)
        self._look_timer.start(120)

    def _look_at_cursor(self) -> None:
        """把鼠标位置换算成归一化偏移，让模型头部/眼睛看过去。"""
        if not self.isVisible():
            return
        look = getattr(self.renderer, "look_at", None)
        if not callable(look):
            return
        try:
            cur = QCursor.pos()
            center = self.mapToGlobal(
                QPoint(self.width() // 2, self.height() // 2))
            # 灵敏度：1.2 倍窗口距离内从正中偏到边缘
            nx = (cur.x() - center.x()) / max(1.0, self.width() * 1.2)
            ny = (cur.y() - center.y()) / max(1.0, self.height() * 1.2)
            look(nx, ny)
        except Exception:  # noqa: BLE001
            pass

    # ---------------- 表情包贴纸（模型自带表情包随机弹出右上角） ----------------
    def _setup_sticker_overlay(self) -> None:
        """模型目录带表情包（profile stickers 配置）时启用随机贴纸弹窗。"""
        cfg = None
        if self.renderer is not None and hasattr(self.renderer, "get_sticker_config"):
            try:
                cfg = self.renderer.get_sticker_config()
            except Exception:  # noqa: BLE001
                cfg = None
        if not cfg:
            return
        import random as _random
        self._sticker_random = _random
        self._sticker_files = list(cfg["files"])
        self._sticker_duration_ms = int(cfg.get("duration_s", 4) * 1000)
        self._sticker_size = int(cfg.get("size", 180))
        self._sticker_min_s = int(cfg.get("min_s", 30))
        self._sticker_max_s = int(cfg.get("max_s", 90))

        self._sticker_label = QLabel(self)
        self._sticker_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._sticker_label.setStyleSheet("background: transparent;")
        self._sticker_label.hide()

        self._sticker_timer = QTimer(self)
        self._sticker_timer.setSingleShot(True)
        self._sticker_timer.timeout.connect(self._show_random_sticker)
        self._arm_sticker_timer()

    def _arm_sticker_timer(self) -> None:
        if not getattr(self, "_stickers_enabled", True):
            return
        import random as _random
        lo = max(5, getattr(self, "_sticker_min_s", 30))
        hi = max(lo, getattr(self, "_sticker_max_s", 90))
        if self._sticker_timer is not None:
            self._sticker_timer.start(_random.randint(lo, hi) * 1000)

    def set_random_stickers(self, enabled: bool) -> None:
        """开关随机表情包贴纸（设置面板 Live2D 页）。"""
        self._stickers_enabled = bool(enabled)
        if enabled:
            self._arm_sticker_timer()
        elif self._sticker_timer is not None:
            self._sticker_timer.stop()
            if self._sticker_label is not None:
                self._sticker_label.hide()

    def is_random_stickers_enabled(self) -> bool:
        return bool(getattr(self, "_stickers_enabled", True))

    def _show_random_sticker(self) -> None:
        """随机弹一张表情包到右上角，展示 duration_s 后消失并排下一次。"""
        if not getattr(self, "_stickers_enabled", True):
            return
        cfg = None
        if self.renderer is not None and hasattr(self.renderer, "get_sticker_config"):
            try:
                cfg = self.renderer.get_sticker_config()
            except Exception:  # noqa: BLE001
                cfg = None
        if not cfg or self._sticker_label is None:
            return
        import random as _random
        self._sticker_min_s = int(cfg.get("min_s", 30))
        self._sticker_max_s = int(cfg.get("max_s", 90))
        size = int(cfg.get("size", self._sticker_size))
        path = _random.choice(cfg["files"])

        pix = self._sticker_cache.get(path)
        if pix is None:
            pix = QPixmap(path)
            if pix.isNull():
                self._arm_sticker_timer()
                return
            pix = pix.scaled(
                size, size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            self._sticker_cache[path] = pix

        self._sticker_label.setPixmap(pix)
        self._sticker_label.adjustSize()
        # 右上角（贴着窗口右上角内边缘）
        self._sticker_label.move(max(0, self.width() - pix.width() - 6), 6)
        self._sticker_label.show()
        self._sticker_label.raise_()
        QTimer.singleShot(self._sticker_duration_ms, self._sticker_label.hide)
        self._arm_sticker_timer()

    # ---------------- 右键菜单（精简版） ----------------
    def _show_context_menu(self, global_pos: QPoint) -> None:
        menu = QMenu(self)

        # === 标题：显示当前渲染器类型 ===
        rtype = self.renderer.get_renderer_type() if self.renderer else "?"
        title = QAction(f"🐳 桌宠 ({rtype})", self)
        title.setEnabled(False)
        menu.addAction(title)
        menu.addSeparator()

        # === 聊天入口 ===
        a1 = QAction("和鲸鱼娘聊聊", self)
        a1.triggered.connect(self.chat_requested.emit)
        menu.addAction(a1)
        menu.addSeparator()

        # === 完整设置面板 ===
        act_settings = QAction("设置面板", self)
        act_settings.triggered.connect(self._emit_open_settings)
        menu.addAction(act_settings)
        menu.addSeparator()

        # === 切换表情（自动适配渲染器：sprite 固定 5 个 / live2d 用模型自带情绪 + 自然）===
        emo_menu = menu.addMenu("切换表情")
        emotions = self.renderer.get_emotion_options() if self.renderer else [
            ('happy', '开心'), ('sad', '悲伤'), ('angry', '生气'),
            ('shy', '害羞'), ('think', '思考'),
        ]
        for name, label in emotions:
            a = QAction(label, self)
            a.triggered.connect(lambda _=False, n=name: self.animator.set_emotion(n))
            emo_menu.addAction(a)
        if not emotions:
            no_emo = QAction("(当前模型无可用表情)", self)
            no_emo.setEnabled(False)
            emo_menu.addAction(no_emo)

        # === 切换发型（仅模型自带发型开关时显示，如 Live2D 冰糖；sprite 无此菜单）===
        if self.renderer is not None and getattr(self.renderer, "supports_hairstyles",
                                                 lambda: False)():
            hairstyles = self.renderer.get_hairstyle_options()
            if hairstyles:
                hair_menu = menu.addMenu("切换发型")
                for name, label in hairstyles:
                    a = QAction(label, self)
                    a.triggered.connect(
                        lambda _=False, n=name: self.animator.set_hairstyle(n))
                    hair_menu.addAction(a)
        # === Live2D 分类外观子菜单（按键说明五大类：特殊/配件/手势等）===
        # 表情/发型已由上面两个子菜单覆盖，这里渲染其余分类；sprite 无此部分。
        menu_groups: list[dict] = []
        try:
            menu_groups = self.renderer.get_menu_groups() if self.renderer else []
        except Exception:  # noqa: BLE001
            menu_groups = []
        for g in menu_groups:
            if g.get("kind") in ("emotion", "hairstyle"):
                continue
            sub = menu.addMenu(g["label"])
            for item_id, label in g["items"]:
                a = QAction(label, self)
                a.triggered.connect(
                    lambda _=False, gid=g["id"], iid=item_id:
                    self.renderer.activate_menu_item(gid, iid))
                sub.addAction(a)
        menu.addSeparator()

        # === 睡觉 / 醒来（仅 sprite；live2d 作息由睡眠触发规则/参数管理） ===
        if rtype != "live2d":
            act_sleep = QAction("睡觉", self)
            act_sleep.triggered.connect(self.animator.set_sleep)
            menu.addAction(act_sleep)
            act_wake = QAction("醒来", self)
            act_wake.triggered.connect(self.animator.set_wake)
            menu.addAction(act_wake)
        menu.addSeparator()

        # === 一次性动作（按渲染器能力）===
        # sprite：转圈 / 伸懒腰 / 起跳 / 游泳（真帧动画）。
        # Live2D：按模型 profile 的动作映射动态生成（引用模型手势/特殊条目）。
        play_menu = menu.addMenu("玩一下")
        if rtype == "live2d":
            try:
                play_options = self.renderer.get_play_options()
            except Exception:  # noqa: BLE001
                play_options = []
            if not play_options:
                play_options = [('tongue', '吐舌头')]
            play_actions = [
                (label, lambda _=False, n=name: self.animator.play_animation(n))
                for name, label in play_options
            ]
        else:
            play_actions = [
                ('转圈圈', self.animator.play_spin),
                ('伸懒腰', self.animator.play_stretch),
                ('起跳', self.animator.play_jump),
                ('游泳', self.animator.play_swim),
            ]
        for label, fn in play_actions:
            a = QAction(label, self)
            a.triggered.connect(fn)
            play_menu.addAction(a)
        menu.addSeparator()

        # === 吃饭 / 文件（仅 sprite 模式有效；live2d 模式仍可调但效果是表情）===
        act_eat = QAction("吃饭", self)
        act_eat.triggered.connect(self._on_eat_menu)
        menu.addAction(act_eat)
        menu.addSeparator()

        # === 状态栏显示/隐藏 ===
        act_status = QAction("显示状态栏"if not self._status_visible else "隐藏状态栏", self)
        act_status.triggered.connect(lambda _: self.toggle_status_bar(not self._status_visible))
        menu.addAction(act_status)

        # === 快捷聊天输入 ===
        quick_chat_visible = getattr(self, '_chat_input_visible', False)
        act_quick_chat = QAction(f"{'快速输入'if not quick_chat_visible else '隐藏输入'}", self)
        act_quick_chat.triggered.connect(self._toggle_quick_chat_menu)
        menu.addAction(act_quick_chat)

        ui_style.style_menu(menu)   # 圆角卡片皮肤
        menu.exec(global_pos)