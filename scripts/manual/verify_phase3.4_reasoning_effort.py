"""手动验证 Phase 3.4 Reasoning effort 透传。

用法:
    cd E:\study\desktop-pet
    $env:PYTHONPATH="E:\study\desktop-pet"
    python scripts\manual\verify_phase3.4_reasoning_effort.py

输出:
    - LLMConfig 不同 reasoning_effort 值(低/中/高/未设)
    - LLMClient._build_payload 是否带这个字段
    - LangChainAgent._build_model 是否把字段传给 ChatOpenAI

不消耗 token,纯本地。
"""
import os
import sys

# 脚本在 scripts/manual/ 下 → 往上两层才是项目根（app 包所在）
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
from app.brain.llm_client import LLMClient, ChatMessage
from app.brain.llm_client import LLMConfig
from app.core.config import LLMConfig as CoreLLMConfig
from app.engine.tools import ToolRegistry


def main():
    print("=== Phase 3.4 Reasoning effort 手动验证 ===\n")

    print("[step 1] LLMConfig 默认 None")
    cfg = CoreLLMConfig()
    print(f"  - reasoning_effort = {cfg.reasoning_effort!r}")
    assert cfg.reasoning_effort is None

    print("\n[step 2] LLMClient._build_payload 在 reasoning_effort=None 时不带字段")
    client = LLMClient(LLMConfig(reasoning_effort=None), system_prompt="sys")
    payload = client._build_payload([ChatMessage(role="user", content="hi")])
    print(f"  - payload 含 reasoning_effort: {'reasoning_effort' in payload}")
    assert "reasoning_effort" not in payload

    print("\n[step 3] 设 low → payload['reasoning_effort']='low'")
    client = LLMClient(LLMConfig(reasoning_effort="low"), system_prompt="sys")
    payload = client._build_payload([ChatMessage(role="user", content="hi")])
    print(f"  - payload['reasoning_effort'] = {payload.get('reasoning_effort')!r}")
    assert payload["reasoning_effort"] == "low"

    print("\n[step 4] 设 high → payload 带 high")
    client = LLMClient(LLMConfig(reasoning_effort="high"), system_prompt="sys")
    payload = client._build_payload([ChatMessage(role="user", content="hi")])
    print(f"  - payload['reasoning_effort'] = {payload.get('reasoning_effort')!r}")
    assert payload["reasoning_effort"] == "high"

    print("\n[step 5] 设空字符串 '' → 当 None 处理,不带字段")
    client = LLMClient(LLMConfig(reasoning_effort=""), system_prompt="sys")
    payload = client._build_payload([ChatMessage(role="user", content="hi")])
    print(f"  - payload 含 reasoning_effort: {'reasoning_effort' in payload}")
    assert "reasoning_effort" not in payload

    print("\n[step 6] LangChainAgent._build_model 透传到 ChatOpenAI")
    agent = LangChainAgent(
        llm_cfg=LLMConfig(reasoning_effort="medium"),
        registry=ToolRegistry(),
        cfg=LangChainAgentConfig(),
        persona="p", thread_id="t",
    )
    model = agent._build_model()
    print(f"  - model.reasoning_effort = {getattr(model, 'reasoning_effort', None)!r}")
    assert getattr(model, "reasoning_effort", None) == "medium"

    agent2 = LangChainAgent(
        llm_cfg=LLMConfig(),
        registry=ToolRegistry(),
        cfg=LangChainAgentConfig(),
        persona="p", thread_id="t",
    )
    model2 = agent2._build_model()
    eff2 = getattr(model2, "reasoning_effort", None)
    print(f"  - 不设时 model.reasoning_effort = {eff2!r}")
    assert eff2 is None

    print("\n=== 全部通过 ===")
    print("\n[note] 在 config.yaml 里:")
    print("    llm:")
    print("      reasoning_effort: medium   # low / medium / high")
    print("       仅支持推理模型(OpenAI o1/o3、DeepSeek-R1、MiniMax 思考模式)。")
    print("       普通聊天模型传了会忽略/报错,谨慎开。")


if __name__ == "__main__":
    main()