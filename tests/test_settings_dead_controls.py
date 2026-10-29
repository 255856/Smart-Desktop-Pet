"""设置面板死控件修复守卫。

背景：审计发现设置面板有 4 个"改了系统零反应"的选项 —— 勾了等于没勾。
本文件锁死这些修复，防止以后又退回空实现。

- 渲染引擎下拉：面板显示 store 值，运行时用 config.yaml 值 → 显示与实际矛盾
- 「永远用第一张 idle」：目标是空实现 no-op
- 「启用流式输出」：langchain_agent 硬编码 streaming=True
- GPT-SoVITS 参考音频/参考文本：一律被 voices profile 覆盖
"""
import ast as _ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _func_node(rel: str, func_name: str):
    tree = _ast.parse(_src(rel))
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) \
                and node.name == func_name:
            return node
    return None


def _attrs(node) -> set:
    return {n.attr for n in _ast.walk(node) if isinstance(n, _ast.Attribute)}


# ---------- 渲染引擎：显示真实生效值 ----------

def test_renderer_ui_shows_real_value_not_store():
    """控制台要显示 config.yaml 的真实渲染器，不能显示 store 里的旧值。"""
    src = _src("app/ui/ui_controller.py")
    assert '"renderer": _store0.get("renderer"' not in src, (
        "又改回读 store 的 renderer —— 会显示用户选的、实际跑另一个"
    )


def test_renderer_tip_explains_config_priority():
    """提示文案要说明"以 config.yaml 为准"，否则用户以为能改。"""
    src = _src("app/core/pet_settings.py")
    assert "restart" in src, (
        "渲染引擎切换没标「重启生效」—— 用户以为改了立刻就换了"
    )


# ---------- 永远用第一张 idle ----------

def test_sprite_lock_first_idle_is_implemented():
    """sprite 的 set_lock_first_idle 不能再是空壳。"""
    node = _func_node("app/animation/sprite_renderer.py", "set_lock_first_idle")
    assert node is not None, "sprite_renderer.set_lock_first_idle 缺失"
    body = _ast.dump(node)
    assert "_lock_first_idle" in body, "set_lock_first_idle 没存开关状态"


def test_sprite_idle_respects_lock():
    """锁了之后待机固定选第一个，不再随机。"""
    node = _func_node("app/animation/sprite_renderer.py", "set_idle")
    assert node is not None
    body = _ast.dump(node)
    assert "_lock_first_idle" in body, "set_idle 没看 _lock_first_idle 标志"


def test_lock_first_idle_hidden_for_live2d():
    """Live2D 没有帧序列，这个选项对它是死的 —— 不该显示。"""
    src = _src("app/core/pet_settings.py")
    assert "lock_first_idle" in src, "锁第一帧选项没了"
    src = _src("app/ui/ui_controller.py")
    assert "_is_sprite" in src, "没按渲染器类型判断锁第一帧是否有效"


def test_sprite_only_tabs_gated_by_renderer():
    """帧率预设 / 帧时长细调 / 淡入淡出都只对 sprite 有效。"""
    src = _src("app/ui/ui_controller.py")
    assert "_is_sprite" in src, (
        "帧时长相关设置没按渲染器类型分流（Live2D 下点了没反应）"
    )
    node = _func_node("app/ui/ui_controller.py", "_override_frame_ms_now")
    assert node is not None
    assert "_is_sprite" in _ast.dump(node), "帧时长覆盖没先判是不是 sprite"


