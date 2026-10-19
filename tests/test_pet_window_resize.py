"""桌宠窗口桌面式缩放守卫。

背景：桌宠窗口之前是 setFixedSize 的固定窗口，想改大小只能去设置面板拖滑块。
现在改成跟普通桌面应用一样，拖窗口边缘/四角直接调整大小
（复用设置窗口已在用的 FramelessResizer）。
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
    return {n.attr for n in _ast.walk(node) if isinstance(n, _ast.Attribute)}


def test_pet_window_attaches_frameless_resizer():
    """桌宠窗口必须挂 FramelessResizer（跟设置窗口同一套边缘缩放）。"""
    node = _func_node("app/ui/pet_window.py", "_build_window")
    assert node is not None
    body = _ast.dump(node)
    assert "FramelessResizer" in body, "桌宠窗口没挂 FramelessResizer"
    assert "freeze_translucent" in body, (
        "桌宠窗口是透明的，FramelessResizer 必须传 freeze_translucent=False，"
        "否则缩放期间整块变黑"
    )


def test_pet_window_no_longer_fixed_size():
    """桌宠窗口不能再 setFixedSize —— 固定尺寸就拖不动边缘了。"""
    src = _src("app/ui/pet_window.py")
    assert "setFixedSize" not in src, (
        "pet_window 还有 setFixedSize，窗口无法拖边缘缩放"
    )


def test_pet_window_min_size_and_resize_event():
    """可缩放窗口要有最小尺寸，并且 resizeEvent 里同步渲染器。"""
    src = _src("app/ui/pet_window.py")
    assert "setMinimumSize" in src, "缺最小尺寸约束"
    node = _func_node("app/ui/pet_window.py", "resizeEvent")
    assert node is not None, "缺 resizeEvent"
    body = _ast.dump(node)
    assert "_sync_display_size" in body, "resizeEvent 没走 _sync_display_size"


def test_sync_display_size_updates_renderer_and_signals():
    """_sync_display_size：同步渲染器画布 + 遮罩重建 + 对外发信号。"""
    node = _func_node("app/ui/pet_window.py", "_sync_display_size")
    assert node is not None
    body = _ast.dump(node)
    assert "set_size" in body, "没同步 renderer.set_size（live2d 画布不会重新 fit）"
    assert "_sample_base_mask" in body or "_resample_mask_after_resize" in body
    assert "display_size_changed" in body, "没发 display_size_changed（无法持久化）"
    assert "scale_changed" in body, "没发 scale_changed（设置面板缩放条不同步）"


def test_resize_grip_region_covers_four_corners():
    """四角缩放手柄：剪影遮罩会裁掉透明角，不留可点区就永远拖不到。"""
    node = _func_node("app/ui/pet_window.py", "_resize_grip_region")
    assert node is not None, "缺 _resize_grip_region"
    body = _ast.dump(node)
    assert body.count("QRegion") >= 4, "四角手柄区域数不对"


def test_frameless_resizer_translucent_switch():
    """FramelessResizer 支持 freeze_translucent=False（桌宠透明窗口用）。"""
    src = _src("app/ui/frameless.py")
    assert "freeze_translucent" in src
    node = _func_node("app/ui/frameless.py", "_freeze_heavy_paint")
    assert node is not None
    assert "_freeze_translucent" in _attrs(node), (
        "_freeze_heavy_paint 没尊重 freeze_translucent 开关"
    )


def test_frameless_resizer_public_helpers():
    """edge_at / is_resizing 供宿主窗口协作（别把缩放当拖动桌宠）。"""
    assert _func_node("app/ui/frameless.py", "edge_at") is not None
    assert _func_node("app/ui/frameless.py", "is_resizing") is not None


def test_pet_window_eventfilter_yields_to_resizer():
    """事件过滤器：落在边缘带内时让路给 FramelessResizer。"""
    node = _func_node("app/ui/pet_window.py", "eventFilter")
    assert node is not None
    body = _ast.dump(node)
    assert "is_resizing" in body, "eventFilter 没检查 resizer.is_resizing"
    assert "edge_at" in body, "eventFilter 没检查边缘命中"


def test_display_size_persisted_in_ui_controller():
    """用户拖出来的自由尺寸要落盘，重启还原。"""
    src = _src("app/ui/ui_controller.py")
    assert "display_size" in src, "display_size 没持久化"
    node = _func_node("app/ui/ui_controller.py", "_on_pet_display_size_changed")
    assert node is not None, "缺 _on_pet_display_size_changed"
    body = _ast.dump(node)
    assert "display_size" in body
    assert "set_display_size" in src, "启动时没恢复 display_size"
