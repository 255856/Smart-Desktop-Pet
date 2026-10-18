# 5 分钟跑起来

## 前置

- Windows 10/11（项目以 Windows 为主，PyQt5 + Win32 API）
- Python 3.10+
- 依赖已装在 `.local-packages/`（仓库自带）。如需 Live2D 渲染：`pip install PyQtWebEngine`
- LLM API Key（DeepSeek / OpenAI / 硅基流动 / MiniMax / Ollama 本地均可）
- 可选：[ActivityWatch](https://activitywatch.net/) 桌面客户端，桌宠能告诉你「过去 5 分钟在做什么」。详见 [activitywatch-integration.md](activitywatch-integration.md)

## 配 config

```powershell
cd E:\study\desktop-pet
copy config.example.yaml config.yaml
# 编辑 config.yaml：填 llm.api_key（或改 base_url 用 Ollama）
```

`brain.backend` 默认 `lightweight`（手写 ReAct，零依赖）。想用 LangChain 后端（带创新点 2 决策耦合、post-LLM 拦截器）改成 `standard`。

## 启动

```powershell
python main.py
```

启动横幅会显示：角色名 / 模型 / Ollama 探测结果 / 工具数 / 记忆条数 / 后端类型 / 数据文件落盘路径。

## 第一次对话

双击桌宠（或右键 → 「聊天」）→ 输入「你好」→ 流式输出 + 自动朗读 + 头顶气泡同步显示。

无 API Key 也能玩，桌宠用 mock 回复兜底（功能演示完整，但不能调真 LLM）。

右键模型 → 「设置」配置 LLM Key / Tavily Key / 启用真模型。设置面板 → 「控制 Tab → 主动关心」段可开 ActivityWatch SQLite 落盘。

## 可选：切 Live2D

`config.yaml`：

```yaml
pet:
  renderer: live2d
  live2d:
    model_dir: "assets/live2d/超频猫猫完整版/超频猫猫"
    scale: 0.5
```

模型 profile 与五段配置见 [live2d-integration.md](live2d-integration.md)。

## 可选：切 GPT-SoVITS 本地 TTS

1. 下载整合包 [GPT-SoVITS-v2pro-20250604-nvidia50.zip](https://github.com/RVC-Boss/GPT-SoVITS)，解压到项目根的 `GPT-SoVITS-v2pro-20250604-nvidia50/`（目录名必须一致）
2. 把参考 wav + .lab 放到 `voice/<角色>/`
3. `config.yaml`：
   ```yaml
   character:
     tts_engine: gptsovits
     gptsovits_url: "http://127.0.0.1:9880"
     gptsovits_ref_audio: "voice/安可/zh_vo_Character_Encore_10_1.wav"
     gptsovits_prompt_text: "要小心哦，这里是很多羊咩故事发生的起点。"
   ```
4. 重启桌宠 → 后台自动拉起 TTS 服务（api_v2），失败自动重连

## 跑测试

```powershell
python -m pytest tests/ -q            # 1270 个测试
python scripts\verify_features.py     # 82+ 项功能验证
```

测试覆盖：Agent 循环 / 抗幻觉 3 层 / LangChain 后端 / 创新点 2（量化 / 决策查表 / post-LLM 拦截器 / 状态注入双路径）/ 情绪解析 / 长期记忆 / Live2D / MCP / Dashboard / Trace。

CI 在 Windows runner 跑（Qt / pywin32 / WMI 亮度 / 电量等都依赖 Windows 专有 API）。

## 接下来去哪

- 想让桌宠读你的活动窗口：装 [ActivityWatch](https://activitywatch.net/)，详见 [activitywatch-integration.md](activitywatch-integration.md)
- 想看 LLM / 工具调用的 trace：跑 Dashboard（`python -m app.web.dashboard`），浏览器开 `http://localhost:8766`
- 想理解桌宠状态怎么影响 LLM 决策：读 [innovation-2-companion-policy.md](innovation-2-companion-policy.md)
- 想自定义 GPT-SoVITS 音色：参考 [tts-integration.md](tts-integration.md)
- 想写自己的 Live2D 模型 profile：参考 [live2d-integration.md](live2d-integration.md)
- 遇到问题：翻 [troubleshooting.md](troubleshooting.md)