def test_sprite_lock_first_idle_behaviour(qapp):
    """行为：勾上后待机只播第一张 idle。"""
    from types import SimpleNamespace

    from app.animation.sprite_renderer import PetAnimator

    class _Anim(PetAnimator):
        def get_widget(self):
            return None

    a1 = SimpleNamespace(name="idle_1", frames=[])
    a2 = SimpleNamespace(name="idle_2", frames=[])
    atlas = SimpleNamespace(
        idle_default=[a1, a2], idle_special=[], sleep=[], sleep_2=[],
        ensure_loaded=lambda *a, **k: None, any=lambda x: None,
    )
    played: list = []
    player = SimpleNamespace(
        play=lambda anim, **kw: played.append(anim),
        current_animation=lambda: None,
    )
    r = _Anim(atlas, player)
    assert r._lock_first_idle is False
    r.set_lock_first_idle(True)
    assert r._lock_first_idle is True
    assert played, "锁定后没有切到 idle"
    assert played[-1] is a1, "锁定后应固定播第一张 idle"


# ---------- 启用流式输出 ----------

def test_langchain_agent_respects_stream_setting():
    """langchain_agent 不能硬编码 streaming=True。"""
    src = _src("app/brain/langchain_agent.py")
    assert "streaming=True" not in src, (
        "langchain_agent 又硬编码 streaming=True —— 流式开关还是死的"
    )
    node = _func_node("app/brain/langchain_agent.py", "_build_model")
    assert node is not None
    body = _ast.dump(node)
    assert "streaming" in body
    assert "llm_cfg" in body, "streaming 没跟设置走"


def test_llm_client_chat_stream_honors_stream_flag():
    """关流式时别再发 SSE + 再 fallback 重发一次（一个请求发两遍）。"""
    node = _func_node("app/brain/llm_client.py", "chat_stream")
    assert node is not None
    body = _ast.dump(node)
    assert "_fetch_non_streaming" in body
    assert "stream" in body


# ---------- GPT-SoVITS 参考音频 / 参考文本 ----------

def test_panel_ref_audio_takes_priority_over_profile():
    """面板手填的参考音频/文本优先，profile 只在没填全时兜底。"""
    src = _src("app/main.py")
    assert "gptsovits_ref_audio" in src
    assert "gptsovits_prompt_text" in src
    # 不能再是"有 profile 就整组覆盖"
    assert "_ref, _pt = _av.ref_audio, _av.prompt_text" not in src, (
        "还是被 profile 整组覆盖 —— 面板填的参考音频/文本永远不生效"
    )


# ---------- 运行时有效、重启就丢 ----------

def test_motion_settings_restored_on_startup():
    """IDLE/WALK/决策间隔：运行时改了有效，重启必须还原。"""
    src = _src("app/ui/ui_controller.py")
    for key in ("motion_idle_s", "motion_walk_s", "motion_decide_s"):
        assert key in src, f"{key} 启动时没读回来（改了设置重启就丢）"
    assert "apply_settings" in src


def test_fps_monitor_restored_on_startup():
    """FPS 气泡监测开关：重启要保持。"""
    src = _src("app/ui/ui_controller.py")
    assert "fps_monitor" in src, "fps_monitor 启动时没读回来"


def test_lock_first_idle_restored_on_startup():
    """锁第一张 idle：重启要保持。"""
    src = _src("app/ui/ui_controller.py")
    assert "lock_first_idle" in src
    assert "set_lock_first_idle" in src


def test_developer_mode_persisted():
    """开发者模式：勾了要能保持（之前不存，重启就关）。"""
    src = _src("app/core/pet_settings.py")
    assert "developer_mode" in src, "开发者模式没进设置项清单"
    src = _src("app/ui/ui_controller.py")
    assert "developer_mode" in src, "主程序没读开发者模式"


def test_developer_mode_changed_signal_is_wired():
    """开发者模式改完，主程序要真的知道。"""
    src = _src("app/ui/ui_controller.py")
    assert "_on_developer_mode_changed" in src, "主程序没处理开发者模式变化"
    node = _func_node("app/ui/ui_controller.py", "_apply_settings_changed")
    assert node is not None, "缺设置应用分发"
    assert "developer_mode" in _ast.dump(node), (
        "开发者模式没走统一的应用分发 —— 改了系统零反应"
    )


# ---------- TTS：音色下拉 / 试听 ----------

