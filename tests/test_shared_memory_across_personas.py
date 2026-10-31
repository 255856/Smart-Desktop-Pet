"""v58: 一只桌宠一份记忆 —— 所有人设共享同一份会话 / 聊天历史 / 主动搭话记录。

对应验收标准：
  A1 小汐 / 大肥鱼 / 小晓 三个人设 = 同一条 checkpoint 线程。
     用**真的** langgraph checkpoint store 跑出来，不是读代码得出的结论。
  A2 token 台账的 session_id 里不再有人设名；token-usage.jsonl 照常能写能读
     （成本面板读的就是它）。
  A3 主动搭话「我最近说了什么」跨人设共享 —— 小汐说的话大肥鱼看得见。
  A4 旧人设线程（char:小汐:main）+ 旧人设聊天历史行 → 迁到共享线程 / 共享桶，
     历史不丢，改之前有备份。
  A5 切人设照样改人设 / 名字 / 头像，但**不改**记忆。
"""
import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.qt_compat import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv[:1])

from app.brain.langchain_config import LangChainAgentConfig  # noqa: E402
from app.brain.sessions import SessionStore  # noqa: E402
from app.brain.token_ledger import (  # noqa: E402
    current_session, default_ledger_path, record_usage, set_session,
)
from app.core.config import CharacterConfig, LLMConfig  # noqa: E402
from app.engine.chat_store import SHARED_BUCKET, ChatStore  # noqa: E402
from app.engine.tools import ToolRegistry  # noqa: E402
from app.ui.chat_window import (  # noqa: E402
    SHARED_SESSION_ID, ChatWindow, _migrate_legacy_char_checkpoints,
)

PERSONAS = ("小汐", "大肥鱼", "小晓")
SPRITE_DIR = str(ROOT / "assets" / "sprites")


def _llm_cfg() -> LLMConfig:
    return LLMConfig(api_key="sk-test-not-a-real-key", model="test-model",
                     stream=False)


def _make_window(name: str, **kw) -> ChatWindow:
    """按人设名造一个聊天窗（走真实的 session_id 派生逻辑）。"""
    return ChatWindow(
        _llm_cfg(),
        CharacterConfig(name=name, persona="你是%s。" % name),
        SPRITE_DIR,
        asr_enabled=False,
        **kw,
    )


# ---------------------------------------------------------------------------
# A1：三个人设 = 同一条会话 / 同一条 checkpoint 线程
# ---------------------------------------------------------------------------

class TestSharedSessionId:
    def test_session_id_identical_for_all_personas(self):
        """三个人设拿到的 session_id 完全一样（且不含人设名）。"""
        ids = {}
        for name in PERSONAS:
            cw = _make_window(name)
            ids[name] = cw._session_id
        assert set(ids.values()) == {SHARED_SESSION_ID}, ids
        for name, sid in ids.items():
            assert name not in sid, (name, sid)

    def test_ledger_session_id_has_no_character_name(self):
        """台账的 session id 不再带人设名（一个钱包）。"""
        cw = _make_window("小汐")
        set_session(cw._session_id or "")
        assert current_session() == SHARED_SESSION_ID
        assert "小汐" not in current_session()

    def test_token_usage_jsonl_roundtrip_and_cost_panel(self, tmp_path):
        """台账照常写 jsonl，成本面板（dashboard）照常读得出来。"""
        cw = _make_window("小汐")
        # 应用真实路径：轻量后端发请求前会 set_session(self._session_id)
        set_session(cw._session_id or "")
        assert current_session() == SHARED_SESSION_ID
        ok = record_usage(
            {"prompt_tokens": 100, "completion_tokens": 20,
             "total_tokens": 120, "cache_hit_tokens": 10,
             "cache_miss_tokens": 90, "cache_write_tokens": 0},
            backend="standard", model="test-model",
        )
        assert ok is True
        path = default_ledger_path()
        assert path.is_file()
        rows = [json.loads(line) for line in
                path.read_text("utf-8").splitlines() if line.strip()]
        mine = [r for r in rows if r.get("session_id") == SHARED_SESSION_ID]
        assert mine, rows[-3:]
        for persona in PERSONAS:
            assert not any(r.get("session_id") == persona for r in rows)

        # 成本面板读的就是这个文件（按标准文件名放一份到临时目录）
        import shutil
        panel_dir = tmp_path / "data"
        panel_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(path), str(panel_dir / "token-usage.jsonl"))
        from app.web.dashboard import _token_summary
        summary = _token_summary(panel_dir)
        assert summary["available"] is True
        assert SHARED_SESSION_ID in (summary.get("by_session") or {})


