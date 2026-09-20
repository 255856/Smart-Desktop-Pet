# Quickstart · 5 分钟跑起来

## 0. 前置

- Windows 10/11（项目以 Windows 为主，PyQt5 + Win32 API）
- Python 3.10+，已装 `pip`
- 依赖已装在 `.local-packages/`（仓库自带，免装）。如需 Live2D：`pip install PyQtWebEngine`
- LLM API Key（DeepSeek / OpenAI / 硅基流动 / MiniMax / Ollama 本地均可）

## 1. 改配置

```powershell
cd E:\study\desktop-pet
copy config.example.yaml config.yaml
# 编辑 config.yaml：填 llm.api_key（或改 base_url 用 Ollama）
```

## 2. 启动

```powershell
python main.py
```

启动横幅会显示：角色名 / 模型 / Ollama 探测结果 / 数据文件落盘路径。

## 3. 第一次对话

- 双击桌宠 → 出现聊天窗
- 输入「你好」→ 流式输出 + 自动朗读
- 桌宠头顶会有气泡同步显示

## 4. 切换 Live2D（可选）

`config.yaml`：
```yaml
pet:
  renderer: live2d
  live2d:
    model_dir: "assets/live2d/超频猫猫完整版/超频猫猫"
    scale: 0.5
```

模型与 profile 模板说明见 [live2d-integration.md](live2d-integration.md)。

## 5. 切换 GPT-SoVITS 本地 TTS（可选）

1. 下载整合包解压到 `GPT-SoVITS-v2pro-20250604-nvidia50/`（链接见 [资源下载说明.md](资源下载说明.md)）
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

## 6. 跑测试

```powershell
python -m pytest tests/ -q            # 325 个
python scripts\verify_features.py     # 94+ 项功能验证
```