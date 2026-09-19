"""Live2D renderer 单元测试（不依赖 PyQtWebEngine 实际加载）。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
sys.path.insert(0, r'E:\study\desktop-pet')
sys.path.insert(0, r'E:\study\desktop-pet\.local-packages')

import tempfile
from pathlib import Path

import pytest


# ---------- 抽象基类：每个 abstract 方法都必须被覆盖 ----------
def test_pet_renderer_is_abstract():
    """PetRenderer 不能直接实例化（abstract class）。"""
    from app.animation.pet_renderer import PetRenderer
    with pytest.raises(TypeError):
        PetRenderer()


def test_pet_renderer_abstract_methods_count():
    """所有 abstract 方法数量 == 18。"""
    from app.animation.pet_renderer import PetRenderer
    assert len(PetRenderer.__abstractmethods__) == 18, (
        f"PetRenderer abstract 方法数变了：{sorted(PetRenderer.__abstractmethods__)}"
    )


def test_pet_renderer_compat_defaults():
    """基类默认实现：set_lock_first_idle / is_lock_first_idle / is_sleeping 都有 no-op 默认值。"""
    from app.animation.pet_renderer import PetRenderer
    # 用 _AbstractImpl 跳过 abstract 检查
    class _Stub(PetRenderer):
        def set_idle(self): pass
        def set_sleep(self): pass
        def set_wake(self): pass
        def set_thinking(self): pass
        def set_emotion(self, n): pass
        def play_reaction(self, w): pass
        def play_animation(self, n): pass
        def play_eat(self): pass
        def play_file(self): pass
        def play_spin(self): pass
        def play_stretch(self): pass
        def play_jump(self): pass
        def play_swim(self): pass
        def start_drag(self): pass
        def end_drag(self): pass
        def set_walk(self, d): pass
        def get_widget(self): return None
        def shutdown(self): pass
    s = _Stub()
    # 调用所有默认实现都不抛
    s.is_sleeping()
    s.is_lock_first_idle()
    s.set_lock_first_idle(True)
    s.set_lock_first_idle(False)
    s.set_parameter('p', 0.5)
    s.set_expression('e')
    s.set_emotion_reaction('happy')
    s.supports_expression_listing()
    s.list_expressions()


# ---------- sprite_renderer 实现完整性 ----------
def test_sprite_renderer_implements_all_abstract():
    from app.animation.sprite_renderer import SpriteRenderer
    from app.animation.pet_renderer import PetRenderer
    abstract = PetRenderer.__abstractmethods__
    missing = [m for m in abstract if not hasattr(SpriteRenderer, m)]
    assert missing == [], f"SpriteRenderer 缺方法：{missing}"


# ---------- Live2DRenderer 实现完整性 ----------
def test_live2d_renderer_implements_all_abstract():
    from app.animation.live2d_renderer import Live2DRenderer
    from app.animation.pet_renderer import PetRenderer
    abstract = PetRenderer.__abstractmethods__
    missing = [m for m in abstract if not hasattr(Live2DRenderer, m)]
    assert missing == [], f"Live2DRenderer 缺方法：{missing}"


# ---------- 工厂 ----------
def test_factory_sprite_mode(qapp):
    """Sprite 工厂：返回 SpriteRenderer 实例。"""
    from app.animation.renderer_factory import create_renderer
    with tempfile.TemporaryDirectory() as tmp:
        sprite_dir = Path(tmp)
        fallback = sprite_dir / "fallback.png"
        fallback.touch()
        r = create_renderer(
            "sprite",
            sprite_dir=sprite_dir,
            fallback_image=fallback,
            window_size=qapp.primaryScreen().size() if qapp.primaryScreen() else type("Q", (), {"width": lambda: 100, "height": lambda: 100})(),
        )
        assert r.__class__.__name__ == "SpriteRenderer"
        r.shutdown()


def test_factory_live2d_no_pyqtwebengine_falls_back(monkeypatch, qapp):
    """PyQtWebEngine 没装时 → fallback sprite。"""
    import app.animation.live2d_renderer as lr_mod
    monkeypatch.setattr(lr_mod, "_WEB_ENGINE_AVAILABLE", False)
    import importlib, app.animation.renderer_factory as rf
    importlib.reload(rf)

    from app.animation.renderer_factory import create_renderer
    with tempfile.TemporaryDirectory() as tmp:
        sprite_dir = Path(tmp)
        fallback = sprite_dir / "fallback.png"
        fallback.touch()
        screen_size = qapp.primaryScreen().size() if qapp.primaryScreen() else type("Q", (), {"width": lambda: 100, "height": lambda: 100})()
        r = create_renderer(
            "live2d",
            sprite_dir=sprite_dir,
            fallback_image=fallback,
            window_size=screen_size,
            live2d_model_dir=Path(tmp) / "no_such_model",
        )
        assert r.__class__.__name__ == "SpriteRenderer"
        r.shutdown()


def test_factory_live2d_no_model_json_falls_back(qapp):
    """live2d_model_dir 存在但没 model3.json → fallback sprite。"""
    from app.animation.renderer_factory import create_renderer
    with tempfile.TemporaryDirectory() as tmp:
        sprite_dir = Path(tmp) / "sprite"
        sprite_dir.mkdir()
        fallback = sprite_dir / "fallback.png"
        fallback.touch()
        model_dir = Path(tmp) / "model"
        model_dir.mkdir()
        screen_size = qapp.primaryScreen().size() if qapp.primaryScreen() else type("Q", (), {"width": lambda: 100, "height": lambda: 100})()
        r = create_renderer(
            "live2d",
            sprite_dir=sprite_dir,
            fallback_image=fallback,
            window_size=screen_size,
            live2d_model_dir=model_dir,
        )
        assert r.__class__.__name__ == "SpriteRenderer"
        r.shutdown()


# ---------- Live2D 模型 profile 动作映射完整性 ----------
_BINGTANG_DIR = Path(r"E:\study\live2d\bingtang\bingtang")
_CHAOPIN_DIR = Path(r"E:\study\live2d\超频猫猫完整版\超频猫猫")

requires_bingtang = pytest.mark.skipif(
    not _BINGTANG_DIR.is_dir(), reason=f"冰糖模型目录不存在: {_BINGTANG_DIR}")
requires_chaopin = pytest.mark.skipif(
    not _CHAOPIN_DIR.is_dir(), reason=f"超频猫猫模型目录不存在: {_CHAOPIN_DIR}")


def _profile_actions(model_dir: Path):
    from app.animation.live2d_model_profile import load_model_profile
    return load_model_profile(model_dir).actions


@requires_bingtang
def test_bingtang_action_map_coverage():
    """冰糖 profile 的动作映射应覆盖所有 play_animation 期望的关键字。"""
    actions = _profile_actions(_BINGTANG_DIR)
    expected = {"jump", "stretch", "spin", "swim", "eat", "file", "think",
                "tongue", "cheek"}
    assert expected.issubset(actions.keys()), (
        f"冰糖 actions 缺关键动作：{expected - set(actions.keys())}"
    )


@requires_bingtang
def test_bingtang_action_map_format():
    """动作映射每项按 kind 区分：pose 需 param/value，expr 需非空 params[{Id,Value,Blend}]。"""
    from app.animation.live2d_model_profile import load_model_profile
    profile = load_model_profile(_BINGTANG_DIR)
    for name, spec in profile.actions.items():
        assert spec.duration_ms > 0, f"actions[{name!r}] 缺 duration_ms"
        if spec.kind == "expr":
            assert spec.params, f"actions[{name!r}] 表情类缺非空 params"
            for p in spec.params:
                assert {"Id", "Value", "Blend"} <= set(p), (
                    f"actions[{name!r}] 表情参数格式错：{p}"
                )
                assert p["Blend"] in ("Add", "Multiply", "Overwrite"), (
                    f"actions[{name!r}] 非法 Blend：{p['Blend']}"
                )
        elif spec.kind == "pose":
            assert spec.param, f"actions[{name!r}] 姿态类缺 param"
        else:
            assert spec.kind == "item", f"actions[{name!r}] 未知 kind：{spec.kind}"
            assert profile.find_item(spec.item) is not None, (
                f"actions[{name!r}] 引用的条目不存在：{spec.item}"
            )


@requires_chaopin
def test_chaopin_action_map_format():
    """超频猫猫 profile：动作映射全部是有效条目引用。"""
    from app.animation.live2d_model_profile import load_model_profile
    profile = load_model_profile(_CHAOPIN_DIR)
    for name, spec in profile.actions.items():
        assert spec.kind == "item", f"超频猫猫 actions[{name!r}] 应引用条目"
        assert profile.find_item(spec.item) is not None, (
            f"超频猫猫 actions[{name!r}] 引用的条目不存在：{spec.item}"
        )


# ---------- Live2D SDK 文件已下载 ----------
def test_cubism_sdk_files_exist():
    """PixiJS + Cubism 4 Core + pixi-live2d-display 插件已下载到项目本地。"""
    sdk_dir = Path(r'E:\study\desktop-pet\app\animation\cubism-sdk')
    pixi = sdk_dir / 'pixi.min.js'
    core = sdk_dir / 'live2dcubismcore.min.js'
    plugin = sdk_dir / 'cubism4.min.js'
    assert pixi.exists(), f"缺失: {pixi}"
    assert core.exists(), f"缺失: {core}"
    assert plugin.exists(), f"缺失: {plugin}"
    assert pixi.stat().st_size > 100_000, f"pixi.min.js 太小: {pixi.stat().st_size}"
    assert core.stat().st_size > 100_000, f"live2dcubismcore.min.js 太小: {core.stat().st_size}"
    assert plugin.stat().st_size > 50_000, f"cubism4.min.js 太小: {plugin.stat().st_size}"


# ---------- Live2D bridge HTML 关键加载逻辑 ----------
def test_bridge_html_uses_cubism4_stack():
    """bridge 必须加载官方 Cubism4 Core + 纯 cubism4 插件，并处理 process polyfill。"""
    bridge = Path(r'E:\study\desktop-pet\app\animation\live2d_bridge.html')
    content = bridge.read_text(encoding='utf-8')
    assert 'live2dcubismcore.min.js' in content
    assert 'cubism4.min.js' in content
    assert 'window.process' in content  # UMD 包需要 process.env.NODE_ENV
    # 不应再引用会因缺 Cubism2 Core 而顶层 throw 的 cubism2x4 合并版
    assert 'src="cubism-sdk/live2d.min.js"' not in content
    # PIXI v7 不能把 div 当 view 传入（getContext 报错）
    assert 'view: document.getElementById' not in content


# ---------- Live2D bridge HTML 存在 ----------
def test_bridge_html_exists():
    bridge = Path(r'E:\study\desktop-pet\app\animation\live2d_bridge.html')
    assert bridge.exists()
    content = bridge.read_text(encoding='utf-8')
    assert "loadModel" in content
    assert "setExpression" in content
    assert "setParam" in content
    assert "playCustomExpression" in content, "缺少自定义表情注册/播放（吐舌/鼓腮/思考/睡觉）"
    assert "resetExpressionState" in content, "缺少恢复自然表情"
    assert "hideWatermark" in content, "缺少 hideWatermark() 处理冰糖水印"
    # 新方案在 drawMesh 主 pass 跳过水印 drawable（含伪装成 ArtMesh 的烘焙文字层）
    assert "drawMesh" in content, "缺少 drawMesh 拦截水印"
    assert "WaterMark" in content, "缺少按名字识别版权卡片水印"
    assert "694" in content, "缺少冰糖模型指纹校验，换模型时可能误杀正常部件"


# ---------- 兼容旧 PetAnimator 接口 ----------
def test_sprite_renderer_compat_methods(qapp):
    """SpriteRenderer 必须实现所有 PetAnimator 旧 API（设置面板 / UIController 还用）。"""
    import tempfile
    from pathlib import Path
    from app.animation.sprite_renderer import SpriteRenderer

    class FakeQSize:
        """让 QLabel.setFixedSize(w, h) 接受（width, height）"""
        def width(self): return 100
        def height(self): return 100

    with tempfile.TemporaryDirectory() as tmp:
        sprite_dir = Path(tmp)
        fallback = sprite_dir / 'fb.png'
        fallback.touch()
        r = SpriteRenderer(
            sprite_dir=sprite_dir,
            fallback_image=fallback,
            window_size=FakeQSize(),
        )
        # 必须存在（UIController.sw.lock_first_idle_changed.connect(self.pet.animator.set_lock_first_idle)）
        assert hasattr(r, 'set_lock_first_idle'), "缺少 set_lock_first_idle（旧代码依赖）"
        assert callable(r.set_lock_first_idle)
        # 调用不应抛异常
        r.set_lock_first_idle(True)
        r.set_lock_first_idle(False)
        # main.py state_tick 用 is_sleeping() 跳过衰减
        assert hasattr(r, 'is_sleeping'), "缺少 is_sleeping（旧代码依赖）"
        assert r.is_sleeping() is False
        # 设置面板 lock_first_idle 查询
        assert hasattr(r, 'is_lock_first_idle')
        assert r.is_lock_first_idle() is False
        r.shutdown()


def test_pet_renderer_state_query_default():
    """PetRenderer.is_sleeping / is_lock_first_idle 默认实现（子类未重写时返回 False）。"""
    from app.animation.pet_renderer import PetRenderer
    # 直接构造实例需要实现所有 abstract — 用 mixin 验证默认值
    from app.animation.sprite_renderer import SpriteRenderer
    import tempfile
    from pathlib import Path

    class FakeQSize:
        def width(self): return 100
        def height(self): return 100

    with tempfile.TemporaryDirectory() as tmp:
        sprite_dir = Path(tmp)
        fallback = sprite_dir / 'fb.png'
        fallback.touch()
        r = SpriteRenderer(
            sprite_dir=sprite_dir,
            fallback_image=fallback,
            window_size=FakeQSize(),
        )
        # 默认值
        assert r.is_sleeping() is False
        assert r.is_lock_first_idle() is False
        r.shutdown()