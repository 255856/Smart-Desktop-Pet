"""UI 控制器：封装所有 UI 窗口管理和信号连接。

职责：
    - 管理 PetWindow、SettingsWindow、ChatWindow、TrayController
    - 处理所有 UI 信号连接（设置面板、托盘、右键菜单等）
    - 管理视觉设置应用（缩放、透明度、帧率等）
    - 管理聊天窗口的打开和复用
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from app.core.qt_compat import QObject, QTimer, QSize, Qt, Signal
from .chat_window import ChatWindow
from app.ui.pet_window import PetWindow
from .settings_window import SettingsWindow
from app.engine.state_manager import StateManager
from app.engine.state import is_night
from app.core.tray import TrayController

log = logging.getLogger(__name__)


class UIController(QObject):
    """封装所有 UI 窗口管理和信号连接。"""

    # 聊天/主动搭话情绪 → Live2D 固定场景 id（未列出的情绪回退直接切表情）
    _EMOTION_SCENES = {
        "happy": "chat_happy", "shy": "chat_shy", "angry": "chat_angry",
        "sad": "chat_sad", "surprised": "chat_surprised",
        "thinking": "chat_thinking",
    }

    def __init__(self, root: Path, cfg, state_mgr: StateManager,
                 pet: PetWindow, tts, brain, motion,
                 parent: QObject | None = None) -> None:
        """
        Args:
            root: 项目根目录
            cfg: Config 对象
            state_mgr: StateManager 实例
            pet: PetWindow 实例
            tts: TTS 实例
            brain: BrainController 实例
            motion: MotionController 实例
            parent: Qt 父对象
        """
        super().__init__(parent)
        self.root = root
        self.cfg = cfg
        self.state_mgr = state_mgr
        self.state = state_mgr.state
        self.pet = pet
        self.tts = tts
        self.brain = brain
        self.motion = motion
        self._chat_window = None
        self._last_food_warn = 0.0

        # --- 设置窗口（传入渲染器引用：live2d 时生成「Live2D」Tab）---
        _sticker_on = True
        _pet = getattr(self, "pet", None)
        if _pet is not None and hasattr(_pet, "is_random_stickers_enabled"):
            try:
                _sticker_on = _pet.is_random_stickers_enabled()
            except Exception:  # noqa: BLE001
                _sticker_on = True
        # Live2D 参数初始值：模型/渲染器默认 ← settings.json 已存值覆盖
        from app.core.settings_store import SettingsStore as _Store
        _store0 = _Store()
        if _pet is not None and hasattr(_pet, "get_sticker_options"):
            _st = _pet.get_sticker_options()
        else:
            _st = {"size": 120, "rotation": 45, "min_s": 40,
                   "max_s": 120, "duration_s": 4}
        for _k, _skey in (("size", "sticker_size"), ("rotation", "sticker_rotation"),
                          ("min_s", "sticker_min_s"), ("max_s", "sticker_max_s"),
                          ("duration_s", "sticker_duration_s")):
            _v = _store0.get(_skey, None)
            if _v is not None:
                _st[_k] = int(_v)
        _r0 = getattr(_pet, "renderer", None)
        _ri_min = int(_store0.get("random_min_s", 20))
        _ri_max = int(_store0.get("random_max_s", 50))
        _mf = int(_store0.get("max_fps",
                              getattr(_r0, "max_fps", 30) if _r0 else 30))
        _renderer_cfg = getattr(self.cfg.pet, "renderer", "sprite")
        _hw_cfg = bool(getattr(self.cfg.pet.live2d, "hide_watermark", True))
        self.settings_window = SettingsWindow(
            char_cfg=self.cfg.character,
            renderer=getattr(_pet, "renderer", None),
            sticker_enabled=_sticker_on,
            always_on_top=bool(getattr(self.cfg.window, "always_on_top", True)),
            live2d_state={
                "sticker": _st,
                "random_interval": (_ri_min, _ri_max),
                "max_fps": _mf,
                # —— 全量面板化初始值（store 已存值覆盖 cfg 默认）——
                "tts": {
                    "engine": _store0.get("tts_engine", getattr(self.cfg.character, "tts_engine", "edge")),
                    "minimax_voice_id": _store0.get("minimax_voice_id", getattr(self.cfg.character, "minimax_voice_id", "")),
                    "gptsovits_url": _store0.get("gptsovits_url", getattr(self.cfg.character, "gptsovits_url", "http://127.0.0.1:9880")),
                    "ref_audio": _store0.get("gptsovits_ref_audio", getattr(self.cfg.character, "gptsovits_ref_audio", "")),
                    "prompt_text": _store0.get("gptsovits_prompt_text", getattr(self.cfg.character, "gptsovits_prompt_text", "")),
                },
                "proactive": {
                    "enabled": _store0.get("proactive_enabled", getattr(self.cfg.brain, "proactive_enabled", True)),
                    "min_minutes": _store0.get("proactive_min_minutes", getattr(self.cfg.brain, "proactive_min_minutes", 25)),
                    "max_minutes": _store0.get("proactive_max_minutes", getattr(self.cfg.brain, "proactive_max_minutes", 45)),
                },
                "renderer": _store0.get("renderer", _renderer_cfg),
                "hide_watermark": _store0.get("hide_watermark", _hw_cfg),
                "character": {
                    "name": _store0.get("character_name", self.cfg.character.name),
                    "persona": _store0.get("persona", self.cfg.character.persona),
                },
            },
        )
        self.settings_window.attach_state(self.state)

        # --- 托盘 ---
        fallback_rel = self.cfg.sprite.fallback
        fallback_image = root / fallback_rel
        # 优先使用 ICO 文件（多尺寸，任务栏显示效果最佳）
        ico_path = root / "assets" / "icon.ico"
        if ico_path.is_file():
            tray_icon = ico_path
        else:
            tray_icon = fallback_image
        self.tray = TrayController(
            app_name=f"{self.cfg.character.name} · 桌面宠物",
            icon_path=tray_icon,
        )

        # --- 连接所有信号 ---
        self._wire_all_signals()

        # --- 模型配置初始化（从 cfg.llm 加载到 UI） ---
        self._init_model_config_ui()
        self._setup_lipsync()
        # 启动时应用 settings.json 里保存的窗口类设置（缩放/透明度/置顶/随机开关）
        self._apply_stored_window_settings()
        # 闲置 30 分钟 / 深夜时段的 Live2D 场景
        self._setup_env_scenes()

    # ============================================================
    #  信号连接
    # ============================================================
    def _wire_all_signals(self) -> None:
        # --- 设置面板信号 ---
        sw = self.settings_window
        sw.settings_changed.connect(self._apply_visual_settings_with_rescale)
        sw.persist_frames_requested.connect(self._do_retune_frames)
        sw.retune_frames_requested.connect(self._do_retune_frames)
        sw.override_frame_ms_requested.connect(self._override_frame_ms_now)
        sw.fps_monitor_toggled.connect(self._on_fps_monitor_toggled)
        sw.lock_first_idle_changed.connect(self.pet.animator.set_lock_first_idle)
        sw.crossfade_changed.connect(self._on_crossfade_changed)
        sw.emotion_requested.connect(self.pet.play_emotion)
        sw.sleep_requested.connect(self.pet.animator.set_sleep)
        sw.idle_requested.connect(self.pet.animator.set_wake)
        sw.chat_requested.connect(self._show_chat_window)
        sw.reset_to_defaults_requested.connect(self._reset_to_default_settings)
        sw.model_config_changed.connect(self._on_model_config_changed)
        sw.voice_changed.connect(self._on_voice_changed)
        # --- Live2D 专属信号（设置窗口没有该 Tab 时不存在）---
        if hasattr(sw, "live2d_item_activated"):
            sw.live2d_item_activated.connect(self._on_live2d_item_activated)
            sw.live2d_reset_requested.connect(self._on_live2d_reset)
            sw.random_exp_changed.connect(self._on_random_exp_changed)
            sw.random_sticker_changed.connect(self._on_random_sticker_changed)
        sw.sticker_options_changed.connect(self._on_sticker_options_changed)
        sw.random_interval_changed.connect(self._on_random_interval_changed)
        sw.max_fps_changed.connect(self._on_max_fps_changed)
        sw.tts_config_changed.connect(self._on_tts_config_changed)
        sw.proactive_changed.connect(self._on_proactive_changed)
        sw.renderer_changed.connect(self._on_renderer_changed)
        sw.hide_watermark_changed.connect(self._on_hide_watermark_changed)
        sw.character_changed.connect(self._on_character_changed)
        # --- 窗口 / 持久化 ---
        sw.always_on_top_changed.connect(self._on_always_on_top_changed)
        sw.save_settings_requested.connect(self._on_save_settings)

        # --- 托盘信号 ---
        self.pet.quit_requested.connect(self._quit)
        self.tray.act_quit.triggered.connect(self._quit)
        self.tray.act_hide.triggered.connect(self.pet.hide)
        self.tray.act_show.triggered.connect(self.pet.show)
        self.tray.act_chat.triggered.connect(self._show_chat_window)
        self.tray.act_settings.triggered.connect(self._show_settings)
        self.tray.act_dashboard.triggered.connect(self._open_dashboard)
        self.tray.act_memory.triggered.connect(self._show_memory_popup)

        # --- PetWindow 右键菜单信号 ---
        self.pet.open_settings_requested.connect(self._show_settings)
        self.pet.retune_ms_requested.connect(self._do_retune_frames)
        self.pet.scale_changed.connect(self._on_pet_scale_changed)
        self.pet.crossfade_toggled.connect(self._on_crossfade_changed)
        self.pet.lock_first_idle_toggled.connect(
            self.pet.animator.set_lock_first_idle)
        self.pet.chat_requested.connect(self._show_chat_window)
        self.pet.reaction_requested.connect(
            lambda _: self.state.on_interact(feeling_gain=5))
        self.pet.chat_requested.connect(
            lambda: self.state.on_interact(likability_gain=2))
        self.pet.eat_requested.connect(self._on_eat_requested)
        self.pet.food_selected.connect(self._on_food_selected)
        self.pet.chat_input_sent.connect(self._on_quick_chat_sent)
        # --- 小游戏 / 每日签到 ---
        self.pet.game_requested.connect(self._on_game_requested)
        self.pet.checkin_requested.connect(self._on_checkin)
        self.pet.checkin_status_fn = lambda: self.state.has_checked_in_today()
        self._gomoku_window = None

        # --- 状态管理器信号 ---
        self.state_mgr.food_low.connect(self._on_food_low)
        self.state_mgr.reminder_fired.connect(self._on_reminder_triggered)

        # --- 智能中枢信号 ---
        self.brain.bubble_requested.connect(self.pet.show_bubble)
        self.brain.remark_ready.connect(self._on_proactive_remark)
        if hasattr(self.brain, "emotion_hint"):
            self.brain.emotion_hint.connect(self._on_proactive_emotion)

        # --- 退出保证存档 ---
        from app.core.qt_compat import QApplication
        QApplication.instance().aboutToQuit.connect(self._on_about_to_quit)

    # ============================================================
    #  状态 / 业务
    # ============================================================
    def _on_food_low(self) -> None:
        """食物过低时弹气泡提醒。"""
        self.pet.show_bubble("我饿了喵~ 想吃点东西！")

    def _on_reminder_triggered(self, text: str) -> None:
        """提醒触发回调。"""
        log.info("提醒触发：%s", text)
        self.pet.show_bubble(f"⏰ 提醒：{text}")
        anim = getattr(self.pet, "animator", None)
        if anim is not None and hasattr(anim, "trigger_scene"):
            anim.trigger_scene("reminder")

    def _on_eat_requested(self) -> None:
        """吃饭：涨饱食度 + 体力 + 心情。"""
        self.state_mgr.on_eat_requested()
        self.pet.show_bubble("🍚 吃饱啦~ 好满足！")

    def _on_food_selected(self, name: str) -> None:
        """右键投喂具体食物：按 foods.json 数值变化（主人手动投喂，不扣金币）。

        图片贴纸 / desc 气泡由 PetWindow 负责；这里只改状态。
        找不到食物库或物品时退回通用吃饭。
        """
        items = getattr(self.brain, "items", None)
        item = items.by_name(name) if items is not None else None
        if item is None:
            log.warning("投喂未找到食物：%s，退回通用吃饭", name)
            self.state_mgr.on_eat_requested()
            return
        from app.engine.works import apply_food
        if apply_food(self.state, item, free=True):
            # 投喂本身是陪伴：额外一点心情；好感已在 apply_food 内按每日上限处理
            self.state.on_interact(feeling_gain=2)
            log.info("手动投喂「%s」：%s", name, self.state.stats_summary())

    # ============================================================
    #  小游戏 / 每日签到
    # ============================================================
    def _on_game_requested(self, game_id: str) -> None:
        """右键「小游戏」入口。"""
        if game_id == "gomoku":
            self._show_gomoku()
        else:
            log.warning("未知小游戏：%s", game_id)

    def _show_gomoku(self) -> None:
        """打开五子棋窗口（重复打开复用已存在窗口）。"""
        from app.ui.gomoku_window import GomokuWindow
        gw = self._gomoku_window
        if gw is None or not gw.isVisible():
            gw = GomokuWindow()
            gw.game_finished.connect(self._on_gomoku_finished)
            gw.comment.connect(self._on_game_comment)
            gw.show()
            self._gomoku_window = gw
        else:
            gw.raise_()
            gw.activateWindow()
        gw.set_wallet(self.state.money, self.state.game_coin_remaining())

    def _on_gomoku_finished(self, result: str, difficulty: str) -> None:
        """一局结束：按难度/结果发放金币（受每日上限约束），桌宠做反应。"""
        from app.ui.gomoku_window import REWARDS
        gw = self._gomoku_window
        amount = 0
        if result in ("win", "lose", "draw"):
            amount = REWARDS.get(difficulty, REWARDS["normal"]).get(result, 0)
        granted = self.state.add_game_reward(amount) if amount else 0
        if gw is not None:
            gw.set_wallet(self.state.money, self.state.game_coin_remaining())
            gw.show_reward(granted, amount > 0 and granted <= 0)
        acts = {
            "win": ("tongue", "stretch", "swim"),
            "lose": ("cheek", "jump", "spin"),
            "draw": ("tongue", "stretch", "swim"),
            "giveup": ("stretch", "swim"),
        }
        # 结算台词由窗口 comment 信号朗读（_on_game_comment），这里只做动作反馈
        self._pet_react(acts.get(result, acts["draw"]))
        self.state.on_interact(feeling_gain=2)
        log.info("五子棋结束 result=%s difficulty=%s 金币 +%.0f",
                 result, difficulty, granted)

    def _on_game_comment(self, text: str) -> None:
        # 五子棋过程 / 结算解说：气泡 + TTS 朗读
        self._pet_speak(text)

    def _pet_speak(self, text: str, duration_ms: int = 4000) -> None:
        # 非聊天场景统一发言：显示气泡，TTS 开启时朗读（口型自动同步）
        self.pet.show_bubble(text, duration_ms=duration_ms)
        if getattr(self.cfg.character, "tts_enabled", False) and self.tts is not None:
            try:
                self.tts.speak(text)
            except Exception:
                log.warning("TTS 朗读失败：%s", text)

    def _on_checkin(self) -> None:
        """每日签到：每天一次 +100 金币。"""
        ok, reward = self.state.daily_checkin(reward=100.0)
        if ok:
            self._pet_speak(f"签到成功！金币 +{reward:.0f}，今天也要陪我玩哦～")
            self._pet_react(("jump", "spin", "cheek"))
            self.state.on_interact(feeling_gain=3)
            log.info("每日签到：金币 +%.0f", reward)
        else:
            self._pet_speak("今天已经签到过啦，明天再来吧～")

    def _pet_react(self, prefer) -> None:
        """播放一个一次性动作（motion 结束自动回 idle，不影响持久表情）。"""
        anim = getattr(self.pet, "animator", None)
        if anim is None or not hasattr(anim, "play_animation"):
            return
        name = None
        opts = []
        if hasattr(anim, "get_play_options"):
            try:
                opts = [n for n, _ in anim.get_play_options()]
            except Exception:  # noqa: BLE001
                opts = []
        for cand in prefer:
            if cand in opts:
                name = cand
                break
        if name is None:
            # sprite 兜底：这些动作两类渲染器都支持
            for cand in prefer:
                if cand in ("jump", "spin", "stretch", "swim"):
                    name = cand
                    break
        if name:
            try:
                anim.play_animation(name)
            except Exception as e:  # noqa: BLE001
                log.warning("桌宠动作播放失败：%s", e)

    def _on_proactive_remark(self, text: str) -> None:
        """主动发言回调。"""
        log.info("主动发言：%s", text)
        self.pet.show_bubble(text)
        if self.cfg.character.tts_enabled:
            self.tts.speak(text)
        self.state.on_interact(feeling_gain=1)

    def _on_chat_reply_ready(self, text: str, emotion, tts_enabled: bool) -> None:
        """聊天结束回调：触发聊天情绪场景 + 可选 TTS。"""
        log.info("chat reply ready: %r / %s tts=%s", text, emotion, tts_enabled)
        self._apply_chat_emotion(emotion)
        if tts_enabled:
            self.tts.speak(text)

    def _apply_chat_emotion(self, emotion) -> None:
        """聊天 / 主动搭话情绪 → 触发对应聊天情绪场景；未知情绪回退直接切表情。"""
        ev = str(getattr(emotion, "value", emotion)).lower()
        anim = getattr(self.pet, "animator", None)
        scene = self._EMOTION_SCENES.get(ev)
        if anim is None:
            return
        if scene is not None and hasattr(anim, "trigger_scene"):
            anim.trigger_scene(scene)
        else:
            try:
                anim.set_emotion(ev)
            except Exception as e:  # noqa: BLE001
                log.warning("切聊天表情失败：%s", e)

    def _on_proactive_emotion(self, emotion) -> None:
        """主动搭话携带的情绪：复用聊天情绪场景。"""
        self._apply_chat_emotion(emotion)

    # ---------- 环境场景（闲置 30 分钟 / 深夜） ----------
    def _setup_env_scenes(self) -> None:
        self._in_idle_lonely = False
        self._in_late_night = False
        self._env_scene_timer = QTimer(self)
        self._env_scene_timer.setInterval(30_000)  # 30 秒检查一次
        self._env_scene_timer.timeout.connect(self._check_env_scenes)
        self._env_scene_timer.start()

    def _check_env_scenes(self) -> None:
        anim = getattr(self.pet, "animator", None)
        if anim is None or not hasattr(anim, "trigger_scene"):
            return
        try:
            if getattr(anim, "is_sleeping", lambda: False)():
                return
            now = time.time()
            last = float(getattr(self.state, "_last_interact_ts", 0.0) or 0.0)
            if last <= 0:
                last = now
            # 许久未理（30 分钟）
            if now - last >= 1800:
                if not self._in_idle_lonely:
                    self._in_idle_lonely = True
                    anim.trigger_scene("idle_lonely")
            elif self._in_idle_lonely:
                self._in_idle_lonely = False
                anim.restore_scene_appearance()
            # 深夜时段（22:00–6:00）
            if is_night(now):
                if not self._in_late_night:
                    self._in_late_night = True
                    anim.trigger_scene("late_night")
            elif self._in_late_night:
                self._in_late_night = False
                anim.restore_scene_appearance()
        except Exception:  # noqa: BLE001
            log.exception("环境场景检查失败")

    def _on_streaming_chunk(self, text: str) -> None:
        """流式输出增量：同步显示到桌宠头顶气泡。"""
        self.pet.show_streaming_bubble(text)
        # 流式文本阶段不张嘴——此时 TTS 还没开始朗读，先动嘴会「对不上口型」。
        # TTS 关闭（纯气泡聊天）时才退化为按文字节奏张嘴。
        if not getattr(self.tts, "enabled", True):
            self._stream_talking = True
            self._update_talking()

    def _on_streaming_done(self) -> None:
        """流式输出结束：隐藏或延迟隐藏桌宠气泡。"""
        self._stream_talking = False
        self._update_talking()
        self.pet.stop_streaming_bubble()

    # ---------------- 口型同步（TTS 播放 / 流式气泡 任一进行中即张嘴） ----------------
    def _setup_lipsync(self) -> None:
        self._tts_talking = False
        self._stream_talking = False
        # TTS 播放钩子（引擎工作线程回调 → Qt 主线程）
        if hasattr(self.tts, "on_speak_start"):
            self.tts.on_speak_start = lambda: QTimer.singleShot(
                0, lambda: self._set_tts_talking(True))
            self.tts.on_speak_end = lambda: QTimer.singleShot(
                0, lambda: self._set_tts_talking(False))

    def _set_tts_talking(self, on: bool) -> None:
        self._tts_talking = bool(on)
        self._update_talking()

    def _update_talking(self) -> None:
        on = getattr(self, "_tts_talking", False) or \
            getattr(self, "_stream_talking", False)
        set_talking = getattr(self.pet.animator, "set_talking", None)
        if callable(set_talking):
            set_talking(bool(on))

    def _on_thinking_started(self) -> None:
        """模型开始思考：桌宠播放 think/think_2 动画。"""
        self.pet.animator.set_thinking()
        self.pet.show_bubble("🤔 思考中…", duration_ms=2000)

    def _on_thinking_stopped(self) -> None:
        """模型思考结束：回到待机。"""
        self.pet.animator.set_idle()

    # ============================================================
    #  视觉设置
    # ============================================================
    def _apply_visual_settings(self) -> None:
        """应用视觉设置（缩放、透明度、行为节拍）。"""
        sw = self.settings_window
        # scale：实时 resize 桌宠窗（sprite 重缩放帧图 / live2d 重 fit 模型）
        new_scale = sw.get_scale()
        if abs(new_scale - self.pet._scale) > 0.001:
            self.pet.apply_display_size(new_scale)
        # 透明度：70% - 100% → setWindowOpacity
        op = sw.get_opacity()
        self.pet.setWindowOpacity(op)
        # 行为：motion 节拍
        self.motion.apply_settings(
            idle_seconds=sw.get_idle_seconds(),
            walk_seconds=sw.get_walk_seconds(),
            decide_seconds=sw.get_decide_seconds(),
        )

    def _is_sprite(self) -> bool:
        """当前渲染器是否 sprite（帧图专属的设置只对它生效）。"""
        r = getattr(self.pet, "renderer", None)
        return r is None or r.get_renderer_type() == "sprite"

    def _apply_visual_settings_with_rescale(self) -> None:
        """_apply_visual_settings 的增强版：如果缩放变了就重新 prescale 图片。"""
        old_scale = self.pet._scale
        self._apply_visual_settings()
        if abs(self.pet._scale - old_scale) > 0.001:
            self._rescale_sprite_pixmaps()

    def _rescale_sprite_pixmaps(self) -> None:
        """缩放窗口尺寸变化后，重新预缩放 atlas 中所有帧的 pixmap。"""
        if not self._is_sprite():
            return  # live2d 没有 atlas，窗口尺寸由 PetWindow 自己 resize
        from app.ui.pet_window import _scale_pixmap_keep_alpha, _clear_pixmap_cache
        _clear_pixmap_cache()
        atlas = self.pet.atlas
        target_size = self.pet._window_size

        def _rescale_anim(a):
            if a is None:
                return
            for f in a.frames:
                if f.original is not None:
                    f.pixmap = _scale_pixmap_keep_alpha(f.original, target_size)
                else:
                    f.pixmap = _scale_pixmap_keep_alpha(f.pixmap, target_size)

        # idle / walk / emotion / sleep / 动作
        for a in atlas.idle: _rescale_anim(a)
        for a in atlas.walk_left: _rescale_anim(a)
        for a in atlas.walk_right: _rescale_anim(a)
        for a in atlas.crawl_left: _rescale_anim(a)
        for a in atlas.crawl_right: _rescale_anim(a)
        for a in (atlas.emotion_happy or []): _rescale_anim(a)
        for a in (atlas.emotion_sad or []): _rescale_anim(a)
        for a in (atlas.emotion_angry or []): _rescale_anim(a)
        for a in (atlas.emotion_shy or []): _rescale_anim(a)
        for a in (atlas.emotion_think or []): _rescale_anim(a)
        for a in atlas.sleep: _rescale_anim(a)
        for a in (atlas.stretch or []): _rescale_anim(a)
        for a in (atlas.touch_head or []): _rescale_anim(a)
        for a in (atlas.touch_body or []): _rescale_anim(a)
        _rescale_anim(atlas.fallback)
        # 当前帧立刻重绘：重启 frame timer（下一拍用新 pixmap）
        self.pet._start_frame_timer()

    def _on_pet_scale_changed(self, new_scale: float) -> None:
        """右键菜单改了缩放 → 实时 resize pet 窗 + 重新缩放显示。"""
        if abs(new_scale - self.pet._scale) < 0.001:
            return
        self.pet.apply_display_size(new_scale)
        self._rescale_sprite_pixmaps()
        # 同步 settings_window 滑块
        self.settings_window.scale_slider.blockSignals(True)
        self.settings_window.scale_slider.setValue(int(new_scale * 100))
        self.settings_window.scale_slider.blockSignals(False)

    def _on_crossfade_changed(self, enabled: bool, ms: int) -> None:
        """淡入淡出设置变化。"""
        if not self._is_sprite():
            return  # sprite 专属（帧切换淡入淡出）
        self.pet.player.set_crossfade_ms(ms if enabled else 0)

    def _override_frame_ms_now(self, ms: int) -> None:
        """立刻改内存里所有帧的 duration_ms，不动磁盘。"""
        if not self._is_sprite():
            return  # sprite 专属
        self.pet.atlas.override_frame_duration_ms(ms)
        self.pet._start_frame_timer()
        # 同步 settings_window UI
        try:
            self.settings_window.set_frame_ms_ui(ms)
        except Exception:  # noqa: BLE001
            pass
        fps = 1000.0 / ms if ms > 0 else 0
        self.settings_window._set_status(
            f"🎯 帧时长已切换：{ms} ms / 帧（{fps:.1f} fps）")

    def _on_fps_monitor_toggled(self, on: bool) -> None:
        """FPS 气泡调试开关：两边保持一致。"""
        self.pet._fps_enabled = bool(on)
        if on:
            self.pet._fps_count = 0
            self.pet._fps_window_start = 0.0
            self.pet.show_bubble("📊 FPS 监测开启", duration_ms=2000)
        else:
            self.pet.show_bubble("📊 FPS 监测关闭", duration_ms=1500)
        # 同步 settings_window 复选框
        try:
            self.settings_window.set_fps_monitor(on)
        except Exception:  # noqa: BLE001
            pass

    def _reset_to_default_settings(self) -> None:
        """重置所有设置为默认值。"""
        self.settings_window._load_defaults()
        self._apply_visual_settings_with_rescale()
        self.pet.animator.set_lock_first_idle(False)
        if self._is_sprite():
            self.pet.player.set_crossfade_ms(0)
            self._override_frame_ms_now(50)
        self.pet._fps_enabled = False
        self.settings_window._set_status("已重置默认值")

    # ============================================================
    #  帧持久化
    # ============================================================
    def _do_retune_frames(self, ms: int) -> None:
        """用户点了「应用帧时长」按钮 —— 重命名所有 PNG + 重新加载 atlas。"""
        if not self._is_sprite():
            self.settings_window._set_status("帧时长仅 sprite 模式可用（Live2D 无帧图）")
            return
        import subprocess
        from pathlib import Path as _P
        try:
            sprite_dir = self.pet.atlas.sprite_dir
            cmd = ["python", str(_P(__file__).resolve().parent.parent
                                 / "tools" / "_legacy" / "rename_to_vpet.py"),
                   "--retune-ms", str(ms)]
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=60)
            log.info("retune 退出码 %d, stdout=%s stderr=%s",
                     result.returncode, result.stdout[:300], result.stderr[:300])
            # 走 SpriteRenderer 自带的 reload_atlas（保留同一个 renderer/player/label 实例，
            # 只换内部 atlas 引用，避免新旧 atlas/animator 双实例不同步的 bug）
            self.pet.reload_atlas(fallback_image=self.cfg.sprite.fallback)
            # 统一覆盖所有帧的 duration_ms
            self.pet.atlas.override_frame_duration_ms(ms)
            # 让新 duration 立即生效
            self.pet._start_frame_timer()
            self.settings_window._set_status(
                f"已 retune 到 {ms} ms（{1000/ms:.1f} fps），atlas 已重新加载")
        except Exception as e:  # noqa: BLE001
            log.error("retune 失败：%s", e)
            self.settings_window._set_status(f"retune 失败：{e}")

    # ============================================================
    #  聊天窗口
    # ============================================================
    def _show_chat_window(self) -> None:
        """打开聊天窗口（托盘菜单或右键菜单触发）。重复打开复用已存在的窗口。"""
        cw = self._chat_window
        if cw is None or not cw.isVisible():
            # 创建 trace recorder（聊天每次会写一条 run 到 SQLite，Dashboard 显示）
            from app.brain.trace import TraceRecorder
            from app.brain.langchain_agent import LangChainAgentConfig
            trace = TraceRecorder(self.root / "data" / "traces.db")
            cw = ChatWindow(
                self.cfg.llm, self.cfg.character,
                self.cfg.sprite.directory,
                asr_enabled=self.cfg.asr.enabled,
                asr_model=self.cfg.asr.model_size,
                asr_language=self.cfg.asr.language,
                registry=self.brain.tool_registry,
                context_provider=self.brain.chat_context,
                trace_recorder=trace,
                backend=self.cfg.brain.backend,
                langchain_cfg=LangChainAgentConfig(
                    enable_checkpointer=self.cfg.brain.langchain.enable_checkpointer,
                    checkpoint_db=str(self.root / self.cfg.brain.langchain.checkpoint_db),
                    max_iterations=self.cfg.brain.langchain.max_iterations,
                    return_intermediate_steps=self.cfg.brain.langchain.return_intermediate_steps,
                ),
                tts=self.tts,
                memory_store=self.brain.memory,
            )
            cw.reply_ready.connect(self._on_chat_reply_ready)
            cw.streaming_chunk.connect(self._on_streaming_chunk)
            cw.streaming_done.connect(self._on_streaming_done)
            cw.thinking_started.connect(self._on_thinking_started)
            cw.thinking_stopped.connect(self._on_thinking_stopped)
            cw.show()
            self._chat_window = cw
        else:
            cw.raise_()
            cw.activateWindow()

    def _on_quick_chat_sent(self, text: str) -> None:
        """快捷输入框发送消息：打开聊天窗并发送。"""
        if not text.strip():
            return
        self._show_chat_window()
        cw = self._chat_window
        if cw is not None:
            cw._quick_send(text)

    # ============================================================
    #  设置窗口
    # ============================================================
    def _show_settings(self) -> None:
        """打开设置窗口。"""
        sw = self.settings_window
        if sw.windowState() & Qt.WindowState.WindowMinimized:
            sw.setWindowState(sw.windowState() & ~Qt.WindowState.WindowMinimized)
        sw.show()
        sw.raise_()
        sw.activateWindow()

    def _open_dashboard(self) -> None:
        """打开 Web Dashboard：先看 App 是否暴露了 open_dashboard，否则自启。"""
        app = self._find_app()
        if app is not None and hasattr(app, "open_dashboard"):
            app.open_dashboard()
        else:
            # 退化：直接启子进程
            from app.main import start_dashboard_subprocess, wait_dashboard_ready, open_in_browser
            import time
            from pathlib import Path
            root = Path.cwd()
            pid = start_dashboard_subprocess(root, port=8765)
            if pid is None:
                self.pet.show_bubble("⚠️ Dashboard 启动失败", duration_ms=3000)
                return
            if wait_dashboard_ready(port=8765, timeout=8.0):
                self.pet.show_bubble("📊 Dashboard 已打开", duration_ms=2000)
                open_in_browser("http://127.0.0.1:8765")
            else:
                self.pet.show_bubble("⚠️ Dashboard 启动超时", duration_ms=3000)

    def _find_app(self):
        """找到 App 实例（通过 Qt 顶层窗口反查不靠谱，用 App 注入更稳）。"""
        # App 把自己挂到了 QApplication 的静态属性上（启动时设置）
        from app.core.qt_compat import QApplication
        qa = QApplication.instance()
        return getattr(qa, "_desktop_pet_app", None) if qa else None

    def _show_memory_popup(self) -> None:
        """打开长期记忆管理面板（复用聊天窗的「记忆」面板）。"""
        self._show_chat_window()
        cw = self._chat_window
        if cw is not None:
            cw._open_memory()

    # ============================================================
    #  模型配置
    # ============================================================
    def _on_model_config_changed(self, cfg: dict) -> None:
        """用户修改了模型配置，更新 cfg.llm 并重建 LLMClient。"""
        self.cfg.llm.base_url = cfg["base_url"]
        self.cfg.llm.api_key = cfg["api_key"]
        self.cfg.llm.model = cfg["model"]
        self.cfg.llm.temperature = cfg["temperature"]
        self.cfg.llm.max_tokens = cfg["max_tokens"]
        self.cfg.llm.timeout = cfg["timeout"]
        self.cfg.llm.stream = cfg["stream"]
        log.info("模型配置已更新：%s @ %s", cfg["model"], cfg["base_url"])
        # 通知 brain 重建 proactive 的 LLMClient
        if self.brain.proactive is not None:
            self.brain.proactive.update_llm_config(self.cfg.llm)

    def _on_voice_changed(self, voice: str) -> None:
        """用户修改了 TTS 语音，更新 TTS 引擎。"""
        if voice:
            self.cfg.character.tts_voice = voice
            self.tts.voice = voice
            log.info("TTS 语音已更新：%s", voice)
        else:
            self.cfg.character.tts_enabled = False
            self.tts.set_enabled(False)
            log.info("TTS 已关闭")

    # ============================================================
    #  Live2D 专属（设置窗口「Live2D」Tab）
    # ============================================================
    def _live2d_renderer(self):
        r = getattr(self.pet, "renderer", None)
        return r if r is not None and hasattr(r, "activate_menu_item") else None

    def _on_live2d_item_activated(self, group_id: str, item_id: str) -> None:
        """Live2D Tab：应用一个分类条目（特殊/发型/配件/手势/表情）。"""
        r = self._live2d_renderer()
        if r is not None:
            r.activate_menu_item(group_id, item_id)
            self.settings_window._set_status(f"Live2D 外观已切换：{item_id}")

    def _on_live2d_reset(self) -> None:
        """Live2D Tab：复位全部外观。"""
        r = self._live2d_renderer()
        if r is not None:
            r.reset_all_appearance()
            self.settings_window._set_status("Live2D 外观已全部复位")

    def _on_random_exp_changed(self, enabled: bool) -> None:
        """Live2D Tab：挂机随机表情开关。"""
        r = self._live2d_renderer()
        if r is not None:
            r.set_random_expressions(bool(enabled))
            self.settings_window.settings_store.set(
                "random_exp_enabled", bool(enabled))
            log.info("挂机随机表情：%s", "开" if enabled else "关")

    def _on_random_sticker_changed(self, enabled: bool) -> None:
        """Live2D Tab：随机表情包贴纸开关。"""
        fn = getattr(self.pet, "set_random_stickers", None)
        if callable(fn):
            fn(bool(enabled))
            self.settings_window.settings_store.set(
                "random_sticker_enabled", bool(enabled))
            log.info("随机表情包贴纸：%s", "开" if enabled else "关")

    def _on_sticker_options_changed(self, opts: dict) -> None:
        """Live2D Tab：贴纸参数（大小/角度/间隔/时长），即时生效并持久化。"""
        fn = getattr(self.pet, "set_sticker_options", None)
        if callable(fn):
            fn(**opts)
            store = self.settings_window.settings_store
            for key, skey in (("size", "sticker_size"), ("rotation", "sticker_rotation"),
                              ("min_s", "sticker_min_s"), ("max_s", "sticker_max_s"),
                              ("duration_s", "sticker_duration_s")):
                if key in opts:
                    store.set(skey, int(opts[key]))

    def _on_random_interval_changed(self, lo: int, hi: int) -> None:
        """Live2D Tab：挂机随机间隔。"""
        r = self._live2d_renderer()
        if r is not None and hasattr(r, "set_random_interval"):
            r.set_random_interval(int(lo), int(hi))
            store = self.settings_window.settings_store
            store.set("random_min_s", int(lo))
            store.set("random_max_s", int(hi))

    def _on_max_fps_changed(self, fps: int) -> None:
        """Live2D Tab：渲染帧率上限。"""
        r = self._live2d_renderer()
        if r is not None and hasattr(r, "set_max_fps"):
            r.set_max_fps(int(fps))
            self.settings_window.settings_store.set("max_fps", int(fps))

    # ---------------- 全量面板化（TTS / 主动关心 / 渲染器 / 水印 / 角色） ----------------
    def _on_tts_config_changed(self, cfgd: dict) -> None:
        """TTS 引擎与参数：持久化 + 后台线程重建引擎（热切换）。"""
        store = self.settings_window.settings_store
        keymap = {"tts_enabled": "tts_enabled", "engine": "tts_engine",
                  "minimax_voice_id": "minimax_voice_id",
                  "gptsovits_url": "gptsovits_url",
                  "ref_audio": "gptsovits_ref_audio",
                  "prompt_text": "gptsovits_prompt_text"}
        for k, skey in keymap.items():
            if k in cfgd:
                store.set(skey, cfgd[k])
        c = self.cfg.character
        c.tts_enabled = bool(cfgd.get("tts_enabled", c.tts_enabled))
        c.tts_engine = str(cfgd.get("engine", c.tts_engine))
        c.minimax_voice_id = str(cfgd.get("minimax_voice_id", c.minimax_voice_id))
        c.gptsovits_url = str(cfgd.get("gptsovits_url", getattr(c, "gptsovits_url", "")))
        c.gptsovits_ref_audio = str(cfgd.get("ref_audio", getattr(c, "gptsovits_ref_audio", "")))
        c.gptsovits_prompt_text = str(cfgd.get("prompt_text", getattr(c, "gptsovits_prompt_text", "")))
        import threading
        threading.Thread(target=self._rebuild_tts_blocking, daemon=True,
                         name="tts-rebuild").start()
        self.settings_window._set_status("🔊 语音引擎配置已保存，正在切换…")

    def _rebuild_tts_blocking(self) -> None:
        """按 cfg 重建 TTS 引擎（后台线程；minimax 首次克隆可能耗时）。"""
        try:
            from app.main import _build_tts
            from pathlib import Path as _P
            root = _P(__file__).resolve().parent.parent
            new_tts = _build_tts(self.cfg, root)
            old_tts = self.tts
            self.tts = new_tts
            self._setup_lipsync()   # 重新挂口型同步钩子
            log.info("TTS 引擎已热切换：%s", type(new_tts).__name__)
        except Exception:  # noqa: BLE001
            log.exception("TTS 引擎重建失败，保留原引擎")

    def _on_proactive_changed(self, cfgd: dict) -> None:
        """控制 Tab：主动关心开关与间隔。"""
        store = self.settings_window.settings_store
        store.set("proactive_enabled", bool(cfgd.get("enabled", True)))
        store.set("proactive_min_minutes", int(cfgd.get("min_minutes", 25)))
        store.set("proactive_max_minutes", int(cfgd.get("max_minutes", 45)))
        p = getattr(self.brain, "proactive", None)
        if p is not None:
            if hasattr(p, "apply_interval"):
                p.apply_interval(int(cfgd.get("min_minutes", 25)),
                                 int(cfgd.get("max_minutes", 45)))
            else:
                p.min_minutes = max(1, int(cfgd.get("min_minutes", 25)))
                p.max_minutes = max(p.min_minutes + 1, int(cfgd.get("max_minutes", 45)))
                if hasattr(p, "_reschedule_decide_timer"):
                    try:
                        p._reschedule_decide_timer()
                    except Exception:  # noqa: BLE001
                        pass
            enabled = bool(cfgd.get("enabled", True))
            if enabled and hasattr(p, "start"):
                p.start()
            elif not enabled and hasattr(p, "stop"):
                p.stop()
        else:
            self.settings_window._set_status("主动关心将在重启后生效（当前未启用）")
        log.info("主动关心：%s", cfgd)

    def _on_renderer_changed(self, renderer: str) -> None:
        """视觉 Tab：渲染器切换（持久化，重启生效）。"""
        self.settings_window.settings_store.set("renderer", str(renderer))
        self.settings_window._set_status(f"渲染器已设为 {renderer}，重启桌宠后生效")

    def _on_hide_watermark_changed(self, on: bool) -> None:
        """Live2D Tab：隐藏水印开关（持久化，重启生效）。"""
        self.settings_window.settings_store.set("hide_watermark", bool(on))
        self.settings_window._set_status("水印设置已保存，重启桌宠后生效")

    def _on_character_changed(self, cfgd: dict) -> None:
        """模型配置 Tab：角色名 / 人设（即时更新 cfg + 持久化）。"""
        if cfgd.get("name"):
            self.cfg.character.name = str(cfgd["name"])
        if cfgd.get("persona"):
            self.cfg.character.persona = str(cfgd["persona"])
        store = self.settings_window.settings_store
        if cfgd.get("name"):
            store.set("character_name", str(cfgd["name"]))
        if cfgd.get("persona"):
            store.set("persona", str(cfgd["persona"]))
        p = getattr(self.brain, "proactive", None)
        if p is not None and cfgd.get("persona"):
            p.persona = str(cfgd["persona"])

    def _on_always_on_top_changed(self, enabled: bool) -> None:
        """视觉 Tab：窗口置顶开关（立即生效并持久化）。"""
        fn = getattr(self.pet, "set_always_on_top", None)
        if callable(fn):
            fn(bool(enabled))
        self.settings_window.settings_store.set("always_on_top", bool(enabled))
        log.info("窗口置顶：%s", "开" if enabled else "关")

    def _on_save_settings(self) -> None:
        """「保存设置」按钮：把当前配置快照写入 settings.json（重启后自动应用）。"""
        sw = self.settings_window
        store = sw.settings_store
        try:
            store.set("scale", sw.get_scale())
            store.set("opacity", sw.get_opacity())
            store.set("always_on_top", sw.is_always_on_top())
            r = self._live2d_renderer()
            if r is not None:
                store.set("random_exp_enabled",
                          bool(r.is_random_expressions_enabled()))
                if hasattr(r, "profile"):
                    store.set("random_min_s", int(r.profile.random_min_s))
                    store.set("random_max_s", int(r.profile.random_max_s))
                store.set("max_fps", int(getattr(r, "max_fps", 30)))
            if hasattr(self.pet, "is_random_stickers_enabled"):
                store.set("random_sticker_enabled",
                          bool(self.pet.is_random_stickers_enabled()))
            if hasattr(self.pet, "get_sticker_options"):
                opts = self.pet.get_sticker_options()
                for key, skey in (("size", "sticker_size"),
                                  ("rotation", "sticker_rotation"),
                                  ("min_s", "sticker_min_s"),
                                  ("max_s", "sticker_max_s"),
                                  ("duration_s", "sticker_duration_s")):
                    store.set(skey, int(opts[key]))
            sw._set_status("✅ 设置已保存，重启后自动生效")
            log.info("设置已手动保存")
        except Exception:  # noqa: BLE001
            log.exception("保存设置失败")
            sw._set_status("❌ 保存失败，详见日志")

    def _apply_stored_window_settings(self) -> None:
        """启动时应用 settings.json 里保存的窗口类设置（保存按钮/自动保存写入）。"""
        store = self.settings_window.settings_store
        try:
            aot = store.get("always_on_top", None)
            if aot is not None:
                fn = getattr(self.pet, "set_always_on_top", None)
                if callable(fn):
                    fn(bool(aot))
            scale = store.get("scale", None)
            if scale is not None:
                self.pet.apply_display_size(float(scale))
            opacity = store.get("opacity", None)
            if opacity is not None:
                self.pet.setWindowOpacity(float(opacity))
            r = self._live2d_renderer()
            if r is not None:
                re_en = store.get("random_exp_enabled", None)
                if re_en is not None:
                    r.set_random_expressions(bool(re_en))
            rs = store.get("random_sticker_enabled", None)
            if rs is not None and hasattr(self.pet, "set_random_stickers"):
                self.pet.set_random_stickers(bool(rs))
            # 贴纸参数 / 随机间隔 / 渲染帧率
            kw = {}
            for key, skey in (("size", "sticker_size"), ("rotation", "sticker_rotation"),
                              ("min_s", "sticker_min_s"), ("max_s", "sticker_max_s"),
                              ("duration_s", "sticker_duration_s")):
                v = store.get(skey, None)
                if v is not None:
                    kw[key] = int(v)
            if kw and hasattr(self.pet, "set_sticker_options"):
                self.pet.set_sticker_options(**kw)
            r = self._live2d_renderer()
            if r is not None:
                rmin = store.get("random_min_s", None)
                rmax = store.get("random_max_s", None)
                if rmin is not None and rmax is not None \
                        and hasattr(r, "set_random_interval"):
                    r.set_random_interval(int(rmin), int(rmax))
                mf = store.get("max_fps", None)
                if mf is not None and hasattr(r, "set_max_fps"):
                    r.set_max_fps(int(mf))
            # 主动关心（间隔与开关）
            pe = store.get("proactive_enabled", None)
            if pe is not None and getattr(self.brain, "proactive", None) is not None:
                if hasattr(self.brain.proactive, "apply_interval"):
                    self.brain.proactive.apply_interval(
                        int(store.get("proactive_min_minutes", 25)),
                        int(store.get("proactive_max_minutes", 45)))
                if not bool(pe) and hasattr(self.brain.proactive, "stop"):
                    self.brain.proactive.stop()
            # 角色名 / 人设
            cn = store.get("character_name", None)
            if cn:
                self.cfg.character.name = str(cn)
            ps = store.get("persona", None)
            if ps:
                self.cfg.character.persona = str(ps)
        except Exception:  # noqa: BLE001
            log.exception("启动应用已保存设置失败")

    def _init_model_config_ui(self) -> None:
        """启动时把 cfg.llm 的值加载到设置面板 UI。"""
        self.settings_window.set_model_config_ui({
            "base_url": self.cfg.llm.base_url,
            "api_key": self.cfg.llm.api_key,
            "model": self.cfg.llm.model,
            "temperature": self.cfg.llm.temperature,
            "max_tokens": self.cfg.llm.max_tokens,
            "timeout": self.cfg.llm.timeout,
            "stream": self.cfg.llm.stream,
            "voice": self.cfg.character.tts_voice,
            "tts_enabled": self.cfg.character.tts_enabled,
        })

    # ============================================================
    #  退出
    # ============================================================
    def _on_about_to_quit(self) -> None:
        """退出前最终存档。"""
        self.state_mgr.on_about_to_quit()

    def _quit(self) -> None:
        """退出应用。"""
        from app.core.qt_compat import QApplication
        QApplication.instance().quit()
