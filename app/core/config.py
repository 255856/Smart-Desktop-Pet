"""配置加载与默认值（基于 pydantic-settings）。

支持从 YAML 和 .env 加载，未提供的字段用 BaseModel 默认值兜底。

外部用法：
    cfg = load_config("config.yaml")        # 优先 YAML，再叠加环境变量
    cfg.has_api_key()                       # 判断是否填了真实 key
    cfg.llm.api_key                          # 嵌套字段访问

YAML 结构：
    llm: { base_url, api_key, model, stream, temperature, max_tokens, timeout }
    character: { name, persona, tts_enabled, tts_voice }
    window: { start_x, start_y, scale, always_on_top, show_in_taskbar }
    reminder: { enabled, data_file }
    app: { open_chat_on_start, start_minimized, log_level }
    sprite: { directory, fallback }
    asr: { enabled, model_size, language }
    brain: { backend, tools_enabled, memory_file, proactive_*, agent_*, langchain_* }
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings


# ---------------- LLM ----------------
class LLMConfig(BaseModel):
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    stream: bool = True
    temperature: float = 0.8
    max_tokens: int = 1024
    timeout: int = 60


# ---------------- Character ----------------
class CharacterConfig(BaseModel):
    name: str = "鲸鱼娘"
    persona: str = ""
    tts_enabled: bool = True
    tts_voice: str = "zh-CN-XiaoxiaoNeural"


# ---------------- Window ----------------
class WindowConfig(BaseModel):
    start_x: int = 200
    start_y: int = 200
    scale: float = 0.6
    always_on_top: bool = True
    show_in_taskbar: bool = False


# ---------------- Reminder ----------------
class ReminderConfig(BaseModel):
    enabled: bool = True
    data_file: str = "reminders.json"


# ---------------- App ----------------
class AppConfig(BaseModel):
    open_chat_on_start: bool = False
    start_minimized: bool = False
    log_level: str = "INFO"


# ---------------- Sprite ----------------
class SpriteConfig(BaseModel):
    directory: str = "assets/sprites"
    fallback: str = "assets/sprites/body_front.png"


# ---------------- ASR ----------------
class ASRConfig(BaseModel):
    enabled: bool = True
    model_size: str = "base"       # tiny/base/small/medium/large-v3
    language: str = "zh"


# ---------------- Brain（智能中枢）----------------
AgentBackend = Literal["lightweight", "standard"]     # 后端选择


class LangChainConfig(BaseModel):
    """LangChain 标准后端的可选参数。"""
    enable_checkpointer: bool = True     # 是否启用 SqliteSaver 记忆持久化
    checkpoint_db: str = "data/langchain_checkpoints.db"
    max_iterations: int = 10          # Agent 最大步数
    return_intermediate_steps: bool = False  # 是否返回中间步骤


class AgentConfig(BaseModel):
    """agent_v2 顶层参数（同时影响 lightweight 和 standard 后端）。"""
    mode: Literal["react", "single"] = "react"     # react=多步规划，single=单轮
    max_turns: int = 6                              # single 模式最大轮数
    max_replans: int = 2                            # react 模式失败后最大重规划次数
    reflector_mode: Literal["heuristic", "llm", "off"] = "heuristic"


class BrainConfig(BaseModel):
    """智能中枢：工具调用 + 长期记忆 + 主动行为 + Agent 后端选择。"""
    backend: AgentBackend = "lightweight"           # "lightweight"=手写 ReAct；"standard"=LangChain
    tools_enabled: bool = True
    memory_file: str = "data/memory.json"
    proactive_enabled: bool = True
    proactive_min_minutes: int = 25
    proactive_max_minutes: int = 45
    agent: AgentConfig = AgentConfig()
    langchain: LangChainConfig = LangChainConfig()


class Live2DConfig(BaseModel):
    """Live2D 渲染器配置（v3.1+）。"""
    model_dir: str = ""      # Cubism 4 模型根目录（含 *.model3.json）
    # 是否隐藏模型自带的水印（免费模型的版权/防盗声明：含 WaterMark 版权卡片，
    # 以及伪装成 ArtMesh 叠加在角色身上的 FREE MODEL / 作者署名等文字层）。
    # 实现上在渲染层 drawMesh 跳过这些 drawable，不改动模型文件与贴图。
    # 仅建议个人桌面自用；公开使用或再分发请保留水印或联系模型作者授权。
    hide_watermark: bool = True
    # 挂机随机表情（空闲时从模型表情池随机切换，互动即暂停）。
    # 表情池与默认节奏在模型目录的 *.model.yaml 里配置，这里只做开关与节奏覆盖。
    random_expression: bool = True
    random_expression_min_s: int = 25
    random_expression_max_s: int = 70


class PetConfig(BaseModel):
    """桌宠渲染器配置（v3.1+）。"""
    renderer: Literal["sprite", "live2d"] = "sprite"   # sprite=PNG帧动画（默认）；live2d=Cubism模型
    live2d: Live2DConfig = Live2DConfig()


# ---------------- 顶层 Config（BaseSettings，自动读 .env）----------------
class Config(BaseSettings):
    """桌宠总配置。

    pydantic-settings 默认从环境变量读取同名大写字段（LLM__API_KEY → llm.api_key）。
    YAML 文件通过 load_config() 手动合并进来。
    """
    model_config = ConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",     # LLM__API_KEY → llm.api_key
        extra="ignore",
        case_sensitive=False,
    )

    llm: LLMConfig = LLMConfig()
    character: CharacterConfig = CharacterConfig()
    window: WindowConfig = WindowConfig()
    reminder: ReminderConfig = ReminderConfig()
    app: AppConfig = AppConfig()
    sprite: SpriteConfig = SpriteConfig()
    asr: ASRConfig = ASRConfig()
    brain: BrainConfig = BrainConfig()
    pet: PetConfig = PetConfig()

    @property
    def name(self) -> str:
        return self.character.name

    def has_api_key(self) -> bool:
        """识别所有「占位符」（用户没填真 key）。"""
        placeholders = {
            "", "PUT-YOUR-API-KEY-HERE",
            "PUT-YOUR-MINIMAX-API-KEY-HERE",
            "PUT-YOUR-MINIMAX-API-KEY-HERE",   # 双写避免误改
        }
        return bool(self.llm.api_key) and self.llm.api_key not in placeholders


def _merge_yaml(target: Config, raw: dict[str, Any]) -> Config:
    """把 YAML 字典按 key 路径合并到 pydantic model 上（保留 env 优先权）。"""
    if not raw:
        return target

    # 逐个 section 深拷贝后用 model_validate 重建
    sections = ("llm", "character", "window", "reminder", "app",
                "sprite", "asr", "brain", "pet")
    new_data = target.model_dump()
    for sec in sections:
        if sec in raw and isinstance(raw[sec], dict):
            cur = new_data.get(sec, {})
            cur.update(raw[sec])
            new_data[sec] = cur

    # brain 内嵌的 agent / langchain 也需要递归
    if "brain" in raw and isinstance(raw["brain"], dict):
        b = dict(new_data["brain"])
        for sub in ("agent", "langchain"):
            if sub in raw["brain"] and isinstance(raw["brain"][sub], dict):
                cur = dict(b.get(sub, {}))
                cur.update(raw["brain"][sub])
                b[sub] = cur
        new_data["brain"] = b

    # pet 内嵌的 live2d 也要递归
    if "pet" in raw and isinstance(raw["pet"], dict):
        p = dict(new_data["pet"])
        if "live2d" in raw["pet"] and isinstance(raw["pet"]["live2d"], dict):
            cur = dict(p.get("live2d", {}))
            cur.update(raw["pet"]["live2d"])
            p["live2d"] = cur
        new_data["pet"] = p

    return Config.model_validate(new_data)


def load_config(path: str | Path = "config.yaml") -> Config:
    """读取 YAML 配置（.env 由 BaseSettings 自动加载，环境变量优先级最高）。

    字段缺失时用 BaseModel 默认值兜底。
    """
    p = Path(path)
    raw: dict[str, Any] = {}
    if p.is_file():
        with p.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    # 1) 先用 BaseSettings 构造（含 env 自动加载）
    cfg = Config(_env_file=os.environ.get("DSH_DOTENV_PATH", ".env") if p.is_file() else None)
    # 2) 再用 YAML 覆盖（YAML 优先于 .env 但低于显式环境变量已生效部分）
    cfg = _merge_yaml(cfg, raw)
    return cfg


__all__ = [
    "Config", "load_config",
    "LLMConfig", "CharacterConfig", "WindowConfig", "ReminderConfig",
    "AppConfig", "SpriteConfig", "ASRConfig", "BrainConfig",
    "AgentConfig", "LangChainConfig",
]