# 情绪系统

## 11 个 Emotion 枚举

```python
# app/voice/character.py
class Emotion(str, Enum):
    IDLE       = "idle"
    HAPPY      = "happy"
    SAD        = "sad"
    ANGRY      = "angry"
    SURPRISED  = "surprised"
    SCARED     = "scared"
    CONFUSED   = "confused"
    SHY        = "shy"
    PROUD      = "proud"
    THINKING   = "thinking"
    TALKING    = "talking"
```

8 个表情（happy / sad / angry / surprised / scared / confused / shy / proud）由 LLM 回复末尾标签驱动，3 个过程态（idle / thinking / talking）由 Agent / UI 切换。

## 标签解析

模型回复末尾应保留 `[happy]` / `[shy]` / `[thinking]` / `[sad]` / `[surprised]` / `[angry]` / `[love]` 之一（见 `app/brain/llm_client.py: CHINESE_SYSTEM_SUFFIX`）。

`parse_reply(text)` 走 3 层兜底：

1. 正则提取末尾标签：`r"\[(happy|sad|angry|surprised|scared|confused|shy|proud|thinking|talking|love)\]"`
2. 关键词匹配：`guess_emotion()` 关键词 → 情绪映射（例「生气」「愤怒」→ angry）
3. fallback：默认 IDLE

## 与 Live2D 模型 profile 的对接

`assets/live2d_profiles/*.model.yaml` 的 `emotions` 段：

```yaml
emotions:
  happy:    [心心眼, 微笑]
  sad:      [哭哭眼, 泪眼]
  shy:      [脸红]
  surprised: [四周星星]
```

未在 profile 声明的情绪 → 走 sprite 路径或忽略。

## 与 sprite 路径的对接

`assets/sprites/Emotion_<name>/` 子目录（如 `Emotion_happy/` `Emotion_shy/`），每个目录里是一组 PNG 帧。`_do_set_emotion(name)` 会从对应目录取一个动画播放一次，回主待机。

## 过程态切换

- THINKING：Agent 开始调用工具 → 桌宠切到 `thinking` 动画（随机 `thinking` 或 `thinking_2` 循环）
- TALKING：TTS 合成完成开始播放 → `on_speak_start` 钩子 → 桌宠切到 `talking`；播放结束 `on_speak_end` → 回 idle
- IDLE：默认；持续 60 秒触发「特殊待机」（歪头 / 打哈欠 / 摇尾巴）

## 桌宠状态 → 表情（创新点 2 联动）

桌宠本体有 4 维状态（mood / satiety / favor / level），`PetWindow._refresh_status` 每次状态变更时调：

1. 调 `s.quantize()` 得 `{mood: LOW/MID/HIGH, satiety: ..., favor: ..., level: ...}`
2. `_map_state_to_emotion(q)` 把 4 维状态映射到一个 Live2D 表情：
   - mood LOW → `sad`
   - mood HIGH + sat HIGH + favor HIGH → `happy`（主人+桌宠都好）
   - mood HIGH + sat LOW → `confused`（想吃但没吃饱）
   - favor LOW + mood 不 LOW → `angry`（好感低）
   - 其他 → `idle`（不切）
3. 调 `self.animator.set_emotion(target)` 切 Live2D 表情

`quantize` 走 `app/core/companion_config.yaml` 的阈值（low_below=33 / mid_below=67 + hysteresis）。

状态差时桌宠表情是 sad，LLM 同时通过 `PostLLMStateGuard` 拒绝高危工具，主人口头说「帮我锁屏」 → 桌宠拒绝 + 表情是 sad，两路信号一致。详见 [innovation-2-companion-policy.md](innovation-2-companion-policy.md) 第 4 节。

## 强制输出中文 + 标签

`CHINESE_SYSTEM_SUFFIX` 在 system prompt 末尾追加：

1. 唯一允许的输出语言是简体中文。禁止出现英文/日文/韩文整段、禁止英文工具独白
2. 禁止使用任何 emoji 表情、图标符号和装饰性符号
3. 回复末尾保留一个情绪标签：[happy] / [shy] / [thinking] / [sad] / [surprised] / [angry] / [love] 之一
4. 简短自然（1~3 句），不要解释你在做什么、不要列 bullet、不要 markdown 标题
5. 工具调用只能通过工具 schema 完成

`sanitize_text()` 在流式 + final 双重清洗：剥 `<think>` 标签、剥 emoji、剥尾部英文段落（CJK 占比 < 30%）。
