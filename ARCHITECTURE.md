# 桌宠智能体架构与演进规划（ARCHITECTURE.md）

> 本文档面向简历读者 / 面试官，说明 **桌面宠物 → 桌面智能体** 的演进路径与技术深度。
>
> 当前版本：`v2.0 — Pet-Agent`（基础 Agent + 工具调用 + 长期记忆 + 主动行为）
> 目标版本：`v3.0 — Multi-Agent Desktop Companion`（RAG + Plan/Reflect + MCP + Sub-agent + Trace）

---

## 1. 一句话定位

> **一个能感知主人状态、调用工具、看屏幕、主动关怀、并在多智能体协作下完成真实任务的本地桌面智能体。**

不是「另一个 LLM 聊天桌宠」，是 **「OpenInterpreter + 桌宠皮肤 + 长期记忆 + 多智能体」** 的工程化产物。

---

## 2. 现状盘点（v2.0 已完成）

| 模块 | 文件 | 技术点 |
|------|------|--------|
| LLM Client | `app/brain/llm_client.py` | OpenAI 兼容 SSE 流式 + 推理模型过滤 + 工具调用增量聚合 |
| Agent Loop | `app/brain/agent.py` | Function Calling，最多 6 轮，多工具并行 + cancel |
| Tool Registry | `app/engine/tools.py` | 12 个内置工具，OpenAI Schema 输出 |
| Memory Store | `app/brain/memory.py` | JSON 落盘 + 子串匹配 + 去重 + 分类注入 |
| Memory Evolution | `app/brain/memory_evolution.py` | 后台线程定期合并/清理 |
| Proactive Brain | `app/brain/proactive.py` | QTimer + 共享 LLMClient，主动关心 |
| Brain Controller | `app/brain/brain_controller.py` | Memory + Tool + Proactive 装配 + 信号 |
| Pet Window | `app/ui/pet_window.py` | 透明/置顶/拖动/触摸热区/边缘隐藏 |
| Sprite Atlas | `app/animation/sprite_atlas.py` | VPet 同款 Frame(duration_ms) |
| State Engine | `app/engine/state.py` | 数值衰减 + 被动回复 + Mode 切换 |
| TTS / ASR | `app/voice/` | edge-tts + faster-whisper |

**单测**：`tests/` 下 6 个测试文件，覆盖 Tool/Memory/ASR/State/Reminder。

---

## 3. v3.0 演进路线（核心 8 个 phase）

### Phase A — 向量化长期记忆（RAG）

**目标**：把字符串子串匹配升级为语义检索。

- 引入 `sentence-transformers` 本地嵌入模型（`paraphrase-multilingual-MiniLM-L12-v2`，约 470MB，离线可用）
- 新增 `VectorMemoryIndex`：基于 `sqlite-vec` 或 `numpy` + 余弦相似度的本地向量库
- 记忆写入双写：JSON 存全文 + 向量库存 embedding
- `recall_memory` 工具升级：先向量召回 top-k，再注入给 LLM
- 兼容现有 JSON 存储（自动迁移）

**简历价值点**：本地 RAG、嵌入模型选型、向量库落地、离线友好

### Phase B — Planner / Executor / Reflector 三层 ReAct

**目标**：把单 Agent Loop 升级为 **Plan-and-Execute + 反思**。

```
用户输入 ─► Planner（生成/修订计划）
                │
                ▼
           Executor（执行下一步：调用工具 / 调子 Agent）
                │
                ▼
           Reflector（评估：成功 / 失败 / 重做 / 改计划）
                │
                └─► 回到 Planner（修订）or 产出最终答案
```

- 新增 `app/brain/planner.py`：把用户目标拆成步骤 DAG（链式 + 分支 + 重试）
- 新增 `app/brain/reflector.py`：基于工具结果评估是否完成 / 是否要重做 / 是否要调整计划
- 失败重试：默认 2 次，超过则回退到 Planner 改计划

**简历价值点**：ReAct / Plan-and-Execute、状态机、失败恢复、自我反思

### Phase C — MCP（Model Context Protocol）协议接入

**目标**：工具从硬编码升级为 **MCP 客户端**，兼容 Anthropic MCP 标准。

- 新增 `app/mcp/client.py`：stdio JSON-RPC 客户端
- 新增 `app/mcp/registry.py`：MCP server 注册表（`mcp_servers.yaml`）
- 内置示例 MCP server：`mcp_servers/filesystem.py`（沙箱文件访问）/ `mcp_servers/terminal.py`（受限 shell）
- ToolRegistry 自动桥接：MCP 工具 ↔ OpenAI tool schema
- 危险工具走 MCP 权限声明 + 用户确认

