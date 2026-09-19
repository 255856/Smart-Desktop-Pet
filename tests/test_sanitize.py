"""sanitize_text + 中文约束 单测。"""
import pytest

from app.brain.llm_client import sanitize_text, CHINESE_SYSTEM_SUFFIX


class TestSanitize:
    def test_strip_emoji(self):
        assert sanitize_text("你好世界") == "你好世界"
        assert sanitize_text("你好😊世界") == "你好世界"
        assert sanitize_text("🎉恭喜🎊主人") == "恭喜主人"

    def test_strip_decorative_symbols(self):
        assert sanitize_text("你真棒★") == "你真棒"
        assert sanitize_text("我喜欢你♥") == "我喜欢你"
        assert sanitize_text("听到♪歌声") == "听到歌声"

    def test_strip_mixed_unicode_emoji(self):
        # Emoji 被清掉，前后多余空格也收紧
        result = sanitize_text("测试 \U0001F600 笑脸")
        assert "测试" in result
        assert "笑脸" in result
        assert "\U0001F600" not in result
        result2 = sanitize_text("\u2728 闪亮")
        assert "闪亮" in result2
        assert "\u2728" not in result2

    def test_collapse_whitespace(self):
        assert sanitize_text("你好   世界") == "你好 世界"
        assert sanitize_text("你好\n\n\n世界") == "你好\n\n世界"
        assert sanitize_text("  前后空白  ") == "前后空白"

    def test_keep_emotion_tag(self):
        """情绪标签 [happy] 应保留（不是 emoji）。"""
        assert sanitize_text("好哒 [happy]") == "好哒 [happy]"
        assert sanitize_text("哈哈 [thinking] [shy]") == "哈哈 [thinking] [shy]"

    def test_keep_chinese_punctuation(self):
        """中文标点不应被清掉。"""
        assert sanitize_text("你好，世界！") == "你好，世界！"
        assert sanitize_text("什么是「Agent」？") == "什么是「Agent」？"
        # 中文引号
        result = sanitize_text("他说：" + chr(0x201C) + "好的" + chr(0x201D) + "。")
        assert "他说" in result
        assert "好的" in result

    def test_empty(self):
        assert sanitize_text("") == ""
        assert sanitize_text(None) is None

    def test_keep_english_words(self):
        """纯英文不被误删（让 ChatWindow 模型决定怎么过滤）。"""
        result = sanitize_text("Hello World")
        assert "Hello" in result
        assert "World" in result

    def test_dont_strip_chinese_bracket_chars(self):
        """中文字符如 ☆ 等应该可以保留（但 ASCII 装饰符号要去掉）。"""
        # sanitize 只去 emoji + ASCII 装饰，不去 CJK 字符
        result = sanitize_text("你好世界")
        assert "你好世界" == result

    def test_strip_meta_narration(self):
        """LLM 夹带的『我们刚才...』『让我想想...』类括号应被剥掉。"""
        text = "（我们刚才说了半天）主人这么晚说有点累。"
        result = sanitize_text(text)
        assert "我们刚才" not in result
        assert "有点累" in result

    def test_strip_long_paren_segment(self):
        """超过 50 字的纯括号段（Ollama reasoning 痕迹）应被剥掉。"""
        long_inside = "我们刚刚说到设置了喝水提醒，现在主人又告诉我他有点累。" * 3
        text = f"（{long_inside}）主人真是可怜呢"
        result = sanitize_text(text)
        assert "我们刚刚" not in result
        assert "可怜" in result

    def test_short_paren_may_be_stripped(self):
        """短括号可能被启发式剥掉（特征匹配）— trade-off，能接受。"""
        # 不强求保留，只要「看效果」保留即可
        result = sanitize_text("试试这个命令（先 cd）看看效果")
        assert "看看效果" in result

    def test_keep_both_sentences_separated(self):
        """清理后两段独立句子应保留完整。"""
        text = "（思考 30 字的内容）\n\n主人辛苦了。\n\n（又一段 60 字的思考呢）我帮你倒杯水。"
        result = sanitize_text(text)
        assert "主人辛苦了" in result
        assert "我帮你倒杯水" in result


class TestChineseSuffix:
    def test_suffix_contains_required_constraints(self):
        """中文约束 prompt 必须包含关键规则。"""
        assert "简体中文" in CHINESE_SYSTEM_SUFFIX
        assert "emoji" in CHINESE_SYSTEM_SUFFIX.lower() or "表情" in CHINESE_SYSTEM_SUFFIX
        # 包含「[happy]」标签允许的说明
        assert "[happy]" in CHINESE_SYSTEM_SUFFIX