def _real_saver(db_path):
    """真 checkpoint store（langgraph 的 AsyncSqliteSaver）。"""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    return AsyncSqliteSaver.from_conn_string(str(db_path))


def _compile_graph(saver):
    """一条真 LangGraph 图：每轮把主人说的话追加进状态。"""
    from typing import Annotated, TypedDict

    from langchain_core.messages import AIMessage
    from langgraph.graph import END, START, StateGraph
    from langgraph.graph.message import add_messages

    class _S(TypedDict):
        msgs: Annotated[list, add_messages]

    def _node(state):
        last = state["msgs"][-1]
        return {"msgs": [AIMessage(content="回：" + str(last.content))]}

    g = StateGraph(_S)
    g.add_node("chat", _node)
    g.add_edge(START, "chat")
    g.add_edge("chat", END)
    return g.compile(checkpointer=saver)


async def _speak(graph, thread_id: str, text: str) -> None:
    """在真 checkpoint store 上说一句话（必须在同一个 event loop 里 await）。"""
    from langchain_core.messages import HumanMessage
    await graph.ainvoke(
        {"msgs": [HumanMessage(content=text)]},
        {"configurable": {"thread_id": thread_id}},
    )


def _state_messages(tup) -> list:
    """从 checkpoint 状态里取出消息列表（channel 名随图定义）。"""
    vals = tup.checkpoint.get("channel_values", {}) or {}
    for key in ("messages", "msgs"):
        if key in vals:
            return list(vals[key])
    for v in vals.values():
        if isinstance(v, list):
            return list(v)
    return []


def _read_state(db_path, thread_id: str):
    async def _go():
        cm = _real_saver(db_path)
        saver = await cm.__aenter__()
        try:
            return await saver.aget_tuple(
                {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}})
        finally:
            await cm.__aexit__(None, None, None)
    return asyncio.run(_go())


class TestSharedCheckpointThread:
    def test_all_personas_resolve_to_same_thread_id(self, tmp_path):
        """真 LangChainAgent 把三个 session_id 解析成同一个 thread_id。"""
        from app.brain.langchain_agent import LangChainAgent

        ckpt = tmp_path / "ckpt.db"
        cfg = LangChainAgentConfig(enable_checkpointer=True,
                                   checkpoint_db=str(ckpt))
        store = SessionStore(tmp_path / "sessions.db")
        threads = {}
        for name in PERSONAS:
            cw = _make_window(name, langchain_cfg=cfg)
            agent = LangChainAgent(
                llm_cfg=_llm_cfg(), registry=ToolRegistry(),
                persona="你是%s。" % name, cfg=cfg,
                session_id=cw._session_id, session_store=store,
            )
            threads[name] = agent.thread_id
        assert len(set(threads.values())) == 1, threads
        assert set(threads.values()) == {SHARED_SESSION_ID}

    def test_history_accumulates_across_personas_in_real_store(self, tmp_path):
        """真 checkpoint store 里：三个人设写的是同一条线程，历史连成一条线。"""
        ckpt = tmp_path / "ckpt.db"
        cfg = LangChainAgentConfig(enable_checkpointer=True,
                                   checkpoint_db=str(ckpt))
        store = SessionStore(tmp_path / "sessions.db")

        threads = {}
        for name in PERSONAS:
            cw = _make_window(name, langchain_cfg=cfg)
            from app.brain.langchain_agent import LangChainAgent
            agent = LangChainAgent(
                llm_cfg=_llm_cfg(), registry=ToolRegistry(),
                persona="你是%s。" % name, cfg=cfg,
                session_id=cw._session_id, session_store=store,
            )
            threads[name] = agent.thread_id

        thread_id = threads["小汐"]

        async def _scenario():
            cm = _real_saver(ckpt)
            saver = await cm.__aenter__()
            try:
                graph = _compile_graph(saver)
                for name in PERSONAS:
                    await _speak(graph, thread_id, "%s 说的话" % name)
                return await saver.aget_tuple({"configurable": {
                    "thread_id": thread_id, "checkpoint_ns": ""}})
            finally:
                await cm.__aexit__(None, None, None)

        # 三个人的话都在同一条线程的状态里
        tup = asyncio.run(_scenario())
        msgs = _state_messages(tup)
        contents = [str(getattr(m, "content", "")) for m in msgs]
        for name in PERSONAS:
            assert any("%s 说的话" % name in c for c in contents), contents

        # 盘上只有一条线程（没有按人设分叉）
        conn = sqlite3.connect(str(ckpt))
        try:
            tids = [r[0] for r in conn.execute(
                "SELECT DISTINCT thread_id FROM checkpoints")]
        finally:
            conn.close()
        assert tids == [SHARED_SESSION_ID], tids