def test_voice_options_follow_engine():
    """语音音色只有 edge 认 —— 切到 gptsovits/minimax 时不该还杵在那儿。"""
    src = _src("app/core/pet_settings.py")
    for key in ("tts_voice", "tts_engine", "current_voice", "minimax_voice_id"):
        assert key in src, f"设置项清单少了 {key}"
    src = _src("app/ui/ui_controller.py")
    node = _func_node("app/ui/ui_controller.py", "_on_tts_config_changed")
    assert node is not None
    body = _ast.dump(node)
    assert "_update_tts_engine_visibility" not in body
    assert "tts_engine" in body, "TTS 引擎变化没往下传"


def test_preview_voice_uses_live_engine_instance():
    """试听要走主程序当前的引擎实例，不能自己 new 一个 edge-tts。"""
    src = _src("app/ui/ui_controller.py")
    node = _func_node("app/ui/ui_controller.py", "_on_preview_voice_requested")
    assert node is not None, "主程序没接试听请求"
    body = _ast.dump(node)
    assert "tts" in body, "试听没走当前引擎的 TTS 实例"
    assert "TTS(" not in body, "不该自己 new TTS 实例"

    # 控制台的试听动作要走控制服务 → 主程序这条链
    src = _src("app/ui/ui_controller.py")
    node = _func_node("app/ui/ui_controller.py", "_control_action")
    assert node is not None
    assert "preview_voice" in _ast.dump(node), "控制台没接试听动作"


# ---------- 主动关心间隔 ----------

def test_proactive_interval_range_matches_backend():
    """设置项的间隔范围要跟 ProactiveBrain 的钳制规则一致。"""
    src = _src("app/core/pet_settings.py")
    assert '"proactive_min_minutes"' in src
    # 下限最少 2 分钟（后端会抬到 2），上限至少 6 分钟
    assert '"min": 2, "max": 720' in src, "间隔下限范围不是 2~720"
    assert '"min": 6, "max": 720' in src, "间隔上限范围不是 6~720"

    # 后端同一套规则（主人要求: 下限最低 2, 上限最低 6）
    backend = _src("app/brain/proactive.py")
    assert "max(2, int(min_minutes))" in backend, "后端下限钳制不是 2"
    assert "max(6, self.min_minutes, int(max_minutes))" in backend, "后端上限钳制不是 6"


# ---------- API 预设 ----------

def test_api_preset_clears_key():
    """换厂商必须换 Key —— 留着旧 Key 必然 401。

    控制台没有「一键预设」按钮了（面板删了），但密钥是掩码回显的：
    掩码值不能被当成新 Key 写回去。
    """
    from app.core.pet_settings import coerce
    assert coerce("model_api_key", "sk-abc12345678") == "sk-abc12345678"
    assert coerce("model_api_key", "******5678") is None, (
        "掩码被当成新 Key 写回去了 —— 真 key 会被冲掉"
    )
    assert coerce("model_api_key", "") is None, "空密钥不该被当成「清空」"


# ---------- 超时时间 ----------

def test_proactive_follows_llm_timeout():
    """面板的「超时时间」要管主动关心（之前它固定走 20 秒）。"""
    node = _func_node("app/brain/proactive.py", "set_request_timeout")
    assert node is not None, "ProactiveBrain 缺 set_request_timeout"
    src = _src("app/ui/ui_controller.py")
    assert "set_request_timeout" in src, "模型配置变更时没同步主动关心的超时"


# ---------- 保存设置 / 重置默认值 ----------

def test_snapshot_to_store_covers_all_groups():
    """「保存设置」要真落盘 —— 设置项清单要覆盖所有分组。"""
    from app.core.pet_settings import SETTING_SCHEMA
    for key in ("crossfade_enabled", "motion_idle_s", "motion_walk_s",
                "motion_decide_s", "fps_monitor", "frame_ms",
                "lock_first_idle", "developer_mode", "proactive_enabled",
                "tts_engine", "scale", "opacity"):
        assert key in SETTING_SCHEMA, f"设置项清单没覆盖 {key}"


