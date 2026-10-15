"""手动验证 Phase 2.4 SessionStore + agent 绑定。

用法:
    cd E:\study\desktop-pet
    $env:PYTHONPATH="E:\study\desktop-pet"
    python manual_tests\verify_phase2.4_sessions.py

输出:
    - 创建 3 个 session
    - 列出看到 3 个,按 last_used_at desc
    - 构造 LangChainAgent 用 session_id 绑定后,验证 thread_id 被覆盖成 session.thread_id
    - 删一个,剩下 2 个

不消耗 token,纯 SQLite。
"""
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.brain.langchain_agent import LangChainAgent, LangChainAgentConfig
from app.brain.llm_client import LLMConfig
from app.brain.sessions import SessionStore
from app.engine.tools import ToolRegistry


def main():
    print("=== Phase 2.4 Session resume 手动验证 ===\n")

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        store = SessionStore(td / "sessions.db")

        print("[step 1] 创建 3 个 session")
        a = store.create(title="项目 A")
        time.sleep(0.05)
        b = store.create(title="项目 B")
        time.sleep(0.05)
        c = store.create(title="项目 C")
        for s in (a, b, c):
            print(f"  - {s.session_id}  thread_id={s.thread_id}  title={s.title!r}")

        print("\n[step 2] list_all() 按 last_used_at desc")
        listed = store.list_all()
        for s in listed:
            print(f"  - {s.session_id}  {s.title!r}  used_at={s.last_used_at:.2f}")
        ids = [s.session_id for s in listed]
        assert ids == [c.session_id, b.session_id, a.session_id], \
            f"顺序错: {ids}"
        print("  → 顺序 OK(最新在前)")

        print("\n[step 3] get_by_thread_id 回查")
        got = store.get_by_thread_id(b.thread_id)
        assert got.session_id == b.session_id
        print(f"  - thread_id={b.thread_id} → session_id={got.session_id}  title={got.title!r}")

        print("\n[step 4] LangChainAgent 绑定 session,thread_id 应被覆盖")
        agent = LangChainAgent(
            llm_cfg=LLMConfig(),
            registry=ToolRegistry(),
            cfg=LangChainAgentConfig(),
            persona="p",
            thread_id="default",   # 这个应该被覆盖
            session_id=a.session_id,
            session_store=store,
        )
        print(f"  - agent.thread_id = {agent.thread_id}")
        print(f"  - a.thread_id    = {a.thread_id}")
        assert agent.thread_id == a.thread_id
        assert agent.thread_id != "default"
        print("  → thread_id 被 session 正确接管")

        print("\n[step 5] touch_session 更新 last_used + message_count")
        old_used = a.last_used_at
        time.sleep(0.05)
        agent.touch_session(message_count=2)
        got_a = store.get(a.session_id)
        print(f"  - 旧 used={old_used:.2f}  新 used={got_a.last_used_at:.2f}")
        print(f"  - message_count={got_a.message_count}")
        assert got_a.message_count == 2
        assert got_a.last_used_at > old_used

        print("\n[step 6] 删除中间那个 session")
        assert store.delete(b.session_id)
        remaining = store.list_all()
        print(f"  - 剩下 {len(remaining)} 个")
        for s in remaining:
            print(f"    - {s.session_id}  {s.title!r}")
        assert len(remaining) == 2
        assert all(s.session_id != b.session_id for s in remaining)
        print("  → 删除 OK,不影响其他")

        print("\n=== 全部通过 ===")
        print("\n[note] SessionStore 真实落盘到 sessions.db,DB 文件在临时目录里,")
        print("       测试结束自动清理。你也可以手动打开看 schema。")


if __name__ == "__main__":
    main()