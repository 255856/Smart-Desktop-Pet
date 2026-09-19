"""Live2D 模型映射配置（profile）单元测试。

不依赖 QWebEngine 渲染；真实模型目录存在时额外做真实解析校验。
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import sys
sys.path.insert(0, r'E:\study\desktop-pet')
sys.path.insert(0, r'E:\study\desktop-pet\.local-packages')

import json
from pathlib import Path

import pytest

from app.animation.live2d_model_profile import (
    Category,
    Live2DModelProfile,
    load_model_profile,
)

_BINGTANG_DIR = Path(r"E:\study\live2d\bingtang\bingtang")
_CHAOPIN_DIR = Path(r"E:\study\live2d\超频猫猫完整版\超频猫猫")

requires_bingtang = pytest.mark.skipif(
    not _BINGTANG_DIR.is_dir(), reason=f"冰糖模型目录不存在: {_BINGTANG_DIR}")
requires_chaopin = pytest.mark.skipif(
    not _CHAOPIN_DIR.is_dir(), reason=f"超频猫猫模型目录不存在: {_CHAOPIN_DIR}")


# ---------- 合成模型目录（不依赖真实模型） ----------
def _make_fake_model(tmp_path: Path) -> Path:
    """构造一个最小的"超频猫猫式"模型目录：Expressions/类别 名称.exp3.json。"""
    model_dir = tmp_path / "fake_model"
    model_dir.mkdir()
    (model_dir / "fake.model3.json").write_text(json.dumps({
        "Version": 3,
        "FileReferences": {
            "Moc": "fake.moc3",
            "Textures": [],
        },
    }), encoding="utf-8")
    (model_dir / "fake.moc3").write_bytes(b"")
    expr_dir = model_dir / "Expressions"
    expr_dir.mkdir()
    exps = {
        "表情 心心眼.exp3.json": [{"Id": "Key6", "Value": 1.0, "Blend": "Add"}],
        "表情 生气脸.exp3.json": [{"Id": "Key15", "Value": 1.0, "Blend": "Add"}],
        "发型 短发.exp3.json": [{"Id": "Key36", "Value": 0.0, "Blend": "Add"}],
        "发型 双马尾.exp3.json": [{"Id": "Key36", "Value": 1.0, "Blend": "Add"}],
        "配件 外套.exp3.json": [{"Id": "Key42", "Value": 1.0, "Blend": "Add"}],
        "特殊 水印.exp3.json": [{"Id": "Key1", "Value": 1.0, "Blend": "Add"}],
    }
    for fname, params in exps.items():
        (expr_dir / fname).write_text(json.dumps({
            "Type": "Live2D Expression", "Parameters": params,
        }, ensure_ascii=False), encoding="utf-8")
    (model_dir / "fake.model.yaml").write_text("""
name: fake
categories:
  - {id: special, label: 特殊, source: 特殊, mode: toggle}
  - {id: hair, label: 发型, source: 发型, mode: exclusive, hairstyle: true}
  - {id: gear, label: 配件, source: 配件, mode: toggle}
  - {id: face, label: 表情, source: 表情, mode: exclusive, emotion: true}
watermark:
  mode: param
  param: Key1
  safe_value: 0
triggers:
  emotions: {happy: 心心眼, angry: 生气脸}
  actions: {jump: {use: 外套, hold_ms: 1200}}
