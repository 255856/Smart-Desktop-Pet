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
    """面板必须显示 config.yaml 的真实渲染器，不能显示 store 里的旧值。"""
    src = _src("app/ui/ui_controller.py")
    assert '"renderer": _store0.get("renderer"' not in src, (
        "面板又改回读 store 的 renderer —— 会显示用户选的、实际跑另一个"
    )
    assert '"renderer": _renderer_cfg' in src, "面板没显示真实生效的 renderer"


def test_renderer_tip_explains_config_priority():
    """提示文案要说明"以 config.yaml 为准"，否则用户以为面板能改。"""
    src = _src("app/ui/settings_window.py")
    assert "pet.renderer" in src, (
        "渲染引擎提示没说明以 config.yaml 为准"
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
    node = _func_node("app/ui/settings_window.py", "_build_tab_visual")
    assert node is not None
    body = _ast.dump(node)
    assert "cb_lock_first_idle" in body
    assert "_renderer_type" in body, "没按渲染器类型显隐锁第一帧选项"


def test_sprite_only_tabs_gated_by_renderer():
    """帧率预设 / 帧时长细调 / 淡入淡出都只对 sprite 有效。"""
    node = _func_node("app/ui/settings_window.py", "_build_tab_fps")
    assert node is not None
    assert "_renderer_type" in _ast.dump(node), (
        "帧时长 Tab 没按渲染器类型显隐（Live2D 下点了没反应）"
    )


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
    src = _src("app/ui/settings_window.py")
    assert "developer_mode" in src
    node = _func_node("app/ui/settings_window.py", "_set_developer_mode")
    assert node is not None
    body = _ast.dump(node)
    assert "settings_store" in body, "开发者模式没持久化"


def test_developer_mode_changed_signal_is_wired():
    """developer_mode_changed signal 必须真发出 + 主程序真接。"""
    node = _func_node("app/ui/settings_window.py", "_set_developer_mode")
    assert node is not None
    body = _ast.dump(node)
    assert "developer_mode_changed" in body, (
        "切换开发者模式时没 emit developer_mode_changed —— 又是死信号"
    )

    src = _src("app/ui/ui_controller.py")
    assert "_on_developer_mode_changed" in src, "主程序没接这个 signal"
    assert "developer_mode_changed.connect" in src, "信号没连上"


# ---------- TTS：音色下拉 / 试听 ----------

def test_voice_options_follow_engine():
    """语音音色下拉只有 edge 认 —— 切到 gptsovits/minimax 时不该还杵在那儿。"""
    node = _func_node("app/ui/settings_window.py", "_update_tts_engine_visibility")
    assert node is not None, "缺 _update_tts_engine_visibility"
    body = _ast.dump(node)
    assert "_row_edge_voice" in body, "edge 音色行没按引擎显隐"
    assert "_row_default_voice" in body
    assert "_row_gsv_ref" in body
    assert "_row_minimax" in body


def test_preview_voice_uses_live_engine_instance():
    """试听要走主程序当前的引擎实例，不能自己 new 一个 edge-tts。"""
    node = _func_node("app/ui/settings_window.py", "_on_preview_voice")
    assert node is not None
    body = _ast.dump(node)
    assert "preview_voice_requested" in body, (
        "面板没把试听请求发给主程序 —— 用的还是固定 edge-tts"
    )
    assert "TTS(" not in body, "面板不该自己 new TTS 实例"

    src = _src("app/ui/ui_controller.py")
    node = _func_node("app/ui/ui_controller.py", "_on_preview_voice_requested")
    assert node is not None, "主程序没接试听请求"
    body = _ast.dump(node)
    assert "tts" in body, "试听没走当前引擎的 TTS 实例"


# ---------- 主动关心间隔 ----------

def test_proactive_interval_range_matches_backend():
    """面板的间隔范围要跟 ProactiveBrain 的钳制规则一致。"""
    src = _src("app/ui/settings_window.py")
    node = _func_node("app/ui/settings_window.py", "_build_tab_control")
    assert node is not None
    body = _ast.dump(node)
    assert "spin_proactive_min" in body
    # 下限最少 5 分钟（后端会抬到 5），上限至少比下限多 5
    assert "setRange(5, 720)" in src, "间隔下限范围没跟后端规则对齐"
    assert "setRange(10, 720)" in src, "间隔上限范围没跟后端规则对齐"

    node = _func_node("app/ui/settings_window.py", "_emit_proactive")
    assert node is not None
    body = _ast.dump(node)
    assert "setValue" in body, "间隔被后端钳制后没回显 —— 面板显示的和实际跑的不是一回事"


# ---------- API 预设 ----------

def test_api_preset_clears_key():
    """换厂商必须换 Key —— 留着旧 Key 必然 401。"""
    node = _func_node("app/ui/settings_window.py", "_on_preset_clicked")
    assert node is not None
    body = _ast.dump(node)
    assert "edt_api_key" in body and "clear" in body, (
        "切 API 预设没清 Key —— 换厂商带着旧 Key 必然 401"
    )


# ---------- 超时时间 ----------

def test_proactive_follows_llm_timeout():
    """面板的「超时时间」要管主动关心（之前它固定走 20 秒）。"""
    node = _func_node("app/brain/proactive.py", "set_request_timeout")
    assert node is not None, "ProactiveBrain 缺 set_request_timeout"
    src = _src("app/ui/ui_controller.py")
    assert "set_request_timeout" in src, "模型配置变更时没同步主动关心的超时"


# ---------- 保存设置 / 重置默认值 ----------

def test_snapshot_to_store_covers_all_groups():
    """「保存设置」要真落盘 —— 之前只存了 scale/opacity/置顶几个。"""
    node = _func_node("app/ui/settings_window.py", "snapshot_to_store")
    assert node is not None, "缺 snapshot_to_store"
    body = _ast.dump(node)
    for key in ("crossfade_enabled", "motion_", "fps_monitor", "frame_ms",
                "lock_first_idle", "developer_mode", "proactive_enabled",
                "tts_engine", "scale", "opacity"):
        assert key in body, f"snapshot_to_store 没覆盖 {key}"


def test_reset_writes_store_and_model_config():
    """「重置默认值」要写 store 并落到模型配置（之前只重置 UI）。"""
    node = _func_node("app/ui/settings_window.py", "reset_to_defaults")
    assert node is not None, "缺 reset_to_defaults"
    body = _ast.dump(node)
    assert "snapshot_to_store" in body, "重置后没写 store —— 重启又被旧值盖回来"
    assert "_on_model_config_changed" in body, "重置后模型配置没落到 cfg"

    src = _src("app/ui/ui_controller.py")
    assert "reset_to_defaults" in src, "重置按钮走的还是旧的 _load_defaults"


def test_load_defaults_does_not_write_store():
    """_load_defaults 是纯 UI 重置（__init__ 里也在用）—— 不能写 store。"""
    node = _func_node("app/ui/settings_window.py", "_load_defaults")
    assert node is not None
    body = _ast.dump(node)
    assert "snapshot_to_store" not in body, (
        "_load_defaults 写 store 的话，每次启动都会把主人存的设置刷成默认"
    )


# ---------- 工具解锁窗口：模态 + 顶层 + 类型独特 ----------

def test_tool_unlock_window_type_and_modality():
    """工具解锁窗口: Tool 类型 + StaysOnTopHint + ApplicationModal。

    主人要求：打开后其他窗口点不动（modal）、固定在最上面（StaysOnTopHint）、
    类型与其他窗口不一样（Tool = 浮动工具窗，不在任务栏）。
    """
    src = _src("app/ui/chat_window/tool_unlock_dialog.py")
    assert "Qt.WindowType.Tool" in src, "没设 Tool 类型"
    assert "WindowStaysOnTopHint" in src, "没设 WindowStaysOnTopHint"
    assert "ApplicationModal" in src, "没设 ApplicationModal（其他窗口能抢焦点）"
    # 注意顺序: setWindowFlags 必须在 setWindowModality 之前（否则 flags 清掉 modality）
    flag_pos = src.index("setWindowFlags")
    mod_pos = src.index("setWindowModality")
    assert flag_pos < mod_pos, (
        "setWindowFlags 必须在 setWindowModality 之前 —— "
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


# ---------- 工具解锁窗口顶部紧凑（不再标题/副标题/图例 3 行）----------

def test_tool_unlock_compact_header():
    """主人反馈顶部太空：把标题/副标题/图例压成两行, 进度文本+条+hint 合并一行。"""
    src = _src("app/ui/chat_window/tool_unlock_dialog.py")
    # 顶部第一行: 标题 + 副标题列 (左) | 风险图例 (右), 用 head_row QHBoxLayout 合并
    assert "head_row" in src, "工具解锁顶部没用 head_row 紧凑布局"
    # 进度条嵌进 prog_row (不再单独一行), 减少垂直空间
    assert "prog_row.addWidget(bar_outer" in src, "进度条没嵌进进度行"
    # 副标题不再单独 root.addWidget 摆一行
    assert "root.addWidget(subtitle)" not in src, "副标题还在单独占一行"


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


def test_settings_window_has_avatar_picker_entry():
    """设置面板「模型配置」tab 有头像入口: 按钮 + signal + 路径显示。"""
    src = _src("app/ui/settings_window.py")
    assert "bot_avatar_changed" in src, (
        "缺 bot_avatar_changed signal —— 选了头像后主程序没法知道"
    )
    assert "_open_avatar_picker" in src, "设置面板缺头像按钮入口"
    assert "选择头像" in src
    # ui_controller 必须接这个 signal
    assert "_on_bot_avatar_changed" in _src("app/ui/ui_controller.py")
    assert "bot_avatar_changed.connect" in _src("app/ui/ui_controller.py")


# ---------- TTS 行显隐：真控件验证 ----------

@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    """settings.json 隔离到临时目录 —— 真机上存的值不该影响断言。"""
    import app.ui.settings_window as sw_mod
    from app.core.settings_store import SettingsStore

    monkeypatch.setattr(
        sw_mod, "SettingsStore",
        lambda path="settings.json": SettingsStore(tmp_path / "settings.json"))


def _tts_rows(qapp, engine: str) -> dict:
    """建设置面板切到某引擎，返回各行的可见状态。"""
    from app.core.qt_compat import Qt
    from app.ui.settings_window import SettingsWindow

    sw = SettingsWindow(parent=None, char_cfg=None, renderer=None)
    idx = sw.cmb_tts_engine.findData(engine)
    if idx >= 0:
        sw.cmb_tts_engine.setCurrentIndex(idx)
    qapp.processEvents()

    def shown(row):
        if row is None:
            return False
        return not row.testAttribute(Qt.WidgetAttribute.WA_WState_Hidden)

    out = {
        "edge_voice": shown(getattr(sw, "_row_edge_voice", None)),
        "default_voice": shown(getattr(sw, "_row_default_voice", None)),
        "minimax": shown(getattr(sw, "_row_minimax", None)),
        "gsv_url": shown(getattr(sw, "_row_gsv_url", None)),
        "gsv_ref": shown(getattr(sw, "_row_gsv_ref", None)),
        "gsv_prompt": shown(getattr(sw, "_row_gsv_prompt", None)),
    }
    sw.deleteLater()
    return out


def test_tts_rows_switch_with_engine(qapp, isolated_store):
    """切引擎时, 只有该引擎认的字段留在面板上。"""
    edge = _tts_rows(qapp, "edge")
    assert edge["edge_voice"] is True, "edge 引擎下该显示音色下拉"
    assert edge["gsv_ref"] is False, "edge 引擎下不该显示 GPT-SoVITS 的参考音频"

    gsv = _tts_rows(qapp, "gptsovits")
    assert gsv["gsv_ref"] is True, "gptsovits 引擎下该显示参考音频"
    assert gsv["gsv_prompt"] is True
    assert gsv["edge_voice"] is False, "gptsovits 下不该显示 edge 音色下拉"
    assert gsv["minimax"] is False, "gptsovits 下不该显示 minimax 的 voice_id"

    mm = _tts_rows(qapp, "minimax")
    assert mm["minimax"] is True, "minimax 引擎下该显示 voice_id"
    assert mm["gsv_url"] is False
