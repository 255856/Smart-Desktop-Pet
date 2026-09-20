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

    def test_strip_emotion_tag(self):
        """末尾情绪标签 [happy]/[shy] 等应该被剥掉（模型不再被要求输出标签）。"""
        assert sanitize_text("好哒 [happy]") == "好哒"
        assert sanitize_text("哈哈 [thinking] [shy]") == "哈哈"

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

    def test_drop_pure_english(self):
        """纯英文（无 CJK 字符）应被兜底丢弃——让上层 fallback，避免 UI 显示英文工具独白。"""
        # 旧版本会保留纯英文；新兜底（_cjk_ratio < 0.15 → 整体丢弃）
        assert sanitize_text("Hello World") == ""
        assert sanitize_text("It is Sunday morning at 08:14") == ""

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

    def test_strip_meta_tool_talk(self):
        """元描述「让我调用工具 / 这应该用XX工具 / 我应该用open_website工具」应被剥掉。"""
        # 典型漏网：模型即便真的调了工具，也会把"我要做什么"塞进 final answer
        text = "打开哔哩哔哩（B站），让主人看动漫。这应该用open_website工具来打开B站的网址。让我调用工具。"
        result = sanitize_text(text)
        assert "工具" not in result
        assert "调用" not in result
        assert "打开哔哩哔哩" in result

    def test_strip_plan_narration(self):
        """元描述「根据角色设定 / 用户说 / 我需要以XX角色来回答」应被剥掉。"""
        text = (
            "鱼娘有什么喜欢做的事情。我需要以鲸鱼娘的可爱软萌角色来回答，保持自然简短，不要长篇大论。"
            "\n\n根据角色设定：- 外貌：蓝渐变长卷发- 性格：可爱软萌，温柔"
            "\n\n我可以提到一些符合角色的爱好嘿嘿~主人问鲸鱼娘的爱好呀~我喜欢游泳呢"
        )
        result = sanitize_text(text)
        # 元描述应被剥掉，保留正文
        assert "根据角色设定" not in result
        assert "我需要" not in result
        assert "我喜欢游泳呢" in result

    def test_strip_user_repeat(self):
        """元描述「用户说 / 主人想要 / 用户想问」应被剥掉。"""
        assert "用户说" not in sanitize_text("用户说想看天气。今天晴朗。")
        assert "主人想要" not in sanitize_text("主人想要一杯水。好的主人，给你倒。")

    def test_strip_mid_emotion_tag_inline(self):
        """句中位置的 [happy] 标签也必须剥除（旧逻辑只剥末尾），前后回答都保留。"""
        out = sanitize_text("好的主人，马上帮你打开抖音~ [happy] 记得刷完早点休息哦。")
        assert "[" not in out and "happy" not in out
        assert "马上帮你打开抖音" in out and "记得刷完早点休息" in out

    def test_strip_process_filler_clause(self):
        """工具过程废话「~打开X的方式有很多种呢」应剥掉，保留前面的真动作。"""
        out = sanitize_text("好的主人，马上帮你打开抖音~ 打开抖音的方式有很多种呢。")
        assert "方式有很多种" not in out
        assert "马上帮你打开抖音" in out

    def test_strip_quote_analysis_sentence(self):
        """句首引用主人指令后做意图分析「说「打开X」，这是要…」应整句剥除。"""
        out = sanitize_text('说"打开星穹铁道"，这是要打开一个游戏应用。好的主人，帮你启动星穹铁道~')
        assert "这是要" not in out and "游戏应用" not in out
        assert "帮你启动星穹铁道" in out

    def test_strip_leading_english_cot_prefix(self):
        """英文 CoT 与中文回答黏在同一句（无句号）时，剥掉英文前缀、保留中文。"""
        out = sanitize_text("- The story involves Rudy meeting his parents old friend搜到啦~动画已经完结啦。")
        assert "The story" not in out and "Rudy" not in out
        assert "搜到啦" in out and "动画已经完结" in out

    def test_keep_normal_english_mixed(self):
        """正常中英混排（VS Code / Chrome 等短英文词）不被误删。"""
        out = sanitize_text("VS Code 打开了吗？我帮你看看，打开 VS Code 和 Chrome 都没问题~")
        assert "VS Code" in out and "Chrome" in out

    def test_strip_unclosed_emotion_tag(self):
        """句尾未闭合的 [happy 也要剥除。"""
        out = sanitize_text("嘿嘿~主人好啊，今天过得怎么样呀？[happy")
        assert "[" not in out and "happy" not in out
        assert "今天过得怎么样呀" in out

    def test_strip_lone_think_close_tag(self):
        """英文 CoT 后孤立的 </think> 结束标签应一并剥除，只留中文。"""
        out = sanitize_text("I should try again.</think> 嗯？刚才没打开吗？再试一次~")
        assert "think" not in out and "try again" not in out
        assert "刚才没打开吗？再试一次" in out

    def test_keep_answer_after_chinese_cot_same_paragraph(self):
        """中文角色规划与真回答同段（无句号分隔）时，锚点后的真回答必须保留。"""
        text = ("根据角色设定：- 外貌：鲸鱼娘，白色长发- 性格：温柔可爱- 擅长：游泳"
                "好呀~我喜欢游泳呢，毕竟人家是鲸鱼嘛~")
        out = sanitize_text(text)
        assert "我喜欢游泳呢" in out
        assert "根据角色设定" not in out and "外貌" not in out

    def test_english_cot_with_scattered_chinese_keeps_only_answer(self):
        """英文 CoT 夹带少量中文引用/语气词时仍应剥掉，只留中文回答。"""
        text = ('user is saying "晚上好啊". It is late on Saturday. '
                "I should respond in a cute soft style with 呀 呢 嘿嘿\n"
                "嘿嘿~主人晚上好呀！都十点多了呢，主人今天过得好吗？")
        out = sanitize_text(text)
        assert "user is saying" not in out and "Saturday" not in out and "soft style" not in out
        assert "嘿嘿~主人晚上好呀" in out and "都十点多了呢" in out

    def test_keep_normal_zhuren_xiangwan_reply(self):
        """回归：角色正常回应「主人想玩 X 呢，我去启动」不能被误判为复述而清空。"""
        text = "主人想玩崩坏：星穹铁道呢，我现在就去启动游戏~"
        assert sanitize_text(text) == text

    def test_strip_tool_soliloquy_keep_answer(self):
        """工具调用独白剥除，保留给主人的最终结果句。"""
        text = "我应该使用 open_website 工具来打开哔哩哔哩。打开哔哩哔哩（B站），让主人看动漫。"
        out = sanitize_text(text)
        assert "open_website" not in out and "工具" not in out
        assert "哔哩哔哩" in out


def test_tts_sentence_split_pattern():
    """句末标点切分（流式逐句 TTS 的正则）。"""
    import re
    pat = re.compile(r"[。！？!?\n;；]+")
    # 多句连续
    text = "你好呀。今天天气不错！真的吗？嗯。"
    ends = [m.end() for m in pat.finditer(text)]
    # 实际切分位置：句号=4, !?=11, ?=14, 。=16
    assert 4 in ends and len(ends) >= 3
    # 单段无标点返回空（剩余累积）
    assert pat.search("主人好呀") is None

    def test_preserves_normal_sentences(self):
        """不包含「工具」一词的正常中文不应受影响。"""
        assert sanitize_text("天气真好，主人今天过得怎么样？") == "天气真好，主人今天过得怎么样？"
        assert sanitize_text("好的主人，记住了") == "好的主人，记住了"


class TestChineseSuffix:
    def test_suffix_contains_required_constraints(self):
        """中文约束 prompt 必须包含关键规则。"""
        assert "简体中文" in CHINESE_SYSTEM_SUFFIX
        assert "emoji" in CHINESE_SYSTEM_SUFFIX.lower() or "表情" in CHINESE_SYSTEM_SUFFIX
