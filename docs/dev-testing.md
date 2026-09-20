# Dev & Testing · 开发与测试

## 测试

### 单元测试（pytest）

```powershell
# 全部
python -m pytest tests/ -q            # 325 个

# 单独跑
python -m pytest tests/test_planner.py -v
python -m pytest tests/test_anti_hallucination.py -v
python -m pytest tests/test_live2d_renderer.py -v
python -m pytest tests/test_live2d_model_profile.py -v
```

测试按域分文件：

| 文件 | 覆盖 |
|------|------|
| `test_agent_reply.py` / `test_react_loop.py` | `AgentLoop` 主循环 + 抗幻觉 |
| `test_anti_hallucination.py` / `_plan.py` | 3 层抗幻觉回归 |
| `test_reasoning_separation.py` | reasoning_delta 分离 |
| `test_planner.py` / `test_parallel_plan.py` | V2 Plan-Execute-Reflect |
| `test_eval.py` | Eval 套件 |
| `test_memory.py` / `test_hallucination.py` | 长期记忆 |
| `test_live2d_renderer.py` | Live2D 渲染器 + SpriteRenderer 接口 |
| `test_live2d_model_profile.py` | `*.model.yaml` 解析 |
| `test_mcp.py` | MCP 协议 |
| `test_agents.py` | Sub-agent 框架 |
| `test_dashboard.py` | FastAPI Dashboard |
| `test_trace.py` | Agent Trace |
| `test_sanitize.py` | 中文输出 + sanitize |
| `test_chat_rendering.py` | 聊天窗 + TTS 同步 |
| `test_installed_apps.py` / `test_open_tools*.py` | 应用启动工具 |
| `test_skill.py` | Skill 系统 |
| `test_state.py` | 状态管理 |
| `test_reminder.py` | 提醒 |
| `test_asr.py` | ASR |
| `test_character.py` | 情绪/标签解析 |
| `test_memory.py` | 长期记忆 |
| `test_langchain_agent.py` | LangChain 后端 |

### 功能验证

```powershell
python scripts/verify_features.py
```

输出 94+ 项 `[OK]` / `[FAIL]`，明细写入 `data/feature_check.txt`。`scripts/verify_features.py` 同时跑 4 个 Eval 用例，写入 `data/eval_report.md`。

## CI 接入

```yaml
# .github/workflows/test.yml (示例)
- name: pytest
  run: python -m pytest tests/ -q
- name: verify_features
  run: python scripts/verify_features.py
```

注：`tests/test_chat_rendering.py`、`test_live2d_renderer.py` 部分用例需要 PyQt5 + 离线 `assets/`。CI 上如没图形环境，跑 `pytest --ignore=tests/test_chat_rendering.py`。

## 开发工具脚本

### `tools/clone_voice.py`

MiniMax 声音克隆交互式向导（开权限后用；本项目主力走 GPT-SoVITS 离线）。

### `tools/_legacy/`

历史脚本归档（一次性截图 / 验证 / 资产生成）。**不要在生产路径调用**，仅留作历史查阅。详见 `tools/_legacy/README.md`。

## 调试技巧

### 启动横幅

`python main.py` 启动横幅会显示：角色名 / 模型 / Ollama 探测结果 / 工具数 / 记忆条数 / 后端类型 / 数据文件路径。

### Crash 日志

未捕获异常会写到 `crash.log`（gitignore）。最近一次崩溃栈顶是 `app/animation/motion.py:308 _step_move` 时通常是运动模块，复制给开发者。

### Trace 查看

```powershell
# 启动 dashboard
python -m app.web.dashboard --port 8765
# 浏览器打开 http://127.0.0.1:8765
# 或聊天窗标题栏 📊 按钮 / /调试 命令
```

每次 Agent 运行都会落 `data/traces.db`，可在 Dashboard 看完整链路。

### 单独跑 Live2D 真实渲染

`tools/verify_live2d.py` 已在 _legacy，新接入建议直接看 `tests/test_live2d_renderer.py` 的真实渲染段（已带 model3.json fixture）。