**简历价值点**：MCP 协议理解与实现、stdio JSON-RPC、沙箱安全设计

### Phase D — Sub-agent 多智能体协作

**目标**：多专家智能体 + 主智能体调度。

- 新增 `app/agents/base.py`：`BaseAgent` 抽象（system prompt + 工具子集 + memory scope）
- 内置三个专家：
  - `CodeAgent`：写代码 / 改代码 / 运行代码（隔离 venv）
  - `ResearchAgent`：联网搜索 / 文档检索 / 综合
  - `LifeAgent`：日程 / 提醒 / 桌宠自身状态（默认 Agent）
- `Orchestrator`：根据用户意图分派到对应专家；专家之间可相互调用（限白名单）
- 每个专家有独立的 system prompt 和受限工具集

**简历价值点**：Multi-agent Orchestration、专家分工、权限隔离

### Phase E — Agent Trace / 可观测性 / Web Dashboard

**目标**：决策可回放、可调试、可评估。

- 新增 `app/brain/trace.py`：每次 Agent 运行记录完整 trace（plan / tool call / tool result / reflection）
- 落盘 SQLite：`data/traces.db`
- 新增 `app/web/server.py`：FastAPI 小型面板
  - `/api/traces` — 最近 N 条 trace
  - `/api/traces/{id}` — 完整 trace（含每步 prompt / response / tool）
  - `/api/memory` — 记忆列表 + 相似度可视化
  - `/api/stats` — 工具调用频次 / 成功率 / 平均耗时
- 桌宠右键菜单加「打开调试面板」

**简历价值点**：可观测性、FastAPI、SQLite、前端可视化

### Phase F — 记忆时间衰减 / 重要性评分 / 冲突检测

**目标**：让记忆有「生命周期」，而不是无限堆。

- `MemoryItem` 加字段：`importance`（0-1）/ `last_access_ts` / `access_count`
- `_select_for_injection` 升级为：importance × 时新度 × 类别优先级 综合排序
- `forget_if_expired` 加 importance 阈值：高重要性记忆永不过期
- 冲突检测升级：用 embedding 余弦相似度检测，新记忆与旧记忆相似度 > 0.9 时让 LLM 决定覆盖/保留/并存
- 后台 MemoryEvolution 升级为 `MemoryCurator`：定期摘要、合并、归纳

**简历价值点**：记忆工程、知识生命周期管理

### Phase G — Evaluator（Agent 评估）

**目标**：让 Agent 质量可量化。

- 新增 `app/eval/`：内置 30+ 评测任务（设提醒 / 记事实 / 算数学 / 开应用 / 多步推理）
- 指标：成功率 / 平均轮数 / 平均 token / 失败模式分布
- CI 友好：`pytest tests/eval/ -m eval` 一键回归
- 评估报告：`data/eval_report.md`（含每条用例的 trace 链接）

**简历价值点**：Agent Evaluation、测试驱动 AI、CI/CD 集成

### Phase H — 工程化打磨

- 类型严格：所有模块 `pyright --strict` 通过
- 文档：每个模块有 docstring + 用法示例
- 配置：`config.yaml` 加 `mcp_servers:` / `sub_agents:` / `eval:` / `dashboard:` 段
- 日志：结构化 JSON 日志，方便 ELK / Loki 接入
- Docker：可选 `Dockerfile`（Linux 也能跑）
- GitHub Actions：lint + test + eval 自动跑

---

## 4. 技术亮点（写在简历「项目描述」栏）

1. **多智能体桌面助理**：内置 Planner-Executor-Reflector 三层 Agent，支持 MCP 协议扩展工具，可调用 Code/Research/Life 三个专家子 Agent
2. **本地 RAG 长期记忆**：本地 sentence-transformers 嵌入 + sqlite-vec 向量库，支持语义检索 / 时间衰减 / 冲突检测
3. **可观测性**：每次 Agent 运行落 trace 到 SQLite，提供 FastAPI Dashboard 回放决策过程
4. **完整工具生态**：12+ 内置工具 + MCP server 扩展，覆盖提醒 / 记忆 / 系统操作 / 截图 / 计算 / 单位换算 / 农历
5. **工程化**：模块化（brain/engine/animation/ui/voice/mcp/agents/eval/web 八层）、单元测试 + Agent 评估、严格类型

---

## 5. 不要做的（Keep It Simple）

- ❌ 不做微调 / LoRA：演示项目不需要训练
- ❌ 不做端侧 LLM：模型推理走云端 API，本地只做记忆 / 工具 / 编排
- ❌ 不做账号体系 / 云同步：单机项目，保护隐私
- ❌ 不做 Web UI 替代桌面：保留桌宠作为主交互面，Dashboard 仅调试用
