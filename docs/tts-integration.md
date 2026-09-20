# TTS Integration · 语音合成接入

## 三种引擎

| 引擎 | 费用 | 音质 | 配置复杂度 | 适用场景 |
|------|------|------|-----------|---------|
| **edge-tts** | 免费 | 中 | 零 | 默认；不想折腾的用户 |
| **GPT-SoVITS** | 完全免费 | 高（需要参考音频） | 中 | 想用自定义音色 |
| **MiniMax** | 付费（声音克隆） | 高 | 中（需申请权限） | 有 MiniMax 商业账号 |

切换：

```yaml
# config.yaml
character:
  tts_engine: edge | gptsovits | minimax
```

## edge-tts（默认）

无需额外配置，`tts_engine: edge` 即可。Windows 上会自动用 pygame 播放，缓存到 `assets/tts_cache/`。

## GPT-SoVITS（本地，免费）

### 准备

1. 下载整合包 [GPT-SoVITS-v2pro-20250604-nvidia50.zip](https://github.com/RVC-Boss/GPT-SoVITS)，解压到项目根目录的 `GPT-SoVITS-v2pro-20250604-nvidia50/`（目录名必须一致）
2. 准备参考音频：3~10 秒 wav + 同名 .lab 文本标注，放到 `voice/<角色>/`

### 启动 TTS 服务

桌宠启动时**自动**在后台拉起 api_v2 服务（端口 9880）。如需手动启动：

```powershell
# 项目根目录
start_tts_api.bat
```

或直接：

```powershell
cd GPT-SoVITS-v2pro-20250604-nvidia50
runtime\python.exe api_v2.py -a 127.0.0.1 -p 9880 -c GPT_SoVITS\configs\tts_infer.yaml
```

### 配置

```yaml
character:
  tts_engine: gptsovits
  gptsovits_url: "http://127.0.0.1:9880"
  gptsovits_ref_audio: "voice/安可/zh_vo_Character_Encore_10_1.wav"
  gptsovits_prompt_text: "要小心哦，这里是很多羊咩故事发生的起点。"
```

### 自愈重启

`GPTSoVITSTTS._synthesize()` 捕获 `ConnectError` / `ReadError` / `OSError` 后自动调 `app.main.start_tts_api_subprocess()` 重启服务，等模型加载完再重试。日志：

```
WARNING - GPT-SoVITS 连接失败，自动重启 api_v2…
INFO - TTS 服务已重启（pid=12345），等待模型加载…
```

## MiniMax（云端克隆）

### 准备

1. MiniMax 商业账号 + 声音克隆权限（`voice clone user forbidden` 错误 = 没开通）
2. 参考音频放到 `voice/<角色>/`

### 自动克隆（启动时）

桌面宠物启动时如果检测到 `minimax_samples` 且 `voice_id` 未注册，会自动调 `tools/clone_voice.py` 上传样本创建音色。

### 配置

```yaml
character:
  tts_engine: minimax
  # minimax_voice_id: liuying          # 自动克隆后回填
  minimax_samples: "voice/流萤"
  # minimax_api_key: ""                # 留空复用 llm.api_key
```

### 手动克隆

```powershell
python tools/clone_voice.py            # 走交互式向导
```

## 流式输出 + TTS 同步

`_on_chunk` 在每个 token 收到时：

1. 累积到 `current_bot_msg.content`
2. 调 `sanitize_text()` 剥离推理痕迹 / 英文独白 / emoji
3. emit `streaming_chunk(sanitized)` → 桌宠头顶气泡 + 口型
4. 模型 done 后发整段 `final_answer` → 触发 TTS 合成

详见 `app/ui/chat_window.py: _on_chunk` + `app/voice/voice.py: on_speak_start / on_speak_end`。

## 参考音频 3~10 秒限制

GPT-SoVITS 强约束：参考音频长度超出范围会报「参考音频在3~10秒范围外」。用 ffmpeg 截：

```powershell
ffmpeg -i input.wav -ss 00:00:01 -t 5 -c copy ref.wav
```

## 中文路径

GPT-SoVITS 服务端在 Windows 下通常能识别中文路径，但保险起见用纯英文文件名的 `.lab` 与 `.wav`。