# ---------------------------------------------------------------------------
# A3：主动搭话的「我最近说了什么」跨人设共享
# ---------------------------------------------------------------------------

class _FakeState:
    def stats_summary(self):
        return "lv1 心情不错"


class TestSharedProactiveRemarks:
    def _brain(self):
        from app.brain.proactive import ProactiveBrain
        return ProactiveBrain(
            llm_cfg=_llm_cfg(), persona="你是小汐。", state=_FakeState(),
            memory=None, char_name="小汐",
        )

    def test_remark_made_as_xiaoxi_visible_to_dayufeiyu(self, monkeypatch):
        """小汐搭的一句话，切到大肥鱼后照样在 prompt 里。"""
        import app.brain.proactive as proactive_mod

        brain = self._brain()
        brain._on_done("小汐今天问主人吃饭了没")
        assert brain._last_remarks == ["小汐今天问主人吃饭了没"]

        # 切人设（brain_controller._on_character_switched 就是这么同步的）
        brain.persona = "你是大肥鱼。"
        brain.char_name = "大肥鱼"

        captured = {}

        class _Sig:
            def __init__(self):
                self.slots = []

            def connect(self, fn):
                self.slots.append(fn)

            def emit(self, *a):
                for fn in self.slots:
                    fn(*a)

        class _FakeWorker:
            """替掉真 worker：同步跑 prepare()，把 prompt 抓下来。"""

            def __init__(self, llm_cfg, system_prompt, messages,
                         request_timeout_s=None, prepare=None):
                self.done = _Sig()
                self.failed = _Sig()
                self._prepare = prepare

            def start(self):
                system, msgs = self._prepare()
                captured["system"] = system
                captured["messages"] = msgs

        monkeypatch.setattr(proactive_mod, "_ChatOnceWorker", _FakeWorker)
        brain._schedule = lambda: None
        brain.rp = None
        brain._read_idle_minutes = lambda: 0.0
        brain._is_in_meeting = lambda: False
        brain._build_aw_context = lambda: ""
        brain._fetch_focus_line = lambda: None
        brain._routine_hint_for_now = lambda: ""

        brain._fire()
        assert captured, "没抓到 prompt"

        user = json.loads(captured["messages"][0].content)
        assert "小汐今天问主人吃饭了没" in user["recent_remarks"], user
        # 切到大肥鱼之后桶还是同一个（不是按人设另起一个）
        brain._on_done("大肥鱼接了一句")
        assert brain._last_remarks == ["小汐今天问主人吃饭了没", "大肥鱼接了一句"]


# ---------------------------------------------------------------------------
# A4：旧线程 / 旧人设历史行 → 迁移到共享，历史不丢 + 有备份
# ---------------------------------------------------------------------------