def test_reset_writes_store_and_model_config():
    """「重置默认值」要写 store 并立即生效（之前只重置 UI）。"""
    from app.core.pet_settings import SettingsService
    node = _func_node("app/core/pet_settings.py", "reset_defaults")
    assert node is not None, "缺 reset_defaults"
    body = _ast.dump(node)
    assert "update" in body, "重置后没写 store —— 重启又被旧值盖回来"

    src = _src("app/ui/ui_controller.py")
    assert "reset_defaults" in src, "重置动作没走设置中枢"
    assert "_apply_settings_changed" in src, "重置后没走统一的应用分发"


def test_load_defaults_does_not_write_store():
    """恢复默认只动非模型项 —— 模型配置是主人特意填的，不能被刷掉。"""
    from app.core.pet_settings import SETTING_SCHEMA
    model_keys = [k for k in SETTING_SCHEMA if k.startswith("model_")]
    assert model_keys, "模型配置不在设置项清单里？"
    src = _src("app/core/pet_settings.py")
    assert 'if key.startswith("model_")' in src, (
        "重置逻辑没区分模型配置 —— 会把主人填的模型名冲掉"
    )


# ---------- 工具解锁窗口：模态 + 顶层 + 类型独特 ----------

def test_tool_unlock_window_type_and_modality():
    """工具解锁窗口: 无边框 + 顶层 + ApplicationModal。

    主人要求：打开后其他窗口点不动（modal）、固定在最上面（StaysOnTopHint）。
    无边框外壳（去原生标题栏）由 app/ui/dialog_shell.py 统一提供。
    """
    src = _src("app/ui/chat_window/tool_unlock_dialog.py")
    shell = _src("app/ui/dialog_shell.py")
    assert "WindowStaysOnTopHint" in src, "没设 WindowStaysOnTopHint"
    assert "ApplicationModal" in src, "没设 ApplicationModal（其他窗口能抢焦点）"
    # 少了 FramelessWindowHint，系统原生标题栏会跟自绘标题栏叠成两个
    assert "FramelessWindowHint" in shell, "弹窗外壳没去原生标题栏"
    # 注意顺序: 外壳先 setWindowFlags, 再 setWindowModality
    # (setWindowFlags 会清掉 modality)
    assert src.index("FramelessDialog(") < src.index("setWindowModality"), (
        "setWindowModality 必须在构造外壳之后 —— "
        "否则 setWindowFlags 会把 modal 状态清掉"
    )


# ---------- 头像悬浮显示桌宠状态 ----------

def test_chat_avatar_hover_hook_installed():
    """鼠标悬浮 chat 顶部头像 → 弹 tooltip 显示桌宠状态。

    之前头像只是个静态图，hover 啥都没有；现在拿最新 PetState.stats_summary()
    拼一行，让用户不用切回桌宠本体就能瞄一眼当前情况。
    """
    src = _src("app/ui/chat_window/__init__.py")
    assert "_AvatarHoverHook" in src, "chat 顶部头像没装 _AvatarHoverHook"
    # _build_ui 是 chat_window 唯一的 UI 构造函数（含 header）
    node = _func_node("app/ui/chat_window/__init__.py", "_build_ui")
    assert node is not None, "chat_window 缺 _build_ui"
    body = _ast.dump(node)
    assert "avatar_label" in body, "avatar_label 没在 _build_ui 里建"
    assert "_AvatarHoverHook" in body, "_build_ui 没装 hover hook"
    assert "installEventFilter" in body


# ---------- 窗口角落缩放 (FramelessResizer) ----------

def test_frameless_resizer_freeze_translucent_param():
    """FramelessResizer 必须支持 freeze_translucent=False —— 半透明窗缩放期间不能整块变黑。"""
    src = _src("app/ui/frameless.py")
    assert "freeze_translucent" in src, (
        "FramelessResizer 没 freeze_translucent 参数 —— "
        "半透明窗口 (chat / memory_panel / werewolf) 缩放期间会变黑"
    )
    # _freeze_heavy_paint 必须尊重该开关
    node = _func_node("app/ui/frameless.py", "_freeze_heavy_paint")
    assert node is not None
    assert "_freeze_translucent" in _attrs(node), (
        "_freeze_heavy_paint 没尊重 freeze_translucent —— "
        "半透明窗口缩放期间仍会被关掉透明背景"
    )


