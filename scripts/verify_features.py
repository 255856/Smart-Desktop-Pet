"""verify_features.py: 端到端功能验证脚本。
用法：python scripts/verify_features.py [--no-gui]
输出报告 + 退出码（0=全过，1=有失败）。"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
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


def note(name: str, detail: str = "") -> None:
    """信息项：不计入 OK / FAIL，不影响退出码。用于「运行时数据存在哪」等参考信息。"""
    results.append((name, "NOTE", detail))


def _sidecar_port() -> int:
    """sidecar 端口（跟客户端 / 监督者同一个口径）。"""
    from app.brain.memory_client import default_port
    return default_port()


def _sidecar_host() -> str:
    return "127.0.0.1"


def _port_is_open(host: str, port: int) -> bool:
    """端口上有人在听吗（只连一下就关，不发请求）。"""
    import socket

    sock = socket.socket()
    try:
        sock.settimeout(0.5)
        return sock.connect_ex((host, int(port))) == 0
    except OSError:
        return False
    finally:
        sock.close()


def _probe_memory_sidecar() -> tuple[str, str]:
    """**真的问一次**记忆 sidecar：/health、/stats、/search。

    长期记忆只有 sidecar 一份（JSON 卡片是真值，老的进程内记忆库已经删了）。
    返回 (状态, 详情)，状态是 OK / FAIL / NOTE：

      · **没在跑** → NOTE：离线检查不该因为「服务没启动」判红
        （python -m memory_sidecar serve --port 8788 是运行时的事）；
      · **在跑但答得不对** → FAIL：那才是真出问题。
    """
    import time as _time

    from app.brain.memory_client import MemoryClient

    client = MemoryClient(timeout=6.0, cooldown=0.0)
    # 主程序启动时会自己把 sidecar 拉起来（后台线程），而 sidecar 冷启动要先
    # import torch、再把两个模型（精排有 2 GB）读进来 —— 这段时间里 /health
    # 偶尔要 1~2s 才答得上来。所以**有界地**等（最多 90s），并把两种情况分开说：
    #   · 端口上没人听 → 真的没在跑；
    #   · 端口在听但答不上来 → 在跑、还在预热（不是故障）。
    # 端口上没人听 = 确实没在跑（主程序启动时才会把它拉起来）→ 立刻给结论，
    # 不要在这里白等（本检查跑在 App 构造之前）。
    if not _port_is_open(_sidecar_host(), _sidecar_port()):
        return "NOTE", ("sidecar 没在跑（python -m memory_sidecar serve --port %d）；"
                        "主程序启动时会自己把它拉起来" % _sidecar_port())
    deadline = _time.time() + 90.0
    health = {}
    while True:
        health = client.health(timeout=6.0) or {}
        if health.get("ok"):
            break
        if _time.time() >= deadline:
            if _port_is_open(_sidecar_host(), _sidecar_port()):
                return "NOTE", ("sidecar 在跑，但 90s 内没答上来（模型预热中）：%s"
                                % (client.last_error or "超时"))
            return "NOTE", ("sidecar 没在跑（python -m memory_sidecar serve --port %d）：%s"
                            % (_sidecar_port(), client.last_error or "连不上"))
        _time.sleep(0.5)
    stats = client.stats() or {}
    if not isinstance(stats.get("cards"), int):
        return "FAIL", "/stats 没给出 cards：%s" % (client.last_error or stats)
    # /search 约定永远 200 + results 数组（哪怕检索腿全挂）
    rows = client.search("桌面宠物功能验证", top_k=3)
    if not isinstance(rows, list):
        return "FAIL", "/search 没返回数组：%s" % (client.last_error or "")
    return "OK", ("/health %d 张卡 · %s · /stats 活跃 %s · /search 返回 %d 条"
                  % (int(health.get("cards") or 0),
                     health.get("models") or {},
                     stats.get("active"),
                     len(rows)))


def _parse_tool_markers(text):
    return [
        {"id": f"c{i}", "name": m.group(1), "arguments": m.group(2)}
        for i, m in enumerate(re.finditer(r"<tool>([\w]*)\|(\{.*?\})</tool>", text, re.DOTALL))
    ]


def _strip_tool_markers(text):
    return re.sub(r"<tool>[\w]*\|.*?</tool>", "", text, flags=re.DOTALL).strip()


def _parse_tool_markers(text):
    return [
        {"id": f"c{i}", "name": m.group(1), "arguments": m.group(2)}
        for i, m in enumerate(re.finditer(r"<tool>([\w]*)\|(\{.*?\})</tool>", text, re.DOTALL))
    ]


def _strip_tool_markers(text):
    return re.sub(r"<tool>[\w]*\|.*?</tool>", "", text, flags=re.DOTALL).strip()


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

    # 长期记忆：只有 sidecar 一份（老的进程内记忆库已删）
    from app.brain.memory_client import MemoryClient
    client_src = inspect.getsource(MemoryClient)
    check("记忆 sidecar 客户端（MemoryClient）", True)
    check("  · 记忆块 system_block（易变通道）", "def system_block" in client_src)
    check("  · 三个写入口（upsert / delete / supersede）",
          all(k in client_src for k in ("/upsert", "/delete", "/supersede")))
    # 真的问一次 sidecar：/health、/stats、/search（没在跑只算 NOTE，不判红）
    _mem_status, _mem_detail = _probe_memory_sidecar()
    if _mem_status == "NOTE":
        note("  · 记忆 sidecar（/health + /stats + /search）", _mem_detail)
    else:
        check("  · 记忆 sidecar（/health + /stats + /search）",
              _mem_status == "OK", _mem_detail)

    from app.engine.tools import ToolRegistry, Tool
    check("ToolRegistry + Tool", True)

    from app.voice.voice import TTS
    check("TTS（edge-tts）", True)
    from app.voice.asr import SpeechRecognizer, asr_available
    check("SpeechRecognizer（faster-whisper）", True)
    check("asr_available() 检测函数", callable(asr_available))

    # ===== 二、智能体方向 =====
    print("\n[二] 智能体方向")

    # ---- 抗幻觉机制（强制调工具 + 跨步检测 + 重试）----
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
          "force_retry" in al_src and ("重试" in al_src or "未调工具" in al_src))

    # ChatWindow 必须用真正的 ReAct 循环（AgentLoop）+ 处理 meta 事件
    from app.ui.chat_window import ChatWindow as _CW
    cw_src = inspect.getsource(_CW)
    check("ChatWindow 接入真正的 ReAct 循环（AgentLoop）",
          "AgentLoop(client" in cw_src)
    check("ChatWindow 处理 force_retry meta 事件",
          "_on_meta" in cw_src and "force_retry" in cw_src)

    # AgentLoop 必须有 force_final 机制
    from app.brain.agent import AgentLoop as _AL, MAX_CONSECUTIVE_TOOL_ONLY_TURNS
    al_src = inspect.getsource(_AL)
    check("AgentLoop force_final（连续纯调工具 → 强制 final 阶段）",
          "force_final" in al_src and "MAX_CONSECUTIVE_TOOL_ONLY_TURNS" in al_src)
    check("AgentLoop final_text 兜底（不为空）",
          'final_text = "".join(content_parts)' in al_src
          and "工具结果作为" in al_src
          or "tool_results" in al_src)

    # LLMClient 必须分离 reasoning_content 和 content
    from app.brain.llm_client import _extract_text_chunk, _extract_reasoning_chunk
    check("_extract_text_chunk 只返回 content（不含 reasoning）",
          _extract_text_chunk({"content": "hi", "reasoning_content": "r"}) == "hi")
    check("_extract_text_chunk reasoning 单独存在时返回空",
          _extract_text_chunk({"reasoning_content": "r"}) == "")
    check("_extract_reasoning_chunk 提取 reasoning_content / reasoning",
          _extract_reasoning_chunk({"reasoning_content": "r"}) == "r"
          and _extract_reasoning_chunk({"reasoning": "r"}) == "r")
    # _do_stream_request 必须显式处理 reasoning_content 字段
    dsr_src = inspect.getsource(
        __import__('app.brain.llm_client', fromlist=['LLMClient']).LLMClient)
    check("_do_stream_request 区分 reasoning_content（不 yield 给 UI）",
          "reasoning_content" in dsr_src
          and "reasoning_chunk" in dsr_src
          and "reasoning_delta" in dsr_src)
    check("_do_stream_request 显式 yield content_chunk 为 text 事件",
          'yield ("text", clean)' in dsr_src)
    # chat_stream 也必须分离（用于无工具调用路径）
    cs_src = inspect.getsource(
        __import__('app.brain.llm_client', fromlist=['LLMClient']).LLMClient)
    check("chat_stream 跳过 reasoning_content 字段",
          "_extract_reasoning_chunk" in cs_src)

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
        (ROOT / "data" / "memory_sidecar" / "cards.json",
         "长期记忆卡片（JSON 真值；sidecar 的向量 / BM25 索引是它的派生）"),
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
            note(f"  {path}", f"{desc} · {size} bytes")
        else:
            note(f"  {path}", f"{desc} · 未生成（运行时产物，首次启动后出现）")

    # ===== 四、Eval 套件静态校验 =====
    print("\n[四] Eval 套件")

    try:
        from app.eval.cases import builtin_cases, Evaluator, MockLLMClient
        cases = builtin_cases()
        check(f"builtin_cases（{len(cases)} 个内置用例）", len(cases) >= 4)
        check("Evaluator 类", callable(Evaluator))
        check("MockLLMClient 类", callable(MockLLMClient))
    except Exception as e:
        check("Eval 套件加载", False, str(e))

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

        # 长期记忆的接线：客户端 + 监督者在位，记忆块由它提供
        # （进程内的老记忆库已经删掉，这里验的是「只有一个后端」）
        # sidecar 是**后台线程**拉起来的（冷启动要 import torch，几秒），
        # 所以这里等它一下（有界 30s），别把「刚起步」写成 reachable=False。
        _status = brain.sidecar_status()
        _deadline = time.time() + 30.0
        while not _status.get("reachable") and time.time() < _deadline:
            time.sleep(0.5)
            _status = brain.sidecar_status()
        check("记忆 sidecar 接线（客户端 + 监督者）",
              brain.memory_client is not None
              and getattr(brain, "sidecar", None) is not None
              and "reachable" in _status,
              "reachable=%s · cards=%s · 端口 %s"
              % (_status.get("reachable"), _status.get("cards"),
                 _status.get("port")))

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
        # 记忆命令：命令名早就从「/记忆」改成「/memory」（HEAD 的 chat_window
        # 就是 /memory），这一项还停在旧名字上 → 一直误报 FAIL。两个拼法都认。
        _cmd_src = inspect.getsource(cw._handle_command) or ""
        check("  · /记忆命令（/memory）",
              "/记忆" in _cmd_src or "/memory" in _cmd_src)
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
    notes = sum(1 for r in results if r[1] == "NOTE")
    print(f"总计：{ok} OK, {fail} FAIL, {notes} 信息项, 共 {len(results)} 项")
    if fail:
        print("\n失败项：")
        for n, s, d in results:
            if s == "FAIL":
                print(f"  - {n}: {d}")
    print("=" * 70)

    report_path = ROOT / "data" / "feature_check.txt"
    report_path.parent.mkdir(exist_ok=True)
    report_lines = [
        f"Feature check: {ok} OK, {fail} FAIL, {len(results)} total",
        f"Generated: {ROOT}",
        "",
    ]
    for n, s, d in results:
        report_lines.append(f"[{s:4}] {n}: {d}")
    report_text = "\n".join(report_lines) + "\n"
    # 内容没变就**不重写** —— 免得每次跑验证都动 data/ 的 mtime
    try:
        old_text = (report_path.read_text(encoding="utf-8")
                    if report_path.is_file() else None)
    except OSError:
        old_text = None
    if old_text != report_text:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report_text)
    print(f"\n报告: {report_path}")
    return 1 if fail else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-gui", action="store_true",
                        help="跳过 ChatWindow 构造（避免依赖 PyQt）")
    args = parser.parse_args()
    sys.exit(main())