""", encoding="utf-8")
    return model_dir


@pytest.fixture()
def fake_model(tmp_path):
    return _make_fake_model(tmp_path)


# ---------- 基础解析 ----------
def test_load_profile_from_yaml(fake_model):
    profile = load_model_profile(fake_model)
    assert profile.name == "fake"
    assert [c.id for c in profile.categories] == ["special", "hair", "gear", "face"]


def test_filename_prefix_classification(fake_model):
    """exp3 文件名"类别 名称"前缀决定分组。"""
    profile = load_model_profile(fake_model)
    face = profile.emotion_category()
    assert face is not None and face.label == "表情"
    assert {it.name for it in face.items} == {"心心眼", "生气脸"}
    hair = profile.hairstyle_category()
    assert hair is not None and {it.name for it in hair.items} == {"短发", "双马尾"}


def test_watermark_item_excluded(fake_model):
    """水印条目（watermark.param 命中）不进入任何菜单。"""
    profile = load_model_profile(fake_model)
    assert profile.find_item("水印") is None
    for c in profile.categories:
        assert all(it.name != "水印" for it in c.items)


def test_emotion_alias_resolution(fake_model):
    profile = load_model_profile(fake_model)
    assert profile.emotion_aliases["happy"] == "心心眼"
    assert profile.emotion_options()[0] == ("natural", "自然表情")


def test_random_pool_defaults_to_emotion_items(fake_model):
    profile = load_model_profile(fake_model)
    profile.random_pool = []
    assert set(profile.random_pool_items()) == {"心心眼", "生气脸"}


# ---------- 无 YAML 的模型（启发式） ----------
def test_load_profile_without_yaml(fake_model):
    """删掉 YAML 也能按文件名启发式解析（自动建组）。"""
    (fake_model / "fake.model.yaml").unlink()
    profile = load_model_profile(fake_model)
    names = {it.name for c in profile.categories for it in c.items}
    assert {"心心眼", "外套", "双马尾"} <= names


# ---------- 真实模型：超频猫猫 ----------
@requires_chaopin
def test_chaopin_profile_full():
    """超频猫猫：五大类 48 条目、触发映射、睡眠参数、动作注入数据。"""
    profile = load_model_profile(_CHAOPIN_DIR)
    labels = {c.label: len(c.items) for c in profile.categories}
    assert labels == {"特殊": 4, "发型": 6, "配件": 10, "手势": 15, "表情": 17}
    # 水印被剔除（特殊类只剩 飞头/飞头（无脖）/前倾/禁止面捕前倾）
    assert profile.find_item("水印") is None
    # 情绪别名
    cat = profile.emotion_category()
    assert cat.item("心心眼") is not None
    # 睡眠与动作
    assert profile.sleep_params == {"Param216": 1.0, "Param218": 1.0}
    assert profile.idle_motion_group == "Idle"
    assert profile.sleep_motion_group == "Sleep"
    # 随机池
    assert profile.random_pool_items()
    # 表情注册表（注入 model3.json 用）
    defs = profile.expression_defs()
    assert len(defs) == 4 + 6 + 10 + 15 + 17
    assert all(d["Name"] and d["File"] for d in defs)


@requires_chaopin
def test_chaopin_settings_payload_injection():
    """model3.json 动态注入：Expressions + Motions，原文件不动。"""
    from app.animation.live2d_renderer import _build_settings_payload
    payload = _build_settings_payload(_CHAOPIN_DIR, "超频猫猫.model3.json",
                                      load_model_profile(_CHAOPIN_DIR))
    data = json.loads(payload.decode("utf-8"))
    fr = data["FileReferences"]
    exprs = fr.get("Expressions", [])
    assert len(exprs) == 52
    motions = fr.get("Motions", {})
    assert "Idle" in motions and "Sleep" in motions
    assert (Path(r"E:\study\live2d\超频猫猫完整版\超频猫猫")
            / motions["Idle"][0]["File"]).is_file()
    # 原文件不被修改
    raw = json.loads((_CHAOPIN_DIR / "超频猫猫.model3.json").read_text(encoding="utf-8"))
    assert "Expressions" not in raw["FileReferences"]
    assert "Motions" not in raw["FileReferences"]


# ---------- 真实模型：冰糖（回归保护） ----------
@requires_bingtang
def test_bingtang_profile_regression():
    """冰糖：model3 模式解析 + 水印剔除 + 发型/情绪分组不回退。"""
    profile = load_model_profile(_BINGTANG_DIR)
    face = profile.emotion_category()
    hair = profile.hairstyle_category()
    assert face is not None and hair is not None
    assert {it.name for it in face.items} == {
        "hah", "angery", "black", "red", "meimao1", "meimao2", "O O"}
    assert {it.name for it in hair.items} == {
        "HAIR11", "HAIR22", "HAIR33", "hairR1", "HAIR2", "HAIR3",
        "Rhair", "danhair", "danhairL"}
    # 水印表情被剔除、Paramheadxy* 被剔除
    assert profile.find_item("expression1") is None
    for it in face.items:
        assert not any(p.startswith("Paramheadxy") for p in it.params)
    # 触发映射
    assert profile.emotion_aliases["happy"] == "hah"
    assert profile.watermark_mode == "drawable"
    # 内联动作与睡眠表情保留
    assert profile.actions["tongue"].params[0]["Id"] == "Paramtongueout"
    assert any(p["Id"] == "ParamEyeLOpen" for p in profile.sleep_expr_params)
    assert profile.idle_motion_group == ""  # 冰糖无动作文件
