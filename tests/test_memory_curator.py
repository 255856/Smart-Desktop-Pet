"""Stage 2.5：MemoryCurator 真的被启动、真的在整理记忆。

以前 MemoryCurator 在全仓**从未被实例化** —— 整理线程从来没启动过，
forget_if_expired（删行）/ merge_similar（改写）一次都没跑过。

这里钉住：
- A1  BrainController 启动时构造并启动 MemoryCurator（daemon 线程存活）
- A2  curator.run_once() 在**临时库**上真的调了 forget_if_expired（删行）
      与 merge_similar（近重复行合并成一条）
- 数据安全：动库之前先备份；同一个库**一天只备一份**（幂等）

⚠️ 所有用例都用临时库，仓库的 data/ 一个字节都不碰。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from app.brain.memory import MemoryCurator, MemoryStore

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def _make_store(tmp_path: Path) -> MemoryStore:
    """临时记忆库（绝不用仓库里的 data/）。

    显式 tfidf 后端：sbert 要下模型（离线环境会卡住），而且行为不可预期。
    """
    return MemoryStore(tmp_path / "memory_probe.db", backend_name="tfidf")


def _read_rows(db_path: Path) -> list[str]:
    """直接读 sqlite —— 看**落盘**的结果，不看内存里的列表。"""
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        cur = conn.execute("SELECT content FROM memories")
        return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def _seed_store(store: MemoryStore) -> None:
    """塞三条：两条近重复 + 一条又老又不重要（会被清理）。"""
    store.add("主人喜欢喝冰美式", category="preference", importance=0.7)
    store.add("主人喜欢喝冰美式!", category="preference", importance=0.6)
    old = store.add("很久以前的一件琐碎小事", category="other", importance=0.1)
    # 造一条「90 天前 + importance < 0.3」的记忆 → forget_if_expired 会删它
    old.created_at = time.time() - 200 * 86400
    old.last_access_ts = old.created_at


# --------------------------------------------------------------------------
# A1：Curator 真的被实例化 + 线程真的活着
# --------------------------------------------------------------------------

@pytest.fixture
def brain(tmp_path):
    """真 BrainController（临时 root —— 不碰仓库 data/ 下的真库）。"""
    from app.core.qt_compat import QApplication
    QApplication.instance() or QApplication(sys.argv[:1])

    from app.core.config import load_config
    from app.engine.state_manager import StateManager
    from app.brain.brain_controller import BrainController

    skill_dst = tmp_path / ".agents" / "skills" / "desktop-pet-tool-usage"
    skill_dst.mkdir(parents=True)
    shutil.copy(ROOT / ".agents" / "skills" / "desktop-pet-tool-usage" / "SKILL.md",
                skill_dst)
    (tmp_path / "config.yaml").write_text(
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
    cfg = load_config(tmp_path / "config.yaml")
    state_mgr = StateManager(tmp_path, cfg, type("Args", (), {"reset_state": False})())
    ctrl = BrainController(tmp_path, cfg, state_mgr.state, state_mgr.reminders)
    yield ctrl
    ctrl.stop_curator()


def test_curator_is_instantiated_and_thread_alive(brain):
    """A1：BrainController 启动后，Curator 实例存在且整理线程活着。"""
    curator = brain.curator
    assert curator is not None, (
        "BrainController 必须构造 MemoryCurator —— "
        "以前它从未被实例化，整理线程一次都没跑过"
    )
    assert isinstance(curator, MemoryCurator)
    assert curator.memory is brain.memory, "Curator 必须管的是控制器自己的记忆库"
    thread = curator._thread
    assert thread is not None, "Curator 必须已经 start()"
    assert thread.is_alive(), "整理线程必须活着"
    assert thread.daemon is True, "整理线程必须是 daemon（不能挡住进程退出）"


def test_curator_stop_ends_thread(brain):
    """停止路径：stop_curator() 之后线程真的结束。"""
    assert brain.curator._thread.is_alive()
    brain.stop_curator()
    assert not brain.curator._thread.is_alive()


# --------------------------------------------------------------------------
# A2：run_once() 真的在整理（临时库上的可观测效果）
# --------------------------------------------------------------------------

def test_run_once_deletes_expired_and_merges_duplicates(tmp_path):
    """A2：run_once() 真的调了 forget_if_expired（删行）+ merge_similar（合并）。"""
    from app.brain.brain_controller import _GuardedMemoryCurator

    store = _make_store(tmp_path)
    _seed_store(store)
    assert len(store.all()) == 3

    curator = _GuardedMemoryCurator(store, check_interval_hours=12)
    stats = curator.run_once()

    # 统计里能看到两件事都发生了
    assert stats["expired_removed"] == 1, f"过期记忆没被删：{stats}"
    assert stats["merged_removed"] == 1, f"近重复记忆没被合并：{stats}"

    # 效果看得见（内存 + 落盘都查）
    contents = [i.content for i in store.all()]
    assert "很久以前的一件琐碎小事" not in contents, "过期记忆还在"
    assert len(contents) == 1, f"两条近重复记忆没合并成一条：{contents}"
    assert contents[0] == "主人喜欢喝冰美式", "应保留 importance 更高的那条"

    rows = _read_rows(tmp_path / "memory_probe.db")
    assert "很久以前的一件琐碎小事" not in rows, "过期记忆还躺在 db 里"
    assert rows == ["主人喜欢喝冰美式"], f"db 里还是两条：{rows}"


def test_run_once_keeps_important_and_recent_memories(tmp_path):
    """整理不能误伤：重要 / 近期 / 高 importance 的都要留着。"""
    from app.brain.brain_controller import _GuardedMemoryCurator

    store = _make_store(tmp_path)
    store.add("主人的生日是2005年2月4号", category="fact", importance=0.9)
    store.add("主人的开发者代号是cxxy", category="fact", importance=0.8)
    old_but_important = store.add("很久以前但很重要的事", category="fact", importance=0.95)
    old_but_important.created_at = time.time() - 300 * 86400
    old_but_important.last_access_ts = old_but_important.created_at

    curator = _GuardedMemoryCurator(store, check_interval_hours=12)
    stats = curator.run_once()

    assert stats["expired_removed"] == 0, "重要记忆不该被过期清理"
    assert len(store.all()) == 3


# --------------------------------------------------------------------------
# 数据安全：动 live 库之前先备份 + 一天只备一份
# --------------------------------------------------------------------------

def test_backup_taken_before_destructive_run(tmp_path):
    """备份必须是**整理前**的快照（否则备份没有回滚价值）。"""
    from app.brain.brain_controller import _GuardedMemoryCurator

    store = _make_store(tmp_path)
    _seed_store(store)

    curator = _GuardedMemoryCurator(store, check_interval_hours=12)
    curator.run_once()

    backups = list((tmp_path / "backups" / "memory_probe").glob("*.db"))
    assert len(backups) == 1, f"应该恰好备了一份：{backups}"
    rows = _read_rows(backups[0])
    assert "很久以前的一件琐碎小事" in rows, (
        "备份里必须还有整理前的那条过期记忆（备份要早于删除）"
    )
    assert len(rows) == 3, f"备份应该是整理前的 3 条：{rows}"


def test_backup_is_idempotent_one_per_day(tmp_path):
    """幂等：同一个库一天只备一份（不能每次启动都写一份把磁盘塞满）。"""
    from app.brain.brain_controller import backup_memory_db

    db = tmp_path / "memory_probe.db"
    store = MemoryStore(db, backend_name="tfidf")
    store.add("主人喜欢喝冰美式", category="preference", importance=0.7)

    first = backup_memory_db(db)
    second = backup_memory_db(db)
    third = backup_memory_db(db)

    assert first is not None and first.is_file()
    assert second == first and third == first, "同一天不该重复写备份"
    backups = list((tmp_path / "backups" / "memory_probe").glob("*.db"))
    assert len(backups) == 1, f"备份文件数应该是 1：{backups}"
    # 备份内容要跟源库一致
    assert _read_rows(backups[0]) == _read_rows(db)


def test_backup_of_missing_db_is_noop(tmp_path):
    """库还没建出来时备份要安静地跳过，不能抛。"""
    from app.brain.brain_controller import backup_memory_db

    assert backup_memory_db(tmp_path / "nope.db") is None
