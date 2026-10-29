"""Stage 2.3 + 2.4 + 2.7：生命周期分类 / 事实改写 / 去重不谎报。

钉死的验收点：
    A1 20 轮新记忆后，库里**同时**有「主人相关」和「小汐自己相关」，
       两者能靠 kind / category 分开
    A2 写入矛盾事实 → 旧行 status='superseded' + superseded_by，
       **两条不并存为 active**
    A3 同一事实写两次 → 工具说「已经记过了」，不新建行，返回的 id 在库里
    A4 forget_memory 能按 id 删
    A5 【记忆规则】每轮都在；system 串对不同 query 逐字节相同
    A6 跑测试不碰仓库 data/
    A8 破坏性写库（改写 / 删行）之前先备份

不需要 API key / 网络 / GUI（BrainController 那条用 offscreen Qt）。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if (ROOT / ".local-packages").is_dir():
    sys.path.insert(0, str(ROOT / ".local-packages"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.brain.memory import KIND_CHOICES, MemoryStore  # noqa: E402
from app.engine.tools import ToolRegistry  # noqa: E402
from app.engine.tools._memory import register as register_memory_tools  # noqa: E402


def _rows(db_path):
    """读库里的全部行（dict 列表）。"""
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT id, content, category, kind, status, superseded_by "
            "FROM memories")
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def _make_store(tmp_path, name="memory.db"):
    store = MemoryStore(tmp_path / name, backend_name="tfidf")
    reg = ToolRegistry()
    register_memory_tools(reg, memory=store)
    return (store, reg.get("remember_fact").fn, reg.get("recall_memory").fn,
            reg.get("forget_memory").fn)


# ---------------------------------------------------------------------------
# A1：生命周期维度 + 「自己的承诺/立场」也要记
# ---------------------------------------------------------------------------


def test_a1_tool_schema_exposes_kind_dimension():
    """工具 schema 必须暴露 kind（生命周期分类）这一维。"""
    reg = ToolRegistry()
    register_memory_tools(reg, memory=object())
    props = reg.get("remember_fact").parameters["properties"]
    assert "kind" in props, "remember_fact 必须暴露 kind 参数"
    assert props["kind"]["enum"] == list(KIND_CHOICES), props["kind"]


def test_a1_description_no_longer_restricted_to_the_master():
    """描述不再限定「关于主人的」，且明确要求**也记自己的承诺/立场**。"""
    reg = ToolRegistry()
    register_memory_tools(reg, memory=object())
    desc = reg.get("remember_fact").description
    assert "关于主人的" not in desc, desc
    assert "你自己的承诺" in desc, desc
    assert "立场" in desc, desc


def test_a1_twenty_turns_keep_both_master_and_self_records(tmp_path):
    """20 轮新记忆后：主人相关 + 小汐自己相关**都在**，且能靠 kind/category 分开。"""
    store, remember, _recall, _forget = _make_store(tmp_path)

    for i in range(10):
        # 主人相关：偏好 → kind=memberImpression
        remember(content=f"主人第{i}次说喜欢喝冰美式", category="preference",
                 importance=0.8)
        # 小汐自己相关：自己的承诺/立场 → category=fact，kind=activeTopic
        remember(content=f"我答应主人第{i}件事要记得主动问", category="fact",
                 importance=0.8, kind="activeTopic")

    items = store.all()
    master = [i for i in items if i.content.startswith("主人")]
    myself = [i for i in items if i.content.startswith("我答应")]

    assert len(master) == 10, [i.content for i in items]
    assert len(myself) == 10, [i.content for i in items]

    # 两类记录能靠 kind / category 分开
    assert {i.kind for i in master} == {"memberImpression"}
    assert {i.category for i in master} == {"preference"}
    assert {i.kind for i in myself} == {"activeTopic"}
    assert {i.category for i in myself} == {"fact"}


def test_a1_self_record_is_recallable(tmp_path):
    """「自己的承诺」也要能被召回（别又落回短期记忆里）。"""
    _store, remember, recall, _forget = _make_store(tmp_path)
    remember(content="我答应主人周末一起去看展", category="fact",
             importance=0.9, kind="activeTopic")
    out = recall(keyword="看展")
    assert "我答应主人周末一起去看展" in out, out


# ---------------------------------------------------------------------------
# A2：事实改写（supersede）
# ---------------------------------------------------------------------------


def test_a2_supersede_marks_old_row_and_does_not_coexist(tmp_path):
    """矛盾事实 → 旧行过时 + superseded_by；两条不并存为 active。"""
    db = tmp_path / "memory.db"
    store = MemoryStore(db, backend_name="tfidf")
    old = store.add("主人喜欢喝冰美式", "preference", importance=0.8)

    new = store.supersede(old.id, "主人喜欢喝拿铁")
    assert new is not None
    assert new.id != old.id
    assert new.status == "active"

    rows = {r["id"]: r for r in _rows(db)}
    assert rows[old.id]["status"] == "superseded"
    assert rows[old.id]["superseded_by"] == new.id
    assert rows[new.id]["status"] == "active"
    assert rows[new.id]["superseded_by"] is None

    # **不并存**：同一件事只有一条 active
    active = [r for r in rows.values() if r["status"] == "active"]
    assert [r["content"] for r in active] == ["主人喜欢喝拿铁"]

    # 召回时旧条不再出现（不然模型会同时看到两个矛盾事实）
    found = store.search(keyword="主人喜欢喝")
    assert [i.content for i in found] == ["主人喜欢喝拿铁"]


def test_a2_supersede_via_tool_rewrites_by_id(tmp_path):
    """工具层：remember_fact(supersedes=旧id) 走改写路径。"""
    db = tmp_path / "memory.db"
    store, remember, _recall, _forget = _make_store(tmp_path)
    old = store.add("主人的猫叫团子", "fact", importance=0.8)

    out = remember(content="主人的猫叫豆豆", supersedes=old.id)
    assert "已改写" in out, out
    assert old.id in out, out

    rows = {r["id"]: r for r in _rows(db)}
    assert rows[old.id]["status"] == "superseded"
    new_id = rows[old.id]["superseded_by"]
    assert new_id and new_id in rows
    assert rows[new_id]["status"] == "active"
    assert rows[new_id]["content"] == "主人的猫叫豆豆"
    # 库里只有一条 active
    assert len([r for r in rows.values() if r["status"] == "active"]) == 1


def test_a2_supersede_unknown_id_reports_error(tmp_path):
    """改写不存在的 id → 如实报错，不假装成功。"""
    _store, remember, _recall, _forget = _make_store(tmp_path)
    out = remember(content="随便一条", supersedes="m不存在的id")
    assert out.startswith("错误"), out


# ---------------------------------------------------------------------------
# A3：去重命中不许谎报「已记住」
# ---------------------------------------------------------------------------


def test_a3_duplicate_says_already_remembered_and_creates_no_row(tmp_path):
    """同一事实写两次 → 「已经记过了」+ 不新建行 + 返回的 id 真实存在。"""
    db = tmp_path / "memory.db"
    store, remember, _recall, _forget = _make_store(tmp_path)

    first = remember(content="主人不吃香菜", category="fact", importance=0.8)
    assert "已记住" in first, first
    rows_after_first = _rows(db)
    assert len(rows_after_first) == 1

    second = remember(content="主人不吃香菜", category="fact", importance=0.8)
    assert "已经记过了" in second, second
    assert "已记住" not in second, second

    # 没有新建行
    rows_after_second = _rows(db)
    assert len(rows_after_second) == 1, rows_after_second
    assert rows_after_second[0]["id"] == rows_after_first[0]["id"]

    # 返回的 id 在库里真实存在（原来返回的是从没存过的 id）
    returned_id = rows_after_second[0]["id"]
    assert returned_id in second, second
    conn = sqlite3.connect(str(db))
    try:
        n = conn.execute("SELECT COUNT(*) FROM memories WHERE id=?",
                         (returned_id,)).fetchone()[0]
    finally:
        conn.close()
    assert n == 1, "返回的 id 必须真实存在于数据库"


def test_a3_store_add_returns_the_existing_row(tmp_path):
    """store.add 去重时返回**库里真存在的那条**（id 真实有效）。"""
    store = MemoryStore(tmp_path / "memory.db", backend_name="tfidf")
    first = store.add("主人的猫叫团子", "fact")
    second = store.add("主人的猫叫团子", "fact")
    assert second.id == first.id
    assert store.count() == 1
    assert store.get(second.id) is not None


# ---------------------------------------------------------------------------
# A4：按 id 删
# ---------------------------------------------------------------------------


def test_a4_forget_memory_by_id(tmp_path):
    """forget_memory(memory_id=...) 能精确删（不用先知道旧措辞）。"""
    db = tmp_path / "memory.db"
    store, _remember, _recall, forget = _make_store(tmp_path)
    item = store.add("主人的猫叫团子", "fact")

    out = forget(memory_id=item.id)
    assert "已删除 1 条" in out, out
    assert store.count() == 0
    assert _rows(db) == []


def test_a4_forget_memory_by_unknown_id_reports_error(tmp_path):
    _store, _remember, _recall, forget = _make_store(tmp_path)
    out = forget(memory_id="m不存在的id")
    assert out.startswith("错误"), out


def test_a4_forget_memory_still_supports_keyword(tmp_path):
    """老路径（关键词子串）不能坏。"""
    store, _remember, _recall, forget = _make_store(tmp_path)
    store.add("主人的猫叫团子", "fact")
    out = forget(keyword="团子")
    assert "已删除 1 条" in out, out
    assert store.count() == 0


# ---------------------------------------------------------------------------
# A8：破坏性写库之前先备份
# ---------------------------------------------------------------------------


def test_a8_supersede_takes_backup_before_writing(tmp_path):
    """改写 live 库之前先留副本（副本里旧行还是 active）。"""
    db = tmp_path / "memory.db"
    store = MemoryStore(db, backend_name="tfidf")
    old = store.add("主人喜欢喝冰美式", "preference", importance=0.8)

    store.supersede(old.id, "主人喜欢喝拿铁")

    backups = sorted(tmp_path.glob("memory.db.bak-*"))
    assert len(backups) == 1, backups
    bak_rows = {r["id"]: r for r in _rows(backups[0])}
    assert bak_rows[old.id]["status"] == "active", "备份里应是改动前的状态"
    assert bak_rows[old.id]["superseded_by"] is None


def test_a8_forget_by_id_takes_backup_before_writing(tmp_path):
    """按 id 删之前先备份。"""
    db = tmp_path / "memory.db"
    store = MemoryStore(db, backend_name="tfidf")
    item = store.add("主人的猫叫团子", "fact")
    store.forget_by_id(item.id)
    backups = sorted(tmp_path.glob("memory.db.bak-*"))
    assert len(backups) == 1, backups
    assert len(_rows(backups[0])) == 1, "备份里那条记忆还在"


def test_a8_forget_by_keyword_takes_backup_before_writing(tmp_path):
    """按关键词删（删行）之前也先备份。"""
    db = tmp_path / "memory.db"
    store = MemoryStore(db, backend_name="tfidf")
    store.add("主人的猫叫团子", "fact")
    store.forget_by_keyword("团子")
    backups = sorted(tmp_path.glob("memory.db.bak-*"))
    assert len(backups) == 1, backups
    assert len(_rows(backups[0])) == 1

# ---------------------------------------------------------------------------
# A5：【记忆规则】每轮都在 + system 串对不同 query 逐字节相同
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def brain(tmp_path_factory):
    """真 BrainController（临时 root —— 不碰仓库 data/）。"""
    from app.core.qt_compat import QApplication
    QApplication.instance() or QApplication(sys.argv[:1])

    from app.core.config import load_config
    from app.engine.state_manager import StateManager
    from app.brain.brain_controller import BrainController

    tmp = tmp_path_factory.mktemp("brain_rule")
    skill_dst = tmp / ".agents" / "skills" / "desktop-pet-tool-usage"
    skill_dst.mkdir(parents=True)
    shutil.copy(ROOT / ".agents" / "skills" /
                "desktop-pet-tool-usage" / "SKILL.md", skill_dst)
    (tmp / "config.yaml").write_text(
        "llm:\n"
        "  base_url: \"http://localhost:1/v1\"\n"
        "  api_key: \"test\"\n"
        "  model: \"test\"\n"
        "character:\n"
        "  name: \"测试\"\n"
        "window:\n"
        "  scale: 0.4\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp / "config.yaml")
    state_mgr = StateManager(tmp, cfg, type("Args", (), {"reset_state": False})())
    return BrainController(tmp, cfg, state_mgr.state, state_mgr.reminders)


def test_a5_memory_rule_is_in_the_stable_context(brain):
    """【记忆规则】在稳定上下文（system prompt 的数据源）里。"""
    ctx = brain.chat_context()
    assert "【记忆规则】" in ctx, ctx[:400]
    # 规则说的是「怎么用记忆」，必须点名真正的工具
    assert "remember_fact" in ctx, ctx[:400]


def test_a5_memory_rule_is_constant_and_system_stays_byte_identical(brain):
    """规则是**常量** ⇒ system 串对不同 query 逐字节相同（P0-11 的不变量）。"""
    brain.memory.add("主人明天要交房租", category="fact", importance=0.9)
    brain.memory.add("主人的猫叫团子", category="fact", importance=0.9)

    sp1 = brain.chat_context()
    ctx1 = brain.chat_context_dynamic("记不记得我明天要交房租")
    sp2 = brain.chat_context()
    ctx2 = brain.chat_context_dynamic("主人的猫叫什么")

    assert sp1.encode("utf-8") == sp2.encode("utf-8"), (
        "system 串不能随 query 变 —— 会打掉 8192-token 前缀缓存")
    assert "【记忆规则】" in sp1 and "【记忆规则】" in sp2
    # 易变段仍然随 query 变（说明上面那个恒等不是假象）
    assert ctx1 != ctx2


def test_a5_memory_rule_is_in_the_final_system_prompt(brain):
    """两个后端最终发给 API 的 system 串里都有【记忆规则】。"""
    from app.brain.agent import AgentLoop
    from app.brain.langchain_agent import LangChainAgent
    from app.brain.llm_client import LLMConfig

    class _Reg:
        def names(self, level=None):
            return ["open_app", "open_website"]

    reg = _Reg()
    persona = "你是小汐，住在主人桌面上的桌宠。"
    llm = LLMConfig(api_key="sk-test", model="test-model", stream=False)
    react = AgentLoop(client=None, registry=reg, level_getter=lambda: 3)
    standard = LangChainAgent(llm_cfg=llm, registry=reg, persona=persona,
                              level_getter=lambda: 3)
    session_ctx = brain.chat_context()
    for sp in (react.assemble_system_prompt(persona=persona + "\n\n"
                                              + session_ctx),
               standard.assemble_system_prompt(persona=persona + "\n\n"
                                                 + session_ctx)):
        assert "【记忆规则】" in sp


def test_a5_memory_rule_is_in_every_real_request(brain, qapp, monkeypatch):
    """真实聊天请求：system 串里每轮都带【记忆规则】。"""
    import app.ui.chat_window as cw_mod
    from app.core.config import CharacterConfig
    from app.ui.chat_window import ChatWindow, Message

    char_cfg = CharacterConfig(name="测试桌宠")
    llm_cfg = type("L", (), {
        "model": "test", "api_key": "dummy-key",
        "base_url": "http://127.0.0.1:1/v1", "timeout": 1,
        "stream": True, "temperature": 0.8, "max_tokens": 16,
    })()
    cw = ChatWindow(llm_cfg, char_cfg, str(ROOT / "assets" / "sprites"),
                    context_provider=brain.chat_context)

    captured = {}
    real_init = cw_mod._StreamWorker.__init__

    def _spy(self, client, messages):
        captured["messages"] = messages
        captured["system_prompt"] = client.system_prompt
        real_init(self, client, messages)

    monkeypatch.setattr(cw_mod._StreamWorker, "__init__", _spy)
    monkeypatch.setattr(cw_mod._StreamWorker, "start", lambda self: None)

    cw.history = [Message(role="user", content="在吗")]
    cw._kickoff_llm()

    assert "messages" in captured, "没抓到请求消息（_kickoff_llm 路径变了？）"
    assert "【记忆规则】" in captured["system_prompt"]


# ---------------------------------------------------------------------------
# A6：跑测试不许碰仓库 data/
# ---------------------------------------------------------------------------


def _snapshot_dir(path: Path) -> dict:
    """目录快照：相对路径 → (mtime_ns, size, sha256)。"""
    out = {}
    if not path.is_dir():
        return out
    for f in sorted(path.rglob("*")):
        if not f.is_file():
            continue
        rel = str(f.relative_to(path))
        st = f.stat()
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        out[rel] = (st.st_mtime_ns, st.st_size, digest)
    return out


def test_a6_running_skill_tests_does_not_touch_repo_data_dir(tmp_path):
    """跑 tests/test_skill.py 前后，仓库 data/ 必须**逐字节不变**。

    这条是 test_skill.py 那个 fixture 的回归闸：它以前用仓库根当 root，
    于是打开**用户 live 记忆库**（data/memory_<角色>.db），每跑一次全量测试
    就迁移一次真库、并在 data/ 留下 .bak-* 备份。
    """
    data_dir = ROOT / "data"
    before = _snapshot_dir(data_dir)

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["DESKTOP_PET_HOME"] = str(tmp_path / "home")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_skill.py", "-q"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]

    after = _snapshot_dir(data_dir)
    assert after == before, (
        "跑测试改了 data/：\n"
        f"新出现：{sorted(set(after) - set(before))}\n"
        f"消失：{sorted(set(before) - set(after))}\n"
        f"变了：{[k for k in set(before) & set(after) if before[k] != after[k]]}")
    assert not list(data_dir.glob("*.bak-*")), "data/ 下出现了 .bak 备份"


def test_a6_skill_test_fixture_uses_tmp_root():
    """源码级守卫：test_skill.py 的 fixture 不许再拿仓库根当 root。"""
    src = (ROOT / "tests" / "test_skill.py").read_text(encoding="utf-8")
    assert "BrainController(ROOT," not in src, (
        "test_skill.py 的 fixture 又用仓库根当 root —— 会打开 live 记忆库")
    assert "BrainController(tmp_path," in src

