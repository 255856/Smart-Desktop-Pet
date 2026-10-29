"""characters/style-guide.md 的契约测试（Stage 1.2 · 行为层）。

风格指南是共享层（system prompt 拼装层）的输入：它会被逐字读入，紧跟人设块追加到
system prompt。所以这里断言的是"文件契约"，不是文风偏好：

- 文件存在、非空、合法 UTF-8；
- 五个三级标题齐全、顺序固定，且没有多余的三级标题；
- 黑名单词条够硬（承自 qq-bridge 角色卡，改写成小汐版）；
- 正文里没有 emoji，没有半角引号混进中文；
- 每条"你可以/或者"示例都非空，且不自己踩黑名单。

示例里的"你不要"行本来就是在演示坏说法，所以黑名单词条检查只作用于
"你可以/或者"行。
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STYLE_GUIDE = ROOT / "characters" / "style-guide.md"

# 共享层拼 system prompt 时指望看到的二级标题
H2_HEADING = "## 怎么说话"

# 共享层测试会找的五个三级标题，顺序固定
REQUIRED_H3 = (
    "### AI 味黑名单",
    "### 说话节奏",
    "### 不要这样",
    "### 要这样",
    "### 对照示例",
)

# 黑名单里必须出现的具体词条（承自 qq-bridge roles/小鲸鱼.md 第六节，改写成小汐版）
REQUIRED_BLACKLIST_TERMS = (
    "首先",
    "其次",
    "最后",
    "综上所述",
    "总而言之",
    "值得注意的是",
    "希望这个回答对你有帮助",
    "有什么可以帮您",
    "随时找我",
    "记得……哦",
    "一定要……哦",
    "祝你……",
    "加油！",
    "作为AI",
    "作为一个语言模型",
    "我无法",
    "这是个好问题",
    "我理解你的感受",
    "你们觉得呢",
    "保证解压",
    "一绝",
    "真管用",
    "刺激多了",
    "包你满意",
    "强烈推荐",
    "绝对有效",
    "强烈安利",
    "绝对",
    "超好用",
    "非常推荐",
    "行吧 正经的",
    "客服腔",
    "本小姐",
    "Markdown 标题",
    "每句都加语气词",
)

# 桌宠是 1:1 场景，qq-bridge 的群聊假设一律不适用
GROUP_CHAT_TOKENS = ("群友", "群聊", "潜水", "表情包", "@")

# 对照示例的行首标签
OWNER_PREFIX = "主人："
BAD_PREFIX = "你不要："
GOOD_PREFIX = "你可以："
SHORT_PREFIX = "或者："
EXPECTED_EXAMPLES = 11

# emoji / 装饰符号的码位区间
EMOJI_RANGES = (
    (0x00A9, 0x00AE),
    (0x1F000, 0x1F0FF),
    (0x1F1E6, 0x1F1FF),
    (0x1F300, 0x1FAFF),
    (0x2122, 0x2122),
    (0x2600, 0x27BF),
    (0x2B00, 0x2BFF),
    (0x3030, 0x303D),
    (0xFE0F, 0xFE0F),
    (0x200D, 0x200D),
)


def _read_style_guide() -> str:
    return STYLE_GUIDE.read_text(encoding="utf-8")


def _section(text: str, heading: str) -> str:
    """取某个三级标题到下一个三级标题之间的正文。"""
    start = text.index(heading)
    body = text[start + len(heading):]
    end = body.find("\n### ")
    return body[:end] if end != -1 else body


def _emoji_chars(text: str) -> list[str]:
    hits: list[str] = []
    for ch in text:
        code = ord(ch)
        if any(low <= code <= high for low, high in EMOJI_RANGES):
            hits.append(ch)
        elif unicodedata.category(ch) == "So":
            hits.append(ch)
    return hits


def test_style_guide_exists_and_is_utf8():
    """文件存在、非空、合法 UTF-8。"""
    assert STYLE_GUIDE.is_file(), "characters/style-guide.md 不存在"
    raw = STYLE_GUIDE.read_bytes()
    assert raw, "characters/style-guide.md 是空文件"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - 失败路径
        pytest.fail(f"characters/style-guide.md 不是合法 UTF-8：{exc}")
    assert text.strip(), "characters/style-guide.md 只有空白字符"
    assert "\ufffd" not in text, "文件里有解码替换字符（编码被弄坏了）"


def test_headings_are_exact_and_ordered():
    """恰好五个三级标题，顺序固定；二级标题是文件第一行且唯一。"""
    lines = _read_style_guide().splitlines()
    h3 = [line.strip() for line in lines if line.startswith("### ")]
    assert h3 == list(REQUIRED_H3), f"三级标题不对或多出别的：{h3}"
    h2 = [line.strip() for line in lines if line.startswith("## ")]
    assert h2 == [H2_HEADING], f"二级标题应该只有一个 {H2_HEADING}：{h2}"
    assert lines[0].strip() == H2_HEADING, "文件第一行必须是二级标题"


def test_blacklist_contains_required_terms():
    """黑名单里必须带着这批具体词条（承自 qq-bridge，改写成小汐版）。"""
    assert len(REQUIRED_BLACKLIST_TERMS) >= 20
    section = _section(_read_style_guide(), "### AI 味黑名单")
    missing = [term for term in REQUIRED_BLACKLIST_TERMS if term not in section]
    assert not missing, f"AI 味黑名单缺少词条：{missing}"


def test_no_emoji():
    """正文里不能出现 emoji 或装饰符号。"""
    hits = _emoji_chars(_read_style_guide())
    assert not hits, f"文件里有 emoji/装饰符号：{sorted(set(hits))}"


def test_no_quote_mangling():
    """中文引号必须全角、成对，不能混进半角双引号或乱码。"""
    text = _read_style_guide()
    assert '"' not in text, "文件里有半角双引号，中文引号应统一用全角"
    assert text.count("“") == text.count("”"), "全角双引号没有成对"
    assert text.count("「") == text.count("」"), "直角引号没有成对"
    assert "锟斤拷" not in text, "文件里有典型乱码片段"


def test_contrastive_examples_are_complete_and_clean():
    """11 组对照示例；"你可以/或者"行非空且不踩黑名单。"""
    lines = _read_style_guide().splitlines()
    owner = [line for line in lines if line.startswith(OWNER_PREFIX)]
    bad = [line for line in lines if line.startswith(BAD_PREFIX)]
    good = [line for line in lines if line.startswith(GOOD_PREFIX)]
    short = [line for line in lines if line.startswith(SHORT_PREFIX)]

    assert len(owner) == EXPECTED_EXAMPLES, f"应有 {EXPECTED_EXAMPLES} 组示例的主人台词"
    assert len(bad) == EXPECTED_EXAMPLES, f"应有 {EXPECTED_EXAMPLES} 行'你不要'"
    assert len(good) == EXPECTED_EXAMPLES, f"应有 {EXPECTED_EXAMPLES} 行'你可以'"
    assert len(short) == EXPECTED_EXAMPLES, f"应有 {EXPECTED_EXAMPLES} 行'或者'"

    for label, group in (("你可以", good), ("或者", short)):
        for line in group:
            content = line.split("：", 1)[1].strip()
            assert content, f"{label} 示例内容是空的：{line}"
            for term in REQUIRED_BLACKLIST_TERMS:
                assert term not in content, f"{label} 示例踩了黑名单词 {term}：{line}"


def test_no_group_chat_assumptions():
    """桌宠是 1:1 场景，不许带群聊假设。"""
    text = _read_style_guide()
    hits = [token for token in GROUP_CHAT_TOKENS if token in text]
    assert not hits, f"风格指南不该出现群聊假设：{hits}"


def test_no_placeholders():
    """交付的文件不能留占位符。"""
    text = _read_style_guide()
    for token in ("TODO", "TBD", "XXX", "待补充", "占位"):
        assert token not in text, f"风格指南还留着占位符：{token}"
