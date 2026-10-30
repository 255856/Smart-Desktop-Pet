# -*- coding: utf-8 -*-
"""桌宠设置的读写与应用（控制台改设置时走这里）。

设置项分两处落盘：
* **settings.json** —— 桌宠自己的运行时设置（缩放、节拍、TTS、Live2D 参数…）
* **config.yaml** —— 跟主配置绑在一起的（模型 API）

控制台的「设置」Tab 改设置 → 这里写入并**立即应用**到运行中的桌宠。
这些逻辑原来长在 Qt 设置窗口里，窗口删掉后成了独立模块 ——
控制服务（app/core/pet_control.py）调用它。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

#: 设置项定义：key → (标签, 类型, 最小, 最大, 分组)
#: 类型：bool / int / float / str / secret
SETTING_SCHEMA: dict[str, dict[str, Any]] = {
    # -- 视觉 ------------------------------------------------------------
    "renderer": {"label": "渲染引擎", "type": "str", "group": "视觉",
                 "options": ["sprite", "live2d"], "restart": True},
    "scale": {"label": "桌宠缩放", "type": "float", "group": "视觉",
              "min": 0.2, "max": 1.0, "step": 0.05},
    "opacity": {"label": "窗口透明度", "type": "float", "group": "视觉",
                "min": 0.7, "max": 1.0, "step": 0.05},
    "always_on_top": {"label": "窗口置顶", "type": "bool", "group": "视觉"},
    "crossfade_enabled": {"label": "切换动画淡入淡出", "type": "bool", "group": "视觉"},
    "crossfade_ms": {"label": "淡入淡出时长（ms）", "type": "int", "group": "视觉",
                     "min": 80, "max": 400, "step": 10},
    "lock_first_idle": {"label": "永远用第一张 idle", "type": "bool", "group": "视觉"},
    "hide_watermark": {"label": "隐藏模型水印", "type": "bool", "group": "视觉",
                       "restart": True},
    # -- 帧时长 ----------------------------------------------------------
    "frame_ms": {"label": "帧时长（ms）", "type": "int", "group": "帧时长",
                 "min": 33, "max": 200, "step": 1},
    "fps_monitor": {"label": "头顶 FPS 气泡", "type": "bool", "group": "帧时长"},
    "max_fps": {"label": "渲染帧率上限", "type": "int", "group": "帧时长",
                "min": 5, "max": 60, "step": 1},
    "live2d_idle_fps": {"label": "Live2D 待机帧率", "type": "int", "group": "帧时长",
                        "min": 5, "max": 60, "step": 1},
    # -- 行为 ------------------------------------------------------------
    "motion_idle_s": {"label": "IDLE 持续（秒）", "type": "int", "group": "行为",
                      "min": 10, "max": 180, "step": 1},
    "motion_walk_s": {"label": "WALK 持续（秒）", "type": "int", "group": "行为",
                      "min": 2, "max": 30, "step": 1},
    "motion_decide_s": {"label": "决策间隔（秒）", "type": "int", "group": "行为",
                        "min": 10, "max": 90, "step": 1},
    "proactive_enabled": {"label": "主动关心", "type": "bool", "group": "行为"},
    "proactive_min_minutes": {"label": "主动关心间隔下限（分）", "type": "int",
                              "group": "行为", "min": 2, "max": 720, "step": 1},
    "proactive_max_minutes": {"label": "主动关心间隔上限（分）", "type": "int",
                              "group": "行为", "min": 6, "max": 720, "step": 1},
    "activitywatch_enabled": {"label": "活动感知（ActivityWatch）", "type": "bool",
                              "group": "行为"},
    "activitywatch_server_url": {"label": "AW 服务地址", "type": "str", "group": "行为"},
    "activitywatch_lookback": {"label": "AW 回看（分）", "type": "int", "group": "行为",
                               "min": 1, "max": 240, "step": 1},
    "activitywatch_max_events": {"label": "AW 最多事件数", "type": "int", "group": "行为",
                                  "min": 1, "max": 100, "step": 1},
    "developer_mode": {"label": "开发者模式", "type": "bool", "group": "行为"},
    # -- Live2D ----------------------------------------------------------
    "random_exp_enabled": {"label": "挂机随机表情", "type": "bool", "group": "Live2D"},
    "random_sticker_enabled": {"label": "随机表情包贴纸", "type": "bool", "group": "Live2D"},
    "sticker_size": {"label": "贴纸大小（px）", "type": "int", "group": "Live2D",
                     "min": 40, "max": 400, "step": 5},
    "sticker_rotation": {"label": "贴纸倾斜（度）", "type": "int", "group": "Live2D",
                         "min": -90, "max": 90, "step": 5},
    "sticker_min_s": {"label": "贴纸最小间隔（秒）", "type": "int", "group": "Live2D",
                      "min": 5, "max": 600, "step": 1},
    "sticker_max_s": {"label": "贴纸最大间隔（秒）", "type": "int", "group": "Live2D",
                      "min": 5, "max": 600, "step": 1},
    "sticker_duration_s": {"label": "贴纸停留（秒）", "type": "int", "group": "Live2D",
                           "min": 1, "max": 30, "step": 1},
    "random_min_s": {"label": "随机表情最小间隔（秒）", "type": "int", "group": "Live2D",
                     "min": 5, "max": 600, "step": 1},
    "random_max_s": {"label": "随机表情最大间隔（秒）", "type": "int", "group": "Live2D",
                     "min": 5, "max": 600, "step": 1},
    # -- 语音 ------------------------------------------------------------
    "tts_enabled": {"label": "语音朗读", "type": "bool", "group": "语音"},
    "tts_engine": {"label": "TTS 引擎", "type": "str", "group": "语音",
                   "options": ["edge", "gptsovits", "minimax"]},
    "tts_voice": {"label": "Edge 音色", "type": "str", "group": "语音"},
    "current_voice": {"label": "默认音色", "type": "str", "group": "语音"},
    "minimax_voice_id": {"label": "MiniMax Voice ID", "type": "str", "group": "语音"},
    "gptsovits_url": {"label": "GPT-SoVITS 地址", "type": "str", "group": "语音"},
    "gptsovits_ref_audio": {"label": "GPT-SoVITS 参考音频", "type": "str", "group": "语音"},
    "gptsovits_prompt_text": {"label": "GPT-SoVITS 参考文本", "type": "str",
                               "group": "语音"},
    # -- 模型 ------------------------------------------------------------
    "model_base_url": {"label": "API 地址", "type": "str", "group": "模型"},
    "model_api_key": {"label": "API Key", "type": "secret", "group": "模型"},
    "model_name": {"label": "模型名称", "type": "str", "group": "模型"},
    "model_temperature": {"label": "Temperature", "type": "float", "group": "模型",
                          "min": 0.0, "max": 2.0, "step": 0.05},
    "model_max_tokens": {"label": "最大 Tokens", "type": "int", "group": "模型",
                         "min": 128, "max": 8192, "step": 128},
    "model_timeout": {"label": "超时时间（秒）", "type": "int", "group": "模型",
                      "min": 10, "max": 300, "step": 5},
    "model_stream": {"label": "流式输出", "type": "bool", "group": "模型"},
}

#: 分组的显示顺序
GROUP_ORDER = ["视觉", "帧时长", "行为", "Live2D", "语音", "模型"]

#: 设置项 key → config.yaml 里 llm 段的键（模型名那几项对不上名字）
MODEL_CONFIG_KEYS = {
    "model_base_url": "base_url",
    "model_api_key": "api_key",
    "model_name": "model",
    "model_temperature": "temperature",
    "model_max_tokens": "max_tokens",
    "model_timeout": "timeout",
    "model_stream": "stream",
}


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def coerce(key: str, raw):
    """把外部传来的值转成设置项该有的类型并夹到合法范围。

    不认识的键返回 None（调用方跳过）。
    """
    spec = SETTING_SCHEMA.get(key)
    if spec is None:
        return None
    kind = spec["type"]
    if kind == "bool":
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return bool(raw)
    if kind == "int":
        try:
            num = int(float(raw))
        except (TypeError, ValueError):
            return None
        if "min" in spec:
            num = int(_clamp(num, spec["min"], spec["max"]))
        return num
    if kind == "float":
        try:
            num = float(raw)
        except (TypeError, ValueError):
            return None
        if "min" in spec:
            num = round(_clamp(num, spec["min"], spec["max"]), 4)
        return num
    text = str(raw or "").strip()
    if kind == "secret":
        # 面板回显的是掩码（*abcd），原样写回去会把真 key 冲掉 —— 掩码值直接忽略。
        if not text or set(text) <= set("*") or "*" in text:
            return None
        return text
    return text


class SettingsService:
    """设置的读写 + 应用。

    存哪：
    * ``settings.json`` —— 绝大多数项
    * ``config.yaml`` —— 模型配置（API 地址 / Key / 模型名…）跟主配置一起，
      改完既进内存也进 yaml，下次启动还是这个。

    apply 由调用方注入（UIController 有 pet / motion / brain 这些活对象），
    这里只负责「值怎么转、存哪、要不要落 yaml」。
    """

    def __init__(self, store, cfg=None, project_root: Optional[Path] = None):
        self.store = store
        self.cfg = cfg
        self.root = Path(project_root) if project_root else None
        self.apply_hook = None      # callable(values: dict) -> None
        self._config_path = (self.root / "config.yaml") if self.root else None

    # -- 读 ---------------------------------------------------------------
    def values(self) -> dict:
        """当前所有设置项的值（密钥字段给掩码，不给原文）。"""
        out = {}
        for key, spec in SETTING_SCHEMA.items():
            if spec["type"] == "secret":
                raw = self.store.get(key, "")
                out[key] = ("*" * 6 + str(raw)[-4:]) if raw else ""
                continue
            val = self.store.get(key, None)
            if val is None:
                val = self._default(key)
            out[key] = val
        return out

    def _default(self, key: str):
        spec = SETTING_SCHEMA.get(key) or {}
        kind = spec.get("type")
        if kind == "bool":
            return False
        if kind == "int":
            return int(spec.get("min", 0))
        if kind == "float":
            return float(spec.get("min", 0.0))
        if kind == "secret":
            return ""
        return ""

    def schema(self) -> list:
        """给面板用的设置项清单（含标签、范围、选项）。"""
        out = []
        for group in GROUP_ORDER:
            for key, spec in SETTING_SCHEMA.items():
                if spec["group"] != group:
                    continue
                item = {"key": key, "label": spec["label"], "type": spec["type"],
                        "group": group, "restart": bool(spec.get("restart"))}
                for extra in ("min", "max", "step", "options"):
                    if extra in spec:
                        item[extra] = spec[extra]
                out.append(item)
        return out

    # -- 写 ---------------------------------------------------------------
    def update(self, changes: dict) -> dict:
        """改一批设置：校验 → 落盘 → 交给 apply_hook 立即生效。

        返回 ``{"ok": bool, "applied": [...], "rejected": [...], "restart": [...]}``。

        跨线程时用 ``prepare`` + ``apply`` 分开：落盘在哪个线程都行，
        碰 Qt 的「应用」那步必须回主线程。
        """
        prepared = self.prepare(changes)
        error = None
        if prepared["accepted"]:
            try:
                self.apply(prepared["accepted"])
            except Exception as e:  # noqa: BLE001
                error = str(e)
        out = {
            "ok": prepared["ok"] and error is None,
            "applied": sorted(prepared["accepted"].keys()),
            "rejected": prepared["rejected"],
            "skipped": prepared["skipped"],
            "restart": prepared["restart"],
        }
        if error:
            out["error"] = error
        return out

    def prepare(self, changes: dict) -> dict:
        """校验 + 落盘（不碰运行中的桌宠）。"""
        accepted = {}
        rejected = []
        skipped = []
        for key, raw in (changes or {}).items():
            value = coerce(key, raw)
            if value is None:
                # 密钥回显的掩码不是错误 —— 只是"没改"
                if key in SETTING_SCHEMA and SETTING_SCHEMA[key]["type"] == "secret":
                    skipped.append(key)
                else:
                    rejected.append(key)
                continue
            accepted[key] = value
        result = {"ok": not rejected, "accepted": accepted, "rejected": rejected,
                  "skipped": skipped, "restart": []}
        if not accepted:
            return result

        store_keys = {}
        config_keys = {}
        for key, value in accepted.items():
            if key.startswith("model_"):
                config_keys[key] = value
            else:
                store_keys[key] = value

        if store_keys:
            self.store.set_many(store_keys)
        if config_keys:
            self._write_config(config_keys)

        result["restart"] = [k for k in accepted
                             if SETTING_SCHEMA.get(k, {}).get("restart")]
        return result

    def apply(self, accepted: dict) -> None:
        """把改好的设置应用到运行中的桌宠（碰 Qt 的活儿）。"""
        if self.apply_hook is None or not accepted:
            return
        self.apply_hook(accepted)

    def _write_config(self, values: dict) -> None:
        """模型配置写进 config.yaml（保留文件里其它内容与注释顺序）。"""
        if self._config_path is None or not self._config_path.is_file():
            return
        try:
            import yaml
            raw = yaml.safe_load(self._config_path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001
            log.exception("读 config.yaml 失败")
            return
        llm = dict(raw.get("llm") or {})
        for key, value in values.items():
            llm[MODEL_CONFIG_KEYS.get(key, key[len("model_"):])] = value
        raw["llm"] = llm
        try:
            self._config_path.write_text(
                yaml.safe_dump(raw, allow_unicode=True, sort_keys=False),
                encoding="utf-8")
        except Exception:  # noqa: BLE001
            log.exception("写 config.yaml 失败")

    # -- 其它动作 ---------------------------------------------------------
    def reset_defaults(self) -> dict:
        """把有默认值的项恢复默认（模型配置不动 —— 那是主人特意填的）。"""
        changes = {}
        for key, spec in SETTING_SCHEMA.items():
            if key.startswith("model_"):
                continue
            if spec["type"] == "bool":
                changes[key] = False
            elif spec["type"] == "int":
                changes[key] = int(spec.get("min", 0))
            elif spec["type"] == "float":
                changes[key] = float(spec.get("min", 0.0))
        return self.update(changes)
