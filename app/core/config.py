"""配置加载与默认值（基于 pydantic-settings）。"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict
from pydantic_settings import BaseSettings

log = logging.getLogger(__name__)


class LLMConfig(BaseModel):
    base_url: str = "https://api.deepseek.com/v1"
    api_key: str = ""
    model: str = "deepseek-chat"
    stream: bool = True
    temperature: float = 0.8
    max_tokens: int = 1024
    timeout: int = 60
    # 推理强度:仅 OpenAI o1/o3 / 部分推理模型支持。"low" / "medium" / "high"，None 表示不传
    reasoning_effort: Optional[str] = None
    # 项目目录白名单：沙箱工具默认只能访问用户家目录；这里把项目目录加进去
    project_dirs: List[str] = []


class VoiceProfile(BaseModel):
    """一个可切换的 TTS 音色（GPT-SoVITS 参考音频）。

    - name：音色显示名（设置面板 / 狼人杀配音下拉里展示）；
    - ref_audio：GPT-SoVITS 参考音频路径（服务端本地可访问）；
    - prompt_text：参考音频对应的文本；
    - edge_voice：可选，edge 引擎下对应的音色名（留空则用全局 tts_voice）。
    """
    name: str = ""
    ref_audio: str = ""
    prompt_text: str = ""
    edge_voice: str = ""


class CharacterConfig(BaseModel):
    # 兜底默认角色 = characters/xiaoxi.yaml（小汐）。
    # 这里以前写的是一个早就废弃的旧角色名（项目早换成小汐了），是个没人
    # 清理的残留 —— 现在跟 characters/xiaoxi.yaml 对齐。
    name: str = "小汐"
    persona: str = ""
    # 角色文件路径 (相对项目根, e.g. "characters/xiaoxi.yaml")
    character_file: str = "characters/xiaoxi.yaml"
    tts_enabled: bool = True
    tts_voice: str = "zh-CN-XiaoxiaoNeural"
    # 语音引擎：edge=Microsoft edge-tts（免费）| minimax=MiniMax 声音克隆
    tts_engine: str = "edge"
    # MiniMax 声音克隆（方案 A）：tools/clone_voice.py 生成的自定义 voice_id
    minimax_voice_id: str = ""
    minimax_api_key: str = ""      # 留空则复用 llm.api_key（同账号）
    minimax_model: str = "speech-01-turbo"
    minimax_group_id: str = ""     # MiniMax 控制台里的 GroupId（部分账号需要）
    # 克隆样本来源：目录或逗号分隔的文件列表。样本指纹未变则跳过
    minimax_samples: str = ""
    # GPT-SoVITS 本地引擎（方案 B）：需先启动本地 api_v2 服务
    gptsovits_url: str = "http://127.0.0.1:9880"
    gptsovits_ref_audio: str = ""    # 参考音频路径（服务端本地可访问）
    gptsovits_prompt_text: str = ""  # 参考音频对应的文本
    # 多音色候选：默认用第一个，狼人杀里可给每个角色单独指定音色
    voices: List[VoiceProfile] = []
    current_voice: str = ""       # 当前默认音色 name；空 = voices[0]

    def active_voice(self) -> Optional[VoiceProfile]:
        """当前默认音色：current_voice 命中则用，否则第一个；都没有返回 None。"""
        if self.voices:
            for v in self.voices:
                if v.name and v.name == self.current_voice:
                    return v
            return self.voices[0]
        return None


class WindowConfig(BaseModel):
    start_x: int = 200
    start_y: int = 200
    scale: float = 0.6
    always_on_top: bool = True
    show_in_taskbar: bool = False


class ReminderConfig(BaseModel):
    enabled: bool = True
    data_file: str = "reminders.json"


class AppConfig(BaseModel):
    open_chat_on_start: bool = False
    start_minimized: bool = False
    log_level: str = "INFO"


class SpriteConfig(BaseModel):
    directory: str = "assets/sprites"
    fallback: str = "assets/sprites/body_front.png"


class ASRConfig(BaseModel):
    enabled: bool = False          # 默认关闭：启动慢、占内存、识别卡主线程
    model_size: str = "base"       # tiny/base/small/medium/large-v3
    language: str = "zh"


AgentBackend = Literal["lightweight", "standard"]


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


class MCPConfig(BaseModel):
    """MCP（Model Context Protocol）外挂工具。

    servers_file 指向的 YAML 里配置要启动的 MCP server 列表
    （每项：name / command / args / env / cwd / description），
    启动时后台拉起子进程、握手并把它们的工具桥接进桌宠工具系统。
    """
    enabled: bool = False
    servers_file: str = "mcp_servers.yaml"
    call_timeout: int = 30


class ChatConfig(BaseModel):
    """对话请求参数（统一所有 chat 客户端超时）。

    request_timeout_s: 单次 chat 请求的整体超时（秒）。覆盖 LLMConfig.timeout，
    解决「每个上层各自传 llm_cfg,忘了设超时」的硬编码问题。0 表示无限等待。
    show_tool_steps: 在 AI 气泡上方展示「已执行操作 · N」卡片（工具名/参数/成败），
        关闭后气泡更简洁，但主人看不到工具调用历史。默认 True（保持现状）。
    user_avatar_path: 主人头像图片绝对路径(PNG/JPG)。空 = 用默认首字「我」头像。
        文件由 ChatWindow 持久化到 data/avatars/user.png，不入库。
    bot_avatar_path: 桌宠头像图片绝对路径。空 = 用 assets/icon.ico → 角色首帧回退。
        文件由 ChatWindow 持久化到 data/avatars/bot.png。
    """
    request_timeout_s: int = 20
    show_tool_steps: bool = True
    user_avatar_path: str = ""
    bot_avatar_path: str = ""


class ActivityWatchConfig(BaseModel):
    """ActivityWatch 集成。

    AW 是开源本地时间追踪工具。主人装 aw-server + aw-watcher-window 后桌宠可：
      1) 通过 MCP 工具 (aw_get_*) 回答主人「我刚才在干嘛」类问题
      2) 给 ProactiveBrain 注入最近活动 context → 主动搭话更贴切

    enabled 默认 False — 主人先装 AW 再手动开。未启用时静默跳过，不影响 ProactiveBrain。
    """
    enabled: bool = False
    server_url: str = "http://127.0.0.1:5600"
    proactive_lookback_minutes: int = 30
    proactive_max_events: int = 15


class BrainConfig(BaseModel):
    """智能中枢：工具调用 + 长期记忆 + 主动行为 + Agent 后端选择。"""
    backend: AgentBackend = "lightweight"           # "lightweight"=手写 ReAct；"standard"=LangChain
    tools_enabled: bool = True
    memory_file: str = "data/memory.json"
    proactive_enabled: bool = True
    proactive_min_minutes: int = 25
    proactive_max_minutes: int = 45
    # 网络搜索：主用 Tavily（需注册免费 key），留空降级 DuckDuckGo（零 key）
    tavily_api_key: str = ""
    agent: AgentConfig = AgentConfig()
    langchain: LangChainConfig = LangChainConfig()


class Live2DConfig(BaseModel):
    """Live2D 渲染器配置（v3.1+）。"""
    model_dir: str = ""      # Cubism 4 模型根目录（含 *.model3.json）
    # 是否隐藏模型自带的水印（WaterMark 版权卡片 / ArtMesh 装饰文字层）
    # 仅建议个人桌面自用；公开使用或再分发请保留水印或联系模型作者授权
    hide_watermark: bool = True
    # 挂机随机表情：空闲时从模型表情池随机切换，互动即暂停
    random_expression: bool = True
    random_expression_min_s: int = 25
    random_expression_max_s: int = 70
    # 渲染帧率上限：隐藏到托盘时渲染会完全暂停（零开销）
    max_fps: int = 24


class PetConfig(BaseModel):
    """桌宠渲染器配置（v3.1+）。"""
    renderer: Literal["sprite", "live2d"] = "sprite"   # sprite=PNG帧动画（默认）；live2d=Cubism模型
    live2d: Live2DConfig = Live2DConfig()


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
    mcp: MCPConfig = MCPConfig()
    chat: ChatConfig = ChatConfig()
    activitywatch: ActivityWatchConfig = ActivityWatchConfig()

    @property
    def name(self) -> str:
        return self.character.name

    def has_api_key(self) -> bool:
        """识别所有「占位符」（用户没填真 key）。"""
        from app.core.api_keys import is_placeholder_key
        return not is_placeholder_key(self.llm.api_key)


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并 override 到 base，返回新 dict（base / override 都不被改）。"""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_character_yaml(project_root: Path, rel_path: str) -> dict:
    """从 characters/<file>.yaml 读人设。返 dict 含 name / persona,
    没读到返 {} (load_config 会保留 cfg 默认值, 并打 warning)。
    """
    if not rel_path:
        return {}
    char_path = Path(rel_path)
    if not char_path.is_absolute():
        char_path = project_root / char_path
    if not char_path.is_file():
        return {}
    try:
        raw = yaml.safe_load(char_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        log.warning("读 character yaml 失败 %s: %s", char_path, e)
        return {}
    char_block = raw.get("character") or {}
    return {
        k: v for k, v in char_block.items()
        if k in ("name", "persona") and isinstance(v, str)
    }


# --------------------------------------------------------------------------
# 角色（characters/*.yaml）：枚举 + 运行时切换 + 持久化
# --------------------------------------------------------------------------
#
# 为什么持久化**不写** config.yaml
# ----------------------------
# config.yaml 里有 llm.api_key。任何「整份 YAML 读出来再写回去」都会把 key
# 重新序列化一遍（格式被打乱 / key 泄露到备份或日志 / 别的段被抹掉）。
# 所以角色选择单独落在 data/character_selection.yaml —— 一个只存角色路径的小文件。
# load_config() 优先读它，读不到 / 读坏了就回退 config.yaml 的 character_file。

#: 角色文件目录（相对项目根）
CHARACTER_DIR = "characters"

#: 角色选择持久化文件（相对项目根）。**不是** config.yaml。
CHARACTER_SELECTION_FILE = "data/character_selection.yaml"


@dataclass
class CharacterEntry:
    """characters/*.yaml 里的一份角色定义。

    ok=False 时 name / persona 为空，error 说明坏在哪（给 UI 提示用）。
    """

    path: str            # 相对项目根的路径，如 "characters/xiaoxi.yaml"
    name: str = ""
    persona: str = ""
    ok: bool = False
    error: str = ""

    @property
    def file_name(self) -> str:
        return Path(self.path).name

    @property
    def display_name(self) -> str:
        """给 UI 看的名字：坏文件退化成文件名。"""
        return self.name or self.file_name


def _project_root(root: str | Path) -> Path:
    """项目根目录：传文件取它的父目录，传目录就用目录本身。"""
    p = Path(root)
    if p.is_file():
        p = p.parent
    try:
        return p.resolve()
    except Exception:  # noqa: BLE001
        return p


def _rel_to_root(root: Path, path: Path) -> str:
    """尽量写成相对项目根的形式（写进配置文件用）。"""
    try:
        return path.resolve().relative_to(root).as_posix()
    except Exception:  # noqa: BLE001
        return path.as_posix()


def load_character_entry(project_root: str | Path, rel_path: str) -> CharacterEntry:
    """读一个角色文件 → CharacterEntry。

    读不到 / 没有 character: 段 / 缺 name 或 persona → ok=False + error。
    **不抛异常**：坏角色文件不能让桌宠启动不了。
    """
    root = _project_root(project_root)
    raw_rel = str(rel_path or "").strip()
    entry = CharacterEntry(path=raw_rel)
    if not raw_rel:
        entry.error = "角色文件路径为空"
        return entry
    p = Path(raw_rel)
    if not p.is_absolute():
        p = root / p
    entry.path = _rel_to_root(root, p)
    if not p.is_file():
        entry.error = "角色文件不存在：%s" % entry.path
        return entry
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        entry.error = "角色文件解析失败：%s" % e
        return entry
    if not isinstance(raw, dict):
        entry.error = "角色文件格式不对（顶层不是字典）"
        return entry
    block = raw.get("character")
    if not isinstance(block, dict):
        entry.error = "缺少 character: 段"
        return entry
    name = block.get("name")
    persona = block.get("persona")
    missing = [k for k, v in (("name", name), ("persona", persona))
               if not (isinstance(v, str) and v.strip())]
    if missing:
        entry.error = "character: 段缺少 %s" % " / ".join(missing)
        return entry
    entry.name = str(name).strip()
    entry.persona = str(persona)
    entry.ok = True
    return entry


def list_characters(project_root: str | Path = ".") -> list[CharacterEntry]:
    """列出 characters/*.yaml 里的所有角色（按文件名排序）。

    坏文件也列出来（ok=False + error）—— UI 要能告诉主人「这个文件坏了」。
    """
    root = _project_root(project_root)
    d = root / CHARACTER_DIR
    if not d.is_dir():
        return []
    out: list[CharacterEntry] = []
    for f in sorted(d.glob("*.yaml")):
        out.append(load_character_entry(root, f.relative_to(root).as_posix()))
    return out


def load_character_selection(project_root: str | Path) -> str:
    """读持久化的角色选择（相对项目根的路径）；没有 / 读不了 → 空串。"""
    root = _project_root(project_root)
    p = root / CHARACTER_SELECTION_FILE
    if not p.is_file():
        return ""
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("读角色选择文件失败 %s: %s", p, e)
        return ""
    if not isinstance(raw, dict):
        return ""
    v = raw.get("character_file")
    return str(v).strip() if isinstance(v, str) else ""


def save_character_selection(project_root: str | Path, rel_path: str) -> Path:
    """把选中的角色写进 data/character_selection.yaml（原子写）。

    **绝不碰 config.yaml**（里面有 API key）。
    """
    root = _project_root(project_root)
    p = root / CHARACTER_SELECTION_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(
        yaml.safe_dump({"character_file": str(rel_path)},
                       allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    tmp.replace(p)
    return p


#: 角色切换监听器（BrainController 注册）：换人设后同步主动发言等子系统。
_character_switch_hooks: list = []


def register_character_switch_hook(hook) -> None:
    """注册「角色已切换」回调 hook(name, persona)。"""
    if hook is not None and hook not in _character_switch_hooks:
        _character_switch_hooks.append(hook)


def unregister_character_switch_hook(hook) -> None:
    """注销角色切换回调。"""
    try:
        _character_switch_hooks.remove(hook)
    except ValueError:
        pass


def notify_character_switch(name: str, persona: str) -> None:
    """通知所有监听器：角色换了（单个回调出错不影响别人）。"""
    for hook in list(_character_switch_hooks):
        try:
            hook(name, persona)
        except Exception as e:  # noqa: BLE001
            log.warning("角色切换回调失败: %s", e)


def switch_character(cfg: "Config | CharacterConfig", project_root: str | Path,
                     rel_path: str,
                     persist: bool = True) -> tuple[bool, str]:
    """运行时切换角色。

    成功：改 cfg.character 的 name / persona / character_file，落盘持久化，
          并通知监听器（主动发言等人设同步）。
    失败：**cfg 一个字都不动**（保持当前角色），返回 (False, 原因)。
    """
    entry = load_character_entry(project_root, rel_path)
    if not entry.ok:
        return False, "切换失败：%s" % entry.error
    ch = getattr(cfg, "character", None)
    if ch is None:
        # 直接传 CharacterConfig 也行（聊天窗口手里只有 character 段）
        ch = cfg
    if not hasattr(ch, "persona"):
        return False, "切换失败：配置对象里没有 character 段"
    ch.name = entry.name
    ch.persona = entry.persona
    ch.character_file = entry.path
    msg = "已切换到「%s」" % entry.name
    if persist:
        try:
            save_character_selection(project_root, entry.path)
        except Exception as e:  # noqa: BLE001
            log.warning("角色选择落盘失败: %s", e)
            msg += "（但没能保存，下次启动会回到原来的角色：%s）" % e
    notify_character_switch(entry.name, entry.persona)
    return True, msg


def load_config(path: str | Path = "config.yaml") -> Config:
    """读取 YAML 配置（.env 由 BaseSettings 自动加载，环境变量优先级最高）。

    字段缺失时用 BaseModel 默认值兜底。

    读 config.yaml 后, 再读 cfg.character.character_file 指向的 yaml,
    用里面的 name / persona 覆盖 cfg.character。character_file 缺失 / 解析
    失败时静默 fallback (保留 config.yaml / 默认值), 不阻断启动。

    **运行时选过的角色优先**: data/character_selection.yaml 里存着主人
    在 UI 里选的角色, 它比 config.yaml 的 character_file 更靠前
    (config.yaml 从头到尾不写 —— 里面有 API key)。选的那个坏了就回退
    config.yaml 的 character_file, 不阻断启动。
    """
    p = Path(path).resolve()
    raw: dict[str, Any] = {}
    if p.is_file():
        with p.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

    cfg = Config(_env_file=os.environ.get("DSH_DOTENV_PATH", ".env") if p.is_file() else None)
    merged = _deep_merge(cfg.model_dump(), raw)
    if "character" not in merged:
        merged["character"] = {}
    # 从 character_file 读人设, override name / persona
    project_root = p.parent
    char_file = merged["character"].get("character_file") or cfg.character.character_file
    # 运行时选过的角色优先于 config.yaml 的 character_file
    saved = load_character_selection(project_root)
    if saved:
        saved_entry = load_character_entry(project_root, saved)
        if saved_entry.ok:
            char_file = saved
            merged["character"]["character_file"] = saved
        else:
            log.warning("持久化的角色 %s 不可用（%s），回退 config.yaml 的 %s",
                        saved, saved_entry.error, char_file)
    char_yaml = _load_character_yaml(project_root, char_file)
    if char_yaml:
        merged["character"].update(char_yaml)
    else:
        if char_file:
            log.warning("character_file=%s 读不到 / 没 name+persona, 走 config.yaml 兜底",
                        char_file)
    return Config.model_validate(merged)


def save_chat_overrides(path: str | Path, cfg: Config) -> None:
    """把 cfg.chat 的可改字段写回 config.yaml 的 chat: 段（merge，不动其它段）。

    用途：用户从 UI 改了头像/工具步骤开关，需要持久化到磁盘。
    """
    p = Path(path)
    raw: dict[str, Any] = {}
    if p.is_file():
        with p.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    raw.setdefault("chat", {})
    # 同步 chat 段里的可改字段
    raw["chat"]["user_avatar_path"] = cfg.chat.user_avatar_path
    raw["chat"]["bot_avatar_path"] = cfg.chat.bot_avatar_path
    raw["chat"]["show_tool_steps"] = cfg.chat.show_tool_steps
    raw["chat"]["request_timeout_s"] = cfg.chat.request_timeout_s
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)


def save_aw_overrides(path: str | Path, cfg: Config) -> None:
    """把 cfg.activitywatch 4 个字段写回 config.yaml 的 activitywatch: 段。

    跟 save_chat_overrides 一样 merge，不动其它段。
    """
    p = Path(path)
    raw: dict[str, Any] = {}
    if p.is_file():
        with p.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    raw.setdefault("activitywatch", {})
    aw = cfg.activitywatch
    raw["activitywatch"]["enabled"] = bool(aw.enabled)
    raw["activitywatch"]["server_url"] = aw.server_url
    raw["activitywatch"]["proactive_lookback_minutes"] = int(
        aw.proactive_lookback_minutes)
    raw["activitywatch"]["proactive_max_events"] = int(aw.proactive_max_events)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)


__all__ = [
    "Config", "load_config", "save_chat_overrides", "save_aw_overrides",
    "LLMConfig", "CharacterConfig", "WindowConfig", "ReminderConfig",
    "AppConfig", "SpriteConfig", "ASRConfig", "BrainConfig",
    "AgentConfig", "LangChainConfig", "MCPConfig", "ChatConfig",
    "ActivityWatchConfig",
    # 角色枚举 / 切换 / 持久化
    "CharacterEntry", "CHARACTER_DIR", "CHARACTER_SELECTION_FILE",
    "list_characters", "load_character_entry", "switch_character",
    "load_character_selection", "save_character_selection",
    "register_character_switch_hook", "unregister_character_switch_hook",
    "notify_character_switch",
]