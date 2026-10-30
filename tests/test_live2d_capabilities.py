"""Live2D 能力驱动显隐守卫（方案A）。

背景：不同 Live2D 模型能力差异很大（有没有表情包贴纸 / 水印 / 挂机随机 /
叠加组）。设置面板必须按 profile 能力显隐控件，否则会出现"勾了没反应"的死选项，
"换模型后界面不一样"这个问题也一直存在。
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
        if isinstance(node, _ast.FunctionDef) and node.name == func_name:
            return node
    return None


def _attrs(node) -> set:
    """函数体里出现过的属性名集合。"""
    return {n.attr for n in _ast.walk(node) if isinstance(n, _ast.Attribute)}


def _names(node) -> set:
    """函数体里出现过的局部变量名集合。"""
    return {n.id for n in _ast.walk(node) if isinstance(n, _ast.Name)}


def _guards_on_attr(node, attr: str) -> bool:
    """是否存在 `if ... <attr> ...` 形式的条件判断。"""
    for sub in _ast.walk(node):
        if not isinstance(sub, _ast.If):
            continue
        for n in _ast.walk(sub.test):
            if isinstance(n, _ast.Attribute) and n.attr == attr:
                return True
    return False


def _guards_on_name(node, name: str) -> bool:
    """是否存在 `if <name>:` 形式的条件判断。"""
    for sub in _ast.walk(node):
        if not isinstance(sub, _ast.If):
            continue
        for n in _ast.walk(sub.test):
            if isinstance(n, _ast.Name) and n.id == name:
                return True
    return False


def _setvisible_with_attr(node, attr: str) -> bool:
    """是否存在 `xxx.setVisible(... caps.<attr> ...)` 形式的显隐控制。"""
    for sub in _ast.walk(node):
        if not isinstance(sub, _ast.Call):
            continue
        if not (isinstance(sub.func, _ast.Attribute)
                and sub.func.attr == "setVisible"):
            continue
        args = list(sub.args) + [k.value for k in sub.keywords]
        for a in args:
            for n in _ast.walk(a):
                if isinstance(n, _ast.Attribute) and n.attr == attr:
                    return True
    return False


# ---------- 能力矩阵：数据层 ----------

def test_model_capabilities_defaults():
    """ModelCapabilities 默认全 False（sprite 渲染器恒为空能力）。"""
    from app.animation.pet_renderer import ModelCapabilities
    caps = ModelCapabilities()
    assert caps.has_emotion_group is False
    assert caps.has_hairstyle_group is False
    assert caps.has_toggle_groups is False
    assert caps.toggle_group_count == 0
    assert caps.has_random_expressions is False
    assert caps.has_stickers is False
    assert caps.has_watermark is False
    assert caps.has_actions is False


def test_pet_renderer_base_capabilities_empty():
    """基类 PetRenderer.capabilities() 返回空能力，不抛异常。"""
    from app.animation.pet_renderer import PetRenderer

    class _Fake(PetRenderer):
        def set_idle(self): ...
        def set_sleep(self): ...
        def set_wake(self): ...
        def set_thinking(self): ...
        def set_emotion(self, name): ...
        def play_reaction(self, where): ...
        def play_animation(self, anim_name): ...
        def play_eat(self): ...
        def play_file(self): ...
        def play_spin(self): ...
        def play_stretch(self): ...
        def play_jump(self): ...
        def play_swim(self): ...
        def start_drag(self): ...
        def end_drag(self): ...
        def set_walk(self, direction): ...
        def get_widget(self): return None
        def shutdown(self): ...

    caps = _Fake().capabilities()
    assert caps.has_stickers is False
    assert caps.has_random_expressions is False


def test_profile_capabilities_reflects_profile_flags():
    """profile.capabilities() 按 profile 自身声明算能力矩阵。"""
    from pathlib import Path as _P

    from app.animation.live2d_model_profile import (
        Category, Live2DModelProfile, MenuItem,
    )

    prof = Live2DModelProfile(model_dir=_P("."))
    prof.categories = [
        Category(id="hair", label="发型", mode="exclusive", hairstyle=True,
                 items=[MenuItem(name="a", label="A", category="hair")]),
        Category(id="face", label="表情", mode="exclusive", emotion=True,
                 items=[MenuItem(name="b", label="B", category="face")]),
        Category(id="gear", label="配件", mode="toggle",
                 items=[MenuItem(name="c", label="C", category="gear")]),
    ]
    prof.random_enabled = True
    prof.stickers_dir = "../贴纸"
    prof.watermark_mode = "part"
    prof.actions = {"jump": object()}

    caps = prof.capabilities()
    assert caps.has_emotion_group is True
    assert caps.has_hairstyle_group is True
    assert caps.has_toggle_groups is True
    assert caps.toggle_group_count == 1
    assert caps.has_random_expressions is True
    assert caps.has_stickers is True
    assert caps.has_watermark is True
    assert caps.has_actions is True


def test_profile_capabilities_minimal_model_all_false():
    """最小模型（只有表情组、没贴纸没水印没随机）：能力全 False。"""
    from pathlib import Path as _P

    from app.animation.live2d_model_profile import (
        Category, Live2DModelProfile, MenuItem,
    )

    prof = Live2DModelProfile(model_dir=_P("."))
    prof.categories = [
        Category(id="face", label="表情", mode="exclusive", emotion=True,
                 items=[MenuItem(name="b", label="B", category="face")]),
    ]
    caps = prof.capabilities()
    assert caps.has_emotion_group is True
    assert caps.has_stickers is False
    assert caps.has_watermark is False
    assert caps.has_random_expressions is False
    assert caps.has_actions is False


def test_renderer_capabilities_drops_missing_sticker_dir():
    """渲染层再校验贴纸目录：目录不存在/没图 → has_stickers 必须降为 False。

    profile 只声明目录名，真实有效性要渲染层查文件系统 —— 否则面板会显示
    贴纸控件，用户调半天什么也不弹。
    """
    node = _func_node("app/animation/live2d_renderer.py", "capabilities")
    assert node is not None, "Live2DRenderer.capabilities() 缺失"
    body = _ast.dump(node)
    assert "get_sticker_config" in body, (
        "Live2DRenderer.capabilities() 没校验贴纸目录是否真实可用"
    )
    assert "has_stickers" in body


# ---------- 能力矩阵：设置项分流 ----------

def test_settings_only_offer_live2d_options_when_capable():
    """Live2D 专属的设置项要按渲染器能力给出去，不能无条件摆出来。"""
    node = _func_node("app/ui/ui_controller.py", "_control_appearance")
    assert node is not None, "_control_appearance 缺失"
    attrs = _attrs(node)
    assert "capabilities" in attrs or "get_menu_groups" in attrs, (
        "控制台没读渲染器能力矩阵"
    )
    # sprite 渲染器没有分类外观 → 控制台该显示「用右键菜单切」的提示
    src = _src("app/ui/ui_controller.py")
    assert "available" in src, "控制台没区分「有没有外观可切」"


def test_settings_sprite_only_options_are_gated():
    """淡入淡出 / 帧时长只对 sprite 有效 —— Live2D 下点了没反应。"""
    node = _func_node("app/ui/ui_controller.py", "_apply_settings_changed")
    assert node is not None
    src = _src("app/ui/ui_controller.py")
    # 这些设置项在应用时要先问渲染器类型
    for name in ("_on_crossfade_changed", "_override_frame_ms_now"):
        fn = _func_node("app/ui/ui_controller.py", name)
        assert fn is not None, f"缺 {name}"
        assert "_is_sprite" in _ast.dump(fn), f"{name} 没先判是不是 sprite"


def test_scene_dialog_optional_combo_rows():
    """场景弹窗：没有表情组/发型组时，对应下拉整行不出现。"""
    node = _func_node("app/ui/live2d_scene_dialog.py", "_build_appearance_form")
    assert node is not None
    names = _names(node)
    assert "emotion_items" in names
    assert "hair_items" in names
    assert _guards_on_name(node, "emotion_items"), (
        "表情下拉还是无条件创建 —— 模型没表情组时会多一行空控件"
    )
    assert _guards_on_name(node, "hair_items"), (
        "发型下拉还是无条件创建 —— 模型没发型组时会多一行空控件"
    )


# ---------- 真控件验证（offscreen 建真窗口, 不只靠 AST 断言） ----------

class _FakeRenderer:
    """假的 Live2D 渲染器：只提供控制服务要用的接口。"""

    def __init__(self, caps, rtype="live2d"):
        self._caps = caps
        self._rtype = rtype

    def capabilities(self):
        return self._caps

    def get_menu_groups(self):
        return [{"id": "face", "label": "表情", "kind": "emotion",
                 "mode": "exclusive", "items": [("a", "开心")]}]

    def get_active_items(self):
        return set()

    def activate_menu_item(self, group_id, item_id, toggle_off=True):
        return None

    def reset_all_appearance(self):
        return None

    def get_renderer_type(self):
        return self._rtype

    def get_emotion_options(self):
        return [("happy", "开心")]

    def supports_hairstyles(self):
        return False

    def get_hairstyle_options(self):
        return []

    def is_random_expressions_enabled(self):
        return self._caps.has_random_expressions

    def get_scene_bundle(self, sid):
        from app.animation.live2d_scene import SceneBundle
        return SceneBundle()

    def get_custom_actions(self):
        return []

    def list_expressions(self):
        return []

    def scene_is_overridden(self, sid):
        # live2d_config.scene_configs() 会问渲染器「这个场景被主人改过没有」。
        return False


def _appearance(caps, rtype="live2d"):
    """跑一遍控制服务的「外观」handler，返回它报出来的内容。"""
    from app.animation.pet_renderer import ModelCapabilities  # noqa: F401
    from app.ui.ui_controller import UIController

    class _Ctrl(UIController):
        def __init__(self):  # noqa: D107 - 只借方法，不跑 Qt 初始化
            self.pet = type("_P", (), {"renderer": _FakeRenderer(caps, rtype)})()
            # _control_appearance 现在还会调 _live2d_models()，
            # 它读 self.cfg.pet.live2d.model_dir —— 补一个最小的 cfg，
            # 免得夹具因为「少一个属性」而不是因为功能行为失败。
            self.cfg = type("_C", (), {
                "pet": type("_P2", (), {
                    "live2d": type("_L", (), {"model_dir": ""})(),
                })(),
            })()

    return _Ctrl()._control_appearance()


def test_appearance_reports_groups_for_live2d():
    """Live2D：控制台要能拿到分类外观。"""
    from app.animation.pet_renderer import ModelCapabilities

    caps = ModelCapabilities(has_emotion_group=True, has_watermark=True)
    out = _appearance(caps, rtype="live2d")
    assert out["available"] is True
    assert out["groups"], "Live2D 没给出分类外观"
    assert out["renderer"] == "live2d"


def test_appearance_not_available_for_sprite():
    """sprite 没有分类外观 —— 控制台该说清楚，而不是给个空列表。"""
    from app.animation.pet_renderer import ModelCapabilities

    caps = ModelCapabilities()
    out = _appearance(caps, rtype="sprite")
    assert out["available"] is False
    assert out["groups"] == []
    assert out["renderer"] == "sprite"
