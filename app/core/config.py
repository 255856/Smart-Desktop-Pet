"""配置加载与默认值。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class LLMConfig:
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    stream: bool = True
    temperature: float = 0.8
    max_tokens: int = 1024
    timeout: int = 60


@dataclass
class CharacterConfig:
    name: str = "鲸鱼娘"
    persona: str = ""
    tts_enabled: bool = True
    tts_voice: str = "zh-CN-XiaoxiaoNeural"


@dataclass
class WindowConfig:
    start_x: int = 200
    start_y: int = 200
    scale: float = 0.6
    always_on_top: bool = True
    show_in_taskbar: bool = False


@dataclass
class ReminderConfig:
    enabled: bool = True
    data_file: str = "reminders.json"


@dataclass
class AppConfig:
    open_chat_on_start: bool = False
    start_minimized: bool = False
    log_level: str = "INFO"


@dataclass
class SpriteConfig:
    directory: str = "assets/sprites"
    fallback: str = "assets/sprites/body_front.png"


@dataclass
class ASRConfig:
    enabled: bool = True
    model_size: str = "base"       # tiny/base/small/medium/large-v3
    language: str = "zh"


@dataclass
class BrainConfig:
    """智能中枢：工具调用 + 长期记忆 + 主动行为。"""
    tools_enabled: bool = True          # 允许模型调用工具（提醒/记忆/开应用…）
    memory_file: str = "data/memory.json"
    proactive_enabled: bool = True      # 空闲时主动找主人说话
    proactive_min_minutes: int = 25
    proactive_max_minutes: int = 45


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    character: CharacterConfig = field(default_factory=CharacterConfig)
    window: WindowConfig = field(default_factory=WindowConfig)
    reminder: ReminderConfig = field(default_factory=ReminderConfig)
    app: AppConfig = field(default_factory=AppConfig)
    sprite: SpriteConfig = field(default_factory=SpriteConfig)
    asr: ASRConfig = field(default_factory=ASRConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)

    @property
    def name(self) -> str:
        return self.character.name

    def has_api_key(self) -> bool:
        # 识别所有「占位符」（用户没填真 key）
        placeholders = {"", "PUT-YOUR-API-KEY-HERE",
                         "PUT-YOUR-MINIMAX-API-KEY-HERE"}
        return bool(self.llm.api_key) and self.llm.api_key not in placeholders


def _deep_get(d: dict[str, Any], keys: list[str], default: Any) -> Any:
    cur: Any = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def load_config(path: str | Path = "config.yaml") -> Config:
    """读取 YAML 配置，缺失字段用 dataclass 默认值兜底。"""
    p = Path(path)
    raw: dict[str, Any] = {}
    if p.is_file():
        with p.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    cfg = Config()
    # LLM
    llm_raw = raw.get("llm", {})
    cfg.llm = LLMConfig(
        base_url=llm_raw.get("base_url", cfg.llm.base_url),
        api_key=llm_raw.get("api_key", cfg.llm.api_key),
        model=llm_raw.get("model", cfg.llm.model),
        stream=llm_raw.get("stream", cfg.llm.stream),
        temperature=float(llm_raw.get("temperature", cfg.llm.temperature)),
        max_tokens=int(llm_raw.get("max_tokens", cfg.llm.max_tokens)),
        timeout=int(llm_raw.get("timeout", cfg.llm.timeout)),
    )
    # Character
    char_raw = raw.get("character", {})
    cfg.character = CharacterConfig(
        name=char_raw.get("name", cfg.character.name),
        persona=char_raw.get("persona", cfg.character.persona),
        tts_enabled=char_raw.get("tts_enabled", cfg.character.tts_enabled),
        tts_voice=char_raw.get("tts_voice", cfg.character.tts_voice),
    )
    # Window
    win_raw = raw.get("window", {})
    cfg.window = WindowConfig(
        start_x=int(win_raw.get("start_x", cfg.window.start_x)),
        start_y=int(win_raw.get("start_y", cfg.window.start_y)),
        scale=float(win_raw.get("scale", cfg.window.scale)),
        always_on_top=bool(win_raw.get("always_on_top", cfg.window.always_on_top)),
        show_in_taskbar=bool(win_raw.get("show_in_taskbar", cfg.window.show_in_taskbar)),
    )
    # Reminder
    rem_raw = raw.get("reminder", {})
    cfg.reminder = ReminderConfig(
        enabled=bool(rem_raw.get("enabled", cfg.reminder.enabled)),
        data_file=rem_raw.get("data_file", cfg.reminder.data_file),
    )
    # App
    app_raw = raw.get("app", {})
    cfg.app = AppConfig(
        open_chat_on_start=bool(app_raw.get("open_chat_on_start", cfg.app.open_chat_on_start)),
        start_minimized=bool(app_raw.get("start_minimized", cfg.app.start_minimized)),
        log_level=app_raw.get("log_level", cfg.app.log_level),
    )
    # Sprite
    sprite_raw = raw.get("sprite", {})
    cfg.sprite = SpriteConfig(
        directory=sprite_raw.get("directory", cfg.sprite.directory),
        fallback=sprite_raw.get("fallback", cfg.sprite.fallback),
    )
    # ASR（语音输入）
    asr_raw = raw.get("asr", {})
    cfg.asr = ASRConfig(
        enabled=bool(asr_raw.get("enabled", cfg.asr.enabled)),
        model_size=asr_raw.get("model_size", cfg.asr.model_size),
        language=asr_raw.get("language", cfg.asr.language),
    )
    # Brain（工具调用 / 记忆 / 主动行为）
    brain_raw = raw.get("brain", {})
    cfg.brain = BrainConfig(
        tools_enabled=bool(brain_raw.get("tools_enabled", cfg.brain.tools_enabled)),
        memory_file=str(brain_raw.get("memory_file", cfg.brain.memory_file)),
        proactive_enabled=bool(brain_raw.get("proactive_enabled", cfg.brain.proactive_enabled)),
        proactive_min_minutes=int(brain_raw.get("proactive_min_minutes",
                                                cfg.brain.proactive_min_minutes)),
        proactive_max_minutes=int(brain_raw.get("proactive_max_minutes",
                                                cfg.brain.proactive_max_minutes)),
    )
    return cfg