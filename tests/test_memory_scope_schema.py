"""Stage 2.2：记忆作用域 + schema 迁移的验收测试。

钉死的行为：
    A8 迁移后新列都在；老行回填（scope='project' / status='active' /
       kind 按 category 映射）；**行数不变**
    A9 scope='user' 的行落在 ~/.desktop-pet/user_memory.db，
       能通过工具（scope='user'）召回；scope='project'（默认）行为不变
    A10 迁移幂等；改库之前先备份（备份里是**改动前**的表结构）

所有测试都跑在 tmp 目录上（临时 db），不碰仓库的 data/。
不需要 API key / 网络 / GUI。
"""
from __future__ import annotations

import sqlite3

import pytest

from app.brain.memory import (
    MemoryStore,
    user_memory_db_path,
    user_memory_dir,
)
from app.engine.tools import ToolRegistry
from app.engine.tools._memory import register as register_memory_tools

#: Stage 2.2 的新列
NEW_COLUMNS = {
    "scope", "kind", "topic", "status",
    "superseded_by", "hit_count", "last_hit_at",
}

#: 老的表结构（Stage 2.2 之前）
LEGACY_SCHEMA = """
CREATE TABLE memories (
    id              TEXT PRIMARY KEY,
    content         TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'other',
    importance      REAL NOT NULL DEFAULT 0.5,
    created_at      REAL NOT NULL,
    last_access_ts  REAL NOT NULL,
    access_count    INTEGER NOT NULL DEFAULT 0
);
"""