def test_memory_panel_and_werewolf_have_resizer():
    """记忆 / 狼人杀 窗口: 无边框 + FramelessResizer (角落拖角缩放)。"""
    mem_src = _src("app/ui/memory_panel.py")
    ww_src = _src("app/ui/werewolf_window.py")
    for name, src in (("memory_panel", mem_src), ("werewolf_window", ww_src)):
        assert "FramelessResizer" in src, f"{name} 没挂 FramelessResizer"
        assert "freeze_translucent=False" in src, (
            f"{name} 是半透明窗口, 缩放期间需要传 freeze_translucent=False"
        )


def test_memory_panel_and_werewolf_have_minimum_size():
    """窗口缩放有底线 —— 拖角缩到看不见任何东西, 体验就崩了。"""
    mem_src = _src("app/ui/memory_panel.py")
    ww_src = _src("app/ui/werewolf_window.py")
    for name, src in (("memory_panel", mem_src), ("werewolf_window", ww_src)):
        assert "setMinimumSize" in src or "setMinimumWidth" in src, (
            f"{name} 没设最小尺寸 —— 拖角能缩到看不见内容"
        )




# ---------- 头像悬浮 tooltip 用迷你进度条 ----------

def test_avatar_hover_tooltip_uses_mini_progress_bars():
    """_AvatarHoverHook._build_tip 输出 HTML + 进度条（不是一行文字）。"""
    node = _func_node("app/ui/chat_window/chrome.py", "_build_tip")
    assert node is not None
    body = _ast.dump(node)
    # 进度条用 ■/□ 块字符 + <font> 染色
    assert "■" in body, "没进度条填充字符"
    assert "font color" in body, "没颜色标记 (没在分高/中/低)"
    # 应有体力/饱食/饮水/健康/好感至少 5 行
    for metric in ("strength", "strength_food", "strength_drink", "health", "likability"):
        assert metric in body, f"tooltip 缺 {metric} 这条进度"


# ---------- 头像目录 + 弹窗 + 设置面板入口 ----------

def test_avatar_dir_lives_under_assets_not_data():
    """主人要求: 头像目录从 data/ 迁到 assets/ (随代码提交)。"""
    src = _src("app/ui/chat_window/__init__.py")
    # 必须有 assets 子目录声明 (不是 data)
    assert "AVATAR_PRESETS_DIR" in src, "缺预设目录常量"
    assert "AVATAR_UPLOADS_DIR" in src, "缺用户上传目录常量"
    # 不应再指 data
    assert 'Path("data") / "avatars"' not in src, (
        "AVATAR_DIR 还指向 data/avatars —— 应该迁到 assets/avatars"
    )
    # 真有 assets 路径
    assert 'Path("assets")' in src


def test_avatar_picker_dialog_exists():
    """新组件: AvatarPickerDialog 区分「预设」「上传」两个 tab。"""
    src = _src("app/ui/chat_window/avatar_picker.py")
    # 必须有 AvatarPickerDialog 类 + 预设/上传 tab
    assert "class AvatarPickerDialog" in src, "缺 AvatarPickerDialog 类"
    assert "预设" in src, "缺预设 tab"
    assert "我上传的" in src, "缺上传 tab"
    # emit avatarSelected 给外部
    assert "avatarSelected" in src
    # 上传走 QFileDialog
    assert "QFileDialog" in src
    # setFixedSize 只收 (w, h) 两个 int —— 传两个 QSize 会 TypeError 崩
    assert "setFixedSize(_size(" not in src, (
        "setFixedSize 传了两个 QSize —— PyQt 签名只收 (int, int), 会崩"
    )


