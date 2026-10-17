"""v30 端到端 probe: 模拟主人 dev mode 设 0 -> 输 '帮我锁屏' -> 看 LLM system_prompt 是否被注入拒绝指令."""
import sys
sys.path.insert(0, r'E:\study\desktop-pet')

from app.engine.state import PetState
from app.brain.hallucination_guard import _strategy_guidance

# 1. 主人 dev mode 设 0
state = PetState()
state.feeling = 0
state.strength = 0
state.strength_food = 0
state.strength_drink = 0
state.likability = 0
state.health = 0
state.normalize()
print("1. state.quantize:", state.quantize())
print()

# 2. 模拟 LLM 调用时 grounding_dynamic_prompt 跑 _strategy_guidance
guidance = _strategy_guidance(state)
print("2. 注入到 system_prompt 的策略:")
print(guidance)
print()

# 3. 验证关键内容
checks = [
    ("拒绝", "直接拒绝"),
    ("工具列举", "lock_screen"),
    ("不要 confirm", "不要走"),
    ("不要 preemptive 问", "preemptive"),
]
for label, kw in checks:
    ok = kw in guidance
    print(f"   [{'OK' if ok else 'FAIL'}] {label}: 含 '{kw}' = {ok}")
print()

# 4. 主人 好感 LOW 也要触发
state2 = PetState()
state2.likability = 5
state2.feeling = 100  # 心情好
state2.strength_food = 100
state2.normalize()
guidance2 = _strategy_guidance(state2)
print("3. favor LOW 也要触发拒绝:")
print("   量化:", state2.quantize())
print("   guidance 有 '直接拒绝':", "直接拒绝" in guidance2)

# 5. 状态都好 (全 HIGH) 不能误伤
state3 = PetState()
state3.feeling = 100
state3.strength_food = 100
state3.likability = 100
state3.normalize()
guidance3 = _strategy_guidance(state3)
print()
print("4. 全 HIGH (好状态) 误伤检查:")
print("   量化:", state3.quantize())
print("   guidance 不应含 '直接拒绝':", "直接拒绝" not in guidance3)
