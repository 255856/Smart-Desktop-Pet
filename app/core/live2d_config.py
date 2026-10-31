# -*- coding: utf-8 -*-
"""Live2D 模型与动作配置（控制台的「Live2D」区用）。

每个模型一份配置（``data/live2d_scenes/<模型目录名>.json``）—— 换模型自动换配置，
不同模型的表情/发型/配件/手势条目本来就不同，这里只按当前模型给出可选项。

提供：
* **模型列表** —— 扫 ``assets/live2d`` 下含 ``*.model3.json`` 的目录
* **触发场景配置** —— 每个场景配一套外观（表情/发型/配件/手势 + 一次性时长）
* **自定义动作** —— 自己命名、绑钩子、配外观；钩子来自模型 profile 与工具动作
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from app.animation.live2d_scene import (
    BUILTIN_SCENES, GAME_SCENES, TOOL_ACTION_HOOKS, SceneBundle, SceneStore,
)

log = logging.getLogger(__name__)

#: 模型根目录（相对仓库根）
MODEL_ROOT = "assets/live2d"


def list_models(root: Path, current: str = "") -> list[dict]:
    """列出可用的 Live2D 模型。

    扫描模型根目录下所有含 ``*.model3.json`` 的目录（有的模型套了好几层）。
    路径给成**相对仓库根**的（跟 config.yaml 里 pet.live2d.model_dir 一个写法）。
    """
    out = []
    base = Path(root)
    if not base.is_dir():
        return out
    repo = base.parent.parent      # assets/live2d → 仓库根
    try:
        for path in sorted(base.rglob("*.model3.json")):
            model_dir = path.parent
            try:
                rel = model_dir.relative_to(repo)
            except ValueError:
                rel = model_dir
            rel = str(rel).replace("\\", "/")
            # docs/demo 下是演示副本，不是正式模型
            if rel.startswith("docs/"):
                continue
            out.append({
                "path": rel,
                "name": model_dir.name,
                "current": rel == str(current).replace("\\", "/"),
            })
    except Exception:  # noqa: BLE001
        log.exception("扫描 Live2D 模型目录失败")
    return out


def scene_catalog() -> list[dict]:
    """可配置的触发场景清单（分组 + 类型 + 当前外观）。"""
    out = []
    for sid, title, kind, group in BUILTIN_SCENES:
        out.append({"id": sid, "title": title, "kind": kind, "group": group})
    for sid, title, kind in GAME_SCENES:
        out.append({"id": sid, "title": title, "kind": kind,
                    "group": "游戏反馈"})
    return out


def hook_options(renderer=None) -> list[dict]:
    """自定义动作可绑的钩子。

    两类来源：
    * **工具动作** —— 固定的一套（喂食 / 转圈 / 跳跃…）
    * **模型自带动作** —— 模型 YAML 里 ``triggers.actions`` 配的键，
      不同模型不一样（这正是要用户自己配的原因）
    """
    out = [{"key": "", "label": "仅手动播放（不绑定）", "source": "none"}]
    for hook, label in TOOL_ACTION_HOOKS:
        out.append({"key": hook, "label": "工具动作 · %s" % label, "source": "tool"})
    if renderer is not None:
        try:
            for key in sorted(getattr(renderer, "profile", None).actions or {}):
                if any(o["key"] == key for o in out):
                    continue
                out.append({"key": key, "label": "模型动作 · %s" % key,
                            "source": "model"})
        except Exception:  # noqa: BLE001
            log.debug("读模型动作列表失败", exc_info=True)
    return out


def bundle_to_dict(bundle) -> dict:
    """SceneBundle → 可 JSON 化的 dict。"""
    if bundle is None:
        return {"emotion": "", "hairstyle": "", "toggles": [], "hold_ms": 0}
    return {
        "emotion": bundle.emotion or "",
        "hairstyle": bundle.hairstyle or "",
        "toggles": list(bundle.toggles or []),
        "hold_ms": int(bundle.hold_ms or 0),
    }


def dict_to_bundle(data: dict) -> SceneBundle:
    """控制台传来的 dict → SceneBundle（只认模型里有的条目名）。"""
    data = data or {}
    return SceneBundle(
        emotion=str(data.get("emotion") or ""),
        hairstyle=str(data.get("hairstyle") or ""),
        toggles=[str(x) for x in (data.get("toggles") or [])],
        hold_ms=int(data.get("hold_ms") or 0),
    )


def summarize(bundle, labels: Optional[dict] = None) -> str:
    """外观组合 → 中文短标签。"""
    bundle = bundle or SceneBundle()
    labels = labels or {}
    parts = []
    if bundle.emotion:
        if bundle.emotion in ("natural", "default", "none"):
            parts.append("表情·自然")
        else:
            parts.append("表情·" + labels.get(bundle.emotion, bundle.emotion))
    if bundle.hairstyle:
        parts.append("发型·" + ("默认" if bundle.hairstyle == "__default__"
                                else labels.get(bundle.hairstyle, bundle.hairstyle)))
    for name in (bundle.toggles or []):
        parts.append(labels.get(name, name))
    return " + ".join(parts) if parts else "默认（不改变）"


def model_action_configs(renderer) -> list[dict]:
    """**模型自带/配置的动作**（profile 的 triggers.actions）—— 控制台直接能播。

    和「自定义动作」的区别：自定义动作是用户自己配的外观组合（表情+发型+配件），
    这里列的是模型 profile 里定义的动作（挥手 / 招手 / 点头…），点一下就播。
    """
    if renderer is None:
        return []
    out: list[dict] = []
    try:
        actions = getattr(getattr(renderer, "profile", None), "actions", None) or {}
    except Exception:  # noqa: BLE001
        return []
    for key, spec in actions.items():
        label = getattr(spec, "label", "") or key
        kind = getattr(spec, "kind", "")
        if kind == "motion":
            what = "模型动作组 %s" % (getattr(spec, "motion_group", "") or "")
        elif kind == "pose_anim":
            what = "姿态动画 %s" % (getattr(spec, "pose_anim", "") or "")
        elif kind == "item":
            what = "外观条目 %s" % (getattr(spec, "item", "") or "")
        else:
            what = kind or "自定义"
        out.append({"key": str(key), "label": str(label),
                    "kind": kind, "summary": what})
    return out


def item_labels(renderer) -> dict:
    """条目名 → 中文标签（拿模型的 profile 里的标签）。"""
    labels = {}
    if renderer is None:
        return labels
    try:
        for group in renderer.get_menu_groups() or []:
            for item_id, label in (group.get("items") or []):
                labels[str(item_id)] = str(label)
    except Exception:  # noqa: BLE001
        log.debug("读外观标签失败", exc_info=True)
    return labels


def scene_configs(renderer) -> list[dict]:
    """所有触发场景的当前配置（含摘要）。"""
    labels = item_labels(renderer)
    out = []
    for entry in scene_catalog():
        bundle = renderer.get_scene_bundle(entry["id"]) if renderer else SceneBundle()
        out.append({
            "id": entry["id"],
            "title": entry["title"],
            "kind": entry["kind"],
            "group": entry["group"],
            "bundle": bundle_to_dict(bundle),
            "summary": summarize(bundle, labels),
            "overridden": bool(renderer.scene_is_overridden(entry["id"]))
            if renderer else False,
        })
    return out


def action_configs(renderer) -> list[dict]:
    """自定义动作列表（含各自的外观摘要与绑定）。"""
    labels = item_labels(renderer)
    out = []
    for a in (renderer.get_custom_actions() if renderer else []) or []:
        bundle = SceneStore.action_bundle(a)
        out.append({
            "id": a.get("id", ""),
            "name": a.get("name", ""),
            "hook": a.get("hook", "") or "",
            "hold_ms": int(a.get("hold_ms") or 0),
            "bundle": bundle_to_dict(bundle),
            "summary": summarize(bundle, labels),
        })
    return out
