# 常见问题

## 桌宠本体

### 启动后看不到

- 看右下角系统托盘（时间旁），单击显隐
- `app.open_chat_on_start: false` + `start_minimized: false` 才能默认显示
- 多次启动会产生多个桌宠（每个进程一个窗口），到任务管理器关掉重复的

### 闪退 / 启动崩溃

1. 看 `crash.log` 末尾（gitignore，每次启动覆盖）
2. 跑 `python scripts/verify_features.py` 看哪步异常
3. 删 `data/chat_history.json` / `data/traces.db` 重试
4. Live2D 模式下 PyQtWebEngine 没装 → 自动 fallback sprite；想用 Live2D：`pip install PyQtWebEngine`

### 拖动卡 / 不流畅

- `window.scale` 调到 0.4~0.6
- `pet.live2d.max_fps` 降到 24
- Live2D 模式下隐藏到托盘时渲染完全暂停；可见时恢复
- 看是否同时跑大模型请求（流式输出 + TTS 同步时短时卡顿是正常的）

## 聊天 / LLM

### 报 `LLMError`

- 检查 `config.yaml` 的 `llm.base_url` / `llm.api_key` / `llm.model`
- 国内 LLM 多走 OpenAI 兼容协议，确保 `base_url` 末尾带 `/v1`
- 用 Ollama：`base_url: http://127.0.0.1:11434/v1`，`model: qwen3.5:4b`

### 推理痕迹污染 UI

- 项目已分离 `reasoning_content` 与 `content`，UI 只显示 `content`
- 部分模型会在 `content` 里塞 `<think>...</think>` 标签，`sanitize_text()` 会剥
- 仍泄漏：调高 `llm.temperature` 到 0.6，或换无推理模型（如 `abab6.5s-chat`）

### 模型不调工具（只回文字）

- `brain.tools_enabled: true`
- `backend=standard`（LangChain）：`_ensure_agent` 在 model 准备阶段会把工具 attach 给模型
- `backend=lightweight`（手写 ReAct）：3 层抗幻觉 `force_tool_use` → `force_retry` → `final` 兜底
- 见 `tests/test_anti_hallucination*.py` 回归用例

### 状态差 LLM 还是 preemptive 问「要确认一下」

主人实测 5+ 次报告过，根因有 3 个，按可能性排查：

1. **`brain.backend` 还是 `lightweight`** — 创新点 2 整套机制（决策查表 / 状态注入 / post-LLM 拦截器）只在 `standard` 走。改成 `standard` 重启
2. **`ui_controller.py` 创建 ChatWindow 时漏传 `state_provider`** — 桌宠从托盘菜单唤起走的是这条路径，`_state_provider` 永远是 None，中间件拿不到 state。`test_v35_ui_controller_passes_state_provider` AST 守卫锁死这条
3. **`PostLLMStateGuard` 改错对象层级** — 改 `response.content` 没用（ModelResponse 没这字段），要改 `response.result[0].content`。`test_v34_post_llm_guard_refuses_preemptive_when_state_bad` 锁死

排查：

```powershell
# 看启动 log 有没有 self._state_provider is None warning
type E:\study\desktop-pet\crash.log
# 选 搜索 "self._state_provider is None" → 有 → 路径 2
# 选 搜索 "改写失败" → 有 → 路径 3
# 都没有 → 路径 1 (backend 没切)
```

### 状态差 LLM 还是弹 confirm 弹窗

跟 preemptive 问不同：弹 confirm 弹窗是 `RiskyToolConfirm` 中间件正常路径（不是 post-LLM 拦截器）。要让它直接拒绝不走弹窗，需要 `lookup_decision` 返 REJECT / REFUSE，验证：

```powershell
# 跑测试
python -m pytest tests/test_companion_policy.py -v
```

## TTS

### 静音 / 不朗读

- `character.tts_enabled: true`
- Windows 上 pygame 需要音频设备；如在远程桌面里无音频 → 改 `tts_engine: edge` 用浏览器
- GPT-SoVITS 没启动 → 看 TTS 引擎启动横幅；自动拉起 `start_tts_api.bat` 失败会重试

### 音色不对

- edge-tts：`character.tts_voice: zh-CN-XiaoxiaoNeural`（默认）
- GPT-SoVITS：`gptsovits_ref_audio` + `gptsovits_prompt_text` 必须匹配
- MiniMax：`minimax_voice_id` 已注册

### GPT-SoVITS 报「参考音频在3~10秒范围外」

