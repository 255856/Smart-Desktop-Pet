# app/brain/_legacy/

这里是 v3.0 写入但**生产路径未挂载**的 Plan-Execute-Reflect 路线遗产，仅供 `tests/test_planner.py`、`tests/test_anti_hallucination_plan.py`、`tests/test_parallel_plan.py`、`scripts/verify_features.py` 等做回归覆盖。

当前主用的 Agent 循环是 `app/brain/agent.py` 的 `AgentLoop`（手写 ReAct 单循环，模型自主决定调什么工具、调用几次、什么时候给 final answer）—— 见 `docs/architecture.md`。

## 各模块原职责

- `plan.py` — `Plan` / `Step` 数据模型 + JSON 解析
- `planner.py` — `Planner`：把用户目标拆成 JSON Plan（含 thought / kind / arguments / parallel_group）
- `executor.py` — `PlanExecutor`：并行执行（同 `parallel_group` 步骤 gather）+ 重试
- `reflector.py` — `HeuristicReflector` / `LLMReflector` / `make_reflector`：评估 ok / retry / replan
- `agent_v2.py` — `AgentLoopV2` + `make_agent_loop`：上面三件套的包装

## 为什么没有挂进 BrainController

`BrainController.create_agent()` 当前直接返回 `AgentLoop`（`app/brain/agent.py`），V2 路线因为以下两点暂未启用：

1. Planner 一次性从模型拿到完整 JSON plan，国内 LLM 偶发产出低质 plan 会拖累响应
2. V2 内部仍然要调 `agent.py.AgentLoop` 作为最里层循环，多一层包装未明显改善用户体验

如果未来要启用，需要在 `BrainController.create_agent()` 里加一个 `brain.agent.enable_planning` 配置开关，并把 V2 包成可选项。