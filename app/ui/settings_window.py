"""桌宠设置面板（重构版）。

新设计：把「帧时长 → 显示 FPS 调试」的整条链路完整搬到设置面板，
按语义分成 4 个 Tab：
     状态    状态条（体力/饱食/口渴/心情/健康/好感 + 摘要）
     帧数    FPS 预设按钮 + 帧时长滑块 + 实时改帧率 + FPS 气泡调试
     视觉    缩放预设 + 滑块 + 透明度 + crossfade + lock idle
     控制    表情 / 睡觉 / 醒来 / 聊天 / 自主行为节拍

打开方式：托盘菜单「设置」 / 桌宠右键菜单「打开设置面板」。

所有控件**实时生效**（不再需要「应用帧时长」重命名文件的方式，改的是内存里
frame.duration_ms；只有用户想把帧时长持久化到 PNG 文件名时才用持久化按钮）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from app.core.qt_compat import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton, QSpinBox,
    QSizePolicy, QSize, QSlider, QTabWidget, QVBoxLayout,
    QWidget, Signal, Qt, QFrame, QColor, QEvent, QGraphicsDropShadowEffect,
    QToolButton, QObject, QScrollArea,
    event_global_pos,
)
from app.ui import ui_style
from app.core.settings_store import SettingsStore
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

    # --- 视觉/行为相关 ---
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
    # --- 调试：显示 FPS 气泡开关 ---
    fps_monitor_toggled = Signal(bool)
    # --- 控制快捷：表情 / 睡觉 / 醒来 ---
    emotion_requested = Signal(str)   # 'happy' / 'sad' / 'angry' / 'shy' / 'think'
    sleep_requested = Signal()
    idle_requested = Signal()
    chat_requested = Signal()
    # --- 模型配置变更（base_url, api_key, model, temperature, max_tokens, timeout）---
    model_config_changed = Signal(dict)
    # --- TTS 语音变更 ---
    voice_changed = Signal(str)
    # --- Live2D 专属（仅 live2d 渲染器时显示该 Tab）---
    live2d_item_activated = Signal(str, str)   # (group_id, item_id)
    live2d_reset_requested = Signal()
    random_exp_changed = Signal(bool)          # 挂机随机表情开关

    def __init__(self, parent: Optional[QWidget] = None,
                 char_cfg: Optional["CharacterConfig"] = None,
                 renderer: Optional[object] = None) -> None:
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
        # v2 美化：无边框圆角窗口（窗口透明，内部白色圆角卡片 + 自绘标题栏）
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setStyleSheet(ui_style.SETTINGS_QSS)
        # 角色配置（用于「试听」按钮显示角色名 + 后续扩展）；允许为 None 以保留向后兼容
        self.char_cfg = char_cfg
        # 渲染器引用（可选）：live2d 时用于生成「Live2D」Tab 与 renderer-aware 表情按钮
        self._renderer = renderer
        self._build_ui()
        self._wire_signals()
        self._load_defaults()
        self._attached_state = None

        # 设置持久化：从 JSON 文件加载已保存的设置
        self.settings_store = SettingsStore()
        self._load_from_store()

    # ============================================================
    #  Public: attach state（V3 状态条）
    # ============================================================
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

    # ============================================================
    #  UI 构造
    # ============================================================
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

        # —— 底部状态栏 + 按钮 ——
        self.lbl_status = QLabel("就绪")
        self.lbl_status.setStyleSheet(f"color: {ui_style.TEXT_SUB};")
        cl.addWidget(self.lbl_status)

        row = QHBoxLayout()
        self.btn_reset = QPushButton("重置默认值")
        self.btn_close = QPushButton("关闭")
        row.addWidget(self.btn_reset)
        row.addStretch(1)
        row.addWidget(self.btn_close)
        cl.addLayout(row)

    # ---------- Tab: 状态 ----------
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
            bar.setStyleSheet(ui_style.stat_bar_qss(ui_style.STAT_BAR_COLORS[key]))
        labels_and_bars = [
            ("体力", self.bar_strength),
            ("饱食", self.bar_food),
            ("口渴", self.bar_drink),
            ("心情", self.bar_feeling),
            ("健康", self.bar_health),
            ("好感", self.bar_likability),
        ]
        for text, bar in labels_and_bars:
            row = QHBoxLayout()
            lbl = QLabel(text); lbl.setFixedWidth(56)
            row.addWidget(lbl); row.addWidget(bar, 1)
            gv.addLayout(row)
        self.lbl_summary = QLabel("（未连接 state）")
        f = self.lbl_summary.font(); f.setBold(True); self.lbl_summary.setFont(f)
        gv.addWidget(self.lbl_summary)
        v.addWidget(g)
        v.addStretch(1)
        return page

    # ---------- Tab: 帧数（核心重构：FPS 预设 + 实时改帧率 + FPS 气泡调试） ----------
    def _build_tab_fps(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setSpacing(10)

        # —— FPS 预设按钮 ——
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

        # —— 帧时长滑块（细调）——
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

        # —— 持久化按钮（慎重：会改磁盘上 PNG 文件名）——
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

        # —— FPS 气泡调试 ——
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

    # ---------- Tab: 视觉 ----------
    def _build_tab_visual(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setSpacing(10)

        # —— 缩放：预设按钮 + 滑块 ——
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

        # —— 透明度 ——
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
        v.addWidget(g_op)

        # —— 动画切换优化（豆包生成图不连贯时用）——
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

    # ---------- Tab: 控制 ----------
    def _build_tab_control(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setSpacing(10)

        # —— 快捷入口：聊天 / 睡觉 / 醒来 ——
        g_short = QGroupBox("快捷入口")
        gv = QHBoxLayout(g_short)
        self.btn_chat  = QPushButton("和她聊聊")
        self.btn_chat.setObjectName("accent_btn")
        self.btn_sleep = QPushButton("睡觉")
        self.btn_wake  = QPushButton("醒来")
        gv.addWidget(self.btn_chat); gv.addWidget(self.btn_sleep); gv.addWidget(self.btn_wake)
        v.addWidget(g_short)

        # —— 表情（renderer-aware：live2d 用模型自带表情，sprite 用固定 5 个）——
        g_emo = QGroupBox("切换表情")
        gv = QGridLayout(g_emo)
        self.emo_btns: list[tuple[QPushButton, str]] = []
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
            self.emo_btns.append((btn, key))
            r, c = divmod(i, 3)
            gv.addWidget(btn, r, c)
        v.addWidget(g_emo)

        # —— 自主行为节拍 ——
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

        v.addStretch(1)
        return page

    # ---------- Tab: Live2D（模型专属外观 / 随机表情） ----------
    def _build_tab_live2d(self) -> QWidget:
        """Live2D 专属设置：按键说明五大类分类条目 + 复位 + 挂机随机表情。

        条目来自渲染器 profile（每模型一份 *.model.yaml），换模型自动跟随。
        """
        page = QWidget()
        v = QVBoxLayout(page)
        v.setSpacing(10)
        renderer = self._renderer

        # —— 模型名 ——
        model_name = ""
        try:
            model_name = getattr(renderer.profile, "name", "") or ""
        except Exception:  # noqa: BLE001
            pass
        if model_name:
            lbl = QLabel(f"当前模型：{model_name}")
            f = lbl.font(); f.setBold(True); lbl.setFont(f)
            v.addWidget(lbl)

        # —— 分类条目（下拉 + 应用）——
        self.live2d_combos: list[tuple[str, QComboBox]] = []
        try:
            groups = renderer.get_menu_groups()
        except Exception:  # noqa: BLE001
            groups = []
        for g in groups:
            box = QGroupBox(g["label"])
            h = QHBoxLayout(box)
            cmb = QComboBox()
            for item_id, label in g["items"]:
                cmb.addItem(label, item_id)
            btn = QPushButton("应用")
            btn.clicked.connect(
                lambda _=False, gid=g["id"], c=cmb:
                self.live2d_item_activated.emit(gid, str(c.currentData())))
            h.addWidget(cmb, 1)
            h.addWidget(btn)
            v.addWidget(box)
            self.live2d_combos.append((g["id"], cmb))

        # —— 复位 + 挂机随机表情 ——
        g_misc = QGroupBox("外观 / 挂机")
        gm = QVBoxLayout(g_misc)
        self.btn_live2d_reset = QPushButton("复位全部外观")
        self.btn_live2d_reset.clicked.connect(self.live2d_reset_requested.emit)
        gm.addWidget(self.btn_live2d_reset)

        rnd_on = False
        try:
            rnd_on = bool(renderer.is_random_expressions_enabled())
        except Exception:  # noqa: BLE001
            pass
        self.cb_random_exp = QCheckBox("挂机随机表情（空闲时随机切换表情，互动即暂停）")
        self.cb_random_exp.setChecked(rnd_on)
        self.cb_random_exp.toggled.connect(self.random_exp_changed.emit)
        gm.addWidget(self.cb_random_exp)
        v.addWidget(g_misc)

        v.addStretch(1)
        return page

    # ---------- Tab: 模型配置 ----------
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

        # —— API 连接 ——
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

        # —— 生成参数 ——
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

        # —— 常用 API 预设 ——
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

        # —— TTS 语音设置 ——
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

        v.addStretch(1)
        return page
    # ============================================================
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

    # ============================================================
    #  加载默认值（所有 setValue/setChecked 都 blockSignals，避免初始化期就触发外部 slot）
    # ============================================================
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

    # ============================================================
    #  从持久化存储加载设置
    # ============================================================
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

    # ============================================================
    #  事件处理
    # ============================================================
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
        # PR-fix-stack-overrun: 互斥按钮 setChecked 必须 blockSignals，否则递归炸栈
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

    # ---- 视觉 ----
    def _on_scale_preset_clicked(self, btn, sc: float) -> None:
        # PR-fix-stack-overrun: 互斥按钮必须 blockSignals
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
        # PR-fix-stack-overrun: 互斥按钮必须 blockSignals
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

    # ---- 控制 ----
    def _on_emotion_clicked(self, btn, key: str) -> None:
        # PR-fix-stack-overrun: 互斥按钮必须 blockSignals
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

    # ---- 模型配置 ----
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

    # ============================================================
    #  Public API（供主程序读）
    # ============================================================
    def get_scale(self) -> float:
        return self.scale_slider.value() / 100.0

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

    # ============================================================
    #  状态条刷新
    # ============================================================
    def refresh_state(self) -> None:
        s = self._attached_state
        if s is None:
            self.lbl_summary.setText("（未连接 state）")
            return
        self.bar_strength.setValue(int(s.strength))
        self.bar_food.setValue(int(s.strength_food))
        self.bar_drink.setValue(int(s.strength_drink))
        self.bar_feeling.setValue(int(s.feeling))
        self.bar_health.setValue(int(s.health))
        self.bar_likability.setValue(int(s.likability))
        self.lbl_summary.setText(s.stats_summary())

    def is_lock_first_idle(self) -> bool:
        return self.cb_lock_first_idle.isChecked()

    def is_crossfade_enabled(self) -> bool:
        return self.cb_crossfade.isChecked()

    def get_crossfade_ms(self) -> int:
        return self.crossfade_ms_slider.value()