def test_settings_window_has_avatar_picker_entry():
    """控制台有头像入口：列头像 + 选了就换。"""
    src = _src("app/ui/ui_controller.py")
    assert "_control_avatars" in src, "控制服务没提供头像列表"
    assert "_on_bot_avatar_changed" in src, "主程序没处理换头像"
    node = _func_node("app/ui/ui_controller.py", "_control_action")
    assert node is not None
    assert "set_avatar" in _ast.dump(node), "控制台没接换头像动作"
    # 控制台前端要有头像选择控件
    web = _src("app/web/dashboard.py")
    assert "settingsAvatars" in web, "控制台没渲染头像区"
    assert "set_avatar" in web, "控制台没发换头像请求"


# ---------- 贴纸/食物图片被 mask 遮罩挡住 ----------

def test_sticker_show_forces_mask_update():
    """主人反馈: 头顶表情包 / 食物图片刚 show 时被遮罩裁掉, 1.5s 后才完整显示。

    _update_click_mask 是 1.5s 一次的定时采样, 贴纸矩形加进 mask 是按当前
    isVisible() 算的 —— show 之后不立即触发, 就被旧 mask 裁到下一轮采样。
    """
    node = _func_node("app/ui/pet_window.py", "_present_sticker")
    assert node is not None, "pet_window 缺 _present_sticker"
    # _ast.dump 会把 QTimer.singleShot(0, ...) 里的数字字面量归一化成 Constant(value=0)
    # 所以不能用字符串匹配, 直接扫所有 Call 节点找 singleShot(_, _update_click_mask)
    found = False
    for sub in _ast.walk(node):
        if (isinstance(sub, _ast.Call)
                and isinstance(sub.func, _ast.Attribute)
                and sub.func.attr == "singleShot"
                and len(sub.args) >= 2
                and isinstance(sub.args[1], _ast.Attribute)
                and sub.args[1].attr == "_update_click_mask"):
            found = True
            break
    assert found, "贴纸 show 之后没立即触发 _update_click_mask —— 1.5s 内被旧 mask 裁"


# ---------- 状态进度条改椭圆 ----------

def test_status_progressbar_uses_ellipse_radius():
    """状态模块的进度条改椭圆 (圆角 = 高度/2)。"""
    # 设置面板删了，状态条挪到桌宠窗口（pet_window）里
    src = _src("app/ui/pet_window.py")
    assert "QProgressBar::chunk" in src, "桌宠状态条没画 chunk 样式"
    assert "border-radius" in src, "桌宠状态条没设圆角"
    # 圆角 3px 配 6px 高 = 椭圆（高度的一半）
    assert "border-radius: 3px" in src, "状态条圆角与高度不配（该是高度的一半）"
    assert "height: 6px" in src or "setFixedHeight(6)" in src, (
        "状态条高度变了但圆角没跟着改 —— 就不是椭圆了"
    )


# ---------- 删控制模块的快捷入口和切换表情 ----------

def test_control_tab_dropped_shortcut_and_emotion_panel():
    """设置面板已删 —— 桌宠的快捷操作都在右键菜单里。"""
    src = _src("app/ui/pet_window.py")
    for name in ("切换表情", "切换发型"):
        assert name in src, f"右键菜单少了「{name}」—— 面板删了得在这儿能用"
    # 设置项清单里不该有这些动作型的东西（它们是右键菜单的事）
    from app.core.pet_settings import SETTING_SCHEMA
    for name in ("btn_chat", "btn_sleep", "emo_btns"):
        assert name not in SETTING_SCHEMA, f"设置项清单混进了 {name}"


# ---------- 设置中枢：校验 / 落盘 / 应用 ----------