def _make_legacy_db(path, rows):
    """造一个「Stage 2.2 之前」的库。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(LEGACY_SCHEMA)
        conn.executemany(
            "INSERT INTO memories (id, content, category, importance, "
            "created_at, last_access_ts, access_count) VALUES (?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _columns(path):
    conn = sqlite3.connect(str(path))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
    finally:
        conn.close()


# ----------------------------
# A8：迁移补列 + 回填 + 行数不变
# ----------------------------


def test_a8_migration_adds_columns_and_backfills(tmp_path):
    db = tmp_path / "memory.db"
    _make_legacy_db(db, [
        ("m1", "主人喜欢喝冰美式", "preference", 0.8, 100.0, 100.0, 0),
        ("m2", "上周去了北海道", "event", 0.5, 100.0, 100.0, 0),
        ("m3", "主人的猫叫团子", "fact", 0.5, 100.0, 100.0, 0),
        ("m4", "主人的老板姓王", "person", 0.5, 100.0, 100.0, 0),
    ])
    before_rows = sqlite3.connect(str(db)).execute(
        "SELECT COUNT(*) FROM memories").fetchone()[0]

    store = MemoryStore(db, backend_name="tfidf")

    cols = _columns(db)
    assert NEW_COLUMNS <= cols, f"缺列：{NEW_COLUMNS - cols}"
    items = {i.id: i for i in store.all()}
    # 回填：scope / status
    assert all(i.scope == "project" for i in items.values()), items
    assert all(i.status == "active" for i in items.values()), items
    # 回填：kind 按 category 映射
    assert items["m1"].kind == "memberImpression"     # preference
    assert items["m2"].kind == "activeTopic"          # event
    assert items["m3"].kind == "fact"                  # fact
    assert items["m4"].kind == "memberImpression"     # person
    # 行数不变
    assert store.count() == before_rows == 4


def test_a8_new_rows_get_default_scope_and_kind(tmp_path):
    s = MemoryStore(tmp_path / "memory.db", backend_name="tfidf")
    it = s.add("主人喜欢喝冰美式", "preference")
    assert it.scope == "project"
    assert it.kind == "memberImpression"
    assert it.status == "active"
    assert it.topic is None
    assert it.superseded_by is None
    assert it.hit_count == 0
    assert it.last_hit_at is None


def test_a8_explicit_kind_and_topic_are_kept(tmp_path):
    s = MemoryStore(tmp_path / "memory.db", backend_name="tfidf")
    it = s.add("正在纠结换不换工作", "other", kind="pendingThought", topic="工作")
    assert it.kind == "pendingThought"
    assert it.topic == "工作"
    # 落盘后还能读回来
    again = MemoryStore(tmp_path / "memory.db", backend_name="tfidf").get(it.id)
    assert again is not None
    assert again.kind == "pendingThought"
    assert again.topic == "工作"


# ----------------------------
# A10：幂等 + 先备份
# ----------------------------


def test_a10_migration_is_idempotent(tmp_path):
    db = tmp_path / "memory.db"
    _make_legacy_db(db, [
        ("m1", "主人喜欢喝冰美式", "preference", 0.8, 100.0, 100.0, 0),
    ])
    s1 = MemoryStore(db, backend_name="tfidf")
    assert s1.count() == 1
    backups_after_first = sorted(tmp_path.glob("memory.db.bak-*"))
    assert len(backups_after_first) == 1, backups_after_first

    # 第二次打开：列都在了 → 不再迁移、不再备份、行数不变
    s2 = MemoryStore(db, backend_name="tfidf")
    assert s2.count() == 1
    assert len(sorted(tmp_path.glob("memory.db.bak-*"))) == 1
    assert s2.get("m1").scope == "project"
    assert s2.get("m1").status == "active"
    assert s2.get("m1").kind == "memberImpression"


def test_a10_backup_is_taken_before_alter(tmp_path):
    """备份里必须是**改动前**的表结构（没有 scope 列）。"""
    db = tmp_path / "memory.db"
    _make_legacy_db(db, [
        ("m1", "主人喜欢喝冰美式", "preference", 0.8, 100.0, 100.0, 0),
        ("m2", "主人的猫叫团子", "fact", 0.5, 100.0, 100.0, 0),
    ])
    MemoryStore(db, backend_name="tfidf")
    backups = sorted(tmp_path.glob("memory.db.bak-*"))
    assert len(backups) == 1, backups
    bak_cols = _columns(backups[0])
    assert "scope" not in bak_cols, "备份应是改动前的库"
    assert "scope" in _columns(db), "live 库应已迁移"
    # 备份里的行还在（用户数据没丢）
    conn = sqlite3.connect(str(backups[0]))
    try:
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2
    finally:
        conn.close()


def test_a10_fresh_db_needs_no_backup(tmp_path):
    """新库直接带新列 → 不产生备份文件。"""
    db = tmp_path / "memory.db"
    s = MemoryStore(db, backend_name="tfidf")
    s.add("主人喜欢喝冰美式", "preference", importance=0.5)   # 首次写入才建库
    assert list(tmp_path.glob("memory.db.bak-*")) == []
    assert NEW_COLUMNS <= _columns(db)


# ----------------------------
# A9：scope 分库 + 工具接口
# ----------------------------


def test_a9_default_user_db_path_is_dot_desktop_pet(monkeypatch):
    monkeypatch.delenv("DESKTOP_PET_HOME", raising=False)
    home = user_memory_dir()
    assert home.name == ".desktop-pet"
    assert user_memory_db_path().name == "user_memory.db"
    assert user_memory_db_path().parent == home


def test_a9_user_scope_goes_to_user_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKTOP_PET_HOME", str(tmp_path / "home"))
    project_db = tmp_path / "project.db"
    store = MemoryStore(project_db, backend_name="tfidf")
    reg = ToolRegistry()
    register_memory_tools(reg, memory=store)
    remember = reg.get("remember_fact").fn
    recall = reg.get("recall_memory").fn

    out = remember(content="主人有两只猫", category="fact",
                   importance=0.8, scope="user")
    assert "已记住" in out, out

    user_db = tmp_path / "home" / "user_memory.db"
    assert user_db.is_file(), "user scope 应写进用户级库"
    # 项目库里一条都没有
    assert store.count() == 0
    # 用户级库里有一条
    user_store = MemoryStore(user_db, backend_name="tfidf")
    assert user_store.count() == 1
    assert user_store.all()[0].scope == "user"

    # 工具能召回（scope='user'）
    out = recall(keyword="猫", scope="user")
    assert "主人有两只猫" in out, out
    # project scope 召回不到（老行为：只查本项目库）
    out = recall(keyword="猫", scope="project")
    assert "没有找到相关记忆" in out, out


def test_a9_default_scope_is_project(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKTOP_PET_HOME", str(tmp_path / "home"))
    store = MemoryStore(tmp_path / "project.db", backend_name="tfidf")
    reg = ToolRegistry()
    register_memory_tools(reg, memory=store)
    remember = reg.get("remember_fact").fn
    recall = reg.get("recall_memory").fn

    remember(content="主人喜欢喝冰美式", category="preference", importance=0.7)
    # 默认写进项目库
    assert store.count() == 1
    assert not (tmp_path / "home" / "user_memory.db").exists()
    # 默认召回走项目库
    out = recall(keyword="冰美式")
    assert "主人喜欢喝冰美式" in out, out


def test_a9_forget_memory_with_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKTOP_PET_HOME", str(tmp_path / "home"))
    store = MemoryStore(tmp_path / "project.db", backend_name="tfidf")
    reg = ToolRegistry()
    register_memory_tools(reg, memory=store)
    remember = reg.get("remember_fact").fn
    forget = reg.get("forget_memory").fn

    remember(content="主人喜欢喝冰美式", category="preference", scope="user")
    assert forget(keyword="冰美式", scope="project").startswith("错误")
    out = forget(keyword="冰美式", scope="user")
    assert "已删除 1 条" in out, out


def test_a9_tool_schema_has_scope_with_project_default():
    """工具 schema：scope 参数存在，enum 覆盖 project/user。"""
    from app.brain.memory import SCOPE_CHOICES
    reg = ToolRegistry()
    register_memory_tools(reg, memory=object())
    for name in ("remember_fact", "recall_memory", "forget_memory"):
        tool = reg.get(name)
        assert tool is not None, name
        props = tool.parameters["properties"]
        assert "scope" in props, name
        assert props["scope"]["enum"] == list(SCOPE_CHOICES), name


def test_a9_store_for_scope_returns_user_store(tmp_path, monkeypatch):
    monkeypatch.setenv("DESKTOP_PET_HOME", str(tmp_path / "home"))
    store = MemoryStore(tmp_path / "project.db", backend_name="tfidf")
    assert store.store_for_scope("project") is store
    user_store = store.store_for_scope("user")
    assert user_store is not store
    assert user_store.data_file == tmp_path / "home" / "user_memory.db"
    # 同一个实例（缓存）
    assert store.store_for_scope("user") is user_store