ffmpeg 截：`ffmpeg -i input.wav -ss 00:00:01 -t 5 -c copy ref.wav`

## ASR

### 按住说话没反应

- `asr.enabled: true`
- 第一次使用 faster-whisper 会下载模型（`tiny`/`base`/`small` 等）；网络需要能访问 huggingface
- 国内网络可配 `HF_ENDPOINT=https://hf-mirror.com`（在系统环境变量）

### ASR 准确率低

- 调大 `asr.model_size: small` 或 `medium`（注意：模型大 = 慢 + 占内存）
- `language: zh` 强制中文

## 长期记忆 / Agent

### 记忆丢 / 没记住

- 工具调用失败 → 看聊天窗工具日志
- `brain.memory_file: data/memory.json` 文件是否被删
- 模型是否真的调了 `remember_fact` —— 看 Dashboard trace

### 主动搭话没触发

- `brain.proactive_enabled: true`
- `proactive_min_minutes` 调到 1 测试（默认 25~45 分钟）
- 看 `crash.log` 里是否有 ProactiveBrain 异常

## 创新点 2（状态决策耦合）

### 决策没生效

- `brain.backend` 必须是 `standard`，`lightweight` 不走查表
- 看 Dashboard `/api/companion` 有没有 `data/companion_log.txt` 实时写入
- 跑测试：`pytest tests/test_companion_policy.py -v`

### 决策 trace 没落盘

- 确认 `data/companion_log.txt` 目录可写
- 看启动 log 有没有 `companion_log` 异常

### `_state_provider is None` warning

- 桌宠从托盘唤起走的 `ui_controller.py:1016` 路径必须传 `state_provider=lambda: self.state_mgr.state`
- `main.py:1016` 启动时开窗那条路径传了 `lambda: core.state`
- 两条路径都传了才能用，看代码确认

## Live2D

### 切到 Live2D 没生效

- `pet.renderer: live2d`
- `pet.live2d.model_dir` 必须存在 + 含 `*.model3.json`
- `pip install PyQtWebEngine`；不满足自动 fallback sprite
- 看启动横幅，Live2D 加载失败会打 WARNING

### 模型部位错位 / 表情触发无效

- 模型目录缺 `*.model.yaml` → 渲染器用启发式 fallback 自动分类；如效果差，照 `assets/live2d_profiles/bingtang.model.yaml` 写一份
- `assets/live2d/cubism-sdk/` 三件套缺失：`live2d.min.js` / `live2dcubismcore.min.js` / `pixi.min.js`

## ActivityWatch

### `mcp_aw_*` 工具回「server 未运行」

- 装 [ActivityWatch](https://activitywatch.net/) 桌面客户端并启动
- 默认端口 5600，浏览器开 `http://localhost:5600` 能看到 Web UI 即说明 aw-server 跑起来了
- 详见 [activitywatch-integration.md](activitywatch-integration.md)

### `mcp_aw_*` 工具回「未检测到 aw-watcher-window / afk」

- aw-server 跑了但 watcher 没装
- Linux：`pip install aw-watcher-window aw-watcher-afk`
- Windows / macOS：桌面客户端自带，aw-server 启动时自动拉起
- 浏览器想记 tab：装 [aw-watcher-web](https://activitywatch.net/downloads/) Chrome/Firefox 扩展

### `mcp_aw_*` 工具回「watcher 在但最近 10 分钟无活动」

- 主人没在用电脑 / 真的空闲 / watcher 卡住
- 重启 aw-server

## MCP / Dashboard

### MCP server 连不上

- `app/mcp/filesystem_server.py` 是 stdio JSON-RPC，需正确启动命令
- 看 Dashboard 里 MCP 工具列表是否带绿色对勾

### Dashboard 打不开

- `python -m app.web.dashboard --port 8765` 手动启动看 stderr
- 端口占用：`netstat -ano | findstr 8765` 杀掉冲突进程
- 防火墙：第一次启动会弹 Windows 防火墙提示

## 打包

### PyInstaller 打包报错

- `packaging/pyinstaller-hooks/hook_preimport.py` 已在 `packaging/desktop-pet.spec` 引用，必须保留
- `packaging/build.py` 会自动收集 `assets/`、`characters/`、`docs/` 等数据文件
- 打包前看 `requirements-build.txt` 与 `packaging/pyinstaller-hooks/hook_preimport.py` 注释

### 打包后看不到桌宠

- `--windowed` 模式下托盘图标在「显示隐藏的图标」里
- 看 `crash.log` 排查；打包后的 `crash.log` 在 exe 同级目录
