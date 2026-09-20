# Emotion System · 情绪系统

## 11 个 Emotion 枚举

```python
# app/voice/character.py
class Emotion(str, Enum):
    IDLE       = "idle"        # 待机
    HAPPY      = "happy"       # 开心
    SAD        = "sad"         # 伤心
    ANGRY      = "angry"       # 生气
    SURPRISED  = "surprised"   # 惊讶
    SCARED     = "scared"      # 害怕
    CONFUSED   = "confused"    # 困惑
    SHY        = "shy"         # 害羞
    PROUD      = "proud"       # 骄傲
    THINKING   = "thinking"    # 思考中（过程态）
    TALKING    = "talking"     # 说话中（过程态）
```

**8 个表情**（happy/sad/angry/surprised/scared/confused/shy/proud）由 LLM 回复末尾标签驱动；
**3 个过程态**（idle/thinking/talking）由 Agent / UI 切换。

## 标签解析

模型回复末尾应保留 `[happy]` / `[shy]` / `[thinking]` / `[sad]` / `[surprised]` / `[angry]` / `[love]` 之一（见 `app/brain/llm_client.py: CHINESE_SYSTEM_SUFFIX`）。

`parse_reply(text)` 走 3 层兜底：

1. **正则提取末尾标签**：`r"\[(happy|sad|angry|surprised|scared|confused|shy|proud|thinking|talking|love)\]"`
2. **关键词匹配**：`guess_emotion()` 关键词 → 情绪映射（如「生气」「愤怒」→ angry）
3. **fallback**：默认 IDLE

## 与 Live2D 模型 profile 的对接

`assets/live2d_profiles/*.model.yaml` 的 `emotions` 段：

```yaml
emotions:
  happy:    [心心眼, 微笑]      # 随机选一个
  sad:      [哭哭眼, 泪眼]
  shy:      [脸红]
  surprised: [四周星星]
```

未在 profile 声明的情绪 → 走 sprite 路径或忽略。

## 与 sprite 路径的对接

`assets/sprites/Emotion_<name>/` 子目录（如 `Emotion_happy/` `Emotion_shy/`），每个目录里是一组 PNG 帧。`_do_set_emotion(name)` 会从对应目录取一个动画播放一次，回主待机。

## 过程态切换

- **THINKING**：Agent 开始调用工具 → 桌宠切到 `thinking` 动画（随机 `thinking` 或 `thinking_2` 循环）
- **TALKING**：TTS 合成完成开始播放 → `on_speak_start` 钩子 → 桌宠切到 `talking`；播放结束 `on_speak_end` → 回 idle
- **IDLE**：默认；持续 60 秒触发「特殊待机」（歪头 / 打哈欠 / 摇尾巴）

## 强制输出中文 + 标签

`CHINESE_SYSTEM_SUFFIX` 在 system prompt 末尾追加：

```
1. 唯一允许的输出语言是简体中文。禁止出现英文/日文/韩文整段、禁止英文工具独白。
2. 禁止使用任何 emoji 表情、图标符号和装饰性符号。
3. 回复末尾保留一个情绪标签：[happy]/[shy]/[thinking]/[sad]/[surprised]/[angry]/[love] 之一。
4. 简短自然（1~3 句），不要解释你在做什么、不要列 bullet、不要 markdown 标题。
5. 工具调用只能通过工具 schema 完成。
```

`sanitize_text()` 在流式 + final 双重清洗：剥 `<think>` 标签、剥 emoji、剥尾部英文段落（CJK 占比 <30%）。