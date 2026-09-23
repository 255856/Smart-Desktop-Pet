"""桌宠设置面板（重构版）。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from app.core.qt_compat import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton, QSpinBox,
    QSizePolicy, QSize, QSlider, QTabWidget, QVBoxLayout,
    QWidget, Signal, Qt, QFrame, QColor, QEvent, QGraphicsDropShadowEffect,
    QToolButton, QObject, QScrollArea, QMenu, QAction,
    event_global_pos,
)
from app.ui import ui_style
from app.core.settings_store import SettingsStore
from app.animation.live2d_scene import (
    BUILTIN_SCENES, SCENE_GROUP_CHAT, SCENE_GROUP_INTERACT, SCENE_GROUP_STATE,
    SceneStore,
)
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from app.core.config import CharacterConfig

log = logging.getLogger(__name__)


class _SettingsDrag(QObject):
    """设置窗口自绘标题栏拖动。"""

    def __init__(self, win: "SettingsWindow"):
        super().__init__(win)
        self._win = win
        self._offset = None
        self._dragging = False

    def eventFilter(self, obj, ev) -> bool:
        t = ev.type()
        if t == QEvent.Type.MouseButtonPress and \
                ev.button() == Qt.MouseButton.LeftButton:
            # 若按在按钮上（最小化/关闭），不拦截，让按钮正常响应
            child = obj.childAt(ev.pos()) if hasattr(obj, "childAt") else None
            if child is not None and isinstance(child, QToolButton):
                return False
            self._dragging = True
            self._offset = event_global_pos(ev) - self._win.frameGeometry().topLeft()
            return True
        if t == QEvent.Type.MouseMove and self._dragging and \
                (ev.buttons() & Qt.MouseButton.LeftButton):
            self._win.move(event_global_pos(ev) - self._offset)
            return True
        if t == QEvent.Type.MouseButtonRelease:
            self._dragging = False
            return True
        return False


class SettingsWindow(QWidget):
    """桌宠设置窗口（Tab 布局）。"""

    settings_changed = Signal()
    # 用户点了「持久化帧时长到文件」（会改 PNG 文件名，较重，有单独按钮）
    persist_frames_requested = Signal(int)   # ms
    # 保留旧接口名，兼容老代码（等价于 persist_frames_requested）
    retune_frames_requested = Signal(int)
    # 用户想直接改内存里所有帧的 duration_ms（立刻生效，不动磁盘）
    override_frame_ms_requested = Signal(int)
    reset_to_defaults_requested = Signal()
    lock_first_idle_changed = Signal(bool)
    crossfade_changed = Signal(bool, int)  # enabled, ms
    fps_monitor_toggled = Signal(bool)
    emotion_requested = Signal(str)   # 'happy' / 'sad' / 'angry' / 'shy' / 'think'
    sleep_requested = Signal()
    idle_requested = Signal()
    chat_requested = Signal()
    model_config_changed = Signal(dict)
    voice_changed = Signal(str)
    live2d_item_activated = Signal(str, str)   # (group_id, item_id)
    live2d_reset_requested = Signal()
    random_exp_changed = Signal(bool)          # 挂机随机表情开关
    random_sticker_changed = Signal(bool)      # 随机表情包贴纸开关
    always_on_top_changed = Signal(bool)       # 窗口置顶开关
    save_settings_requested = Signal()         # 点了「保存设置」按钮
    sticker_options_changed = Signal(dict)     # {size, rotation, min_s, max_s, duration_s}
    random_interval_changed = Signal(int, int)  # 挂机随机间隔（秒）
    max_fps_changed = Signal(int)              # 渲染帧率上限
    tts_config_changed = Signal(dict)          # {tts_enabled, engine, minimax_voice_id, gptsovits_url, ref_audio, prompt_text}
    proactive_changed = Signal(dict)           # {enabled, min_minutes, max_minutes}
    renderer_changed = Signal(str)             # sprite / live2d（重启生效）
    hide_watermark_changed = Signal(bool)      # 隐藏水印（重启生效）
    character_changed = Signal(dict)           # {name, persona}

    def __init__(self, parent: Optional[QWidget] = None,
                 char_cfg: Optional["CharacterConfig"] = None,
                 renderer: Optional[object] = None,
                 sticker_enabled: bool = True,
                 always_on_top: bool = True,
                 live2d_state: Optional[dict] = None,
                 game_action_store: Optional[object] = None) -> None:
        super().__init__(parent)
        self.setObjectName("settings_root")
        self.setWindowTitle("桌宠设置")
        # 设置窗口图标
        _ico = Path(__file__).resolve().parent.parent.parent / "assets" / "icon.ico"
        if _ico.is_file():
            from app.core.qt_compat import QIcon
            self.setWindowIcon(QIcon(str(_ico)))
        self.setMinimumSize(QSize(520, 760))
        self.resize(620, 800)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(ui_style.SETTINGS_QSS)
        # 角色配置（用于「试听」按钮显示角色名 + 后续扩展）；允许为 None 以保留向后兼容
        self.char_cfg = char_cfg
        # 渲染器引用（可选）：live2d 时用于生成「Live2D」Tab 与 renderer-aware 表情按钮
        self._renderer = renderer
        # 随机表情包贴纸初始开关状态（来自 PetWindow）
        self._sticker_enabled = bool(sticker_enabled)
        # 窗口置顶初始状态（来自 cfg.window.always_on_top）
        self._always_on_top = bool(always_on_top)
        # Live2D 参数初始值（贴纸/随机间隔/帧率；来自 ui_controller 合并 store 后）
        st = live2d_state or {}
        self._live2d_state = {
            "sticker": st.get("sticker", {}),
            "random_interval": st.get("random_interval", (20, 50)),
            "max_fps": int(st.get("max_fps", 30)),
        }
        # 全量面板化初始值（TTS/主动关心/渲染器/水印/角色）
        self._panel_state = {
            "tts": st.get("tts", {}),
            "proactive": st.get("proactive", {}),
            "renderer": st.get("renderer", "sprite"),
            "hide_watermark": bool(st.get("hide_watermark", True)),
            "character": st.get("character", {}),
        }
        # 小游戏动作反馈配置（必须在 _build_ui 前就绪，Live2D Tab 构建时会用到）
        if game_action_store is not None:
            self._game_store = game_action_store
        else:
            from app.engine.game_actions import GameActionStore
            self._game_store = GameActionStore(
                Path(__file__).resolve().parent.parent.parent / 'data' / 'game_actions.json')
        self._game_action_labels: dict = {}
        self._build_ui()
        self._wire_signals()
        self._load_defaults()
        self._attached_state = None

        # 设置持久化：从 JSON 文件加载已保存的设置
        self.settings_store = SettingsStore()
        self._load_from_store()

    #  Public: attach state（V3 状态条）
    def attach_state(self, state) -> None:
        """挂 PetState，状态变化自动刷新进度条 + 同时触发存档。"""
        if self._attached_state is state:
            return
        self._attached_state = state
        if state is not None:
            # 在原有 on_change 上叠加刷新UI，不覆盖存档回调
            original_on_change = getattr(state, 'on_change', None)
            def _combined():
                self.refresh_state()
                if original_on_change is not None:
                    try:
                        original_on_change()
                    except Exception:  # noqa: BLE001
                        pass
            state.on_change = _combined
            self.refresh_state()

    #  UI 构造
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 18)
        root.setSpacing(0)

        # 白色圆角卡片容器（窗口透明，卡片负责圆角 + 阴影）
        card = QFrame(self)
        card.setObjectName("window_card")
        card.setStyleSheet(ui_style.WINDOW_CARD_QSS)
        _shadow = QGraphicsDropShadowEffect(card)
        _shadow.setBlurRadius(48)
        _shadow.setOffset(0, 10)
        _shadow.setColor(QColor(90, 78, 200, 55))
        card.setGraphicsEffect(_shadow)
        cl = QVBoxLayout(card)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(8)
        root.addWidget(card, 1)

        # 自绘标题栏（可拖动）
        header = QFrame(card)
        header.setObjectName("titlebar")
        header.setStyleSheet(ui_style.TITLEBAR_QSS)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 8, 10, 8)
        hl.setSpacing(10)

        title = QLabel("桌宠设置")
        title.setObjectName("titlebar_title")
        hl.addWidget(title)
        hl.addStretch(1)

        self.btn_min = QToolButton()
        self.btn_min.setObjectName("win_btn")
        self.btn_min.setText("─")
        self.btn_min.setToolTip("最小化")
        self.btn_min.clicked.connect(self.showMinimized)
        hl.addWidget(self.btn_min)

        self.btn_title_close = QToolButton()
        self.btn_title_close.setObjectName("win_btn_close")
        self.btn_title_close.setText("✕")
        self.btn_title_close.setToolTip("关闭")
        self.btn_title_close.clicked.connect(self.close)
        hl.addWidget(self.btn_title_close)

        cl.addWidget(header)

        # 标题栏拖动
        self._drag_filter = _SettingsDrag(self)
        title.installEventFilter(self._drag_filter)
        header.installEventFilter(self._drag_filter)

        tabs = QTabWidget()
        tabs.addTab(self._build_tab_status(),  "状态")
        tabs.addTab(self._build_tab_fps(),     "帧时长")
        tabs.addTab(self._build_tab_visual(),  "视觉")
        tabs.addTab(self._build_tab_control(), "控制")
        tabs.addTab(self._build_tab_model(),   "模型配置")
        # Live2D 专属 Tab：模型有分类外观（menu groups）时才显示
        if self._renderer is not None:
            try:
                if self._renderer.get_menu_groups():
                    tabs.addTab(self._build_tab_live2d(), "Live2D")
            except Exception:  # noqa: BLE001
                pass
        cl.addWidget(tabs, 1)

        self.lbl_status = QLabel("就绪")
        self.lbl_status.setStyleSheet(f"color: {ui_style.TEXT_SUB};")
        cl.addWidget(self.lbl_status)

        row = QHBoxLayout()
        self.btn_save = QPushButton("保存设置")
        self.btn_save.setObjectName("accent_btn")
        self.btn_save.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_save.clicked.connect(self.save_settings_requested.emit)
        self.btn_reset = QPushButton("重置默认值")
        self.btn_close = QPushButton("关闭")
        row.addWidget(self.btn_save)
        row.addWidget(self.btn_reset)
        row.addStretch(1)
        row.addWidget(self.btn_close)
        cl.addLayout(row)

    def _build_tab_status(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setSpacing(6)

        g = QGroupBox("桌宠状态")
        gv = QVBoxLayout(g)
        self.bar_strength = QProgressBar(); self.bar_food = QProgressBar()
        self.bar_drink = QProgressBar();   self.bar_feeling = QProgressBar()
        self.bar_health = QProgressBar();  self.bar_likability = QProgressBar()
        for key, bar in (("strength", self.bar_strength), ("food", self.bar_food),
                         ("drink", self.bar_drink), ("feeling", self.bar_feeling),
                         ("health", self.bar_health), ("likability", self.bar_likability)):
            bar.setRange(0, 100)
            bar.setTextVisible(False)
            bar.setFixedHeight(14)
            bar.setStyleSheet(ui_style.stat_bar_qss(*ui_style.STAT_BAR_COLORS[key]))
        labels_and_bars = [
            ("体力", self.bar_strength, "strength"),
            ("饱食", self.bar_food, "strength_food"),
            ("口渴", self.bar_drink, "strength_drink"),
            ("心情", self.bar_feeling, "feeling"),
            ("健康", self.bar_health, "health"),
            ("好感", self.bar_likability, "likability"),
        ]
        self._stat_bars = [(attr, bar) for _t, bar, attr in labels_and_bars]
        self._stat_value_labels: dict = {}
        for text, bar, attr in labels_and_bars:
            row = QHBoxLayout()
            lbl = QLabel(text); lbl.setFixedWidth(40)
            lbl.setStyleSheet("color:#4b4b5e; font-size:9pt;")
            row.addWidget(lbl); row.addWidget(bar, 1)
            vlbl = QLabel("0")
            vlbl.setFixedWidth(58)
            vlbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            vlbl.setStyleSheet(
                "color:#8a8a9c; font-size:8pt; border:none;"
                "background:transparent;")
            self._stat_value_labels[attr] = vlbl
            row.addWidget(vlbl)
            gv.addLayout(row)
        self.lbl_summary = QLabel("（未连接 state）")
        f = self.lbl_summary.font(); f.setBold(True); self.lbl_summary.setFont(f)
        self.lbl_summary.setStyleSheet("color:#6b6b7d; font-size:9pt; padding-top:2px;")
        gv.addWidget(self.lbl_summary)
        v.addWidget(g)
        v.addStretch(1)
        return page

    def _build_tab_fps(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setSpacing(10)

        g_fps = QGroupBox("帧率预设")
        gv = QVBoxLayout(g_fps)
        row = QHBoxLayout()
        self.fps_presets: list[tuple[QPushButton, int, int]] = []  # (btn, fps, ms)
        for fps, ms in [(12, 83), (15, 67), (20, 50), (24, 42), (30, 33)]:
            btn = QPushButton(f"{fps} fps")
            btn.setCheckable(True)
            # 选中态由全局皮肤统一渲染（淡紫底白字）
            self.fps_presets.append((btn, fps, ms))
            row.addWidget(btn)
        gv.addLayout(row)
        self.lbl_fps_apply = QLabel("当前：未选择（启动时按 PNG 文件自带 ms 段）")
        self.lbl_fps_apply.setStyleSheet(f"color: {ui_style.TEXT_SUB};")
        gv.addWidget(self.lbl_fps_apply)
        v.addWidget(g_fps)

        g_ms = QGroupBox("帧时长细调")
        gv = QVBoxLayout(g_ms)
        row = QHBoxLayout()
        self.frame_ms_slider = QSlider(Qt.Orientation.Horizontal)
        self.frame_ms_slider.setRange(33, 200)   # 5 fps ~ 30 fps
        self.frame_ms_label = QLabel("50 ms  (20.0 fps)")
        self.frame_ms_label.setMinimumWidth(120)
        self.frame_ms_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        f = self.frame_ms_label.font(); f.setBold(True); self.frame_ms_label.setFont(f)
        row.addWidget(self.frame_ms_slider, 1)
        row.addWidget(self.frame_ms_label)
        gv.addLayout(row)

        # 实时帧时长范围说明
        self.lbl_ms_range = QLabel("范围：33 ms (30 fps) ←→ 200 ms (5 fps)    默认 50 ms = 20 fps")
        self.lbl_ms_range.setStyleSheet(
            f"color: {ui_style.TEXT_SUB}; font-size: 11px;")
        gv.addWidget(self.lbl_ms_range)
        v.addWidget(g_ms)

        g_save = QGroupBox("持久化")
        gv = QVBoxLayout(g_save)
        warn = QLabel("仅在你想永久保存当前帧时长到素材文件时使用。改完后下次启动仍是该帧率。")
        warn.setStyleSheet(f"color: #b45309; font-size: 11px;")
        gv.addWidget(warn)
        row = QHBoxLayout()
        self.btn_persist_frames = QPushButton("把帧时长写入 PNG 文件名")
        self.btn_persist_frames.setObjectName("persist_btn")
        row.addWidget(self.btn_persist_frames)
        row.addStretch(1)
        gv.addLayout(row)
        v.addWidget(g_save)

        g_debug = QGroupBox("调试：桌宠头顶 FPS 气泡")
        gv = QVBoxLayout(g_debug)
        row = QHBoxLayout()
        self.cb_fps_monitor = QCheckBox("开启 FPS 实时监测")
        row.addWidget(self.cb_fps_monitor, 1)
        gv.addLayout(row)
        tip = QLabel("开启后桌宠头顶气泡每秒显示「⚡ 19.8 FPS」这种字样，可用于验证帧率是否真的改了。")
        tip.setStyleSheet(f"color: {ui_style.TEXT_SUB}; font-size: 11px;")
        gv.addWidget(tip)
        v.addWidget(g_debug)

        v.addStretch(1)
        return page

    def _build_tab_visual(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setSpacing(10)

        g_renderer = QGroupBox("渲染器")
        gr = QVBoxLayout(g_renderer)
        rrow = QHBoxLayout()
        rrow.addWidget(QLabel("渲染引擎"))
        self.cmb_renderer = QComboBox()
        self.cmb_renderer.addItem("Sprite 帧图（经典）", "sprite")
        self.cmb_renderer.addItem("Live2D 模型", "live2d")
        idx_r = self.cmb_renderer.findData(self._panel_state.get("renderer", "sprite"))
        self.cmb_renderer.setCurrentIndex(idx_r if idx_r >= 0 else 0)
        self.cmb_renderer.currentIndexChanged.connect(self._emit_renderer)
        rrow.addWidget(self.cmb_renderer)
        rrow.addStretch(1)
        gr.addLayout(rrow)
        tip_r = QLabel("切换渲染引擎需重启桌宠后生效。")
        tip_r.setStyleSheet(f"color: {ui_style.TEXT_SUB}; font-size: 11px;")
        gr.addWidget(tip_r)
        v.addWidget(g_renderer)

        g_scale = QGroupBox("桌宠缩放")
        gv = QVBoxLayout(g_scale)
        row = QHBoxLayout()
        self.scale_presets: list[tuple[QPushButton, float]] = []
        for pct in [30, 50, 75, 100]:
            btn = QPushButton(f"{pct}%")
            btn.setCheckable(True)
            self.scale_presets.append((btn, pct / 100.0))
            row.addWidget(btn)
        gv.addLayout(row)

        row = QHBoxLayout()
        self.scale_slider = QSlider(Qt.Orientation.Horizontal)
        self.scale_slider.setRange(20, 100)
        self.scale_label = QLabel("0.40x")
        self.scale_label.setMinimumWidth(70)
        self.scale_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        f = self.scale_label.font(); f.setBold(True); self.scale_label.setFont(f)
        row.addWidget(self.scale_slider, 1)
        row.addWidget(self.scale_label)
        gv.addLayout(row)
        v.addWidget(g_scale)

        g_op = QGroupBox("窗口透明度")
        gv = QVBoxLayout(g_op)
        row = QHBoxLayout()
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(70, 100)
        self.opacity_label = QLabel("100 %")
        self.opacity_label.setMinimumWidth(70)
        self.opacity_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        f = self.opacity_label.font(); f.setBold(True); self.opacity_label.setFont(f)
        row.addWidget(self.opacity_slider, 1)
        row.addWidget(self.opacity_label)
        gv.addLayout(row)
        self.cb_always_on_top = QCheckBox("窗口置顶（始终保持在其他窗口上方）")
        self.cb_always_on_top.setChecked(self._always_on_top)
        self.cb_always_on_top.toggled.connect(self.always_on_top_changed.emit)
        gv.addWidget(self.cb_always_on_top)
        v.addWidget(g_op)

        g_anim = QGroupBox("动画切换优化")
        gv = QVBoxLayout(g_anim)
        row = QHBoxLayout()
        self.cb_crossfade = QCheckBox("切换动画时淡入淡出")
        row.addWidget(self.cb_crossfade, 1)
        gv.addLayout(row)

        row = QHBoxLayout()
        self.crossfade_ms_slider = QSlider(Qt.Orientation.Horizontal)
        self.crossfade_ms_slider.setRange(80, 400)
        self.crossfade_ms_label = QLabel("200 ms")
        self.crossfade_ms_label.setMinimumWidth(90)
        self.crossfade_ms_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lbl = QLabel("淡入淡出时长：")
        row.addWidget(lbl); row.addWidget(self.crossfade_ms_slider, 1); row.addWidget(self.crossfade_ms_label)
        gv.addLayout(row)

        row = QHBoxLayout()
        self.cb_lock_first_idle = QCheckBox("永远用第一张 idle")
        row.addWidget(self.cb_lock_first_idle, 1)
        gv.addLayout(row)
        v.addWidget(g_anim)

        v.addStretch(1)
        return page

    def _build_tab_control(self) -> QWidget:
        # 外层包 QScrollArea，防止内容过多时按钮被压缩、文字糊成黑条
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        v = QVBoxLayout(inner)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(10)

        g_short = QGroupBox("快捷入口")
        gv = QHBoxLayout(g_short)
        self.btn_chat  = QPushButton("和她聊聊")
        self.btn_chat.setObjectName("accent_btn")
        self.btn_sleep = QPushButton("睡觉")
        self.btn_wake  = QPushButton("醒来")
        gv.addWidget(self.btn_chat); gv.addWidget(self.btn_sleep); gv.addWidget(self.btn_wake)
        v.addWidget(g_short)

        # live2d 模型在 Live2D 页已有完整表情/外观，这里只给引导，不重复铺一长串按钮；
        # sprite 渲染器则保留 5 个基础情绪。
        g_emo = QGroupBox("切换表情")
        self.emo_btns: list[tuple[QPushButton, str]] = []
        has_live2d_menu = False
        if self._renderer is not None:
            try:
                has_live2d_menu = bool(self._renderer.get_menu_groups())
            except Exception:  # noqa: BLE001
                has_live2d_menu = False
        if has_live2d_menu:
            gv = QVBoxLayout(g_emo)
            tip = QLabel("完整的表情、发型、配件、手势请到「Live2D」标签页切换。")
            tip.setWordWrap(True)
            tip.setStyleSheet(f"color: {ui_style.TEXT_SUB}; font-size: 11px;")
            gv.addWidget(tip)
        else:
            gv = QGridLayout(g_emo)
            emo_list: list[tuple[str, str]] = []
            if self._renderer is not None:
                try:
                    emo_list = [(label, key) for key, label in
                                self._renderer.get_emotion_options()]
                except Exception:  # noqa: BLE001
                    emo_list = []
            if not emo_list:
                emo_list = [('开心', 'happy'), ('悲伤', 'sad'), ('生气', 'angry'),
                            ('害羞', 'shy'), ('思考', 'think')]
            for i, (text, key) in enumerate(emo_list):
                btn = QPushButton(text)
                btn.setCheckable(True)
                btn.setMinimumHeight(32)
                self.emo_btns.append((btn, key))
                r, c = divmod(i, 3)
                gv.addWidget(btn, r, c)
        v.addWidget(g_emo)

        g_motion = QGroupBox("自主行为")
        gv = QVBoxLayout(g_motion)
        pairs = [
            ("IDLE 持续（秒）",   10, 180, 45, "idle_s"),
            ("WALK 持续（秒）",    2,  30, 10, "walk_s"),
            ("决策间隔（秒）",   10,  90, 30, "decide_s"),
        ]
        self.motion_sliders: dict[str, tuple[QSlider, QLabel]] = {}
        for label_text, lo, hi, default, key in pairs:
            lbl_cap = QLabel(f"{label_text}（{lo} - {hi}）")
            row = QHBoxLayout()
            slider = QSlider(Qt.Orientation.Horizontal)
            slider.setRange(lo, hi); slider.setValue(default)
            val = QLabel(f"{default} s"); val.setMinimumWidth(56)
            val.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            f = val.font(); f.setBold(True); val.setFont(f)
            row.addWidget(slider, 1); row.addWidget(val)
            gv.addWidget(lbl_cap); gv.addLayout(row)
            self.motion_sliders[key] = (slider, val)
        v.addWidget(g_motion)

        st_p = self._panel_state.get("proactive", {})
        g_pro = QGroupBox("主动关心")
        gp = QGridLayout(g_pro)
        self.cb_proactive = QCheckBox("启用（空闲时主动说一句话）")
        self.cb_proactive.setChecked(bool(st_p.get("enabled", True)))
        self.cb_proactive.toggled.connect(self._emit_proactive)
        gp.addWidget(self.cb_proactive, 0, 0, 1, 3)
        gp.addWidget(QLabel("间隔下限（分）"), 1, 0)
        self.spin_proactive_min = QSpinBox()
        self.spin_proactive_min.setRange(1, 720)
        self.spin_proactive_min.setValue(int(st_p.get("min_minutes", 25)))
        self.spin_proactive_min.valueChanged.connect(self._emit_proactive)
        gp.addWidget(self.spin_proactive_min, 1, 1)
        gp.addWidget(QLabel("上限（分）"), 2, 0)
        self.spin_proactive_max = QSpinBox()
        self.spin_proactive_max.setRange(5, 720)
        self.spin_proactive_max.setValue(int(st_p.get("max_minutes", 45)))
        self.spin_proactive_max.valueChanged.connect(self._emit_proactive)
        gp.addWidget(self.spin_proactive_max, 2, 1)
        v.addWidget(g_pro)

        v.addStretch(1)
        return page


    def _build_tab_live2d(self) -> QWidget:
        """Live2D 专属设置：外观/挂机置顶，五大类以可点选 chip 网格呈现。

        条目来自渲染器 profile（每模型一份 *.model.yaml），换模型自动跟随。
        - 每个分类默认只露前 3 个 chip，其余点「更多 ▾」展开；
        - toggle 组（特殊/配件/手势）：chip 可多选叠加，再点取消；
        - exclusive 组（发型/表情）：chip 单选，首项为「默认 / 自然」。
        整页在 QScrollArea 内，条目再多也不会被裁切。
        """
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        inner.setObjectName("live2d_page")
        scroll.setWidget(inner)
        outer.addWidget(scroll)

        v = QVBoxLayout(inner)
        v.setContentsMargins(8, 10, 8, 12)
        v.setSpacing(10)
        renderer = self._renderer

        # 触发场景配置：场景行摘要 label + 打开的配置弹窗（保持引用防 GC）
        self._scene_summary_labels: dict[str, QLabel] = {}
        self._scene_dlgs: list = []
        self._custom_action_layout = None

        model_name = ""
        try:
            model_name = getattr(renderer.profile, "name", "") or ""
        except Exception:  # noqa: BLE001
            pass
        head = QFrame()
        head.setObjectName("live2d_model_head")
        hl = QVBoxLayout(head)
        hl.setContentsMargins(16, 11, 16, 11)
        hl.setSpacing(2)
        cap = QLabel("LIVE2D 模型")
        cap.setObjectName("model_caption")
        nm = QLabel(model_name or "未命名模型")
        nm.setObjectName("model_name")
        hl.addWidget(cap)
        hl.addWidget(nm)
        v.addWidget(head)

        g_misc = QGroupBox("外观 / 挂机")
        gm = QVBoxLayout(g_misc)
        gm.setSpacing(8)
        self.btn_live2d_reset = QPushButton("复位全部外观")
        self.btn_live2d_reset.setObjectName("accent_btn")
        self.btn_live2d_reset.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_live2d_reset.clicked.connect(self._on_live2d_reset)
        gm.addWidget(self.btn_live2d_reset)

        rnd_on = False
        try:
            rnd_on = bool(renderer.is_random_expressions_enabled())
        except Exception:  # noqa: BLE001
            pass
        self.cb_random_exp = QCheckBox("挂机随机表情/动作（空闲时随机切换，互动即暂停）")
        self.cb_random_exp.setChecked(rnd_on)
        self.cb_random_exp.toggled.connect(self.random_exp_changed.emit)
        gm.addWidget(self.cb_random_exp)

        self.cb_random_sticker = QCheckBox("随机表情包贴纸（角色右上方弹出）")
        self.cb_random_sticker.setChecked(self._sticker_enabled)
        self.cb_random_sticker.toggled.connect(self.random_sticker_changed.emit)
        gm.addWidget(self.cb_random_sticker)

        self.cb_hide_watermark = QCheckBox("隐藏模型水印（重启生效）")
        self.cb_hide_watermark.setChecked(self._panel_state.get("hide_watermark", True))
        self.cb_hide_watermark.toggled.connect(self.hide_watermark_changed.emit)
        gm.addWidget(self.cb_hide_watermark)

        ri = self._live2d_state.get("random_interval", (20, 50))
        row = QHBoxLayout()
        row.addWidget(QLabel("随机间隔（秒）"))
        self.spin_random_min = QSpinBox()
        self.spin_random_min.setRange(5, 600)
        self.spin_random_min.setValue(int(ri[0]))
        self.spin_random_max = QSpinBox()
        self.spin_random_max.setRange(5, 600)
        self.spin_random_max.setValue(int(ri[1]))
        for w in (self.spin_random_min, self.spin_random_max):
            w.valueChanged.connect(self._emit_random_interval)
        dash = QLabel("–")
        row.addWidget(self.spin_random_min)
        row.addWidget(dash)
        row.addWidget(self.spin_random_max)
        row.addStretch(1)
        gm.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("渲染帧率"))
        self.cmb_max_fps = QComboBox()
        for fps in (15, 24, 30, 60):
            self.cmb_max_fps.addItem(f"{fps} fps", fps)
        cur_fps = self._live2d_state.get("max_fps", 30)
        idx = self.cmb_max_fps.findData(int(cur_fps))
        self.cmb_max_fps.setCurrentIndex(idx if idx >= 0 else 2)
        self.cmb_max_fps.currentIndexChanged.connect(self._emit_max_fps)
        row.addWidget(self.cmb_max_fps)
        row.addStretch(1)
        gm.addLayout(row)
        v.addWidget(g_misc)

        st_cfg = self._live2d_state.get("sticker", {})
        g_st = QGroupBox("表情包贴纸")
        gs = QGridLayout(g_st)
        gs.setVerticalSpacing(6)

        gs.addWidget(QLabel("大小"), 0, 0)
        self.slider_sticker_size = QSlider(Qt.Orientation.Horizontal)
        self.slider_sticker_size.setRange(60, 300)
        self.slider_sticker_size.setValue(int(st_cfg.get("size", 120)))
        self.lbl_sticker_size = QLabel(f"{self.slider_sticker_size.value()} px")
        for w in (self.slider_sticker_size,):
            w.valueChanged.connect(self._emit_sticker_options)
        gs.addWidget(self.slider_sticker_size, 0, 1)
        gs.addWidget(self.lbl_sticker_size, 0, 2)

        gs.addWidget(QLabel("倾斜角度"), 1, 0)
        self.slider_sticker_rot = QSlider(Qt.Orientation.Horizontal)
        self.slider_sticker_rot.setRange(0, 90)
        self.slider_sticker_rot.setValue(int(st_cfg.get("rotation", 45)))
        self.lbl_sticker_rot = QLabel(f"{self.slider_sticker_rot.value()}°")
        self.slider_sticker_rot.valueChanged.connect(self._emit_sticker_options)
        gs.addWidget(self.slider_sticker_rot, 1, 1)
        gs.addWidget(self.lbl_sticker_rot, 1, 2)

        gs.addWidget(QLabel("弹出间隔（秒）"), 2, 0)
        row = QHBoxLayout()
        self.spin_sticker_min = QSpinBox()
        self.spin_sticker_min.setRange(5, 600)
        self.spin_sticker_min.setValue(int(st_cfg.get("min_s", 40)))
        self.spin_sticker_max = QSpinBox()
        self.spin_sticker_max.setRange(5, 600)
        self.spin_sticker_max.setValue(int(st_cfg.get("max_s", 120)))
        self.spin_sticker_min.valueChanged.connect(self._emit_sticker_options)
        self.spin_sticker_max.valueChanged.connect(self._emit_sticker_options)
        row.addWidget(self.spin_sticker_min)
        row.addWidget(QLabel("–"))
        row.addWidget(self.spin_sticker_max)
        row.addStretch(1)
        gs.addLayout(row, 2, 1)

        gs.addWidget(QLabel("显示时长（秒）"), 3, 0)
        self.spin_sticker_duration = QSpinBox()
        self.spin_sticker_duration.setRange(1, 15)
        self.spin_sticker_duration.setValue(int(st_cfg.get("duration_s", 4)))
        self.spin_sticker_duration.valueChanged.connect(self._emit_sticker_options)
        gs.addWidget(self.spin_sticker_duration, 3, 1)
        v.addWidget(g_st)

        self._scene_card = self._build_scene_card(renderer)
        v.addWidget(self._scene_card)
        self._custom_action_card = self._build_custom_action_card(renderer)
        v.addWidget(self._custom_action_card)
        # 游戏动作反馈（所有小游戏共用一套场景配置）
        self._game_action_card = self._build_game_action_card()
        v.addWidget(self._game_action_card)

        v.addStretch(1)
        return page

    # ---------- 游戏动作反馈（所有小游戏共用一套场景） ----------
    def _build_game_action_card(self) -> QGroupBox:
        from app.engine.game_actions import GAME_EVENTS
        box = QGroupBox("游戏动作反馈（五子棋 / 狼人杀通用）")
        box.setObjectName("live2d_card")
        vl = QVBoxLayout(box)
        vl.setContentsMargins(12, 22, 12, 12)
        vl.setSpacing(6)
        hint = QLabel(
            "开始游戏后保持默认动作，胜利/失败时播放对应动作，退出游戏恢复默认；"
            "五子棋、狼人杀共用这一套，同时兼容 Live2D 与帧动画。")
        hint.setWordWrap(True)
        hint.setStyleSheet(
            "color:#8a8a9c; font-size:8pt; border:none; background:transparent;")
        vl.addWidget(hint)
        self.cb_game_action = QCheckBox("启用游戏动作反馈")
        self.cb_game_action.setChecked(bool(self._game_store.enabled))
        self.cb_game_action.toggled.connect(self._on_game_action_enabled)
        vl.addWidget(self.cb_game_action)
        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_game_reset = QPushButton("恢复默认")
        self.btn_game_reset.setObjectName("more_btn")
        self.btn_game_reset.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_game_reset.clicked.connect(self._on_game_action_reset)
        row.addWidget(self.btn_game_reset)
        vl.addLayout(row)
        for eid, title, _default in GAME_EVENTS:
            vl.addWidget(self._build_game_action_row(eid, title))
        return box

    def _build_game_action_row(self, event_id: str, title: str) -> QFrame:
        row = QFrame()
        row.setObjectName("scene_row")
        row.setStyleSheet(self._ROW_QSS)
        h = QHBoxLayout(row)
        h.setContentsMargins(10, 5, 8, 5)
        h.setSpacing(8)
        name = QLabel(title)
        name.setMinimumWidth(200)
        name.setStyleSheet(
            "color:#2c2c38; font-size:9pt; border:none; background:transparent;")
        h.addWidget(name)
        summ = QLabel(self._game_store.summary(event_id))
        summ.setStyleSheet(
            "color:#9a9aad; font-size:8pt; border:none; background:transparent;")
        summ.setWordWrap(False)
        h.addWidget(summ, 1)
        self._game_action_labels[event_id] = summ
        btn = QPushButton("配置")
        btn.setObjectName("more_btn")
        btn.setFixedWidth(54)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(
            lambda _=False, e=event_id, t=title: self._open_game_action_dialog(e, t))
        h.addWidget(btn)
        return row

    def _open_game_action_dialog(self, event_id: str, title: str) -> None:
        # 与「触发场景配置」同款无边框弹窗，编辑该情形保持的动作
        from app.ui.live2d_scene_dialog import GameActionEditDialog
        dlg = GameActionEditDialog(
            self._game_store, event_id, title,
            renderer=self._renderer, parent=self)
        dlg.saved.connect(self._refresh_game_action_labels)
        dlg.show()
        self._scene_dlgs.append(dlg)

    def _refresh_game_action_labels(self) -> None:
        for eid, lbl in self._game_action_labels.items():
            lbl.setText(self._game_store.summary(eid))

    def _on_game_action_enabled(self, on: bool) -> None:
        self._game_store.set_enabled(on)

    def _on_game_action_reset(self) -> None:
        self._game_store.reset()
        self.cb_game_action.setChecked(self._game_store.enabled)
        for _eid, _lbl in self._game_action_labels.items():
            _lbl.setText(self._game_store.summary(_eid))

    #  触发场景配置（Live2D）
    _ROW_QSS = (
        "QFrame#scene_row{background:#ffffff; border:1px solid #eceaf5;"
        "border-radius:10px;} QFrame#scene_row:hover{border-color:#d8d2f7;}")

    def _bundle_summary(self, renderer, b) -> str:
        """把一个外观组合概括成中文短标签。"""
        parts = []
        if b.emotion:
            if b.emotion in ("natural", "default", "none"):
                parts.append("表情·自然（普通眼）")
            else:
                parts.append(f"表情·{b.emotion}")
        if b.hairstyle:
            parts.append("发型·默认" if b.hairstyle == "__default__"
                         else f"发型·{b.hairstyle}")
        if b.toggles:
            parts.append("·".join(b.toggles))
        return " + ".join(parts) if parts else "默认（不改变）"

    def _build_scene_card(self, renderer) -> QGroupBox:
        """触发场景配置：按 情绪 / 状态 / 互动 分组列场景行。"""
        box = QGroupBox("触发场景配置")
        box.setObjectName("live2d_card")
        vl = QVBoxLayout(box)
        vl.setContentsMargins(12, 22, 12, 12)
        vl.setSpacing(5)
        hint = QLabel("给每个场景搭配表情、发型、配件或手势；全部留空表示触发时不改变。")
        hint.setWordWrap(True)
        hint.setStyleSheet(
            "color:#8a8a9c; font-size:8pt; border:none; background:transparent;")
        vl.addWidget(hint)
        for group in (SCENE_GROUP_CHAT, SCENE_GROUP_STATE, SCENE_GROUP_INTERACT):
            cap = QLabel(group)
            cap.setStyleSheet(
                "color:#7c6cf0; font-size:9pt; font-weight:700; border:none;"
                "background:transparent; padding:6px 2px 0 2px;")
            vl.addWidget(cap)
            for sid, title, kind, grp in BUILTIN_SCENES:
                if grp == group:
                    vl.addWidget(self._build_scene_row(renderer, sid, title, kind))
        return box

    def _build_scene_row(self, renderer, sid: str, title: str, kind: str) -> QFrame:
        row = QFrame()
        row.setObjectName("scene_row")
        row.setStyleSheet(self._ROW_QSS)
        h = QHBoxLayout(row)
        h.setContentsMargins(10, 5, 8, 5)
        h.setSpacing(8)
        name = QLabel(title)
        name.setFixedWidth(168)
        name.setStyleSheet(
            "color:#2c2c38; font-size:9pt; border:none; background:transparent;")
        h.addWidget(name)
        b = renderer.get_scene_bundle(sid)
        summ = QLabel(self._bundle_summary(renderer, b))
        summ.setStyleSheet(
            "color:#9a9aad; font-size:8pt; border:none; background:transparent;")
        summ.setWordWrap(False)
        h.addWidget(summ, 1)
        self._scene_summary_labels[sid] = summ
        tag = QLabel("一次性" if kind == "transient" else "持续")
        tag.setFixedWidth(44)
        tag.setStyleSheet(
            "color:#a0a0b4; font-size:8pt; border:none; background:transparent;")
        h.addWidget(tag)
        btn = QPushButton("配置")
        btn.setObjectName("more_btn")
        btn.setFixedWidth(54)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(
            lambda _=False, s_=sid, t=title, k=kind:
            self._open_scene_dialog(s_, t, k))
        h.addWidget(btn)
        return row

    def _open_scene_dialog(self, sid: str, title: str, kind: str) -> None:
        from app.ui.live2d_scene_dialog import SceneEditDialog
        dlg = SceneEditDialog(self._renderer, sid, title, kind, self)
        dlg.saved.connect(self._refresh_scene_summaries)
        dlg.show()
        self._scene_dlgs.append(dlg)

    def _refresh_scene_summaries(self) -> None:
        renderer = self._renderer
        for sid, lbl in self._scene_summary_labels.items():
            lbl.setText(self._bundle_summary(
                renderer, renderer.get_scene_bundle(sid)))
        self._refresh_custom_action_rows()

    def _build_custom_action_card(self, renderer) -> QGroupBox:
        """自定义动作：初始为空，用户新建/编辑/删除/播放。"""
        box = QGroupBox("自定义动作（仅 Live2D，可绑定工具动作）")
        box.setObjectName("live2d_card")
        vl = QVBoxLayout(box)
        vl.setContentsMargins(12, 22, 12, 12)
        vl.setSpacing(6)
        hint = QLabel("自己命名动作并搭配外观，可绑定到工具动作，也可在右键「玩一下」里手动播放。")
        hint.setWordWrap(True)
        hint.setStyleSheet(
            "color:#8a8a9c; font-size:8pt; border:none; background:transparent;")
        vl.addWidget(hint)
        host = QWidget()
        host.setStyleSheet("background:transparent;")
        self._custom_action_host = host
        self._custom_action_layout = QVBoxLayout(host)
        self._custom_action_layout.setContentsMargins(0, 0, 0, 0)
        self._custom_action_layout.setSpacing(6)
        self._custom_action_layout.addStretch(1)
        vl.addWidget(host)
        add = QPushButton("＋ 新建动作")
        add.setObjectName("accent_btn")
        add.setCursor(Qt.CursorShape.PointingHandCursor)
        add.clicked.connect(lambda _=False: self._open_action_dialog(None))
        vl.addWidget(add)
        self._refresh_custom_action_rows()
        return box

    def _refresh_custom_action_rows(self) -> None:
        if self._custom_action_layout is None:
            return
        renderer = self._renderer
        while self._custom_action_layout.count() > 1:
            it = self._custom_action_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        hook_labels = dict(self._tool_action_hooks())
        for a in renderer.get_custom_actions():
            self._custom_action_layout.insertWidget(
                self._custom_action_layout.count() - 1,
                self._build_custom_action_row(renderer, a, hook_labels))

    @staticmethod
    def _tool_action_hooks():
        from app.animation.live2d_scene import TOOL_ACTION_HOOKS
        return TOOL_ACTION_HOOKS

    def _build_custom_action_row(self, renderer, a: dict, hook_labels: dict) -> QFrame:
        row = QFrame()
        row.setObjectName("scene_row")
        row.setStyleSheet(self._ROW_QSS)
        h = QHBoxLayout(row)
        h.setContentsMargins(10, 5, 8, 5)
        h.setSpacing(8)
        name = QLabel(a.get("name", "动作"))
        name.setFixedWidth(110)
        name.setStyleSheet(
            "color:#2c2c38; font-size:9pt; font-weight:700; border:none;"
            "background:transparent;")
        h.addWidget(name)
        hook = a.get("hook", "")
        meta = ("绑定 · " + hook_labels.get(hook, hook)) if hook else "仅手动"
        desc = self._bundle_summary(renderer, SceneStore.action_bundle(a))
        info = QLabel(f"{meta}｜{desc}")
        info.setStyleSheet(
            "color:#9a9aad; font-size:8pt; border:none; background:transparent;")
        h.addWidget(info, 1)
        play = QPushButton("播放")
        play.setObjectName("more_btn")
        play.setFixedWidth(54)
        play.setCursor(Qt.CursorShape.PointingHandCursor)
        play.clicked.connect(
            lambda _=False, cid=a.get("id"): renderer.play_custom_action(cid))
        h.addWidget(play)
        edit = QPushButton("编辑")
        edit.setObjectName("more_btn")
        edit.setFixedWidth(54)
        edit.setCursor(Qt.CursorShape.PointingHandCursor)
        edit.clicked.connect(lambda _=False, x=a: self._open_action_dialog(x))
        h.addWidget(edit)
        return row

    def _open_action_dialog(self, action) -> None:
        from app.ui.live2d_scene_dialog import ActionEditDialog
        dlg = ActionEditDialog(self._renderer, action, self)
        dlg.saved.connect(self._refresh_scene_summaries)
        dlg.show()
        self._scene_dlgs.append(dlg)

    def _on_live2d_reset(self) -> None:
        """复位全部外观：通知 renderer 清空当前叠加，恢复自然。"""
        self.live2d_reset_requested.emit()


    def _build_tab_model(self) -> QWidget:
        """模型配置：LLM API 地址、密钥、模型名、参数。"""
        # 内容较多（API + 参数 + 预设 + TTS），外层包 QScrollArea 防止压缩重叠
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        v = QVBoxLayout(inner)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(10)

        g_api = QGroupBox("API 连接")
        gv = QVBoxLayout(g_api)
        form = QFormLayout()
        form.setSpacing(8)

        self.edt_base_url = QLineEdit()
        self.edt_base_url.setPlaceholderText("https://api.deepseek.com/v1")
        self.edt_base_url.textChanged.connect(self._on_model_config_changed)
        form.addRow("API 地址：", self.edt_base_url)

        self.edt_api_key = QLineEdit()
        self.edt_api_key.setPlaceholderText("sk-xxxxxxx")
        self.edt_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.edt_api_key.textChanged.connect(self._on_model_config_changed)
        self.btn_toggle_key = QPushButton("显示")
        self.btn_toggle_key.setFixedWidth(64)
        self.btn_toggle_key.setFixedHeight(34)
        self.btn_toggle_key.setStyleSheet("padding: 4px 8px;")
        self.btn_toggle_key.setToolTip("显示/隐藏密钥")
        self.btn_toggle_key.clicked.connect(self._toggle_api_key_visibility)
        key_row = QHBoxLayout()
        key_row.setSpacing(8)
        key_row.addWidget(self.edt_api_key, 1)
        key_row.addWidget(self.btn_toggle_key)
        form.addRow("API Key：", key_row)

        self.cmb_model = QComboBox()
        self.cmb_model.setEditable(True)
        self.cmb_model.addItems([
            "deepseek-chat", "deepseek-reasoner",
            "gpt-4o", "gpt-4o-mini", "gpt-3.5-turbo",
            "MiniMax-M1", "MiniMax-M2",
            "Qwen2.5-72B-Instruct", "Qwen2.5-14B-Instruct",
            "Llama-3.1-70B-Instruct",
        ])
        self.cmb_model.currentTextChanged.connect(self._on_model_config_changed)
        self.cmb_model.lineEdit().editingFinished.connect(self._on_model_config_changed)
        form.addRow("模型名称：", self.cmb_model)

        gv.addLayout(form)

        self.lbl_model_status = QLabel("状态：未检测")
        self.lbl_model_status.setStyleSheet(f"color: {ui_style.TEXT_SUB}; font-size: 11px;")
        gv.addWidget(self.lbl_model_status)
        v.addWidget(g_api)

        g_param = QGroupBox("生成参数")
        gv = QVBoxLayout(g_param)
        pform = QFormLayout()
        pform.setSpacing(8)

        row = QHBoxLayout()
        self.slider_temperature = QSlider(Qt.Orientation.Horizontal)
        self.slider_temperature.setRange(0, 200)  # 0.00 - 2.00
        self.slider_temperature.setTickInterval(50)
        self.slider_temperature.valueChanged.connect(self._on_temperature_changed)
        self.lbl_temperature = QLabel("0.80")
        self.lbl_temperature.setMinimumWidth(40)
        self.lbl_temperature.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        f = self.lbl_temperature.font(); f.setBold(True); self.lbl_temperature.setFont(f)
        row.addWidget(self.slider_temperature, 1)
        row.addWidget(self.lbl_temperature)
        pform.addRow("Temperature：", row)

        self.spin_max_tokens = QSpinBox()
        self.spin_max_tokens.setRange(128, 8192)
        self.spin_max_tokens.setSingleStep(128)
        self.spin_max_tokens.setMaximumWidth(240)
        self.spin_max_tokens.valueChanged.connect(lambda _: self._on_model_config_changed())
        pform.addRow("最大 Tokens：", self.spin_max_tokens)

        self.spin_timeout = QSpinBox()
        self.spin_timeout.setRange(10, 300)
        self.spin_timeout.setSuffix("s")
        self.spin_timeout.setMaximumWidth(240)
        self.spin_timeout.valueChanged.connect(lambda _: self._on_model_config_changed())
        pform.addRow("超时时间：", self.spin_timeout)

        self.cb_stream = QCheckBox("启用流式输出（逐字显示）")
        self.cb_stream.setChecked(True)
        self.cb_stream.toggled.connect(lambda _: self._on_model_config_changed())
        pform.addRow("", self.cb_stream)

        gv.addLayout(pform)
        v.addWidget(g_param)

        g_preset = QGroupBox("常用 API 预设")
        gv = QVBoxLayout(g_preset)
        row = QHBoxLayout()
        self.preset_btns: list[tuple[QPushButton, dict]] = []
        presets = [
            ("DeepSeek", {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"}),
            ("OpenAI",   {"base_url": "https://api.openai.com/v1", "model": "gpt-4o"}),
            ("硅基流动",  {"base_url": "https://api.siliconflow.cn/v1", "model": "deepseek-ai/DeepSeek-V3"}),
            ("本地",      {"base_url": "http://127.0.0.1:8000/v1", "model": "local-model"}),
        ]
        for name, cfg in presets:
            btn = QPushButton(name)
            btn.clicked.connect(lambda _=False, b=btn, c=cfg: self._on_preset_clicked(b, c))
            self.preset_btns.append((btn, cfg))
            row.addWidget(btn)
        gv.addLayout(row)
        v.addWidget(g_preset)

        g_voice = QGroupBox("TTS 语音")
        gv = QVBoxLayout(g_voice)
        vform = QFormLayout()
        vform.setSpacing(8)

        self.cmb_voice = QComboBox()
        self.cmb_voice.addItems([
            "zh-CN-XiaoxiaoNeural",
            "zh-CN-XiaoyiNeural",
            "zh-CN-XiaomoNeural",
            "zh-CN-XiaoruiNeural",
            "zh-CN-XiaohanNeural",
            "zh-CN-XiaoshuangNeural",
            "zh-CN-YunxiNeural",
            "zh-CN-YunxiaNeural",
            "zh-CN-YunjianNeural",
            "zh-CN-YunfengNeural",
            "zh-CN-YunhaoNeural",
            "zh-CN-YunzeNeural",
        ])
        self.cmb_voice.currentTextChanged.connect(self._on_voice_changed)
        vform.addRow("语音音色：", self.cmb_voice)

        self.cb_tts_enabled = QCheckBox("启用语音朗读")
        self.cb_tts_enabled.setChecked(True)
        self.cb_tts_enabled.toggled.connect(self._on_tts_enabled_changed)
        vform.addRow("", self.cb_tts_enabled)

        self.btn_preview_voice = QPushButton("试听")
        self.btn_preview_voice.setFixedWidth(80)
        self.btn_preview_voice.setFixedHeight(32)
        self.btn_preview_voice.clicked.connect(self._on_preview_voice)
        vform.addRow("试听：", self.btn_preview_voice)

        gv.addLayout(vform)
        v.addWidget(g_voice)

        st_tts = self._panel_state.get("tts", {})
        g_tts_cfg = QGroupBox("语音引擎与参数")
        gform = QFormLayout(g_tts_cfg)
        gform.setSpacing(8)

        self.cmb_tts_engine = QComboBox()
        self.cmb_tts_engine.addItem("edge（免费在线 TTS）", "edge")
        self.cmb_tts_engine.addItem("minimax（声音克隆，需账号开通）", "minimax")
        self.cmb_tts_engine.addItem("gptsovits（本地 GPT-SoVITS，免费）", "gptsovits")
        idx_e = self.cmb_tts_engine.findData(st_tts.get("engine", "edge"))
        self.cmb_tts_engine.setCurrentIndex(idx_e if idx_e >= 0 else 0)
        gform.addRow("引擎：", self.cmb_tts_engine)

        self.edit_minimax_voice_id = QLineEdit(str(st_tts.get("minimax_voice_id", "")))
        self.edit_minimax_voice_id.setPlaceholderText("tools/clone_voice.py 生成的 voice_id")
        gform.addRow("MiniMax voice_id：", self.edit_minimax_voice_id)

        self.edit_gptsovits_url = QLineEdit(str(st_tts.get("gptsovits_url", "http://127.0.0.1:9880")))
        gform.addRow("GSV 服务地址：", self.edit_gptsovits_url)

        self.edit_gptsovits_ref = QLineEdit(str(st_tts.get("ref_audio", "")))
        self.edit_gptsovits_ref.setPlaceholderText("参考音频 wav 的完整路径（3~10 秒）")
        gform.addRow("参考音频：", self.edit_gptsovits_ref)

        self.edit_gptsovits_prompt = QLineEdit(str(st_tts.get("prompt_text", "")))
        self.edit_gptsovits_prompt.setPlaceholderText("参考音频里说的那句话")
        gform.addRow("参考文本：", self.edit_gptsovits_prompt)

        self.tts_tip = QLabel("修改任一项立即保存并热切换引擎。minimax 需账号开通声音克隆；"
                              "gptsovits 需先启动本地 api_v2 服务。")
        self.tts_tip.setWordWrap(True)
        self.tts_tip.setStyleSheet(f"color: {ui_style.TEXT_SUB}; font-size: 11px;")
        gform.addRow("", self.tts_tip)
        v.addWidget(g_tts_cfg)

        st_char = self._panel_state.get("character", {})
        g_char = QGroupBox("角色设定")
        cform = QFormLayout(g_char)
        cform.setSpacing(8)
        self.edit_char_name = QLineEdit(str(st_char.get("name", "")))
        cform.addRow("角色名：", self.edit_char_name)
        self.edit_char_persona = QLineEdit(str(st_char.get("persona", "")))
        self.edit_char_persona.setPlaceholderText("角色人设一句话描述（聊天的系统人设会实时采用）")
        cform.addRow("人设：", self.edit_char_persona)
        v.addWidget(g_char)

        # 新控件 → 统一发射（放在控件创建之后连接）
        self.cmb_tts_engine.currentIndexChanged.connect(self._emit_tts_config)
        self.edit_minimax_voice_id.editingFinished.connect(self._emit_tts_config)
        self.edit_gptsovits_url.editingFinished.connect(self._emit_tts_config)
        self.edit_gptsovits_ref.editingFinished.connect(self._emit_tts_config)
        self.edit_gptsovits_prompt.editingFinished.connect(self._emit_tts_config)
        self.cb_tts_enabled.toggled.connect(self._emit_tts_config)
        self.edit_char_name.editingFinished.connect(self._emit_character)
        self.edit_char_persona.editingFinished.connect(self._emit_character)

        v.addStretch(1)
        return page
    def _wire_signals(self) -> None:
        # FPS 预设
        for btn, fps, ms in self.fps_presets:
            btn.clicked.connect(lambda _=False, b=btn, m=ms, f=fps:
                self._on_fps_preset_clicked(b, m, f))
        # 帧时长细调
        self.frame_ms_slider.valueChanged.connect(self._on_frame_ms_changed)
        # 持久化
        self.btn_persist_frames.clicked.connect(self._on_persist_clicked)
        # FPS 气泡调试
        self.cb_fps_monitor.toggled.connect(self._on_fps_monitor_toggled)

        # 缩放
        for btn, sc in self.scale_presets:
            btn.clicked.connect(lambda _=False, b=btn, s=sc: self._on_scale_preset_clicked(b, s))
        self.scale_slider.valueChanged.connect(self._on_scale_changed)

        # 透明度
        self.opacity_slider.valueChanged.connect(self._on_opacity_changed)

        # crossfade
        self.cb_crossfade.toggled.connect(self._on_crossfade_toggle)
        self.crossfade_ms_slider.valueChanged.connect(self._on_crossfade_ms_changed)

        # lock idle
        self.cb_lock_first_idle.toggled.connect(self._on_lock_first_idle_toggled)

        # 快捷入口
        self.btn_chat.clicked.connect(self.chat_requested.emit)
        self.btn_sleep.clicked.connect(self.sleep_requested.emit)
        self.btn_wake.clicked.connect(self.idle_requested.emit)

        # 表情
        for btn, key in self.emo_btns:
            btn.clicked.connect(lambda _=False, b=btn, k=key:
                self._on_emotion_clicked(b, k))

        # 行为节拍
        for key, (slider, val_lbl) in self.motion_sliders.items():
            slider.valueChanged.connect(lambda v, k=key: self._on_motion_changed(k, v))

        # 模型预设按钮
        for btn, cfg in self.preset_btns:
            btn.clicked.connect(lambda _=False, b=btn, c=cfg: self._on_preset_clicked(b, c))

        # 底部按钮
        self.btn_reset.clicked.connect(self.reset_to_defaults_requested.emit)
        self.btn_close.clicked.connect(self.close)

    #  加载默认值（所有 setValue/setChecked 都 blockSignals，避免初始化期就触发外部 slot）
    def _load_defaults(self) -> None:
        # scale
        self.scale_slider.blockSignals(True)
        self.scale_slider.setValue(40)
        self.scale_slider.blockSignals(False)
        self.scale_label.setText("0.40x")
        self._highlight_scale_preset(0.40)
        # opacity
        self.opacity_slider.blockSignals(True)
        self.opacity_slider.setValue(100)
        self.opacity_slider.blockSignals(False)
        self.opacity_label.setText("100 %")
        # frame_ms
        self.frame_ms_slider.blockSignals(True)
        self.frame_ms_slider.setValue(50)
        self.frame_ms_slider.blockSignals(False)
        self.frame_ms_label.setText("50 ms  (20.0 fps)")
        self._highlight_fps_preset(50)
        # crossfade
        self.crossfade_ms_slider.blockSignals(True)
        self.crossfade_ms_slider.setValue(200)
        self.crossfade_ms_slider.blockSignals(False)
        self.crossfade_ms_label.setText("200 ms")
        # 互斥按钮 + checkbox：统一 blockSignals 避免初始化期发信号
        self.cb_crossfade.blockSignals(True)
        self.cb_crossfade.setChecked(False)
        self.cb_crossfade.blockSignals(False)

        self.cb_lock_first_idle.blockSignals(True)
        self.cb_lock_first_idle.setChecked(False)
        self.cb_lock_first_idle.blockSignals(False)
        # motion
        for key, val in [("idle_s", 45), ("walk_s", 10), ("decide_s", 30)]:
            slider, label = self.motion_sliders[key]
            slider.blockSignals(True)
            slider.setValue(val)
            slider.blockSignals(False)
            label.setText(f"{val} s")
        # FPS 调试默认关
        self.cb_fps_monitor.blockSignals(True)
        self.cb_fps_monitor.setChecked(False)
        self.cb_fps_monitor.blockSignals(False)
        # 模型配置默认值
        self.slider_temperature.blockSignals(True)
        self.slider_temperature.setValue(80)
        self.slider_temperature.blockSignals(False)
        self.lbl_temperature.setText("0.80")
        self.spin_max_tokens.blockSignals(True)
        self.spin_max_tokens.setValue(1024)
        self.spin_max_tokens.blockSignals(False)
        self.spin_timeout.blockSignals(True)
        self.spin_timeout.setValue(60)
        self.spin_timeout.blockSignals(False)
        self.cb_stream.blockSignals(True)
        self.cb_stream.setChecked(True)
        self.cb_stream.blockSignals(False)
        self.lbl_model_status.setText("状态：未配置 API Key")
        # 预设按钮：setChecked 会触发 clicked（因为是 checkable）→ 导致重复 emit，所以也要 block
        for btn, _, _ in getattr(self, 'fps_presets', []):
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
        for btn, _ in getattr(self, 'scale_presets', []):
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
        # 重新高亮（不触发信号）
        self._highlight_fps_preset(50)
        self._highlight_scale_preset(0.40)
        # 表情互斥：全不选
        for btn, _ in self.emo_btns:
            btn.blockSignals(True)
            btn.setChecked(False)
            btn.blockSignals(False)
        self._set_status("默认值已加载")

    #  从持久化存储加载设置
    def _load_from_store(self) -> None:
        """从 SettingsStore 加载已保存的设置（如果有的话）。"""
        store = self.settings_store
        # scale
        scale = store.get("scale", None)
        if scale is not None:
            self.scale_slider.blockSignals(True)
            self.scale_slider.setValue(int(scale * 100))
            self.scale_slider.blockSignals(False)
            self.scale_label.setText(f"{scale:.2f}x")
            self._highlight_scale_preset(scale)
        # opacity
        opacity = store.get("opacity", None)
        if opacity is not None:
            self.opacity_slider.blockSignals(True)
            self.opacity_slider.setValue(int(opacity * 100))
            self.opacity_slider.blockSignals(False)
            self.opacity_label.setText(f"{int(opacity * 100)} %")
        # frame_ms
        frame_ms = store.get("frame_ms", None)
        if frame_ms is not None:
            self.frame_ms_slider.blockSignals(True)
            self.frame_ms_slider.setValue(frame_ms)
            self.frame_ms_slider.blockSignals(False)
            fps = 1000.0 / frame_ms if frame_ms > 0 else 0
            self.frame_ms_label.setText(f"{frame_ms} ms  ({fps:.1f} fps)")
            self._highlight_fps_preset(frame_ms)
            self.lbl_fps_apply.setText(f"当前：{fps:.1f} fps（{frame_ms} ms / 帧）· 已立即应用")
        # crossfade
        cf_enabled = store.get("crossfade_enabled", None)
        if cf_enabled is not None:
            self.cb_crossfade.blockSignals(True)
            self.cb_crossfade.setChecked(cf_enabled)
            self.cb_crossfade.blockSignals(False)
        cf_ms = store.get("crossfade_ms", None)
        if cf_ms is not None:
            self.crossfade_ms_slider.blockSignals(True)
            self.crossfade_ms_slider.setValue(cf_ms)
            self.crossfade_ms_slider.blockSignals(False)
            self.crossfade_ms_label.setText(f"{cf_ms} ms")
        # lock_first_idle
        lock_idle = store.get("lock_first_idle", None)
        if lock_idle is not None:
            self.cb_lock_first_idle.blockSignals(True)
            self.cb_lock_first_idle.setChecked(lock_idle)
            self.cb_lock_first_idle.blockSignals(False)
        # fps_monitor
        fps_mon = store.get("fps_monitor", None)
        if fps_mon is not None:
            self.cb_fps_monitor.blockSignals(True)
            self.cb_fps_monitor.setChecked(fps_mon)
            self.cb_fps_monitor.blockSignals(False)
        # motion
        for key, default_val in [("idle_s", 45), ("walk_s", 10), ("decide_s", 30)]:
            val = store.get(f"motion_{key}", None)
            if val is not None:
                slider, label = self.motion_sliders[key]
                slider.blockSignals(True)
                slider.setValue(val)
                slider.blockSignals(False)
                label.setText(f"{val} s")
        # model config
        model_cfg = {
            "base_url": store.get("model_base_url", ""),
            "api_key": store.get("model_api_key", ""),
            "model": store.get("model_name", ""),
            "temperature": store.get("model_temperature", 0.8),
            "max_tokens": store.get("model_max_tokens", 1024),
            "timeout": store.get("model_timeout", 60),
            "stream": store.get("model_stream", True),
        }
        if any(model_cfg.values()):
            self.set_model_config_ui(model_cfg)
        # TTS voice
        voice = store.get("tts_voice", None)
        if voice:
            self.cmb_voice.blockSignals(True)
            idx = self.cmb_voice.findText(voice)
            if idx >= 0:
                self.cmb_voice.setCurrentIndex(idx)
            else:
                self.cmb_voice.setEditText(voice)
            self.cmb_voice.blockSignals(False)
        tts_enabled = store.get("tts_enabled", None)
        if tts_enabled is not None:
            self.cb_tts_enabled.blockSignals(True)
            self.cb_tts_enabled.setChecked(tts_enabled)
            self.cb_tts_enabled.blockSignals(False)

    #  事件处理
    def _on_fps_preset_clicked(self, btn, ms: int, fps: int) -> None:
        """用户点了 FPS 预设按钮：勾上对应按钮 + 把细调滑块同步 + 立刻改内存帧时长。

        PR-fix-stack-overrun: checkable QPushButton.setChecked 会触发 clicked/toggled，
        如果在互斥循环里不 blockSignals → 递归 → Python stack overrun
        （exit -1073740791，用户看到的"设置面板打不开一启动就崩"）。
        """# 互斥：其它预设按钮取消勾选（必须 blockSignals 避免递归）
        for b, _, _ in self.fps_presets:
            b.blockSignals(True)
            b.setChecked(b is btn)
            b.blockSignals(False)
        # 同步细调滑块（会触发 _on_frame_ms_changed，已经有 emit，所以这里不需要重复 emit）
        self.frame_ms_slider.blockSignals(True)
        self.frame_ms_slider.setValue(ms)
        self.frame_ms_slider.blockSignals(False)
        # 标签 + emit
        self.lbl_fps_apply.setText(
            f"当前：{fps} fps（{ms} ms / 帧）· 已立即应用")
        self.override_frame_ms_requested.emit(ms)
        self._set_status(f"立即切换到 {fps} fps（内存中所有帧改为 {ms} ms / 帧）")

    def _on_frame_ms_changed(self, v: int) -> None:
        fps = 1000.0 / v if v > 0 else 0
        self.frame_ms_label.setText(f"{v} ms  ({fps:.1f} fps)")
        # 如果正好是某个预设，把按钮勾上；否则预设全不勾
        self._highlight_fps_preset(v)
        self.lbl_fps_apply.setText(f"当前：{fps:.1f} fps（{v} ms / 帧）· 已立即应用")
        self.settings_store.set("frame_ms", v)
        self.override_frame_ms_requested.emit(v)

    def _highlight_fps_preset(self, ms: int) -> None:
        matched = False
        for btn, fps, preset_ms in self.fps_presets:
            btn.blockSignals(True)
            if preset_ms == ms:
                btn.setChecked(True)
                matched = True
            else:
                btn.setChecked(False)
            btn.blockSignals(False)
        if not matched:
            for btn, _, _ in self.fps_presets:
                btn.blockSignals(True)
                btn.setChecked(False)
                btn.blockSignals(False)

    def _on_persist_clicked(self) -> None:
        """把当前 ms 持久化到 PNG 文件名（较重，单独按钮确认用）。"""
        ms = self.frame_ms_slider.value()
        self.persist_frames_requested.emit(ms)

    def _on_lock_first_idle_toggled(self, on: bool) -> None:
        """锁定第一帧 idle 动画开关（持久化 + 发信号）。"""
        self.settings_store.set("lock_first_idle", on)
        self.lock_first_idle_changed.emit(on)

    def _on_fps_monitor_toggled(self, on: bool) -> None:
        self.settings_store.set("fps_monitor", on)
        self.fps_monitor_toggled.emit(on)
        if on:
            self._set_status("FPS 气泡监测已开启，桌宠头顶每秒会显示一次实时 FPS")
        else:
            self._set_status("FPS 气泡监测已关闭")

    def _on_scale_preset_clicked(self, btn, sc: float) -> None:
        for b, _ in self.scale_presets:
            b.blockSignals(True)
            b.setChecked(b is btn)
            b.blockSignals(False)
        self.scale_slider.blockSignals(True)
        self.scale_slider.setValue(int(sc * 100))
        self.scale_slider.blockSignals(False)
        self.scale_label.setText(f"{sc:.2f}x")
        self.settings_changed.emit()

    def _on_scale_changed(self, v: int) -> None:
        self.scale_label.setText(f"{v / 100:.2f}x")
        self._highlight_scale_preset(v / 100.0)
        self.settings_store.set("scale", v / 100.0)
        self.settings_changed.emit()

    def _highlight_scale_preset(self, sc: float) -> None:
        for btn, preset_sc in self.scale_presets:
            btn.blockSignals(True)
            btn.setChecked(abs(preset_sc - sc) < 0.001)
            btn.blockSignals(False)

    def _on_opacity_changed(self, v: int) -> None:
        self.opacity_label.setText(f"{v} %")
        self.settings_store.set("opacity", v / 100.0)
        self.settings_changed.emit()

    def _on_crossfade_toggle(self, on: bool) -> None:
        self.settings_store.set("crossfade_enabled", on)
        self.crossfade_changed.emit(on, self.crossfade_ms_slider.value())

    def _on_crossfade_ms_changed(self, v: int) -> None:
        self.crossfade_ms_label.setText(f"{v} ms")
        self.settings_store.set("crossfade_ms", v)
        if self.cb_crossfade.isChecked():
            self.crossfade_changed.emit(True, v)

    def _on_emotion_clicked(self, btn, key: str) -> None:
        for b, _ in self.emo_btns:
            b.blockSignals(True)
            b.setChecked(b is btn)
            b.blockSignals(False)
        self.emotion_requested.emit(key)
        names = {'happy': '开心', 'sad': '悲伤', 'angry': '生气',
                 'shy': '害羞', 'think': '思考'}
        self._set_status(f"切换到表情：{names.get(key, key)}")

    def _on_motion_changed(self, key: str, v: int) -> None:
        if key in self.motion_sliders:
            self.motion_sliders[key][1].setText(f"{v} s")
        self.settings_store.set(f"motion_{key}", v)
        self.settings_changed.emit()

    def _on_model_config_changed(self) -> None:
        """用户修改了模型配置，发出信号并持久化。"""
        cfg = self.get_model_config()
        # 持久化
        self.settings_store.set("model_base_url", cfg["base_url"])
        self.settings_store.set("model_api_key", cfg["api_key"])
        self.settings_store.set("model_name", cfg["model"])
        self.settings_store.set("model_temperature", cfg["temperature"])
        self.settings_store.set("model_max_tokens", cfg["max_tokens"])
        self.settings_store.set("model_timeout", cfg["timeout"])
        self.settings_store.set("model_stream", cfg["stream"])
        # 发信号
        self.model_config_changed.emit(cfg)
        self._set_status(f"模型配置已更新：{cfg['model']} @ {cfg['base_url'][:40]}...")

    def _on_temperature_changed(self, v: int) -> None:
        self.lbl_temperature.setText(f"{v / 100:.2f}")
        self._on_model_config_changed()

    def _toggle_api_key_visibility(self) -> None:
        if self.edt_api_key.echoMode() == QLineEdit.EchoMode.Password:
            self.edt_api_key.setEchoMode(QLineEdit.EchoMode.Normal)
            self.btn_toggle_key.setText("隐藏")
        else:
            self.edt_api_key.setEchoMode(QLineEdit.EchoMode.Password)
            self.btn_toggle_key.setText("显示")

    def _on_preset_clicked(self, btn, cfg: dict) -> None:
        """点击预设按钮，自动填充对应 API 地址和模型名。"""
        self.edt_base_url.blockSignals(True)
        self.edt_base_url.setText(cfg["base_url"])
        self.edt_base_url.blockSignals(False)
        self.cmb_model.blockSignals(True)
        self.cmb_model.setEditText(cfg["model"])
        self.cmb_model.blockSignals(False)
        self._on_model_config_changed()
        self._set_status(f"已应用预设：{btn.text()}")

    def _on_voice_changed(self, voice: str) -> None:
        """用户修改了 TTS 语音，发出信号并持久化。"""
        self.settings_store.set("tts_voice", voice)
        self.voice_changed.emit(voice)
        self._set_status(f"语音已切换：{voice}")

    def _on_tts_enabled_changed(self, enabled: bool) -> None:
        """用户切换了 TTS 开关，持久化。"""
        self.settings_store.set("tts_enabled", enabled)
        self.voice_changed.emit(self.cmb_voice.currentText() if enabled else "")

    def _on_preview_voice(self) -> None:
        """试听当前选中的语音。"""
        from app.voice.voice import TTS
        voice = self.cmb_voice.currentText()
        # 角色名：优先 char_cfg，兜底「桌宠」，避免 None 触发 AttributeError
        name = "桌宠"
        if getattr(self, "char_cfg", None) is not None:
            name = getattr(self.char_cfg, "name", None) or name
        preview_text = f"你好呀，我是{name}，很高兴见到你。"
        try:
            tts = TTS(voice=voice)
            tts.speak(preview_text)
            self._set_status(f"正在试听：{voice}")
        except Exception as e:  # noqa: BLE001
            self._set_status(f"试听失败：{e}")

    def get_model_config(self) -> dict:
        """获取当前模型配置（供主程序读取）。"""
        return {
            "base_url": self.edt_base_url.text().strip(),
            "api_key": self.edt_api_key.text().strip(),
            "model": self.cmb_model.currentText().strip(),
            "temperature": self.slider_temperature.value() / 100.0,
            "max_tokens": self.spin_max_tokens.value(),
            "timeout": self.spin_timeout.value(),
            "stream": self.cb_stream.isChecked(),
        }

    def set_model_config_ui(self, cfg: dict) -> None:
        """从外部设置模型配置 UI（启动时加载 config.yaml 的值）。"""
        self.edt_base_url.blockSignals(True)
        self.edt_base_url.setText(cfg.get("base_url", ""))
        self.edt_base_url.blockSignals(False)
        self.edt_api_key.blockSignals(True)
        self.edt_api_key.setText(cfg.get("api_key", ""))
        self.edt_api_key.blockSignals(False)
        model = cfg.get("model", "")
        if model:
            idx = self.cmb_model.findText(model)
            if idx >= 0:
                self.cmb_model.setCurrentIndex(idx)
            else:
                self.cmb_model.setEditText(model)
        temp = cfg.get("temperature", 0.8)
        self.slider_temperature.blockSignals(True)
        self.slider_temperature.setValue(int(temp * 100))
        self.slider_temperature.blockSignals(False)
        self.lbl_temperature.setText(f"{temp:.2f}")
        self.spin_max_tokens.blockSignals(True)
        self.spin_max_tokens.setValue(cfg.get("max_tokens", 1024))
        self.spin_max_tokens.blockSignals(False)
        self.spin_timeout.blockSignals(True)
        self.spin_timeout.setValue(cfg.get("timeout", 60))
        self.spin_timeout.blockSignals(False)
        self.cb_stream.blockSignals(True)
        self.cb_stream.setChecked(cfg.get("stream", True))
        self.cb_stream.blockSignals(False)
        # TTS 语音
        voice = cfg.get("voice", "zh-CN-XiaoxiaoNeural")
        self.cmb_voice.blockSignals(True)
        idx = self.cmb_voice.findText(voice)
        if idx >= 0:
            self.cmb_voice.setCurrentIndex(idx)
        else:
            self.cmb_voice.setEditText(voice)
        self.cmb_voice.blockSignals(False)
        self.cb_tts_enabled.blockSignals(True)
        self.cb_tts_enabled.setChecked(cfg.get("tts_enabled", True))
        self.cb_tts_enabled.blockSignals(False)
        # 检测 API 状态
        if cfg.get("api_key"):
            self.lbl_model_status.setText("状态：已配置 API Key")
            self.lbl_model_status.setStyleSheet("color: #16a34a; font-size: 11px;")
        else:
            self.lbl_model_status.setText("状态：未配置 API Key")
            self.lbl_model_status.setStyleSheet("color: #b45309; font-size: 11px;")

    def _set_status(self, msg: str) -> None:
        self.lbl_status.setText(msg)

    #  Public API（供主程序读）
    def get_scale(self) -> float:
        return self.scale_slider.value() / 100.0

    def is_always_on_top(self) -> bool:
        """窗口置顶开关当前状态。"""
        return self.cb_always_on_top.isChecked()

    def _emit_tts_config(self) -> None:
        """TTS 任意控件变化 → 打包当前配置发信号。"""
        self.tts_config_changed.emit({
            "tts_enabled": self.cb_tts_enabled.isChecked(),
            "engine": str(self.cmb_tts_engine.currentData() or "edge"),
            "minimax_voice_id": self.edit_minimax_voice_id.text().strip(),
            "gptsovits_url": self.edit_gptsovits_url.text().strip(),
            "ref_audio": self.edit_gptsovits_ref.text().strip(),
            "prompt_text": self.edit_gptsovits_prompt.text().strip(),
        })

    def _emit_character(self) -> None:
        self.character_changed.emit({
            "name": self.edit_char_name.text().strip(),
            "persona": self.edit_char_persona.text().strip(),
        })

    def _emit_proactive(self) -> None:
        lo = min(self.spin_proactive_min.value(), self.spin_proactive_max.value())
        hi = max(self.spin_proactive_min.value(), self.spin_proactive_max.value())
        self.proactive_changed.emit({
            "enabled": self.cb_proactive.isChecked(),
            "min_minutes": lo,
            "max_minutes": hi,
        })

    def _emit_renderer(self) -> None:
        data = self.cmb_renderer.currentData()
        if data:
            self.renderer_changed.emit(str(data))

    def _emit_sticker_options(self) -> None:
        # 同步滑条旁的数值标签
        self.lbl_sticker_size.setText(f"{self.slider_sticker_size.value()} px")
        self.lbl_sticker_rot.setText(f"{self.slider_sticker_rot.value()}°")
        self.sticker_options_changed.emit({
            "size": self.slider_sticker_size.value(),
            "rotation": self.slider_sticker_rot.value(),
            "min_s": self.spin_sticker_min.value(),
            "max_s": self.spin_sticker_max.value(),
            "duration_s": self.spin_sticker_duration.value(),
        })

    def _emit_random_interval(self) -> None:
        lo = min(self.spin_random_min.value(), self.spin_random_max.value())
        hi = max(self.spin_random_min.value(), self.spin_random_max.value())
        self.random_interval_changed.emit(lo, hi)

    def _emit_max_fps(self) -> None:
        fps = self.cmb_max_fps.currentData()
        if fps is not None:
            self.max_fps_changed.emit(int(fps))

    def get_opacity(self) -> float:
        return self.opacity_slider.value() / 100.0

    def get_idle_seconds(self) -> int:
        return self.motion_sliders["idle_s"][0].value()

    def get_walk_seconds(self) -> int:
        return self.motion_sliders["walk_s"][0].value()

    def get_decide_seconds(self) -> int:
        return self.motion_sliders["decide_s"][0].value()

    def get_frame_ms(self) -> int:
        return self.frame_ms_slider.value()

    def set_fps_monitor(self, on: bool) -> None:
        """主程序从右键菜单收到 FPS 调试开关时同步 UI（保持两边一致）。"""
        self.cb_fps_monitor.blockSignals(True)
        self.cb_fps_monitor.setChecked(on)
        self.cb_fps_monitor.blockSignals(False)

    def set_frame_ms_ui(self, ms: int) -> None:
        """主程序从其它来源改帧率时同步 UI。"""
        self.frame_ms_slider.blockSignals(True)
        self.frame_ms_slider.setValue(ms)
        self.frame_ms_slider.blockSignals(False)
        fps = 1000.0 / ms if ms > 0 else 0
        self.frame_ms_label.setText(f"{ms} ms  ({fps:.1f} fps)")
        self._highlight_fps_preset(ms)

    #  状态条刷新
    _MODE_CN = {"Happy": "开心", "Normal": "正常",
                "PoorCondition": "状态不佳", "Ill": "生病"}

    def _status_summary_plain(self, s) -> str:
        mode_cn = self._MODE_CN.get(getattr(s.mode, "value", str(s.mode)), "正常")
        return f"Lv.{int(s.level)}    金币 {s.money:.0f}    状态 {mode_cn}"

    def refresh_state(self) -> None:
        s = self._attached_state
        if s is None:
            self.lbl_summary.setText("（未连接 state）")
            return
        like_max = float(getattr(s, "likability_max", 100.0))
        vals = {
            "strength": min(100.0, max(0.0, float(s.strength))),
            "strength_food": min(100.0, max(0.0, float(s.strength_food))),
            "strength_drink": min(100.0, max(0.0, float(s.strength_drink))),
            "feeling": min(100.0, max(0.0, float(s.feeling))),
            "health": min(100.0, max(0.0, float(s.health))),
            "likability": min(like_max, max(0.0, float(s.likability))),
        }
        for attr, bar in self._stat_bars:
            v = vals[attr]
            bar.setRange(0, int(like_max) if attr == "likability" else 100)
            bar.setValue(int(v))
            lbl = self._stat_value_labels.get(attr)
            if lbl is not None:
                lbl.setText(f"{v:.0f}/{like_max:.0f}" if attr == "likability"
                            else f"{v:.0f}")
        self.lbl_summary.setText(self._status_summary_plain(s))

    def is_lock_first_idle(self) -> bool:
        return self.cb_lock_first_idle.isChecked()

    def is_crossfade_enabled(self) -> bool:
        return self.cb_crossfade.isChecked()

    def get_crossfade_ms(self) -> int:
        return self.crossfade_ms_slider.value()
