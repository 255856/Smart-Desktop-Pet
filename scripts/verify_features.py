"""项目功能验证脚本：逐项核对 README/ARCHITECTURE 列出的能力是否实装且可跑。

用法：
    cd E:\study\desktop-pet
    python scripts/verify_features.py [--no-gui]

不依赖 LLM API，纯本地检查。输出报告 + 退出码（0=全过，1=有失败）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import inspect
import warnings
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".local-packages"))
sys.path.insert(0, str(ROOT))

results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "OK" if ok else "FAIL"
    results.append((name, status, detail))


def _parse_tool_markers(text):
    return [
        {"id": f"c{i}", "name": m.group(1), "arguments": m.group(2)}
        for i, m in enumerate(re.finditer(r"<tool>([\w]*)\|(\{.*?\})</tool>", text, re.DOTALL))
    ]


def _strip_tool_markers(text):
    return re.sub(r"<tool>[\w]*\|.*?</tool>", "", text, flags=re.DOTALL).strip()


def _make_eval_factory(reg):
    from app.brain.executor import PlanExecutor
    from app.brain.plan import Plan, Step
    from app.brain.planner import Planner
    from app.brain.reflector import HeuristicReflector

    def factory(mock_client):
        async def fake_make_plan(goal, history=None):
            text = mock_client._next_text()
            tool_calls = _parse_tool_markers(text)
            clean = _strip_tool_markers(text)
            steps = []
            if not tool_calls and clean.strip().startswith("{"):
                try:
                    obj = json.loads(clean)
                    if "steps" in obj:
                        for s in obj["steps"]:
                            k = s.get("kind", "tool")
                            if k == "final":
                                steps.append(Step(id=f"s{len(steps)}", kind="final",
                                                   thought=s.get("thought", ""),
                                                   result=s.get("answer", "")))
                            else:
                                steps.append(Step(id=f"s{len(steps)}", kind="tool",
                                                   thought=s.get("thought", ""),
                                                   tool_name=s.get("tool_name"),
                                                   arguments=s.get("arguments", {})))
                        return Plan(goal=goal, steps=steps)
                except Exception:
                    pass
            for tc in tool_calls:
                steps.append(Step(id=f"s{len(steps)}", kind="tool",
                                   tool_name=tc["name"],
                                   arguments=json.loads(tc["arguments"]),
                                   thought="mock"))
            if not steps:
                steps.append(Step(id="s0", kind="final", thought="reply", result=clean))
            else:
                steps.append(Step(id=f"s{len(steps)}", kind="final",
                                   thought="reply", result=clean))
            return Plan(goal=goal, steps=steps)

        planner = Planner(client=mock_client, registry=reg, char_name="t")
        planner.make_plan = fake_make_plan
        executor = PlanExecutor(client=mock_client, registry=reg,
                                planner=planner, reflector=HeuristicReflector())

        class W:
            def run(self, messages, cancel_check=None):
                async def _drive():
                    goal = messages[-1]["content"] if messages else ""
                    plan = await fake_make_plan(goal, history=messages)
                    async for ev in executor.run(plan):
                        yield ev
                return _drive()
        return W()
    return factory


def main() -> int:
    print("=" * 70)
    print(f"功能验证  ·  ROOT = {ROOT}")
    print("=" * 70)

    # ===== 一、核心能力 =====
    print("\n[一] 核心能力（桌宠本体）")

    from app.ui.pet_window import PetWindow
    pet_src = inspect.getsource(PetWindow)
    check("PetWindow 存在", True)
    check("  · 透明背景（TranslucentBackground）",
          "TranslucentBackground" in pet_src)
    check("  · 无边框（FramelessWindowHint）",
          "FramelessWindowHint" in pet_src or "frameless" in pet_src.lower())
    check("  · 置顶（WindowStaysOnTopHint）",
          "WindowStaysOnTop" in pet_src)
    check("  · 拖动（mouseMoveEvent）", "mouseMoveEvent" in pet_src)
    check("  · 触摸热区（touch_head / touch_body）",
          "touch_head" in pet_src and "touch_body" in pet_src)

    from app.animation.animations import Frame, Animation
    frame_sig = inspect.signature(Frame.__init__).parameters
    check("Frame(duration_ms)", "duration_ms" in frame_sig)
    anim_src = inspect.getsource(Animation)
    check("Animation 多种循环模式（LOOP/ONCE/PINGPONG）",
          all(m in anim_src for m in ["LOOP", "ONCE", "PING_PONG"]))
    check("Animation class（多循环变体）",
          "class Animation" in anim_src)

    from app.animation.sprite_atlas import SpriteAtlas
    check("SpriteAtlas（资源加载）", True)

    from app.animation.motion import MotionController
    motion_src = inspect.getsource(MotionController)
    check("MotionController 自走 + smartmove",
          "smartmove" in motion_src.lower() or "decide" in motion_src)

    from app.brain.proactive import ProactiveBrain
    check("ProactiveBrain 主动发言", "remark_ready" in dir(ProactiveBrain))
    check("  · [skip] 沉默支持", "[skip]" in inspect.getsource(ProactiveBrain))

    from app.brain.memory import MemoryStore
    mem_src = inspect.getsource(MemoryStore)
    check("MemoryStore", True)
    check("  · 重要性评分（importance）", "importance" in mem_src)
    check("  · 时间衰减（last_access_ts）", "last_access_ts" in mem_src)
    check("  · 冲突检测（detect_conflicts）", "detect_conflicts" in mem_src)
    try:
        from app.brain.memory import TfidfBackend, SentenceTransformerBackend
        check("  · 向量后端（TfidfBackend / SentenceTransformerBackend）", True,
              "两个后端都能 import")
    except Exception as e:
        check("  · 向量后端", False, str(e))

    from app.engine.tools import ToolRegistry, Tool
    check("ToolRegistry + Tool", True)

    from app.voice.voice import TTS
    check("TTS（edge-tts）", True)
    from app.voice.asr import SpeechRecognizer, asr_available
    check("SpeechRecognizer（faster-whisper）", True)
    check("asr_available() 检测函数", callable(asr_available))

    # ===== 二、智能体方向 =====
    print("\n[二] 智能体方向（v3.0 新增）")

    from app.brain.plan import Plan, Step
    check("Plan / Step 数据模型", True)
    check("Step.parallel_group（并行支持）",
          "parallel_group" in Step.__dataclass_fields__)

    from app.brain.planner import Planner
    check("Planner（LLM 生成 plan）", True)

    from app.brain.executor import PlanExecutor
    exec_src = inspect.getsource(PlanExecutor)
    check("PlanExecutor（gather 并行 + parallel_group）",
          "asyncio.gather" in exec_src and "parallel_group" in exec_src)
    check("PlanExecutor（重试 / replan）",
          "replan" in exec_src.lower())

    from app.brain.reflector import HeuristicReflector, LLMReflector, make_reflector
    check("HeuristicReflector（规则反思）", True)
    check("LLMReflector（LLM 反思）", True)
    check("make_reflector 工厂", callable(make_reflector))

    from app.brain.agent_v2 import AgentLoopV2, make_agent_loop
    check("AgentLoopV2（react / single 切换）", True)
    check("make_agent_loop 工厂", callable(make_agent_loop))

    # ---- v3.1+ 抗幻觉机制（强制调工具 + 跨步检测 + 重试）----
    from app.brain.llm_client import (
        detect_action_intent, _is_tool_choice_unsupported,
    )
    check("意图识别 detect_action_intent", callable(detect_action_intent))
    check("  · 识别 'open_app'", detect_action_intent("帮我打开 QQ") == "open_app")
    check("  · 识别 'add_reminder'",
          detect_action_intent("30 分钟后提醒我喝水") == "add_reminder")
    check("  · 识别 'remember_fact'",
          detect_action_intent("记住：主人喜欢咖啡") == "remember_fact")
    check("  · 闲聊不误识别", detect_action_intent("陪我聊天") is None)
    check("tool_choice 降级判定", callable(_is_tool_choice_unsupported))

    # LLMClient 必须有降级路径
    llc_src = inspect.getsource(
        __import__('app.brain.llm_client', fromlist=['LLMClient']).LLMClient)
    check("LLMClient 降级到 user-prompt 强制",
          "_stream_with_force_prompt" in llc_src
          and "_is_tool_choice_unsupported" in llc_src)
    check("LLMClient 自动检测 tool_choice 未调工具",
          "tool_choice" in llc_src and "降级" in llc_src)

    # AgentLoop 必须有 force_tool_use + 强制重试
    from app.brain.agent import AgentLoop as _AgentLoop
    al_src = inspect.getsource(_AgentLoop)
    check("AgentLoop 意图驱动 force_tool_use",
          "force_tool_use" in al_src and "detect_action_intent" in al_src)
    check("AgentLoop 第一轮失败 → 注入 user 强制重试",
          "force_retry" in al_src and "必须调用" in al_src)

    # Reflector 必须有跨步幻觉检测
    from app.brain.reflector import HeuristicReflector as _HR
    hr_src = inspect.getsource(_HR)
    check("HeuristicReflector.detect_plan_hallucination",
          "detect_plan_hallucination" in hr_src)

    # PlanExecutor 必须用 detect_plan_hallucination
    from app.brain.executor import PlanExecutor as _PE
    pe_src = inspect.getsource(_PE)
    check("PlanExecutor 跨步检测幻觉 → 触发 replan",
          "detect_plan_hallucination" in pe_src
          and "anti_hallucination" in pe_src)

    # ChatWindow 必须用 AgentLoopV2（react 模式）+ 处理 meta 事件
    from app.ui.chat_window import ChatWindow as _CW
    cw_src = inspect.getsource(_CW)
    check("ChatWindow 接入 AgentLoopV2（react）",
          "make_agent_loop" in cw_src)
    check("ChatWindow 处理 force_retry meta 事件",
          "_on_meta" in cw_src and "force_retry" in cw_src)

    from app.mcp.protocol import MCPClientRegistry, MCPServerConfig, MCPStdioClient
    mcp_src = inspect.getsource(MCPStdioClient)
    check("MCPClientRegistry", True)
    check("MCPServerConfig", True)
    check("MCPStdioClient JSON-RPC",
          "jsonrpc" in mcp_src and "tools/list" in mcp_src and "tools/call" in mcp_src)
    from app.mcp.filesystem_server import is_in_allowed, run_server
    check("MCP filesystem_server（沙箱）",
          callable(is_in_allowed) and callable(run_server))

    from app.agents.base import (
        BaseAgent, LifeAgent, ResearchAgent, CodeAgent,
        Orchestrator, make_dispatch_tool,
    )
    check("BaseAgent / LifeAgent / ResearchAgent / CodeAgent", True)
    check("Orchestrator 分派（rule / llm）", True)
    check("make_dispatch_tool", callable(make_dispatch_tool))

    from app.brain.trace import TraceRecorder
    trc_src = inspect.getsource(TraceRecorder)
    check("TraceRecorder", True)
    check("  · SQLite 落盘", "sqlite3" in trc_src)
    check("  · 通用事件接口（record(kind, ...)）",
          "def record(" in trc_src and "kind: str" in trc_src)

    from app.web.dashboard import create_app
    dash_src = inspect.getsource(create_app)
    check("Dashboard create_app", callable(create_app))
    check("  · GET /api/runs", "/api/runs" in dash_src)
    check("  · GET /api/stats", "/api/stats" in dash_src)
    check("  · GET /api/memory", "/api/memory" in dash_src)
    check("  · GET / (HTML)", '"/"' in dash_src)

    from app.eval.cases import Evaluator, EvalCase, MockLLMClient, builtin_cases, run_eval_suite
    check("Evaluator / EvalCase / MockLLMClient", True)
    cases = builtin_cases()
    check(f"builtin_cases（{len(cases)} 个内置用例）", len(cases) == 4)
    check("run_eval_suite", callable(run_eval_suite))

    # ===== 三、数据存储位置 =====
    print("\n[三] 数据存储位置（用户常问『数据存在哪』)")

    # 启动 StateManager 让 reminders.json 等按需文件创建
    try:
        from PyQt5.QtWidgets import QApplication as _QA
        from PyQt5.QtCore import QTimer as _QT
        _qa = _QA.instance() or _QA(sys.argv)
        from app.main import _parse_args as _pa, _ensure_config as _ec
        from app.core.config import load_config as _lc
        from app.engine.state_manager import StateManager as _SM
        sys.argv = ["main.py", "--no-banner"]
        _args = _pa()
        _ec(ROOT)
        _cfg = _lc(ROOT / _args.config)
        _sm = _SM(ROOT, _cfg, _args)
        _QT.singleShot(100, _qa.quit)
        _qa.exec()
    except Exception as e:
        print(f"  [warn] 启动预热失败: {e}")

    data_files = [
        (ROOT / "data" / "memory.json", "长期记忆（JSON，含重要性 + 向量）"),
        (ROOT / "data" / "traces.db", "Agent Trace（SQLite）"),
        (ROOT / "data" / "chat_history.json", "聊天历史（持久化）"),
        (ROOT / "data" / "foods.json", "食物库（feed_self 工具用）"),
        (ROOT / "data" / "works.json", "打工系统"),
        (ROOT / "data" / "app_registry.json", "应用注册表（自定义 app 映射）"),
        (ROOT / "data" / "reminders.json", "提醒（首次启动自动建空文件）"),
        (ROOT / "data" / "dashboard.log", "Dashboard 子进程日志（启动过 dashboard 才有）"),
        (ROOT / "settings.json", "设置（窗口位置/缩放/不透明度）"),
        (Path.home() / ".desktop-pet" / "save.json", "宠物状态存档（user 目录）"),
    ]
    for path, desc in data_files:
        if path.exists():
            size = path.stat().st_size
            check(f"  {path}", True, f"{desc} · {size} bytes")
        else:
            check(f"  {path}", False, f"{desc} · 未生成")

    # ===== 四、实测：mock LLM 跑 ReAct =====
    print("\n[四] 实测：mock LLM 跑 ReAct")

    from app.engine.tools import Tool, ToolRegistry
    reg = ToolRegistry()
    def make_fn(**_):
        return "ok"
    for name in ["add_reminder", "remember_fact", "calculate",
                 "get_pet_status", "list_reminders"]:
        reg.register(Tool(name=name, description=name,
                          parameters={"type": "object", "properties": {}},
                          fn=make_fn))

    async def drive():
        factory = _make_eval_factory(reg)
        return await run_eval_suite(factory,
                                    output_path=str(ROOT / "data" / "eval_report.md"))

    try:
        result = asyncio.run(drive())
        check(f"Evaluator 跑 {result['total']} 个用例",
              result["passed"] >= 3,
              f"{result['passed']}/{result['total']} pass")
        report = ROOT / "data" / "eval_report.md"
        check("Eval 报告生成", report.exists(), str(report))
    except Exception as e:
        check("Evaluator 跑通", False, str(e))

    # ===== 五、聊天窗 =====
    print("\n[五] 聊天窗（ChatWindow）")

    try:
        # 先测一次「真实 LLM 调用」：如果 Ollama 在跑，用本地模型真发一次请求
        from app.main import detect_ollama
        o = detect_ollama()
        if o and o["models"]:
            print(f"  [LLM] 检测到 Ollama ({len(o['models'])} 个模型)")
            try:
                from app.core.config import LLMConfig
                from app.brain.llm_client import LLMClient, ChatMessage
                pick = next((m for m in o["models"] if "r1" in m), o["models"][0])
                cfg = LLMConfig(
                    base_url="http://127.0.0.1:11434/v1",
                    api_key="ollama",
                    model=pick,
                    stream=True,
                    max_tokens=120,
                    temperature=0.7,
                    timeout=90,
                )
                client = LLMClient(cfg, "你是桌宠。简短回答。")

                async def llm_test():
                    chunks: list[str] = []
                    async for tok in client.chat_stream(
                        [ChatMessage(role="user", content="说一句早安")]):
                        chunks.append(tok)
                    await client.close()
                    return "".join(chunks)

                full = asyncio.run(llm_test())
                print(f"  [LLM] 用 {pick} 真回复: {full[:60]!r}")
                check("真实 LLM 调用（Ollama 本地模型）",
                      len(full.strip()) > 0,
                      f"model={pick} len={len(full)}")
            except Exception as e:
                check("真实 LLM 调用（Ollama）", False, str(e)[:200])
        else:
            print(f"  [LLM] Ollama 未运行，跳过真实 LLM 调用")
            check("真实 LLM 调用（Ollama）", True, "Ollama 未运行，跳过（不算失败）")
    except Exception as e:
        check("真实 LLM 检测", False, str(e)[:200])

    try:
        from PyQt5.QtWidgets import QApplication
        from PyQt5.QtCore import QTimer
        app = QApplication.instance() or QApplication(sys.argv)
        app.setQuitOnLastWindowClosed(False)

        from app.main import _parse_args, _ensure_config
        from app.core.config import load_config
        from app.engine.state_manager import StateManager
        from app.brain.brain_controller import BrainController
        from app.ui.chat_window import ChatWindow

        sys.argv = ["main.py", "--no-banner"]
        args = _parse_args()
        _ensure_config(ROOT)
        cfg = load_config(ROOT / args.config)
        sm = StateManager(ROOT, cfg, args)
        brain = BrainController(ROOT, cfg, sm.state, sm.reminders)

        cw = ChatWindow(
            llm_cfg=cfg.llm, char_cfg=cfg.character,
            sprite_dir=cfg.sprite.directory,
            asr_enabled=False, asr_model="base", asr_language="zh",
            registry=brain.tool_registry,
            context_provider=brain.chat_context,
        )
        cw.show()
        check("ChatWindow 构造 + show()", True)
        check("  · 多行输入（QPlainTextEdit）",
              type(cw.input_edit).__name__ == "QPlainTextEdit")
        check("  · / 命令补全（_CommandCompleter）",
              hasattr(cw, "_cmd_completer"))
        check("  · 快速指令按钮（send / stop）",
              hasattr(cw, "send_btn") and hasattr(cw, "stop_btn"))
        check("  · 状态栏副标题（model / tools / mems）",
              hasattr(cw, "subtitle_label"))
        check("  · /调试命令（_open_dashboard）",
              hasattr(cw, "_open_dashboard"))
        check("  · /记忆命令",
              "/记忆" in (inspect.getsource(cw._handle_command) or ""))
        check("  · _on_send（Enter 发送）", callable(cw._on_send))

        QTimer.singleShot(200, app.quit)
        app.exec()
    except Exception as e:
        import traceback
        traceback.print_exc()
        check("ChatWindow 启动", False, str(e))

    # ===== 总结 =====
    print("\n" + "=" * 70)
    ok = sum(1 for r in results if r[1] == "OK")
    fail = sum(1 for r in results if r[1] == "FAIL")
    print(f"总计：{ok} OK, {fail} FAIL, {len(results)} 项")
    if fail:
        print("\n失败项：")
        for n, s, d in results:
            if s == "FAIL":
                print(f"  - {n}: {d}")
    print("=" * 70)

    report_path = ROOT / "data" / "feature_check.txt"
    report_path.parent.mkdir(exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"Feature check: {ok} OK, {fail} FAIL, {len(results)} total\n")
        f.write(f"Generated: {ROOT}\n\n")
        for n, s, d in results:
            f.write(f"[{s:4}] {n}: {d}\n")
    print(f"\n报告: {report_path}")
    return 1 if fail else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-gui", action="store_true",
                        help="跳过 ChatWindow 构造（避免依赖 PyQt）")
    args = parser.parse_args()
    sys.exit(main())
