"""Live2D 能力驱动显隐守卫（方案A）。

背景：不同 Live2D 模型能力差异很大（有没有表情包贴纸 / 水印 / 挂机随机 /
叠加组）。设置面板必须按 profile 能力显隐控件，否则会出现"勾了没反应"的死选项，
"换模型后界面不一样"这个问题也一直存在。
"""
import ast as _ast
from pathlib import Path

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


# ---------- 能力矩阵：UI 层（AST 守卫） ----------

def test_settings_window_gates_live2d_widgets_on_capabilities():
    """设置面板 Live2D Tab：三类控件必须按 caps 显隐，不能无条件摆进布局。"""
    node = _func_node("app/ui/settings_window.py", "_build_tab_live2d")
    assert node is not None, "_build_tab_live2d 缺失"
    attrs = _attrs(node)
    assert "capabilities" in attrs, "面板没读渲染器能力矩阵"
    for attr in ("has_random_expressions", "has_stickers", "has_watermark"):
        assert attr in attrs, f"面板没用 caps.{attr} 做显隐判断"
    # 控件仍然要创建（属性恒在，别处引用不能 AttributeError），
    # 但摆进布局必须走条件
    for name in ("cb_random_exp", "cb_random_sticker", "cb_hide_watermark"):
        assert name in attrs, f"{name} 没创建"


def test_settings_window_creates_widgets_regardless_of_capability():
    """控件对象恒创建 —— 只在 addWidget 层面条件化，属性不会缺失。"""
    node = _func_node("app/ui/settings_window.py", "_build_tab_live2d")
    for attr in ("has_random_expressions", "has_stickers", "has_watermark"):
        assert _guards_on_attr(node, attr), (
            f"caps.{attr} 没用在 if 条件里（控件会无条件显示）"
        )


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
