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
