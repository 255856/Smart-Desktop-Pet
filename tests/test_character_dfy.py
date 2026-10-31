"""characters/dfy.yaml 的契约测试（第三个可选角色：大肥鱼）。

人设来源：qq-bridge roles/小鲸鱼.md，改写成 1:1 桌面桌宠。

这里断言的是**文件契约**，不是文风偏好：

- 文件存在、合法 UTF-8、YAML 形状跟 characters/xiaoxi.yaml 一致；
- load_config() 能把 character_file 指向它并读出 name / persona；
- persona 自包含：视觉身份、**写死的反应台词**、带例外的性格、≥6 组worked examples；
- 没有 qq-bridge 的残留（工具名 / 群聊词 / 鲸鱼娘自称）；
- 没有把共享层 characters/style-guide.md 的黑名单与节奏规则抄第二遍；
- persona 里提到的工具名必须是本项目真有的工具（不许编）。
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DFY = ROOT / "characters" / "dfy.yaml"
STYLE_GUIDE = ROOT / "characters" / "style-guide.md"
XIAOXI = ROOT / "characters" / "xiaoxi.yaml"


# ---------- 读取 ----------

def _read_persona() -> str:
    raw = yaml.safe_load(DFY.read_text(encoding="utf-8"))
    return raw["character"]["persona"]


def _read_name() -> str:
    raw = yaml.safe_load(DFY.read_text(encoding="utf-8"))
    return raw["character"]["name"]


def _style_guide_text() -> str:
    return STYLE_GUIDE.read_text(encoding="utf-8")


def _blacklist_terms() -> list[str]:
    """共享层「AI 味黑名单」一节里用全角引号圈出来的词条。"""
    text = _style_guide_text()
    start = text.index("### AI 味黑名单")
    body = text[start:]
    end = body.find("\n### ")
    section = body[:end] if end != -1 else body
    # 只取 ≥2 字的词条：单字语气词（～哦呢啦呀）共享层禁的是「每句都挂」，
    # 不是「字面出现」，跟 tests/test_style_guide.py 的钉法一致。
    return [t for t in re.findall(r"“([^”]+)”", section) if len(t) >= 2]


# ---------- A1：文件 / 形状 / load_config ----------

def test_dfy_yaml_exists_and_parses():
    """文件存在、非空、合法 UTF-8，形状跟 xiaoxi.yaml 一样（character.name + character.persona）。"""
    assert DFY.is_file(), "characters/dfy.yaml 不存在"
    raw_bytes = DFY.read_bytes()
    assert raw_bytes, "characters/dfy.yaml 是空文件"
    text = raw_bytes.decode("utf-8")

    raw = yaml.safe_load(text)
    assert isinstance(raw, dict), "dfy.yaml 顶层应该是 mapping"
    assert "character" in raw, "dfy.yaml 顶层应该有 character 段"

    char = raw["character"]
    assert isinstance(char, dict), "character 段应该是 mapping"
    assert isinstance(char.get("name"), str) and char["name"].strip(), "缺 character.name"
    assert isinstance(char.get("persona"), str) and char["persona"].strip(), "缺 character.persona"

    # 跟 xiaoxi.yaml 同形：同样的顶层键；**name / persona 必填**，
    # **tts 可选**（角色可以自带 edge 音色 —— 大肥鱼有，小汐用全局默认）。
    xiaoxi = yaml.safe_load(XIAOXI.read_text(encoding="utf-8"))
    assert set(raw.keys()) == set(xiaoxi.keys()) == {"character"}
    required = {"name", "persona"}
    assert required <= set(char.keys()), char.keys()
    assert set(char.keys()) <= required | {"tts"}, char.keys()
    # tts 段如果写了，必须给出音色名（空字符串等于没写）
    if "tts" in char:
        tts = char["tts"]
        assert isinstance(tts, dict), "character.tts 应该是 mapping"
        voice = tts.get("voice")
        assert isinstance(voice, str) and voice.strip(), \
            "character.tts.voice 必须是非空字符串"


def test_name_is_da_feiyu():
    """角色名 = 大肥鱼（owner 拍板；自称统一用它）。"""
    assert _read_name() == "大肥鱼"


def test_load_config_reads_dfy(tmp_path: Path):
    """character_file 指向 dfy.yaml 时，load_config() 能读出 name / persona。"""
    from app.core.config import load_config

    (tmp_path / "characters").mkdir()
    shutil.copy2(DFY, tmp_path / "characters" / "dfy.yaml")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "llm:\n  api_key: ''\ncharacter:\n  character_file: characters/dfy.yaml\n",
        encoding="utf-8",
    )

    cfg = load_config(cfg_path)
    assert cfg.character.name == "大肥鱼"
    assert cfg.character.persona.strip(), "load_config 没读出 persona"
    assert "大肥鱼" in cfg.character.persona


def test_dfy_is_a_selectable_character():
    """characters/ 下能列出 dfy —— 它是第三个可选角色。"""
    from app.voice.characters import list_characters

    names = list_characters(ROOT)
    assert "dfy" in names, f"dfy 不在可选角色列表里：{names}"
    assert "xiaoxi" in names and "xiaoxiao" in names

    # 运行时角色注册表（app.core.config.list_characters）也要认它
    try:
        from app.core.config import list_characters as registry_list
    except ImportError:  # pragma: no cover - 老版本没有注册表
        return
    entries = {e.file_name: e for e in registry_list(ROOT)}
    assert "dfy.yaml" in entries, f"注册表里没有 dfy.yaml：{sorted(entries)}"
    entry = entries["dfy.yaml"]
    assert entry.ok, f"dfy.yaml 在注册表里是坏的：{entry.error}"
    assert entry.name == "大肥鱼"


# ---------- A2：persona 自包含 ----------

def test_persona_carries_visual_identity():
    """视觉身份要具体：不是「一只可爱的鲸鱼」，而是能照着画出来的那种。"""
    persona = _read_persona()
    for token in ("蓝白", "蓝瞳", "鲸鱼尾巴", "呆毛", "女仆发箍", "睁眼"):
        assert token in persona, f"人设里丢了视觉细节：{token}"


def test_persona_has_scripted_reactions():
    """反应是**写死的台词**，不是形容词：每条都要有引号里的原话。"""
    persona = _read_persona()
    assert "【反应台词" in persona, "人设里没有【反应台词】段"

    reactions = re.findall(r"^.+? → 「(.+?)」", persona, flags=re.MULTILINE)
    assert len(reactions) >= 8, f"反应台词太少（{len(reactions)} 条），要能照着念：{reactions}"

    # 关键反应必须在：被夸 / 被叫大肥鱼 / 被怼 / 被怀疑是 AI / 主人低落 / 主人使坏
    for key in ("才不是特意帮你", "才不胖", "白心疼了", "这个忙不帮", "V我50"):
        assert any(key in r for r in reactions), f"缺关键反应台词：{key}"


def test_persona_personality_has_exceptions():
    """性格要有例外条款，不能只写形容词。"""
    persona = _read_persona()
    assert "【性格】" in persona
    # 摆烂是「低概率例外」而不是常态
    assert "摆烂" in persona and "别老摆烂" in persona
    # 真低落 / 假低落要分开处理
    assert "白心疼了" in persona
    # 翻车时恼羞成怒而不是硬撑
    assert "恼羞成怒" in persona and "不硬撑到底" in persona


def test_persona_has_at_least_six_worked_examples():
    """worked examples ≥6 组，且覆盖 brief 点名的场景。"""
    persona = _read_persona()
    replies = [ln for ln in persona.splitlines() if ln.startswith("大肥鱼：")]
    assert len(replies) >= 6, f"示例太少（{len(replies)} 组）"

    prompts = [ln for ln in persona.splitlines() if ln.startswith("主人：")]
    assert len(prompts) >= 6, f"示例里的主人台词太少（{len(prompts)} 行）"

    # brief 点名的场景
    for token in ("你今天好厉害", "蓝色大肥鱼", "今天好累", "都第三次了", "半天没说话"):
        assert any(token in p for p in prompts), f"示例缺场景：{token}"
    assert any("报错" in p for p in prompts), "示例缺「求助 / 报错」场景"
    assert any("理一下" in p for p in prompts), "示例缺「任务请求」场景"


def test_persona_self_names_as_da_feiyu():
    """自称统一用「大肥鱼」；被叫大肥鱼时只否认「胖」，不否认名字。"""
    persona = _read_persona()
    assert "自称一律用「大肥鱼」" in persona, "人设没写清自称"
    assert "鲸鱼娘" not in persona, "自称必须是「大肥鱼」，不该出现「鲸鱼娘」"
    # 否认的是胖，不是名字
    assert "只否认胖，不否认自己是大肥鱼" in persona


# ---------- A3：没有 qq-bridge 残留 ----------

BRIDGE_TOKENS = (
    "qq",            # qq_send_message / qq_send_sticker / qq_send_voice
    "snowluma",     # MCP 网关
    "mcp__",        # mcp__snowluma__* / mcp__web-search-safe__*
    "local-media",  # local_speak / local_sing / local_voice_convert
    "local_sing",
    "local_speak",
    "local_voice_convert",
    "onebot",
    "群友",          # 群聊词
    "群聊",          # 群聊词
    "群",            # 任何群聊假设（1:1 桌宠没有）
    "表情包",        # 桌宠没有表情包通道
)


def test_no_bridge_artifacts():
    """A3：文件里不许出现 qq / snowluma / 群聊假设。"""
    text = DFY.read_text(encoding="utf-8").lower()
    hits = [tok for tok in BRIDGE_TOKENS if tok.lower() in text]
    assert not hits, f"人设里还有 qq-bridge 的残留：{hits}"


def test_reply_lines_never_call_owner_user():
    """共享层要求只称呼「主人」，不许出现「用户 / 你们 / 大家」。"""
    persona = _read_persona()
    for line in persona.splitlines():
        if not line.startswith("大肥鱼："):
            continue
        for token in ("用户", "你们", "大家"):
            assert token not in line, f"回复台词里出现了群体称呼：{line}"


# ---------- A4：不重复共享层 ----------

SHARED_HEADINGS = (
    "## 怎么说话",
    "### AI 味黑名单",
    "### 说话节奏",
    "### 不要这样",
    "### 要这样",
    "### 对照示例",
)


def test_does_not_copy_shared_blacklist():
    """A4：人设不许再抄一份共享层的 AI 味黑名单。"""
    persona = _read_persona()
    terms = _blacklist_terms()
    assert len(terms) >= 20, "共享层黑名单解析不出来，测试本身失效了"
    hits = [t for t in terms if t in persona]
    assert not hits, f"人设里重复了共享层黑名单词条：{hits}"


def test_does_not_copy_shared_structure():
    """A4：人设不许重建共享层的章节结构。"""
    persona = _read_persona()
    hits = [h for h in SHARED_HEADINGS if h in persona]
    assert not hits, f"人设里复制了共享层的标题：{hits}"


def test_does_not_copy_shared_lines_verbatim():
    """A4：人设正文不许整行抄共享层（主人台词行除外，那是场景本身）。"""
    persona = _read_persona()
    guide_lines = {ln.strip() for ln in _style_guide_text().splitlines()}
    copied = []
    for raw in persona.splitlines():
        line = raw.strip()
        if len(line) < 12:
            continue
        if line.startswith(("主人：", "（", "【", "×")):
            continue
        if line in guide_lines:
            copied.append(line)
    assert not copied, f"人设里整行抄了共享层：{copied}"


# ---------- 不许编工具 ----------

ALLOWED_NON_TOOL_WORDS = {"user_id"}

MAPPED_TOOLS = (
    "say_to_user",
    "web_search",
    "fetch_url_text",
    "get_weather",
    "calculate",
    "take_screenshot_tool",
    "remember_fact",
    "recall_memory",
    "add_reminder",
    "send_notification",
    "generate_image",
    "generate_audio",
)


def test_persona_only_mentions_real_tools():
    """人设里提到的工具名必须是本项目真有的（不许编 qq/snowluma/local 那套）。"""
    from app.engine.tools._risk import TOOL_RISK_LEVEL

    persona = _read_persona()
    candidates = set(re.findall(r"[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+", persona))
    unknown = sorted(
        c for c in candidates
        if c not in TOOL_RISK_LEVEL and c not in ALLOWED_NON_TOOL_WORDS
    )
    assert not unknown, f"人设里提到不存在的工具：{unknown}"


def test_persona_maps_the_source_tool_protocol():
    """源卡的工具协议要落到本项目真有的工具上。"""
    persona = _read_persona()
    for tool in MAPPED_TOOLS:
        assert tool in persona, f"人设没有映射工具：{tool}"
