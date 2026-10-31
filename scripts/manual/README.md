# 手动测试脚本(主程序未接线时,直接验证引擎层)

每个脚本都是一个完整的 PowerShell 命令,直接在项目根目录跑即可。
所有脚本都是「只读 + 不发网络请求」(用 mock / fake LLM),不会真的消耗 token。

## 跑哪个看哪个 Phase

| 脚本 | 验证 Phase | 期望看到 |
|---|---|---|
| `verify_phase2.2_plan_mode.py` | 2.2 Plan mode | plan() 异步生成器产出 plan-mode 指令提示,不调任何工具 |
| `verify_phase2.3_agents_md.py` | 2.3 AGENTS.md 自动加载 | cwd 下的 AGENTS.md 内容被拼到 system_prompt |
| `verify_phase2.4_sessions.py` | 2.4 SessionStore | 多次 create 后 list 看到多个 session,thread_id 唯一 |
| `verify_phase3.1_task_tool.py` | 3.1 Subagent Task 工具 | Task 工具能调起子 agent factory |
| `verify_phase3.2_skills.py` | 3.2 Skills 系统 | skills_dir 里的 .md 进 system_prompt |
| `verify_phase3.3_memory.py` | 3.3 User-wide memory | memory_store.system_block() 进 system_prompt |
| `verify_phase3.4_reasoning_effort.py` | 3.4 Reasoning effort | reasoning_effort 非空时进 payload,空时不进 |

## 怎么跑

PowerShell(Windows):
```powershell
cd E:\study\desktop-pet
$env:PYTHONPATH="E:\study\desktop-pet"
python scripts\manual\verify_phase2.2_plan_mode.py
```

## 跟 pytest 跑过的 994 个测试的关系

这些脚本测的是「单条数据通路」(端到端、人工可读输出);pytest 测的是「全部单元 + 集成覆盖」。
两者重叠但视角不同:pytest 是机器检查,这里是给你「眼见为实」。