def test_settings_schema_covers_all_groups():
    """每个分组都要有设置项，且每项都得有标签和类型。"""
    from app.core.pet_settings import GROUP_ORDER, SETTING_SCHEMA
    groups = {spec["group"] for spec in SETTING_SCHEMA.values()}
    for g in GROUP_ORDER:
        assert g in groups, f"分组 {g} 一个设置项都没有"
    for key, spec in SETTING_SCHEMA.items():
        assert spec.get("label"), f"{key} 没标签"
        assert spec.get("type") in ("bool", "int", "float", "str", "secret"), \
            f"{key} 的类型不对：{spec.get('type')}"


def test_settings_values_mask_the_api_key():
    """控制台不能拿到 API Key 原文。"""
    from app.core.pet_settings import SettingsService
    from app.core.settings_store import SettingsStore
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        store = SettingsStore(Path(td) / "settings.json")
        store.set("model_api_key", "sk-secret-value-1234")
        svc = SettingsService(store)
        shown = svc.values()["model_api_key"]
        assert "sk-secret" not in shown, "API Key 原文漏回控制台了"
        assert shown.endswith("1234"), "掩码应保留末尾几位便于辨认"


def test_settings_update_persists_and_calls_apply_hook():
    """改设置要落盘 + 立即应用到桌宠。"""
    from app.core.pet_settings import SettingsService
    from app.core.settings_store import SettingsStore
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "settings.json"
        store = SettingsStore(path)
        svc = SettingsService(store)
        seen = {}
        svc.apply_hook = lambda values: seen.update(values)
        result = svc.update({"scale": 0.8, "opacity": 0.9})
        assert result["ok"] is True
        assert set(result["applied"]) == {"scale", "opacity"}
        assert seen == {"scale": 0.8, "opacity": 0.9}, "没走到应用钩子"
        # 落盘了
        assert abs(float(store.get("scale")) - 0.8) < 0.01


def test_settings_update_clamps_out_of_range():
    """越界的值要夹回合法范围，不能照单全收。"""
    from app.core.pet_settings import SettingsService
    from app.core.settings_store import SettingsStore
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        store = SettingsStore(Path(td) / "settings.json")
        svc = SettingsService(store)
        svc.update({"scale": 99.0, "opacity": 0.1})
        assert float(store.get("scale")) <= 1.0
        assert float(store.get("opacity")) >= 0.7


def test_settings_update_rejects_unknown_keys():
    """不认识的键要拒掉（别把乱七八糟的东西写进 settings.json）。"""
    from app.core.pet_settings import SettingsService
    from app.core.settings_store import SettingsStore
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        store = SettingsStore(Path(td) / "settings.json")
        svc = SettingsService(store)
        result = svc.update({"没有这个键": 1})
        assert result["ok"] is False
        assert "没有这个键" in result["rejected"]


def test_settings_reset_keeps_model_config():
    """恢复默认不动模型配置 —— 那是主人特意填的。"""
    from app.core.pet_settings import SettingsService
    from app.core.settings_store import SettingsStore
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        store = SettingsStore(Path(td) / "settings.json")
        store.set("model_name", "my-model")
        store.set("scale", 0.5)
        svc = SettingsService(store)
        svc.reset_defaults()
        assert store.get("model_name") == "my-model", "模型名被恢复默认冲掉了"
        assert float(store.get("scale")) != 0.5, "缩放没恢复默认"


def test_settings_schema_lists_ranges_for_panel():
    """面板要能渲染滑块 —— 范围必须给出去。"""
    from app.core.pet_settings import SettingsService
    from app.core.settings_store import SettingsStore
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        svc = SettingsService(SettingsStore(Path(td) / "settings.json"))
        items = {i["key"]: i for i in svc.schema()}
        assert "min" in items["scale"] and "max" in items["scale"]
        assert items["renderer"]["options"] == ["sprite", "live2d"]
        assert items["renderer"]["restart"] is True, "切渲染器要重启，提示得标出来"

