"""UI 控制器：封装所有 UI 窗口管理和信号连接。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from app.core.qt_compat import QObject, QTimer, Qt, Signal
from .chat_window import ChatWindow
from app.ui.pet_window import PetWindow
from app.engine.state_manager import StateManager
from app.engine.state import is_night
from app.core.tray import TrayController

log = logging.getLogger(__name__)


def _is_under(path: Path, base: Path) -> bool:
    """path 是不是 base 下面的（防目录穿越）。"""
    try:
        Path(path).resolve().relative_to(Path(base).resolve())
        return True
    except (ValueError, OSError):
        return False


def _attr(obj, name: str, default=None):
    """取实例属性。

    PySide6 的 QObject 在 ``__init__`` 没被调用时，普通 getattr 会抛
    RuntimeError（而不是 AttributeError），拿不到默认值。
    """
    try:
        return object.__getattribute__(obj, name)
    except AttributeError:
        return default


class UIController(QObject):
    """封装所有 UI 窗口管理和信号连接。"""

    # 口型同步信号桥（TTS 工作线程 emit，自动 queued 到主线程）
    _lipSyncChanged = Signal(bool)

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
        # 开发者模式开关：dashboard / chat_window 可查 —— 默认关闭，
        # 面板的 _set_developer_mode emit developer_mode_changed 会在 _on_developer_mode_changed 同步过来
        self.developer_mode = False
        self._chat_window = None
        # retune（帧时长重命名）子进程 worker —— 保留引用防 GC
        self._retune_worker = None
        # 端口探测 worker（等 demo / trace 子进程就绪）—— 保留引用防 GC
        self._port_waiter = None
        self._last_food_warn = 0.0

        # 设置中枢：读写 settings.json + 把改动应用到运行中的桌宠。
        # 控制台（浏览器）改设置走它 —— 主程序开本地控制端口接请求。
        from app.core.pet_settings import SettingsService
        from app.core.settings_store import SettingsStore as _SS
        # 启动时用 settings.json 里的 activitywatch 值覆盖 cfg
        try:
            from app.core.settings_store import SettingsStore
            _store_pre = SettingsStore()
            for _key, _attr, _cast in (
                    ("activitywatch_enabled", "enabled", bool),
                    ("activitywatch_server_url", "server_url", str),
                    ("activitywatch_lookback", "proactive_lookback_minutes", int),
                    ("activitywatch_max_events", "proactive_max_events", int)):
                if _store_pre.get(_key) is not None:
                    setattr(cfg.activitywatch, _attr,
                            _cast(_store_pre.get(_key)))
        except Exception:
            log.exception("apply stored aw cfg")
        self.settings_store = _SS()
        self.settings = SettingsService(self.settings_store, cfg=self.cfg,
                                        project_root=root)
        self.settings.apply_hook = self._apply_settings_changed
        self.pet.food_money_fn = lambda: self.state.money
        self.pet.pet_name_fn = lambda: self.cfg.character.name

        # 真实升级(EXP 累积)回调:气泡祝贺 + 解锁提示
        # dev mode 拖 slider 走的路径不触发这里,只有真实升级才祝贺
        try:
            prev = getattr(self.state, "on_level_up", None)
            def _level_up_bridge(new_lv: int) -> None:
                if prev is not None:
                    try:
                        prev(new_lv)
                    except Exception:  # noqa: BLE001
                        pass
                self._on_pet_level_up(new_lv)
            self.state.on_level_up = _level_up_bridge
        except Exception:  # noqa: BLE001
            log.debug("接 on_level_up 失败(非致命)", exc_info=True)

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

        self._wire_all_signals()

        self._init_model_config_ui()
        self._setup_lipsync()
        # 启动时应用 settings.json 里保存的窗口类设置（缩放/透明度/置顶/随机开关）
        self._apply_stored_window_settings()
        # 控制服务：控制台（浏览器）改设置走它
        self._start_control_server()
        # 闲置 30 分钟 / 深夜时段的 Live2D 场景
        self._setup_env_scenes()
        # 语音唤醒在 chat_window 创建后再启动（见 _show_chat_window）

    #  控制服务
    def _start_control_server(self) -> None:
        """开本地控制端口：控制台改设置 / 驱动桌宠动作都走它。

        HTTP 线程不能直接碰 Qt 控件（会崩）—— 所有 handler 都经
        ``_main_call`` 投递回主线程执行。
        """
        try:
            from app.core.pet_control import PetControlServer
            from app.core.main_thread import MainThreadInvoker
            self._main_invoker = MainThreadInvoker(self)
            self.control_server = PetControlServer(
                get_settings=lambda: self._main_call(self.settings.values),
                get_schema=lambda: self._main_call(self.settings.schema),
                update_settings=self._update_settings,
                get_appearance=lambda: self._main_call(self._control_appearance),
                list_avatars=lambda: self._main_call(self._control_avatars),
                do_action=lambda action, params: self._main_call(
                    self._control_action, action, params),
            )
            self.control_server.start()
        except Exception:  # noqa: BLE001
            log.exception("桌宠控制服务启动失败")

    def _update_settings(self, changes: dict) -> dict:
        """改设置：落盘在当前线程，应用回主线程（碰 Qt 的活儿）。"""
        prepared = self.settings.prepare(changes)
        if not prepared["accepted"]:
            return {"ok": prepared["ok"], "applied": [],
                    "rejected": prepared["rejected"],
                    "skipped": prepared["skipped"], "restart": []}
        try:
            self._main_call(self.settings.apply, prepared["accepted"])
        except Exception as e:  # noqa: BLE001
            log.exception("应用设置失败")
            return {"ok": False, "applied": [], "rejected": prepared["rejected"],
                    "skipped": prepared["skipped"], "restart": [],
                    "error": str(e)}
        return {"ok": True,
                "applied": sorted(prepared["accepted"].keys()),
                "rejected": prepared["rejected"],
                "skipped": prepared["skipped"],
                "restart": prepared["restart"]}

    def _main_call(self, fn, *args, **kwargs):
        """把调用丢回主线程执行（Qt 控件只能主线程碰）。"""
        invoker = getattr(self, "_main_invoker", None)
        if invoker is None:
            return fn(*args, **kwargs)
        return invoker.call(lambda: fn(*args, **kwargs))

    @property
    def control_url(self) -> str:
        """控制服务地址（控制台连它）。"""
        srv = getattr(self, "control_server", None)
        return srv.url() if srv is not None else ""

    #  信号连接
    def _wire_all_signals(self) -> None:
        self.pet.quit_requested.connect(self._quit)
        self.tray.act_quit.triggered.connect(self._quit)
        self.tray.act_hide.triggered.connect(self.pet.hide)
        self.tray.act_show.triggered.connect(self.pet.show)
        self.tray.act_chat.triggered.connect(self._show_chat_window)
        self.tray.act_console.triggered.connect(self._open_dashboard)
        self.tray.act_demo.triggered.connect(self._open_demo)
        self.tray.act_trace.triggered.connect(self._open_trace)
        self.tray.act_memory.triggered.connect(self._show_memory_popup)

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
        self.pet.game_requested.connect(self._on_game_requested)
        self.pet.checkin_requested.connect(self._on_checkin)
        self.pet.checkin_status_fn = lambda: self.state.has_checked_in_today()
        self._gomoku_window = None
        self._werewolf_window = None
        self._xiangqi_window = None

        self.state_mgr.food_low.connect(self._on_food_low)
        self.state_mgr.reminder_fired.connect(self._on_reminder_triggered)

        # 闹钟 / 倒计时到点的统一出口：置顶窗 + 循环提示音 + TTS 播报
        from app.ui.alarm_window import AlarmPresenter
        self.alarm = AlarmPresenter(speak_fn=self._speak_alarm)

        from app.core.qt_compat import QApplication
        at_quit = QApplication.instance()
        if at_quit is not None:
            at_quit.aboutToQuit.connect(self._on_about_to_quit)

        # countdown 到点走的是 brain 的通道（后台线程触发），和提醒汇到一处
        if hasattr(self.brain, "alarm_fired"):
            self.brain.alarm_fired.connect(self._on_alarm_fired)

        self.brain.bubble_requested.connect(self.pet.show_bubble)
        # clear_bubble 工具需要一条真能把气泡收掉的通道
        hide = getattr(self.pet, "hide_bubble", None) or \
            getattr(self.pet, "_hide_bubble", None)
        if callable(hide) and hasattr(self.brain, "bubble_cleared"):
            self.brain.bubble_cleared.connect(hide)
        self.brain.remark_ready.connect(self._on_proactive_remark)
        if hasattr(self.brain, "emotion_hint"):
            self.brain.emotion_hint.connect(self._on_proactive_emotion)

    #  状态 / 业务
    def _on_food_low(self) -> None:
        """食物过低时弹气泡提醒。"""
        self.pet.show_bubble("我饿了喵~ 想吃点东西！")

    def _on_reminder_triggered(self, text: str) -> None:
        """提醒到点（add_reminder）。走闹钟通道：置顶窗 + 提示音 + 播报。"""
        log.info("提醒触发：%s", text)
        self._fire_alarm(text, kind="⏰ 闹钟到点")

    def _on_alarm_fired(self, text: str, kind: str = "⏰ 倒计时结束") -> None:
        """countdown 到点（后台线程经 brain 信号转上来）。"""
        log.info("倒计时到点：%s", text)
        self._fire_alarm(text, kind=kind)

    def _on_developer_mode_changed(self, enabled: bool) -> None:
        """开发者模式开关变化。"""
        log.info("developer mode = %s", enabled)
        self.developer_mode = bool(enabled)

    def _on_dev_state_pushed(self, snapshot: dict) -> None:
        """滑块改动发来 → 改 PetState + 同步 RiskyToolConfirm。

        流程:
          1. snapshot 每项 (attr, value) setattr 到 self.state
          2. self.state.normalize() 把数值钳到合法范围
          3. 同步给 LangChainAgent 的 confirm_ctrl
          4. 回灌 settings UI（progress bar + lbl_summary）
          5. 触发 state.on_change 让 pet_window / live2d 表情同步
          6. 立即 write_sync（不走 5s 防抖，dev mode 下主程序期望滑块每次拖动立刻落盘）
        """
        try:
            for attr, val in (snapshot or {}).items():
                if not hasattr(self.state, attr):
                    log.warning(
                        "dev_state_pushed: PetState 没字段 %s, 跳过", attr)
                    continue
                setattr(self.state, attr, float(val))
            self.state.normalize()
            # 同步给 LangChainAgent（策略层 + 反馈层 + confirm_ctrl）
            cw = self._chat_window
            if cw is not None:
                lc = getattr(cw, "_lc_agent", None)
                if lc is not None and hasattr(lc, "set_companion_state"):
                    try:
                        lc.set_companion_state(self.state)
                    except Exception as e:  # noqa: BLE001
                        log.warning(
                            "sync to LangChainAgent failed: %s", e)
                # 同步给 LangChainAgent 的 confirm_ctrl
                ctrl = getattr(lc, "_confirm_ctrl", None) if lc else None
                if ctrl is not None:
                    ctrl.set_companion_state(self.state)
            # 触发 on_change 让 pet/Live2D 同步（走 _combined → mark_dirty）
            try:
                if self.state.on_change:
                    self.state.on_change()
            except Exception:  # noqa: BLE001
                pass
            # 立即写盘（不走 5s 防抖）
            try:
                if self.state_mgr is not None and self.state_mgr.save_store is not None:
                    self.state_mgr.save_store.write_sync(self.state)
            except Exception as e:  # noqa: BLE001
                log.warning("dev_state_pushed 立即写盘失败: %s", e)
            # 等级变化 → 清掉工具可见性缓存,下次 LLM 调用走新等级
            if "level" in (snapshot or {}):
                self._on_tool_level_changed()
            log.debug("dev_state_pushed applied: %s", snapshot)
        except Exception as e:  # noqa: BLE001
            log.warning("_on_dev_state_pushed 失败: %s", e)

    def _on_tool_level_changed(self) -> None:
        """等级变化后刷新工具可见性(LLM 每次调用动态读 level_provider,
        这里只做日志 + 解锁对话框若开着实时刷新)。"""
        lvl = int(getattr(self.state, "level", 1) or 1)
        log.info("桌宠等级变化 → Lv.%d (工具可见集下轮刷新)", lvl)
        # chat_window 若开着工具解锁对话框,关掉让它重开读新等级(简化处理)
        cw = self._chat_window
        if cw is not None:
            try:
                cw._on_dev_level_changed(lvl)
            except Exception:  # noqa: BLE001
                pass

    def _on_pet_level_up(self, new_level: int) -> None:
        """真实升级(EXP 累积) → 桌宠气泡祝贺 + 刷 UI。

        跟 dev mode 手动拖 level slider 区分:那条不走 on_level_up 回调。
        """
        try:
            from app.engine.tools._unlock import UNLOCK_CELEBRATE, UNLOCK_REWARDS
            msg = UNLOCK_CELEBRATE.get(int(new_level), "")
            title = UNLOCK_REWARDS.get(int(new_level), "")
            if not msg:
                msg = f"主人,我已经 Lv.{new_level} 了!"
            if title:
                msg = f"{msg}\n[解锁 {title}]"
            # 通过桌宠气泡 + TTS 播报
            try:
                self.pet.show_bubble(msg)
            except Exception:  # noqa: BLE001
                log.debug("气泡显示失败(非致命)", exc_info=True)
            try:
                if self.tts is not None and getattr(self.cfg.character, "tts_enabled", False):
                    self.tts.speak(msg)
            except Exception:  # noqa: BLE001
                log.debug("祝贺 TTS 播报失败(非致命)", exc_info=True)
            log.info("桌宠升级至 Lv.%d,已发祝贺", new_level)
        except Exception as e:  # noqa: BLE001
            log.warning("_on_pet_level_up 失败: %s", e)

    def _fire_alarm(self, text: str, kind: str = "⏰ 提醒") -> None:
        """到点提醒的统一处理：气泡 + 置顶窗 + 提示音 + 系统通知。"""
        self.pet.show_bubble(f"{kind}：{text}")
        anim = getattr(self.pet, "animator", None)
        if anim is not None and hasattr(anim, "trigger_scene"):
            try:
                anim.trigger_scene("reminder")
            except Exception:  # noqa: BLE001
                log.debug("reminder 动画触发失败", exc_info=True)
        alarm = getattr(self, "alarm", None)
        if alarm is not None:
            # speak=True：让桌宠朗读提醒内容
            alarm.fire(text, kind=kind, sound=True, speak=True)
        # 系统通知（桌宠自己的置顶窗已是可靠通道，失败也无所谓）
        from app.core import toast
        toast.show(kind, text)

    def _speak_alarm(self, text: str) -> None:
        """闹钟播报。TTS 不可用就只留提示音，不报错打扰主人。"""
        tts = getattr(self, "tts", None)
        if tts is None:
            return
        try:
            tts.speak(text)
        except Exception:  # noqa: BLE001
            log.warning("闹钟 TTS 播报失败", exc_info=True)

    def _on_eat_requested(self) -> None:
        """吃饭：涨饱食度 + 体力 + 心情。"""
        self.state_mgr.on_eat_requested()
        self.pet.show_bubble("吃饱啦~ 好满足！")

    def _on_food_selected(self, name: str) -> None:
        """右键投喂具体食物：按 foods.json 价格扣金币并应用数值变化。

        图片贴纸 / desc 气泡由 PetWindow 负责；这里只改状态（兜底，PetWindow 已预检）。
        金币不足或找不到食物时退回通用吃饭。
        """
        items = getattr(self.brain, "items", None)
        item = items.by_name(name) if items is not None else None
        if item is None:
            log.warning("投喂未找到食物：%s，退回通用吃饭", name)
            self.state_mgr.on_eat_requested()
            return
        from app.engine.works import apply_food
        if apply_food(self.state, item, free=False):
            # 投喂本身是陪伴：额外一点心情；好感已在 apply_food 内按每日上限处理
            self.state.on_interact(feeling_gain=2)
            log.info("购买投喂「%s」：%s", name, self.state.stats_summary())
        else:
            self._pet_speak("金币不足，先去玩小游戏或签到赚金币吧～")

    #  小游戏 / 每日签到
    def _on_game_requested(self, game_id: str) -> None:
        """右键「小游戏」入口。"""
        if game_id == "gomoku":
            self._show_gomoku()
        elif game_id == "werewolf":
            self._show_werewolf()
        elif game_id == "xiangqi":
            self._show_xiangqi()
        else:
            log.warning("未知小游戏：%s", game_id)

    def _show_werewolf(self) -> None:
        """打开狼人杀窗口（标准 9 人局，桌宠当主持人上帝）。"""
        from app.ui.werewolf_window import WerewolfWindow
        ww = self._werewolf_window
        if ww is None or not ww.isVisible():
            enable_llm = bool(self.cfg.has_api_key())
            pet_name = getattr(self.cfg.character, "name", "桌宠") or "桌宠"
            ww = WerewolfWindow(
                llm_cfg=self.cfg.llm, enable_llm=enable_llm,
                player_name="你", pet_name=pet_name,
                voices=getattr(self.cfg.character, "voices", []),
                chat_cfg=self.cfg.chat)
            ww.host_spoke.connect(self._on_werewolf_host)
            ww.speak_line.connect(self._on_ww_speak_line)
            ww.game_finished.connect(self._on_werewolf_finished)
            ww.game_session_active.connect(self._on_game_session)
            ww.show()
            self._werewolf_window = ww
        else:
            ww.raise_()
            ww.activateWindow()

    def _on_werewolf_host(self, text: str, voice: str = "") -> None:
        # 主持人（桌宠）台词：气泡 + TTS；voice 非空时用指定音色
        self._pet_speak(text, duration_ms=5000, voice=voice)

    def _on_ww_speak_line(self, text: str, voice: str) -> None:
        # NPC 发言朗读：按该角色配音音色合成（无气泡）
        if not voice or self.tts is None:
            return
        profile = self._voice_profile_by_name(voice)
        if profile is not None:
            try:
                self.tts.speak_as(text, profile)
            except Exception:
                log.warning("NPC 朗读失败：%s", text)

    def _voice_profile_by_name(self, name: str):
        for v in getattr(self.cfg.character, "voices", []) or []:
            if getattr(v, "name", "") == name:
                return v
        return None

    def _on_werewolf_finished(self, result: dict) -> None:
        # 胜 30 金币、负/参与 5 金币
        amount = 30 if result.get("player_won") else 5
        granted = self.state.add_game_reward(amount) if amount else 0
        self.state.on_interact(feeling_gain=2)
        if granted:
            self.pet.show_bubble(f"本局金币 +{granted:.0f}", duration_ms=2500)
        self._game_react("game_win" if result.get("player_won")
                         else "game_lose")
        log.info("狼人杀结束 winner=%s player_won=%s 金币 +%.0f",
                 result.get("winner"), result.get("player_won"), granted)

    def _show_gomoku(self) -> None:
        """打开五子棋窗口（重复打开复用已存在窗口）。"""
        from app.ui.gomoku_window import GomokuWindow
        gw = self._gomoku_window
        if gw is None or not gw.isVisible():
            gw = GomokuWindow()
            gw.game_finished.connect(self._on_gomoku_finished)
            gw.comment.connect(self._on_game_comment)
            gw.game_session_active.connect(self._on_game_session)
            gw.show()
            self._gomoku_window = gw
            self._game_enter()
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
        # 结算动作：胜利 / 失败（和棋、认输都归入失败反馈）
        _gomoku_ev = {"win": "game_win", "lose": "game_lose",
                      "draw": "game_lose", "giveup": "game_lose"}
        self._game_react(_gomoku_ev.get(result, "game_lose"))
        self.state.on_interact(feeling_gain=2)
        log.info("五子棋结束 result=%s difficulty=%s 金币 +%.0f",
                 result, difficulty, granted)

    def _show_xiangqi(self) -> None:
        """打开中国象棋窗口（重复打开复用已存在窗口）。"""
        from app.ui.xiangqi_window import XiangqiWindow
        xw = self._xiangqi_window
        if xw is None or not xw.isVisible():
            xw = XiangqiWindow()
            xw.game_finished.connect(self._on_xiangqi_finished)
            xw.comment.connect(self._on_game_comment)
            xw.pet_mood.connect(self._on_xiangqi_pet_mood)
            xw.game_session_active.connect(self._on_game_session)
            xw.show()
            self._xiangqi_window = xw
            self._game_enter()
        else:
            xw.raise_()
            xw.activateWindow()
        xw.set_wallet(self.state.money, self.state.game_coin_remaining())

    def _on_xiangqi_pet_mood(self, mood: str) -> None:
        """象棋窗口报告的桌宠情绪：分派到 thinking / 表情键 / 复位。"""
        anim = getattr(self.pet, "animator", None)
        if anim is None:
            return
        try:
            if mood == "thinking":
                anim.set_thinking()
            elif mood == "idle":
                anim.set_idle()
            elif mood:
                # 其他键走表情键
                self.pet.play_emotion(mood)
        except Exception:  # noqa: BLE001
            log.warning("象棋桌宠情绪切换失败：%s", mood)

    def _on_xiangqi_finished(self, result: str, difficulty: str) -> None:
        """一局结束：按难度/结果发放金币（受每日上限约束），桌宠做反应。"""
        from app.ui.xiangqi_window import REWARDS
        xw = self._xiangqi_window
        amount = 0
        # 困毙/和棋：不发金币（按 draw 处理）
        if result in ("win", "lose", "draw"):
            amount = REWARDS.get(difficulty, REWARDS["normal"]).get(result, 0)
        granted = self.state.add_game_reward(amount) if amount else 0
        if xw is not None:
            xw.set_wallet(self.state.money, self.state.game_coin_remaining())
            xw.show_reward(granted, amount > 0 and granted <= 0)
        # 困毙/和棋不触发胜负外观
        _xq_ev = {"win": "game_win", "lose": "game_lose",
                  "draw": "game_lose", "giveup": "game_lose",
                  "resign": "game_win",
                  "stalemate": None}
        ev = _xq_ev.get(result)
        if ev is not None:
            self._game_react(ev)
        self.state.on_interact(feeling_gain=2)
        log.info("象棋结束 result=%s difficulty=%s 金币 +%.0f",
                 result, difficulty, granted)

    def _on_game_comment(self, text: str) -> None:
        # 五子棋过程 / 结算解说：气泡 + TTS 朗读
        self._pet_speak(text)

    def _on_game_session(self, active: bool) -> None:
        # 进入一局→切换并保持游戏外观；关闭退出→恢复场景外观
        if active:
            self._game_enter()
        else:
            self._game_exit()

    def _game_trigger(self, scene_id: str) -> None:
        # 触发一个游戏场景外观（仅 Live2D；sprite 无场景系统，静默）
        r = getattr(self.pet, 'renderer', None)
        try:
            if r is not None and hasattr(r, 'trigger_scene'):
                r.trigger_scene(scene_id)
        except Exception as e:  # noqa: BLE001
            log.warning("游戏场景触发失败（%s）：%s", scene_id, e)

    def _game_enter(self) -> None:
        """进入游戏：保持「游戏开始」外观。"""
        self._game_trigger("game_start")

    def _game_react(self, event_id: str) -> None:
        # 胜负结算：切换到胜利/失败外观
        self._game_trigger(event_id)

    def _game_exit(self) -> None:
        """退出游戏：收起游戏外观，恢复自然。"""
        r = getattr(self.pet, 'renderer', None)
        try:
            if r is not None and hasattr(r, 'restore_scene_appearance'):
                r.restore_scene_appearance()
        except Exception as e:  # noqa: BLE001
            log.warning("游戏退出恢复外观失败：%s", e)

    def _pet_speak(self, text: str, duration_ms: int = 4000,
                   voice: str = "") -> None:
        # 非聊天场景统一发言：显示气泡，TTS 开启时朗读（口型自动同步）
        self.pet.show_bubble(text, duration_ms=duration_ms)
        if getattr(self.cfg.character, "tts_enabled", False) and self.tts is not None:
            try:
                profile = self._voice_profile_by_name(voice) if voice else None
                if profile is not None:
                    self.tts.speak_as(text, profile)
                else:
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
        """聊天结束回调：触发聊天情绪场景 + 可选 TTS（仅在流式期间没 speak 时才 speak 整段）。"""
        log.info("chat reply ready: %r / %s tts=%s", text, emotion, tts_enabled)
        self._apply_chat_emotion(emotion)
        if tts_enabled and self.tts is not None:
            # 流式期间 chat_window 已按句 speak 过；这里补播剩余尾部
            cw = getattr(self, "_chat_window", None)
            if cw is None:
                self.tts.speak(text)
            else:
                spoken = getattr(cw, "_tts_spoken_chars", 0)
                if spoken < len(text):
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
            # 30 分钟未互动
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
        # TTS 关闭时按文字节奏张嘴（开了 TTS 等引擎信号，避免对不上口型）
        if not getattr(self.tts, "enabled", True):
            self._stream_talking = True
            self._update_talking()

    def _on_streaming_done(self) -> None:
        """流式输出结束：隐藏或延迟隐藏桌宠气泡。"""
        self._stream_talking = False
        self._update_talking()
        self.pet.stop_streaming_bubble()

    def _setup_lipsync(self) -> None:
        self._tts_talking = False
        self._stream_talking = False
        # TTS 工作线程回调 → 本信号桥 → queued 到主线程（不能用 QTimer.singleShot）
        # 信号只 connect 一次（TTS 热切换会重进本方法）
        if not getattr(self, "_lipsync_wired", False):
            self._lipSyncChanged.connect(self._set_tts_talking)
            self._lipsync_wired = True
        if hasattr(self.tts, "on_speak_start"):
            # TTS 工作线程调 on_speak_start()/on_speak_end()（无参）
            self.tts.on_speak_start = lambda: self._lipSyncChanged.emit(True)
            self.tts.on_speak_end = lambda: self._lipSyncChanged.emit(False)

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
        self.pet.show_bubble("思考中…", duration_ms=2000)

    def _on_thinking_stopped(self) -> None:
        """模型思考结束：回到待机。"""
        self.pet.animator.set_idle()

    #  视觉设置
    def _setting(self, key, default=None):
        """读一个设置项的当前值。"""
        val = self.settings_store.get(key, None)
        return default if val is None else val

    def _apply_visual_settings(self) -> None:
        """应用视觉设置（缩放、透明度、行为节拍）。"""
        # scale：实时 resize 桌宠窗（sprite 重缩放帧图 / live2d 重 fit 模型）
        new_scale = float(self._setting("scale", self.pet._scale))
        if abs(new_scale - self.pet._scale) > 0.001:
            self.pet.apply_display_size(new_scale)
        # 透明度：70% - 100% → setWindowOpacity
        op = float(self._setting("opacity", 1.0))
        self.pet.setWindowOpacity(op)
        # 行为：motion 节拍
        self.motion.apply_settings(
            idle_seconds=int(self._setting("motion_idle_s", 45)),
            walk_seconds=int(self._setting("motion_walk_s", 10)),
            decide_seconds=int(self._setting("motion_decide_s", 30)),
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
        from app.ui.pixmap_utils import scale_pixmap_keep_alpha, clear_pixmap_cache
        clear_pixmap_cache()
        atlas = self.pet.atlas
        target_size = self.pet._window_size

        def _rescale_anim(a):
            if a is None:
                return
            for f in a.frames:
                if f.original is not None:
                    f.pixmap = scale_pixmap_keep_alpha(f.original, target_size)
                else:
                    f.pixmap = scale_pixmap_keep_alpha(f.pixmap, target_size)

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
        """桌宠缩放变化 → 持久化。

        缩放来源：控制台的缩放设置。
        窗口尺寸调整已由 PetWindow.apply_display_size 完成（先 resize 再 emit，
        所以此处 pet._scale 通常已是新值，不能用「_scale 是否变化」早退）。
        """
        new_scale = round(float(new_scale), 2)
        if abs(self.pet._scale - new_scale) > 0.001:
            self.pet.apply_display_size(new_scale)
        self._rescale_sprite_pixmaps()
        self.settings_store.set("scale", float(new_scale))

    def _on_preview_voice_requested(self, text: str) -> None:
        """试听：用**当前引擎的真实 TTS 实例**播。

        面板之前自己 new 了一个 edge-tts，引擎是 gptsovits/minimax 时，
        听到的压根不是你配的音色。
        """
        tts = getattr(self, "tts", None)
        if tts is None:
            log.warning("试听失败：语音引擎还没初始化")
            return
        # speak() 在关闭状态下会直接丢弃 —— 试听时临时打开一下
        was_on = bool(getattr(tts, "enabled", False))
        try:
            if not was_on:
                tts.set_enabled(True)
            tts.speak(text)
        except Exception as e:  # noqa: BLE001
            log.warning("试听失败: %s", e)
            return
        finally:
            if not was_on:
                tts.set_enabled(False)

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
        fps = 1000.0 / ms if ms > 0 else 0
        log.info("帧时长已切换：%s ms / 帧（%s fps）", ms, round(fps, 1))

    def _on_fps_monitor_toggled(self, on: bool) -> None:
        """FPS 气泡调试开关。"""
        self.pet._fps_enabled = bool(on)
        if on:
            self.pet._fps_count = 0
            self.pet._fps_window_start = 0.0
            self.pet.show_bubble("FPS 监测开启", duration_ms=2000)
        else:
            self.pet.show_bubble("FPS 监测关闭", duration_ms=1500)

    def _on_bot_avatar_changed(self, path: str) -> None:
        """桌宠头像换好：写 cfg.character.bot_avatar_path + 让 chat 刷新头像。

        控制台选完头像传过来, 这俩写回主配置 + 通知
        已开的聊天窗重置头像缓存 / chat 气泡头像（下次新消息用）。
        """
        try:
            self.cfg.character.bot_avatar_path = path
        except Exception:  # noqa: BLE001
            pass
        if self._chat_window is not None and hasattr(self._chat_window, "set_avatar"):
            try:
                from pathlib import Path as _P
                self._chat_window.set_avatar(_P(path))
            except Exception:  # noqa: BLE001
                log.debug("chat_window.set_avatar 失败", exc_info=True)
        log.info("桌宠头像已更新：%s", path)

    def _on_developer_mode_changed(self, enabled: bool) -> None:
        """开发者模式开关：记下状态供 dashboard / chat_window 查询。"""
        self.developer_mode = bool(enabled)
        log.info("开发者模式: %s", "开" if enabled else "关")

    def _reset_to_default_settings(self) -> dict:
        """重置所有设置为默认值（写进 settings.json 并立即应用）。"""
        result = self.settings.reset_defaults()
        self._apply_visual_settings_with_rescale()
        self.pet.animator.set_lock_first_idle(False)
        if self._is_sprite():
            self.pet.player.set_crossfade_ms(0)
            self._override_frame_ms_now(50)
        self.pet._fps_enabled = False
        log.info("已重置默认值")
        return result

    #  帧持久化
    def _do_retune_frames(self, ms: int) -> None:
        """用户点了「应用帧时长」按钮 —— 重命名所有 PNG + 重新加载 atlas。

        重命名脚本是个 subprocess（原来直接在 GUI 线程跑，timeout=60），
        桌宠会整个冻住最长 60 秒。现在丢到 worker 线程跑，
        跑完再回主线程刷新 atlas / 改状态。
        """
        if not self._is_sprite():
            log.info("帧时长仅 sprite 模式可用（Live2D 无帧图）")
            return
        from pathlib import Path as _P
        from app.core.qt_compat import QThread

        script = _P(__file__).resolve().parent.parent / "tools" / "_legacy" / "rename_to_vpet.py"

        class _RetuneWorker(QThread):
            finished_ok = Signal(int, str, str)

            def __init__(self, cmd):
                super().__init__()
                self._cmd = cmd

            def run(self):
                import subprocess
                try:
                    r = subprocess.run(self._cmd, capture_output=True, text=True,
                                       timeout=60)
                    self.finished_ok.emit(r.returncode, r.stdout or "", r.stderr or "")
                except Exception as e:  # noqa: BLE001
                    self.finished_ok.emit(-1, "", repr(e))

        log.info("正在重命名帧文件…")
        cmd = ["python", str(script), "--retune-ms", str(ms)]
        worker = _RetuneWorker(cmd)
        worker.finished_ok.connect(
            lambda code, out, err: self._on_retune_done(ms, code, out, err))
        self._retune_worker = worker   # 保留引用防 GC
        worker.start()

    def _on_retune_done(self, ms: int, code: int, out: str, err: str) -> None:
        """retune 子进程结束（主线程）：刷新 atlas + 改帧时长 + 更新状态。"""
        log.info("retune 退出码 %d, stdout=%s stderr=%s", code, out[:300], err[:300])
        try:
            # 走 SpriteRenderer 自带的 reload_atlas（保留同一个 renderer/player/label 实例，
            # 只换内部 atlas 引用，避免新旧 atlas/animator 双实例不同步的 bug）
            self.pet.reload_atlas(fallback_image=self.cfg.sprite.fallback)
            # 统一覆盖所有帧的 duration_ms
            self.pet.atlas.override_frame_duration_ms(ms)
            # 让新 duration 立即生效
            self.pet._start_frame_timer()
            if code == 0:
                log.info("已 retune 到 %s ms（%s fps），atlas 已重新加载",
                         ms, round(1000 / ms, 1))
            else:
                log.warning("retune 失败（退出码 %s）：%s", code, err[:200])
        except Exception as e:  # noqa: BLE001
            log.error("retune 失败：%s", e)

    #  聊天窗口
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
                chat_cfg=self.cfg.chat,
                # 引擎层上下文对象
                cwd=str(self.root),
                # session_store: brain 上挂的持久化对象（降级时为 None）
                session_store=getattr(self.brain, "session_store", None),
                # skills_dir: brain.skills_dir（非空路径）
                skills_dir=str(getattr(self.brain, "skills_dir", "") or ""),
                # state_provider: 每次拉最新 state，dev mode 改值能反映到新 agent
                state_provider=lambda: self.state_mgr.state,
                # level_provider: 返回当前桌宠等级,LLM 工具列表按等级过滤
                level_provider=lambda: int(getattr(self.state_mgr.state, "level", 1) or 1),
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

    #  控制台
    def _show_settings(self) -> None:
        """打开控制台（设置搬进浏览器控制台了，这里直接开页面）。"""
        self._open_dashboard()

    def _open_dashboard(self) -> None:
        """在浏览器打开开发者控制台（设置都在里面）。"""
        app = self._find_app()
        if app is not None and hasattr(app, "open_dashboard"):
            try:
                app.open_dashboard()
                return
            except Exception:  # noqa: BLE001
                log.debug("app.open_dashboard 失败", exc_info=True)
        from app.main import (start_dashboard_subprocess, wait_dashboard_ready,
                               TRACE_PORT)
        from pathlib import Path as _P
        pid = start_dashboard_subprocess(_P(self.root), port=TRACE_PORT)
        if pid is not None and wait_dashboard_ready(port=TRACE_PORT, timeout=8.0):
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{TRACE_PORT}")

    def _wait_port_async(self, port: int, timeout: float, slot) -> None:
        """在 worker 线程里等端口就绪。

        原来是在 GUI 线程上 socket 轮询 + time.sleep(0.2)，
        桌宠会整个冻住 6~8 秒。现在丢到 worker 线程，
        结果通过信号回到主线程再处理（slot 必须是本对象的方法，
        这样才是 queued 连接，回主线程执行）。
        """
        from app.core.qt_compat import QThread

        class _PortWaiter(QThread):
            ready = Signal(bool)

            def __init__(self, p, t):
                super().__init__()
                self._port = p
                self._timeout = t

            def run(self):
                import socket
                import time
                deadline = time.time() + self._timeout
                while time.time() < deadline:
                    try:
                        with socket.create_connection(("127.0.0.1", self._port),
                                                     timeout=0.5):
                            self.ready.emit(True)
                            return
                    except OSError:
                        time.sleep(0.2)
                self.ready.emit(False)

        w = _PortWaiter(port, timeout)
        w.ready.connect(slot)
        self._port_waiter = w   # 保留引用防 GC
        w.start()

    def _open_demo(self) -> None:
        """打开纯静态 Live2D Demo（本地 8765）：优先用 App 入口，否则直接启子进程。"""
        app = self._find_app()
        if app is not None and hasattr(app, "open_demo"):
            app.open_demo()
            return
        from app.main import (start_demo_subprocess, wait_dashboard_ready,
                              open_in_browser, DEMO_PORT)
        from pathlib import Path
        root = Path.cwd()
        if start_demo_subprocess(root, port=DEMO_PORT) is None:
            self.pet.show_bubble("Live2D Demo 启动失败", duration_ms=3000)
            return
        self._wait_port_async(DEMO_PORT, 6.0, self._on_demo_ready)

    def _on_demo_ready(self, ok: bool) -> None:
        """Live2D Demo 端口探测结束（主线程）。"""
        if ok:
            from app.main import open_in_browser, DEMO_PORT
            open_in_browser(f"http://127.0.0.1:{DEMO_PORT}/")
        else:
            self.pet.show_bubble("Live2D Demo 启动超时", duration_ms=3000)

    def _open_trace(self) -> None:
        """打开 FastAPI Agent Trace 可视化面板（开发者，8766）。"""
        app = self._find_app()
        if app is not None and hasattr(app, "open_dashboard"):
            app.open_dashboard()
            return
        from app.main import (start_dashboard_subprocess, wait_dashboard_ready,
                              open_in_browser, TRACE_PORT)
        from pathlib import Path
        root = Path.cwd()
        if start_dashboard_subprocess(root, port=TRACE_PORT) is None:
            self.pet.show_bubble("Agent Trace 启动失败", duration_ms=3000)
            return
        self._wait_port_async(TRACE_PORT, 8.0, self._on_trace_ready)

    def _on_trace_ready(self, ok: bool) -> None:
        """Agent Trace 端口探测结束（主线程）。"""
        if ok:
            from app.main import open_in_browser, TRACE_PORT
            open_in_browser(f"http://127.0.0.1:{TRACE_PORT}")
        else:
            self.pet.show_bubble("Agent Trace 启动超时", duration_ms=3000)

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

    #  模型配置
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
            # 面板的「超时时间」也要管主动关心 —— 之前它固定走
            # chat.request_timeout_s（默认 20 秒），面板写 60 实际还是 20
            setter = getattr(self.brain.proactive, "set_request_timeout", None)
            if callable(setter):
                setter(int(self.cfg.llm.timeout))

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

    #  Live2D 专属（控制台「Live2D 外观」区）
    def _live2d_renderer(self):
        """当前是不是 Live2D 渲染器（sprite 没有分类外观）。"""
        r = getattr(self.pet, "renderer", None)
        if r is None or not hasattr(r, "activate_menu_item"):
            return None
        try:
            if r.get_renderer_type() != "live2d":
                return None
        except Exception:  # noqa: BLE001
            return None
        return r

    def _on_live2d_item_activated(self, group_id: str, item_id: str) -> None:
        """Live2D：应用一个分类条目（特殊/发型/配件/手势/表情）。"""
        r = self._live2d_renderer()
        if r is not None:
            r.activate_menu_item(group_id, item_id)
            log.info("Live2D 外观已切换：%s", item_id)

    def _on_live2d_reset(self) -> None:
        """Live2D：复位全部外观。"""
        r = self._live2d_renderer()
        if r is not None:
            r.reset_all_appearance()
            log.info("Live2D 外观已全部复位")

    def _on_random_exp_changed(self, enabled: bool) -> None:
        """Live2D：挂机随机表情开关。"""
        r = self._live2d_renderer()
        if r is not None:
            r.set_random_expressions(bool(enabled))
            self.settings_store.set("random_exp_enabled", bool(enabled))
            log.info("挂机随机表情：%s", "开" if enabled else "关")

    def _on_random_sticker_changed(self, enabled: bool) -> None:
        """Live2D：随机表情包贴纸开关。"""
        fn = getattr(self.pet, "set_random_stickers", None)
        if callable(fn):
            fn(bool(enabled))
            self.settings_store.set("random_sticker_enabled", bool(enabled))
            log.info("随机表情包贴纸：%s", "开" if enabled else "关")

    def _on_sticker_options_changed(self, opts: dict) -> None:
        """Live2D：贴纸参数（大小/角度/间隔/时长），即时生效并持久化。"""
        fn = getattr(self.pet, "set_sticker_options", None)
        if callable(fn):
            fn(**opts)
            store = self.settings_store
            for key, skey in (("size", "sticker_size"), ("rotation", "sticker_rotation"),
                              ("min_s", "sticker_min_s"), ("max_s", "sticker_max_s"),
                              ("duration_s", "sticker_duration_s")):
                if key in opts:
                    store.set(skey, int(opts[key]))

    def _on_random_interval_changed(self, lo: int, hi: int) -> None:
        """Live2D：挂机随机间隔。"""
        r = self._live2d_renderer()
        if r is not None and hasattr(r, "set_random_interval"):
            r.set_random_interval(int(lo), int(hi))
            store = self.settings_store
            store.set("random_min_s", int(lo))
            store.set("random_max_s", int(hi))

    def _on_max_fps_changed(self, fps: int) -> None:
        """Live2D：渲染帧率上限。"""
        r = self._live2d_renderer()
        if r is not None and hasattr(r, "set_max_fps"):
            r.set_max_fps(int(fps))
            self.settings_store.set("max_fps", int(fps))

    def _on_tts_config_changed(self, cfgd: dict) -> None:
        """TTS 引擎与参数：持久化 + 后台线程重建引擎（热切换）。"""
        store = self.settings_store
        keymap = {"tts_enabled": "tts_enabled", "engine": "tts_engine",
                  "minimax_voice_id": "minimax_voice_id",
                  "gptsovits_url": "gptsovits_url",
                  "ref_audio": "gptsovits_ref_audio",
                  "prompt_text": "gptsovits_prompt_text",
                  "current_voice": "current_voice"}
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
        if cfgd.get("current_voice"):
            c.current_voice = str(cfgd["current_voice"])
        import threading
        threading.Thread(target=self._rebuild_tts_blocking, daemon=True,
                         name="tts-rebuild").start()
        log.info("语音引擎配置已保存，正在切换…")

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
        """主动关心开关与间隔。"""
        store = self.settings_store
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
            log.info("主动关心将在重启后生效（当前未启用）")
        log.info("主动关心：%s", cfgd)

    def _on_aw_config_changed(self, cfgd: dict) -> None:
        """活动感知：主人改了 enabled / server URL / 回看 / 数量, 立刻
        同步给 ProactiveBrain.apply_aw_config (下次 _fire 就生效)。也持久化
        到 settings_store + config.yaml (跟 chat_overrides 一样, 不重写全 yaml)。
        """
        enabled = bool(cfgd.get("enabled", False))
        server_url = str(cfgd.get("server_url", "http://127.0.0.1:5600"))
        lookback = max(1, int(cfgd.get("proactive_lookback_minutes", 30) or 30))
        max_events = max(1, int(cfgd.get("proactive_max_events", 15) or 15))

        # 1) 持久化: settings_store + config.yaml
        try:
            store = self.settings_store
            store.set("activitywatch_enabled", enabled)
            store.set("activitywatch_server_url", server_url)
            store.set("activitywatch_lookback", lookback)
            store.set("activitywatch_max_events", max_events)
            # 同步到 cfg (内存) — 不落 yaml, 避免每次热改就改配置文件
            if hasattr(self, "cfg") and self.cfg is not None:
                self.cfg.activitywatch.enabled = enabled
                self.cfg.activitywatch.server_url = server_url
                self.cfg.activitywatch.proactive_lookback_minutes = lookback
                self.cfg.activitywatch.proactive_max_events = max_events
        except Exception:
            log.exception("persist aw config")

        # 2) 推给 ProactiveBrain
        p = getattr(self.brain, "proactive", None)
        if p is None:
            log.info("主动关心未启用, AW 配置已存, 重启后生效")
            return
        # 构造一个轻量 cfg-like 对象给 proactive (避免新增模型类)
        from types import SimpleNamespace
        aw_cfg = SimpleNamespace(
            enabled=enabled,
            server_url=server_url,
            proactive_lookback_minutes=lookback,
            proactive_max_events=max_events,
        )
        if hasattr(p, "apply_aw_config"):
            p.apply_aw_config(aw_cfg)
        else:
            p._aw_cfg = aw_cfg
        log.info("aw cfg 更新: %s", cfgd)

    def _on_renderer_changed(self, renderer: str) -> None:
        """渲染器切换（持久化，重启生效）。"""
        self.settings_store.set("renderer", str(renderer))
        log.info("渲染器已设为 %s，重启桌宠后生效", renderer)

    def _on_hide_watermark_changed(self, on: bool) -> None:
        """隐藏水印开关（持久化，重启生效）。"""
        self.settings_store.set("hide_watermark", bool(on))
        log.info("水印设置已保存，重启桌宠后生效")

    # v30: _on_character_changed 已删 — 人设改读 characters/<file>.yaml,
    # 不在设置面板运行时改。重启用 load_config 读最新 yaml。

    def _on_always_on_top_changed(self, enabled: bool) -> None:
        """窗口置顶开关（立即生效并持久化）。"""
        fn = getattr(self.pet, "set_always_on_top", None)
        if callable(fn):
            fn(bool(enabled))
        self.settings_store.set("always_on_top", bool(enabled))
        log.info("窗口置顶：%s", "开" if enabled else "关")

    def _on_save_settings(self) -> None:
        """把运行中的实际值回写 settings.json（渲染器 / 贴纸这类由运行时决定）。"""
        store = self.settings_store
        try:
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
            log.info("设置已手动保存")
        except Exception:  # noqa: BLE001
            log.exception("保存设置失败")

    def _apply_stored_window_settings(self) -> None:
        """启动时应用 settings.json 里保存的窗口类设置（保存按钮/自动保存写入）。"""
        store = self.settings_store
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
            # 行为节拍（idle / walk / decide）—— 之前运行时改了有效，
            # 重启就丢（store 里有值但启动时没人读）
            motion_kw = {}
            for key, skey in (("idle_seconds", "motion_idle_s"),
                              ("walk_seconds", "motion_walk_s"),
                              ("decide_seconds", "motion_decide_s")):
                v = store.get(skey, None)
                if v is not None:
                    motion_kw[key] = int(v)
            if motion_kw:
                try:
                    self.motion.apply_settings(**motion_kw)
                except Exception:  # noqa: BLE001
                    log.debug("恢复行为节拍失败", exc_info=True)
            # FPS 气泡监测开关 —— 同样之前重启就丢
            fps_mon = store.get("fps_monitor", None)
            if fps_mon is not None:
                self.pet._fps_enabled = bool(fps_mon)
            # 锁定第一张 idle
            lock_idle = store.get("lock_first_idle", None)
            if lock_idle is not None:
                setter = getattr(self.pet.animator, "set_lock_first_idle", None)
                if callable(setter):
                    setter(bool(lock_idle))
            # v30: 角色名 / 人设 不再从 settings.json 读, 改从 characters/<file>.yaml
            # load_config 启动时已读 yaml 覆盖 cfg.character, 不需要这里再补。
        except Exception:  # noqa: BLE001
            log.exception("启动应用已保存设置失败")

    def _init_model_config_ui(self) -> None:
        """启动时把 cfg.llm 的值灌进设置中枢（控制台读的是这份）。"""
        self.settings_store.set_many({
            "model_base_url": self.cfg.llm.base_url,
            "model_api_key": self.cfg.llm.api_key,
            "model_name": self.cfg.llm.model,
            "model_temperature": self.cfg.llm.temperature,
            "model_max_tokens": self.cfg.llm.max_tokens,
            "model_timeout": self.cfg.llm.timeout,
            "model_stream": self.cfg.llm.stream,
        })

    #  控制服务：把控制台改的设置应用到运行中的桌宠
    def _apply_settings_changed(self, changes: dict) -> None:
        """一批设置改完了 → 立即应用到桌宠。

        设置中枢已经落盘了，这里只管「让桌宠真的变」。
        """
        changes = changes or {}
        if not changes:
            return

        # -- 视觉 --------------------------------------------------------
        if "scale" in changes:
            self._on_pet_scale_changed(float(changes["scale"]))
        if "opacity" in changes:
            self.pet.setWindowOpacity(float(changes["opacity"]))
        if "always_on_top" in changes:
            self._on_always_on_top_changed(bool(changes["always_on_top"]))
        if "crossfade_enabled" in changes or "crossfade_ms" in changes:
            enabled = bool(self._setting(
                "crossfade_enabled", True))
            ms = int(self._setting("crossfade_ms", 200))
            self._on_crossfade_changed(enabled, ms)
        if "lock_first_idle" in changes:
            setter = getattr(self.pet.animator, "set_lock_first_idle", None)
            if callable(setter):
                setter(bool(changes["lock_first_idle"]))

        # -- 帧时长 ------------------------------------------------------
        if "frame_ms" in changes:
            self._override_frame_ms_now(int(changes["frame_ms"]))
        if "fps_monitor" in changes:
            self._on_fps_monitor_toggled(bool(changes["fps_monitor"]))
        if "max_fps" in changes:
            self._on_max_fps_changed(int(changes["max_fps"]))

        # -- 行为 --------------------------------------------------------
        if any(k in changes for k in ("motion_idle_s", "motion_walk_s",
                                      "motion_decide_s")):
            self.motion.apply_settings(
                idle_seconds=int(self._setting("motion_idle_s", 45)),
                walk_seconds=int(self._setting("motion_walk_s", 10)),
                decide_seconds=int(self._setting("motion_decide_s", 30)),
            )
        if any(k in changes for k in ("proactive_enabled",
                                      "proactive_min_minutes",
                                      "proactive_max_minutes")):
            self._on_proactive_changed({
                "enabled": bool(self._setting("proactive_enabled", True)),
                "min_minutes": int(self._setting("proactive_min_minutes", 25)),
                "max_minutes": int(self._setting("proactive_max_minutes", 45)),
            })
        if any(k.startswith("activitywatch_") for k in changes):
            self._on_aw_config_changed({
                "enabled": bool(self._setting("activitywatch_enabled", False)),
                "server_url": str(self._setting(
                    "activitywatch_server_url", "http://127.0.0.1:5600")),
                "proactive_lookback_minutes": int(
                    self._setting("activitywatch_lookback", 30)),
                "proactive_max_events": int(
                    self._setting("activitywatch_max_events", 15)),
            })
        if "developer_mode" in changes:
            self._on_developer_mode_changed(bool(changes["developer_mode"]))

        # -- Live2D ------------------------------------------------------
        if "random_exp_enabled" in changes:
            self._on_random_exp_changed(bool(changes["random_exp_enabled"]))
        if "random_sticker_enabled" in changes:
            self._on_random_sticker_changed(bool(changes["random_sticker_enabled"]))
        if any(k.startswith("sticker_") for k in changes):
            self._on_sticker_options_changed({
                "size": int(self._setting("sticker_size", 120)),
                "rotation": int(self._setting("sticker_rotation", 45)),
                "min_s": int(self._setting("sticker_min_s", 40)),
                "max_s": int(self._setting("sticker_max_s", 120)),
                "duration_s": int(self._setting("sticker_duration_s", 4)),
            })
        if "random_min_s" in changes or "random_max_s" in changes:
            self._on_random_interval_changed(
                int(self._setting("random_min_s", 20)),
                int(self._setting("random_max_s", 50)))

        # -- 语音 --------------------------------------------------------
        if any(k.startswith("tts_") or k in ("current_voice", "minimax_voice_id",
                                            "gptsovits_url",
                                            "gptsovits_ref_audio",
                                            "gptsovits_prompt_text")
               for k in changes):
            self._on_tts_config_changed({
                "tts_enabled": bool(self._setting("tts_enabled", True)),
                "engine": str(self._setting("tts_engine", "edge")),
                "minimax_voice_id": str(self._setting("minimax_voice_id", "")),
                "gptsovits_url": str(self._setting(
                    "gptsovits_url", "http://127.0.0.1:9880")),
                "ref_audio": str(self._setting("gptsovits_ref_audio", "")),
                "prompt_text": str(self._setting("gptsovits_prompt_text", "")),
                "current_voice": str(self._setting("current_voice", "")),
            })
        if "tts_voice" in changes:
            self._on_voice_changed(str(changes["tts_voice"]))

        # -- 模型 --------------------------------------------------------
        if any(k.startswith("model_") for k in changes):
            self._on_model_config_changed({
                "base_url": str(self._setting("model_base_url", "")),
                "api_key": str(self._setting("model_api_key", "")),
                "model": str(self._setting("model_name", "")),
                "temperature": float(self._setting("model_temperature", 0.8)),
                "max_tokens": int(self._setting("model_max_tokens", 4096)),
                "timeout": int(self._setting("model_timeout", 60)),
                "stream": bool(self._setting("model_stream", True)),
            })

        # -- 渲染器 / 水印（重启生效，只落盘） -----------------------------
        if "renderer" in changes:
            self.settings_store.set("renderer", str(changes["renderer"]))
        if "hide_watermark" in changes:
            self.settings_store.set("hide_watermark", bool(changes["hide_watermark"]))

    #  控制服务：运行时动作
    def _control_action(self, action: str, params: dict) -> dict:
        """控制台点的那些「一次性动作」。"""
        action = str(action or "")
        params = params or {}
        if action == "live2d_activate":
            group_id = str(params.get("group_id") or "")
            item_id = str(params.get("item_id") or "")
            r = self._live2d_renderer()
            if r is None:
                return {"ok": False, "error": "当前不是 Live2D 渲染器"}
            r.activate_menu_item(group_id, item_id)
            return {"ok": True, "active": sorted(r.get_active_items())}
        if action == "live2d_reset":
            r = self._live2d_renderer()
            if r is None:
                return {"ok": False, "error": "当前不是 Live2D 渲染器"}
            r.reset_all_appearance()
            return {"ok": True, "active": []}
        if action == "preview_voice":
            text = str(params.get("text") or "").strip()
            if not text:
                name = getattr(self.cfg.character, "name", "") or "桌宠"
                text = f"你好呀，我是{name}，很高兴见到你。"
            self._on_preview_voice_requested(text)
            return {"ok": True, "text": text}
        if action == "set_avatar":
            path = str(params.get("path") or "")
            if not path:
                return {"ok": False, "error": "没给头像路径"}
            self._on_bot_avatar_changed(path)
            return {"ok": True, "path": path}
        if action == "reset_defaults":
            return self._reset_to_default_settings()
        if action == "dev_state":
            snapshot = params.get("snapshot")
            if not isinstance(snapshot, dict):
                return {"ok": False, "error": "没给状态快照"}
            self._on_dev_state_pushed(snapshot)
            return {"ok": True}
        if action == "retune_frames":
            try:
                ms = int(params.get("ms") or 50)
            except (TypeError, ValueError):
                return {"ok": False, "error": "帧时长不是数字"}
            self._do_retune_frames(ms)
            return {"ok": True}
        # -- Live2D：模型 / 场景 / 动作配置 -------------------------------
        if action == "live2d_models":
            return {"ok": True, "models": self._live2d_models()}
        if action == "live2d_switch_model":
            return self._live2d_switch_model(str(params.get("path") or ""))
        if action == "live2d_scene_save":
            return self._live2d_scene_save(
                str(params.get("scene_id") or ""), params.get("bundle"))
        if action == "live2d_scene_reset":
            return self._live2d_scene_reset(str(params.get("scene_id") or ""))
        if action == "live2d_action_save":
            return self._live2d_action_save(params)
        if action == "live2d_action_delete":
            return self._live2d_action_delete(str(params.get("id") or ""))
        if action == "live2d_action_play":
            return self._live2d_action_play(str(params.get("id") or ""))
        if action == "live2d_preview":
            return self._live2d_preview(params.get("bundle"))
        return {"ok": False, "error": "未知动作：%s" % action}

    #  Live2D：模型 / 场景 / 动作配置
    def _live2d_models(self) -> list:
        """可用的 Live2D 模型列表（当前用的那个标记 current）。"""
        from app.core.live2d_config import list_models
        root = _attr(self, "root")
        if root is None:
            return []
        cfg = _attr(self, "cfg")
        current = ""
        if cfg is not None:
            current = str(getattr(cfg.pet.live2d, "model_dir", "") or "")
        return list_models(Path(root) / "assets" / "live2d", current)

    def _live2d_switch_model(self, rel_path: str) -> dict:
        """换模型：写进 config.yaml（重启桌宠生效）。

        Live2D 渲染器带 WebView，热切换要整个重建 —— 不在这儿冒险，
        写好配置、提示重启就行。
        """
        rel_path = str(rel_path or "").strip().replace("\\", "/")
        if not rel_path:
            return {"ok": False, "error": "没给模型路径"}
        base = (Path(self.root) / "assets" / "live2d").resolve()
        target = (Path(self.root) / rel_path).resolve()
        if not _is_under(target, base):
            return {"ok": False, "error": "模型路径不在 assets/live2d 里"}
        if not target.is_dir() or not list(target.glob("*.model3.json")):
            return {"ok": False, "error": "那目录里没有 *.model3.json"}
        self.cfg.pet.live2d.model_dir = rel_path
        self._write_yaml_section({"pet": {"live2d": {"model_dir": rel_path}}})
        return {"ok": True, "path": rel_path, "restart": True,
                "note": "模型已切换，重启桌宠后生效"}

    def _write_yaml_section(self, section: dict) -> None:
        """把一段配置写进 config.yaml（保留文件里其它内容）。"""
        path = Path(self.root) / "config.yaml"
        try:
            import yaml
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            log.exception("读 config.yaml 失败")
            return
        for key, value in (section or {}).items():
            if isinstance(value, dict) and isinstance(raw.get(key), dict):
                raw[key].update(value)
            else:
                raw[key] = value
        try:
            path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                            encoding="utf-8")
        except Exception:  # noqa: BLE001
            log.exception("写 config.yaml 失败")

    def _live2d_scene_save(self, scene_id: str, bundle) -> dict:
        """保存一个触发场景的外观组合。"""
        from app.core.live2d_config import dict_to_bundle
        r = self._live2d_renderer()
        if r is None:
            return {"ok": False, "error": "当前不是 Live2D 渲染器"}
        if not scene_id:
            return {"ok": False, "error": "没给场景 id"}
        r.set_scene_bundle(scene_id, dict_to_bundle(bundle or {}))
        return {"ok": True, "scene_id": scene_id}

    def _live2d_scene_reset(self, scene_id: str) -> dict:
        """恢复一个触发场景的默认外观。"""
        r = self._live2d_renderer()
        if r is None:
            return {"ok": False, "error": "当前不是 Live2D 渲染器"}
        r.reset_scene_bundle(scene_id)
        return {"ok": True, "scene_id": scene_id}

    def _live2d_action_save(self, params: dict) -> dict:
        """新建 / 更新一个自定义动作。"""
        from app.core.live2d_config import dict_to_bundle
        r = self._live2d_renderer()
        if r is None:
            return {"ok": False, "error": "当前不是 Live2D 渲染器"}
        params = params or {}
        name = str(params.get("name") or "").strip() or "未命名动作"
        hook = str(params.get("hook") or "")
        bundle = dict_to_bundle(params.get("bundle") or {})
        hold = int(params.get("hold_ms") or bundle.hold_ms or 0)
        cid = str(params.get("id") or "")
        if cid:
            r.update_custom_action(cid, name=name, hook=hook, bundle=bundle,
                                   hold_ms=hold)
        else:
            r.add_custom_action(name, hook, bundle, hold)
        return {"ok": True, "id": cid, "name": name}

    def _live2d_action_delete(self, cid: str) -> dict:
        r = self._live2d_renderer()
        if r is None:
            return {"ok": False, "error": "当前不是 Live2D 渲染器"}
        ok = r.remove_custom_action(cid)
        return {"ok": bool(ok), "id": cid}

    def _live2d_action_play(self, cid: str) -> dict:
        r = self._live2d_renderer()
        if r is None:
            return {"ok": False, "error": "当前不是 Live2D 渲染器"}
        r.play_custom_action(cid)
        return {"ok": True, "id": cid}

    def _live2d_preview(self, bundle) -> dict:
        """试穿一套外观（约 2.6 秒后恢复）。"""
        from app.core.live2d_config import dict_to_bundle
        r = self._live2d_renderer()
        if r is None:
            return {"ok": False, "error": "当前不是 Live2D 渲染器"}
        r.preview_bundle(dict_to_bundle(bundle or {}))
        return {"ok": True}

    def _control_appearance(self) -> dict:
        """Live2D 外观：分类 chip + 触发场景配置 + 自定义动作 + 可绑钩子。

        不同模型的表情/发型/配件/手势条目不一样 —— 这里按当前模型给，
        场景配置存在 ``data/live2d_scenes/<模型目录名>.json``，换模型自动换一份。
        """
        from app.core.live2d_config import (action_configs, hook_options,
                                            item_labels, scene_configs)
        r = self._live2d_renderer()
        if r is None:
            return {"available": False, "groups": [], "active": [],
                    "renderer": "sprite", "scenes": [], "actions": [],
                    "hooks": [], "models": self._live2d_models()}
        try:
            groups = r.get_menu_groups()
        except Exception:  # noqa: BLE001
            groups = []
        try:
            active = sorted(r.get_active_items())
        except Exception:  # noqa: BLE001
            active = []
        return {
            "available": True,
            "groups": groups,
            "active": active,
            "renderer": "live2d",
            "model": str(getattr(r, "model_dir", "")),
            "modelName": str(getattr(getattr(r, "profile", None), "name", "") or ""),
            "labels": item_labels(r),
            "scenes": scene_configs(r),
            "actions": action_configs(r),
            "hooks": hook_options(r),
            "models": self._live2d_models(),
        }

    def _control_avatars(self) -> list:
        """可选头像（预设 + 用户上传）。"""
        out = []
        root = Path(self.root) / "assets" / "avatars"
        for folder, label in (("presets", "预设"), ("uploads", "上传")):
            d = root / folder
            if not d.is_dir():
                continue
            for p in sorted(d.glob("*.png")):
                out.append({"path": str(p), "name": p.stem, "group": label})
        return out

    #  退出
    def _on_about_to_quit(self) -> None:
        """退出前最终存档 + 收掉闹钟（别让提示音在进程退出后还响）。"""
        alarm = getattr(self, "alarm", None)
        if alarm is not None:
            try:
                alarm.shutdown()
            except Exception:  # noqa: BLE001
                log.debug("关闭报警窗失败", exc_info=True)
        try:
            from app.voice.alarm_sound import stop_all
            stop_all()
        except Exception:  # noqa: BLE001
            pass
        self.state_mgr.on_about_to_quit()

    def _quit(self) -> None:
        """退出应用。"""
        from app.core.qt_compat import QApplication
        QApplication.instance().quit()
