"""角色情绪解析单元测试。"""
import pytest
from app.voice.character import Emotion, parse_reply, guess_emotion, ParsedReply


class TestCharacter:
    def test_parse_happy_tag(self):
        result = parse_reply("你好呀~ [happy]")
        assert result.emotion == Emotion.HAPPY
        assert "happy" not in result.text

    def test_parse_sad_tag(self):
        result = parse_reply("呜呜~ [sad]")
        assert result.emotion == Emotion.SAD

    def test_parse_no_tag(self):
        result = parse_reply("你好")
        assert result.emotion == Emotion.HAPPY  # 默认
        assert result.tag_found is False   # 没标默认情绪时 tag_found=False

    def test_parse_happy_tag_found(self):
        """[happy] 显式标签时 tag_found=True，避免被兜底覆盖。"""
        result = parse_reply("你好呀~ [happy]")
        assert result.emotion == Emotion.HAPPY
        assert result.tag_found is True

    def test_parse_sad_tag_found(self):
        result = parse_reply("呜呜~ [sad]")
        assert result.emotion == Emotion.SAD
        assert result.tag_found is True

    def test_parse_middle_tag_not_stripped(self):
        result = parse_reply("I feel happy today")
        assert "happy" in result.text  # 不在末尾，不剥离

    def test_guess_emotion_happy(self):
        assert guess_emotion("好耶~ 好开心") == Emotion.HAPPY

    def test_guess_emotion_sad(self):
        assert guess_emotion("呜呜，好难过") == Emotion.SAD

    def test_parse_empty(self):
        result = parse_reply("")
        assert result.text == ""
        assert result.emotion == Emotion.HAPPY