def test_tool_unlock_has_titlebar_window_chrome():
    """工具解锁窗口无边框, 主人要求加 min/max/close 跟其他窗口一致。

    之前顶部只有一个灰色栏, 看起来跟其他窗口不一样;
    现在无边框 + 圆角卡片 + 自绘标题栏(─/▢/X) + 拖动,
    由 app/ui/dialog_shell.py 统一提供, 工具解锁窗 / 头像选择窗共用。
    """
    shell = _src("app/ui/dialog_shell.py")
    # 标题栏: 必须用 ui_style.TITLEBAR_QSS (渐变) + QToolButton#win_btn 样式,
    # 自造 objectName 会没样式 → 渲染成系统默认灰色方块 (主人反馈的"灰色栏")
    assert "TITLEBAR_QSS" in shell, "标题栏没用 ui_style.TITLEBAR_QSS"
    assert 'setObjectName("titlebar")' in shell, "标题栏 objectName 不是 titlebar"
    for objname in ("win_btn", "win_btn_close"):
        assert f'setObjectName("{objname}")' in shell, (
            f"标题栏按钮没用 {objname} 样式 —— 会显示成系统默认灰方块"
        )
    # 关闭按钮接到 reject
    assert "btn_close.clicked.connect(self.reject)" in shell
    # 最小化接到 showMinimized
    assert "btn_min.clicked.connect(self.showMinimized)" in shell
    # 最大化有 toggle
    assert "isMaximized" in shell and "showMaximized" in shell, (
        "最大化按钮没接 toggle 逻辑"
    )
    # 标题栏可拖动
    assert "TitleBarDrag" in shell, "标题栏没装拖动"
    assert "installEventFilter(self._drag_filter)" in shell


def test_popup_windows_share_frameless_shell():
    """工具解锁窗 / 头像选择窗必须走同一个无边框外壳。

    之前一个用系统原生标题栏、一个自绘，叠成双标题栏，跟聊天窗风格对不上。
    现在统一走 FramelessDialog: 圆角卡片 + 自绘标题栏 + 柔阴影。
    """
    unlock = _src("app/ui/chat_window/tool_unlock_dialog.py")
    picker = _src("app/ui/chat_window/avatar_picker.py")
    assert "FramelessDialog" in unlock, "工具解锁窗没走 FramelessDialog 外壳"
    assert "class AvatarPickerDialog(FramelessDialog)" in picker, (
        "头像选择窗没走 FramelessDialog 外壳"
    )
    # 外壳必须真的去掉了原生标题栏, 否则会跟自绘标题栏叠一起
    shell = _src("app/ui/dialog_shell.py")
    assert "FramelessWindowHint" in shell
    assert "WA_TranslucentBackground" in shell, "外壳没开透明背景（圆角阴影会露白边）"


# ---------- TTS：引擎切换时只留该引擎认的字段 ----------

def test_tts_settings_follow_engine():
    """切引擎时, 只有该引擎认的字段该留在控制台上。

    控制台是网页表单（没有「显隐行」这回事），但主程序侧要按引擎
    分流：edge 音色只对 edge 生效，GPT-SoVITS 的参考音频只对 gptsovits 生效。
    """
    from app.core.pet_settings import SETTING_SCHEMA
    for key in ("tts_voice", "current_voice", "gptsovits_ref_audio",
                "gptsovits_prompt_text", "minimax_voice_id"):
        assert key in SETTING_SCHEMA, f"设置项清单少了 {key}"

    src = _src("app/ui/ui_controller.py")
    node = _func_node("app/ui/ui_controller.py", "_on_tts_config_changed")
    assert node is not None
    body = _ast.dump(node)
    assert "tts_engine" in body, "引擎没往下传"
    assert "gptsovits_ref_audio" in body, "GPT-SoVITS 参考音频没同步到 cfg"
    assert "minimax_voice_id" in body, "MiniMax voice_id 没同步到 cfg"


def test_tts_engine_options_are_listed():
    """引擎下拉要有三个选项，不然换不了厂商。"""
    from app.core.pet_settings import SETTING_SCHEMA
    assert SETTING_SCHEMA["tts_engine"]["options"] == \
        ["edge", "gptsovits", "minimax"]
