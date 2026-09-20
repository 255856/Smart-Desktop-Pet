# 🐳 桌面宠物 (Desktop Pet)

本地多智能体桌面助理桌宠：ReAct Agent + 长期记忆 + MCP 工具生态 + Live2D/Sprite 双渲染 + 流式 TTS。

> 详细文档全在 [`docs/`](docs/)：`quickstart` · `architecture` · `live2d-integration` · `tts-integration` · `config-reference` · `tool-catalog` · `emotion-system` · `dev-testing` · `troubleshooting`

## 特性

- **多渲染**：PNG 帧动画 / Live2D（Cubism 4 + QWebEngineView）可切换；Live2D 不可用自动 fallback sprite
- **ReAct Agent**：手写 ReAct 单循环，3 层抗幻觉（force_tool_use / force_retry / final 兜底）；可选 LangChain 后端
- **长期记忆**：TF-IDF / sentence-transformers 可插拔，含重要性评分 + 时间衰减 + 冲突检测
- **MCP**：stdio JSON-RPC，可外挂任何 MCP 兼容 server
- **可观测**：Agent Trace 落 SQLite，FastAPI Dashboard 可视化（端口 8765）
- **TTS**：edge-tts（默认）/ GPT-SoVITS（本地）/ MiniMax（云端克隆）；流式输出与口型同步
- **ASR**：faster-whisper 按住说话
- **主动搭话**：空闲 N~M 分钟自动冒泡

## 快速验证

```powershell
cd E:\study\desktop-pet
python -m pytest tests/ -q                        # 325 个测试
python scripts/verify_features.py                  # 功能验证（94+ 项）
python main.py                                     # 启动桌宠
```

## 快速开始

1. **装依赖**：依赖已装在 `.local-packages/`，无网络也能跑；如需 PyQtWebEngine：`pip install PyQtWebEngine`
2. **配置**：复制 `config.example.yaml` 为 `config.yaml`，填 `llm.api_key`（或改用 Ollama）
3. **资源**：语音 / Live2D 模型 / GPT-SoVITS 整合包下载见 [`docs/资源下载说明.md`](docs/资源下载说明.md)
4. **启动**：`python main.py`（后台自动开 TTS 服务 if `tts_engine: gptsovits`）

## 文件结构

```
desktop-pet/
├── main.py                     # 启动壳（导入 app.main）
├── config.example.yaml         # 配置示例
├── requirements.txt
├── app/
│   ├── main.py                 # 真正的入口（横幅 + Ollama 探测 + 装配）
│   ├── core/                   # 基础设施（config / settings / save / tray）
│   ├── brain/                  # 智能中枢（agent / llm / memory / proactive / trace）
│   │   └── _legacy/            # v3.0 Plan-Execute-Reflect 路线（仅 tests 引用）
│   ├── animation/              # 渲染（sprite / live2d + factory）
│   ├── voice/                  # TTS / ASR / 角色情绪
│   ├── engine/                 # 引擎（state / tools / chat_store / reminder / dashboard）
│   │   └── tools/              # 31 个工具（按 _core/_file/_math/_memory/_pet/_reminder/_shortcuts/_system/_time 分组）
│   ├── ui/                     # 桌宠本体 + 聊天窗 + 设置面板 + UI 控制器
│   ├── mcp/                    # MCP 协议 + filesystem server
│   ├── agents/                 # Sub-agent 骨架（Life / Research / Code / Orchestrator）
│   ├── web/                    # FastAPI Dashboard
│   └── eval/                   # Agent Eval 套件
├── assets/
│   ├── sprites/                # 帧动画（gitignore）
│   ├── live2d_profiles/        # 模型 profile 模板（bingtang / 超频猫猫）
│   └── icons/
├── characters/                 # 多角色 YAML（鲸鱼娘 / 小深 / …）
├── voice/                      # GPT-SoVITS 训练/参考音频（gitignore，仅 .gitkeep）
├── GPT-SoVITS-v2pro-20250604-nvidia50/   # 整合包（gitignore，仅 .gitkeep）
├── docs/                       # 详细文档
├── scripts/                    # run.py / verify_features.py
├── tests/                      # 325 个测试
└── tools/                      # clone_voice.py + _legacy/（历史脚本归档）
```

## 详细文档

- [docs/quickstart.md](docs/quickstart.md) — 5 分钟跑起来
- [docs/architecture.md](docs/architecture.md) — 启动时序 + Live2D 渲染子图 + Agent 流程
- [docs/live2d-integration.md](docs/live2d-integration.md) — `.model3.json` + `*.model.yaml` + cubism-sdk 来源
- [docs/tts-integration.md](docs/tts-integration.md) — edge / gptsovits / minimax 三引擎 + 自愈逻辑
- [docs/config-reference.md](docs/config-reference.md) — `config.yaml` 全字段逐项
- [docs/tool-catalog.md](docs/tool-catalog.md) — 31 个工具分组
- [docs/emotion-system.md](docs/emotion-system.md) — 11 个 Emotion 枚举 + 标签解析
- [docs/dev-testing.md](docs/dev-testing.md) — pytest / verify_features / CI 接入
- [docs/troubleshooting.md](docs/troubleshooting.md) — FAQ 展开
- [docs/资源下载说明.md](docs/资源下载说明.md) — voice / sprites / GPT-SoVITS / Live2D 模型下载

## FAQ

- **桌宠启动后看不到**？看右下角系统托盘，单击显隐
- **聊天窗闪退**？看 `crash.log` 末尾错误，删 `data/chat_history.json` 重试
- **LLM 报错**？检查 `config.yaml` 的 `llm.api_key` / `base_url`；可切 Ollama（`base_url: http://127.0.0.1:11434/v1`）
- **切换 Live2D**？`config.yaml` `pet.renderer: live2d` + `pet.live2d.model_dir`；不可用自动 fallback sprite
- **切换 TTS**？`config.yaml` `character.tts_engine: edge | gptsovits | minimax`