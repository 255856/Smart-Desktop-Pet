# 创新点 2: 桌宠养成状态对 Agent 决策的耦合机制

> 实现状态: 已落地 (v21 全套 + v22 主程序接入), 全套 1186 测试通过
> 论文对应: §3 (机制) / §4 (实验 1 量化) / §4.4.1 (评估)

## 1. 一句话概括

把"桌宠养成状态"从只能影响桌宠头顶表情, 升级成能影响 **Agent 整个决策路径** —— 心情、饱食、等级三档离散化, 跟 60 个工具的 5 档风险等级组合, 走"权限层 + 策略层 + 反馈层"三层耦合。论文核心命题 `Action = f(intent, tools, context, S)` 已经落地代码。

## 2. 改了哪些代码

按工程路径, 改动分布在 5 个新文件 + 4 个修改文件:

### 2.1 新增文件

| 路径 | 角色 |
|---|---|
| `app/engine/tools/_risk.py` | 60 个工具的 5 档风险表 (`TOOL_RISK_LEVEL`) + `risk_for_tool()` / `is_risky()` |
| `app/brain/companion_policy.py` | 27 条规则 (`RULES_TABLE`) + `lookup_decision()` |
| `app/brain/_companion_registry.py` | thread_id → PetState 注册表, 让 dynamic_prompt 拿到 state |
| `app/experiments/state_decision_consistency.py` | 实验 1 跑数脚本 (100 scenario × 27 state) |
| `tests/test_{risk,companion_policy,state_outcome,risky_companion,strategy_prompt,outcome_hook,settings_dev_mode,state_decision_consistency}.py` | 单元测试 |

### 2.2 修改文件

| 路径 | 改了哪里 |
|---|---|
| `app/engine/state.py` | `PetState.quantize()` (LOW/MID/HIGH 三档 + hysteresis) + `apply_outcome()` (feedback 闭环) |
| `app/brain/hallucination_guard.py` | `_RISKY_TOOLS` 改从 `_risk` 派生 + `RiskyToolConfirm.set_companion_state()` + 中间件优先查表 + `grounding_dynamic_prompt` 加策略层 |
| `app/brain/langchain_agent.py` | `set_companion_state()` + `_finish_run_with_outcome()` (成功/失败回写 PetState) |
| `app/ui/settings_window.py` | 「开发者模式」开关 (控制 Tab) + sliders + 决策预览 + **策略层预览** (状态 Tab) |
| `app/ui/ui_controller.py` | 接 `dev_state_pushed` signal → 同步给 `RiskyToolConfirm` + `LangChainAgent` |

## 3. 三层耦合机制

### 3.1 权限层 (核心命题 f = f(..., S) 落地)

`lookup_decision(pet_state, tool_name)` 把 `S × R × Intent → {ALLOW, CONFIRM, REJECT, REFUSE}`:

- `S` = 桌宠量化三档 (sat / mood / level)
- `R` = 60 个工具的 5 档风险
- 27 条规则按 priority 升序匹配, 具体见 `companion_policy.RULES_TABLE`
- 默认 fallback: SAFE/LOW/MEDIUM → ALLOW, HIGH → CONFIRM, IRREVERSIBLE → REJECT

### 3.2 策略层 (system_prompt 动态注入)

`_strategy_guidance(state)` 根据桌宠状态生成长短不一的策略段, 拼到 `grounding_dynamic_prompt`:

- 心情 LOW → "请用简短的话回复, 一次只走一步, 不要主动建议额外操作"
- 心情 HIGH + 饱食 HIGH + 等级 ≥ 15 → "桌宠和主人都状态好, 你可以主动建议一两步能增强效率的扩展动作"
- 饱食 LOW → "优先完成核心任务, 避免多步长链路"

注入路径走 `_companion_registry[thread_id]` → `grounding_dynamic_prompt` 读 `request.config["configurable"]["thread_id"]` 取回。

### 3.3 反馈层 (任务结果 → PetState, 闭环)

`PetState.apply_outcome(outcome_type, tool_name, risk_level)`:

| outcome_type | feeling | food | exp |
|---|---|---|---|
| `success` | +0.5+0.2·risk | — | +risk |
| `failure` | -0.3 | -0.1 | — |
| `rejected` | -1.5 | — | — |
| `accepted` | +1.0 | — | +2 |

触发点:
- `LangChainAgent._finish_run_with_outcome(success=True/False)` → run 末尾整体调
- `RiskyToolConfirm._fire_companion_reject_outcome()` → 桌宠自主 REJECT/REFUSE 拦阻时调

## 4. 实验 1 量化数据 (论文 §4.4.1)

跑数脚本: `python -m app.experiments.state_decision_consistency`

| 指标 | 实际 | 论文目标 | 状态 |
|---|---|---|---|
| 决策差异率 | **54%** | ≥ 30% | PASS |
| 规则命中率 | **100%** | ≥ 85% | PASS |

54% 表示: 100 个 scenario 中, 桌宠状态在 (sat, mood, level) 三档变化时, **54 个任务**至少有一个状态下决策不一样 (跟其它状态比)。100% 表示 `lookup_decision` 永远跟 `RULES_TABLE` 优先级排序一致, 查表路径无 bug。

详细数据在 `app/experiments/results/state_decision_consistency.json` (论文 §4 引用)。

## 5. 性能 / 资源开销

- `_companion_registry`: 进程内 `dict` + 锁, O(1) 读写
- `lookup_decision`: 27 条规则 + 5 档风险判定, 单次 < 0.01ms
- `grounding_dynamic_prompt`: 每次模型调用前调, 加 ~200 字策略文本 (OpenAI/MiniMax token 增量 < 0.5%)
- `PetState.apply_outcome`: O(1) 操作, 不读盘

## 6. 答辩相关 (论文可引数据)

- §4.4.1 实验 1 数据: 见上表 + `state_decision_consistency.json`
- §4.3.1 权限层: 代码 `companion_policy.py` 27 条规则 + 表 5.1 待论文整理
- §4.3.2 策略层: `_strategy_guidance` 函数 + 4 种 case (HIGH/HIGH, HIGH sat + MID mood, LOW mood, sat LOW)
- §4.3.3 反馈层: `apply_outcome` 函数 + 主程序接入
- §6.2 风险等级表 5 档: 见 `_risk.TOOL_RISK_LEVEL`

## 7. 进一步工作 (留给后续任务)

| 编号 | 任务 | 范围 |
|---|---|---|
| E | 实证 A/B (7 天 × 12 人) | 论文 §4.4.2, 需要招募志愿 |
| F | 消融研究 (逐条禁用 27 规则) | 论文 §4.4.3 |
| G | 个性化自适应映射 | 论文扩展点 F |
| H | LLM-as-judge 评测 (三档 mood 下模型风格差异) | 论文扩展点 H |
