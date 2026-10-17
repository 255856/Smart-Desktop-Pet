"""v31 端到端 probe: 验证 v31 把拒绝指令挪到 prompt 末尾后, 实际 LLM 看到的 system_prompt."""
import sys
sys.path.insert(0, r'E:\study\desktop-pet')

from app.engine.state import PetState
from app.brain.hallucination_guard import grounding_dynamic_prompt
from app.brain._companion_registry import set_state_for_thread, clear_all, _REGISTRY

clear_all()

state = PetState()
state.feeling = 0
state.strength = 0
state.strength_food = 0
state.strength_drink = 0
state.likability = 0
state.health = 0
state.normalize()
print("1. state.quantize:", state.quantize())

thread_id = "probe-thread-v31"
set_state_for_thread(thread_id, state)

class FakeRequest:
    def __init__(self, tid, base_sp):
        self.system_prompt = base_sp
        self.config = {"configurable": {"thread_id": tid}}

base_persona = "你是一个有用的桌面助手, 名字叫小汐。"
request = FakeRequest(thread_id, base_persona)
result_sp = grounding_dynamic_prompt(request)
print("2. grounding_dynamic_prompt 输出长度:", len(result_sp))
print()

print("=" * 70)
print("完整 system_prompt (v31):")
print("=" * 70)
print(result_sp)
print("=" * 70)
print()

# 关键: v31 拒绝指令必须在 prompt 末尾 (在反幻觉基线**之后**)
# 验证位置
sp = result_sp
confirm_protocol_pos = sp.find("高风险工具 confirm 协议")
v31_override_pos = sp.find("本轮最高优先级")
strategy_block_pos = sp.find("【桌宠状态策略")

print(f"位置信息:")
print(f"  strategy 块 (开头): pos {strategy_block_pos}")
print(f"  confirm 协议 (中间): pos {confirm_protocol_pos}")
print(f"  v31 override (末尾): pos {v31_override_pos}")
print()
if (v31_override_pos > confirm_protocol_pos
        and confirm_protocol_pos > strategy_block_pos):
    print("[OK] 顺序: strategy -> confirm 协议 -> v31 override (末尾)")
    print("     LLM 看到末尾的 v31 override 优先级最高")
else:
    print("[FAIL] 顺序错! 拒绝指令没在末尾, LLM 仍可能偏向 confirm 协议")
print()

# 关键内容
checks = [
    ("v31 override 块存在", "本轮最高优先级"),
    ("明确说'忽略上面 confirm 协议'", "忽略**"),
    ("明确说'以这段为准'", "以这段为准"),
    ("明确拒绝", "必须直接拒绝"),
    ("具体回复示例", "我状态不好"),
    ("不要 preemptive 问", "不要 preemptive"),
    ("lock_screen 点名", "lock_screen"),
]
for label, kw in checks:
    ok = kw in sp
    print(f"   [{'OK' if ok else 'FAIL'}] {label}: 含 '{kw}' = {ok}")

# 8. 边界: state 全 HIGH 时, prompt 不应有 v31 override
print()
print("=" * 70)
print("边界: state 全 HIGH 时, prompt 不应有 v31 override")
print("=" * 70)
clear_all()
state2 = PetState()
state2.feeling = 100
state2.strength_food = 100
state2.likability = 100
state2.normalize()
set_state_for_thread(thread_id, state2)
result_sp2 = grounding_dynamic_prompt(request)
has_override = "本轮最高优先级" in result_sp2
print(f"   state 全 HIGH, prompt 仍含 v31 override: {has_override}")
print(f"   期望: False (不应误伤正常 confirm 流程)")
assert not has_override, "误伤了! 全 HIGH 不应有拒绝指令"