class TestMigration:
    def test_legacy_checkpoint_thread_migrated_with_backup(self, tmp_path):
        """旧人设线程改名成共享线程：历史还在，备份也在。"""
        ckpt = tmp_path / "ckpt.db"
        legacy = "char:小汐:main"

        async def _seed():
            cm = _real_saver(ckpt)
            saver = await cm.__aenter__()
            try:
                graph = _compile_graph(saver)
                await _speak(graph, legacy, "旧人设留下的话")
            finally:
                await cm.__aexit__(None, None, None)

        asyncio.run(_seed())

        before = _read_state(ckpt, legacy)
        assert any("旧人设留下的话" in str(getattr(m, "content", ""))
                   for m in _state_messages(before))

        res = _migrate_legacy_char_checkpoints(ckpt)
        assert res["migrated"] == [legacy], res
        assert res["rows"] > 0
        backup = Path(res["backup"])
        assert backup.is_file(), backup
        assert backup.parent == tmp_path / "backups"

        # 旧线程没了，新线程接管，历史一条不少
        after = _read_state(ckpt, SHARED_SESSION_ID)
        contents = [str(getattr(m, "content", ""))
                    for m in _state_messages(after)]
        assert any("旧人设留下的话" in c for c in contents), contents

        conn = sqlite3.connect(str(ckpt))
        try:
            tids = [r[0] for r in conn.execute(
                "SELECT DISTINCT thread_id FROM checkpoints")]
        finally:
            conn.close()
        assert tids == [SHARED_SESSION_ID], tids

        # 再迁一次是空操作（幂等）
        assert _migrate_legacy_char_checkpoints(ckpt) == {
            "migrated": [], "backup": "", "rows": 0}

    def test_chat_history_buckets_merge_into_shared(self, tmp_path):
        """聊天历史：各人设的行并成一套，别的桶（测试残留）不动。"""
        db = tmp_path / "chat.db"
        xiaoxi = ChatStore(str(db), character_name="小汐")
        xiaoxi.add("user", "小汐说的话")
        xiaoxi.add("assistant", "小汐的回话")
        junk = ChatStore(str(db), character_name="test")
        junk.add("user", "测试残留")

        shared = ChatStore(str(db))
        assert [m.content for m in shared.all()] == []

        moved = shared.merge_bucket("小汐")
        assert moved == 2

        after = ChatStore(str(db))
        assert [m.content for m in after.all()] == ["小汐说的话", "小汐的回话"]
        # 别的桶原样留着，没被并进来、也没被删
        assert [m.content for m in after.all("test")] == ["测试残留"]
        conn = sqlite3.connect(str(db))
        try:
            total = list(conn.execute("SELECT count(*) FROM messages"))[0][0]
        finally:
            conn.close()
        assert total == 3

    def test_shared_store_bucket_is_persona_agnostic(self, tmp_path):
        """共享桶：谁写进去的，所有人设都读得到。"""
        db = tmp_path / "chat.db"
        for name in PERSONAS:
            ChatStore(str(db), character_name=SHARED_BUCKET).add(
                "user", "%s 说的话" % name)
        shared = ChatStore(str(db), character_name=SHARED_BUCKET)
        assert [m.content for m in shared.all()] == [
            "%s 说的话" % name for name in PERSONAS]


# ---------------------------------------------------------------------------
# A5：切人设照样改人设 / 名字 / 头像，但不改记忆
# ---------------------------------------------------------------------------

class TestPersonaSwitchKeepsMemory:
    def test_switch_changes_persona_but_not_session_or_history(self, monkeypatch):
        import app.core.config as config_mod

        # 别把主人选的人设写进仓库 data/
        monkeypatch.setattr(config_mod, "save_character_selection",
                            lambda *a, **k: Path("memory"))

        cw = _make_window("小汐")
        before_sid = cw._session_id
        before_bucket = cw.chat_store.character_name
        assert before_bucket == SHARED_BUCKET

        ok, _msg = cw.switch_character("characters/dfy.yaml")
        assert ok is True
        # 人设 / 名字变了
        assert cw.char_cfg.name == "大肥鱼"
        assert "大肥鱼" in cw.windowTitle()
        assert cw.title_label.text() == "和 大肥鱼 的对话"
        # **长期记忆没变**（一只桌宠一份记忆）—— owner 拍板保留的那部分
        assert cw.chat_store.character_name == before_bucket == SHARED_BUCKET
        # **但换人设 = 开新会话**（owner 后来拍板）：
        #   - checkpoint 换一条新线程（旧的仍在盘上，可查）
        #   - 模型上下文从此刻重新开始（旧消息留在聊天区，但不再进上下文）
        #   原因：共享历史里全是旧人设的对话（实测 41 条「小汐」、18 条「喵」）
        #   → 模型跟着旧人设演。
        assert cw._session_id != before_sid
        assert cw._session_id.startswith(SHARED_SESSION_ID)
        assert cw._ctx_start == len(cw.history)
        # **台账 session 不变** —— 一个桌宠一个钱包（成本不能按人设拆）
        assert cw._ledger_session == SHARED_SESSION_ID
