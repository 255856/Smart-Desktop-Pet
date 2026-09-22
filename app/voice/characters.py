"""多角色配置加载。"""
from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Optional

import yaml

from app.core.config import CharacterConfig, Config

log = logging.getLogger(__name__)


def character_yaml_path(name: str, base: Path) -> Path:
    """characters/<name>.yaml 的绝对路径（不一定存在）。"""
    return base / "characters" / f"{name}.yaml"


def load_character(name: str, base: Path) -> Optional[CharacterConfig]:
    """读取 characters/<name>.yaml，返回 CharacterConfig；缺失返回 None。"""
    p = character_yaml_path(name, base)
    if not p.is_file():
        log.warning("character yaml 不存在：%s", p)
        return None
    try:
        raw = yaml.safe_load(p.read_text("utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("character yaml 读取失败：%s (%s)", p, e)
        return None
    char_raw = raw.get("character") or raw  # 顶层和 character 段都容忍

    tts_raw = char_raw.get("tts", {}) or {}

    return CharacterConfig(
        name=char_raw.get("name", name),
        persona=char_raw.get("persona", ""),
        tts_enabled=bool(tts_raw.get("enabled", True)),
        tts_voice=tts_raw.get("voice", "zh-CN-XiaoxiaoNeural"),
    )


def apply_character(cfg: Config, name: Optional[str], base: Path) -> Config:
    """用 character yaml 覆盖 cfg.character 字段。返回新 Config（不改原对象）。"""
    if not name:
        return cfg
    ch = load_character(name, base)
    if ch is None:
        return cfg
    new_char = CharacterConfig(
        name=ch.name or cfg.character.name,
        persona=ch.persona or cfg.character.persona,
        tts_enabled=ch.tts_enabled,
        tts_voice=ch.tts_voice or cfg.character.tts_voice,
    )
    return replace(cfg, character=new_char)


def list_characters(base: Path) -> list[str]:
    """characters/ 下所有可用的角色名。"""
    d = base / "characters"
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.yaml"))
