# -*- coding: utf-8 -*-
"""记忆库合并的**唯一实现**（CLI 脚本 + 面板共用同一份代码）—— 离线自检。

背景：Stage 2.6 写了 CLI 脚本 §scripts/merge_memory_libraries.py§，
Stage 5 又在面板里写了一份（§/api/memory/merge-preview§ +
§POST /api/maintenance/merge-memory§）。两份实现口径不一致，面板那份还漏了
三件事（冲突行没插进 live 库 / 不打 conflict 标记 / legacy 是复制不是移动）。
现在两边都调 §app/brain/memory_merge.py§ —— 本文件把「只有一份实现」钉死。

覆盖（对应验收标准）：
  A1  CLI 与面板对同一份输入给出**完全一样**的判定与结果（逐行比对）
  A2  面板合并把**冲突行插进 live 库**并打标记
      （memories.topic='conflict' + memory_conflicts 配对表）
  A3  面板合并把 legacy 库**移动**到 backups/ 归档（字节一致、可读、行数一致），
      data/ 顶层只剩**一个**记忆库
  A4  面板的备份**只打包它动的文件**（两个记忆库），不含 langchain_checkpoints.db
  A5  面板 /api/memory/merge-preview 的四类判定与 CLI 一致
  A6  面板的危险操作仍然是 POST-only + 要 confirm 标志
  A7  仓库里的 data/ 全程一个字节都没动

全部在 pytest 的临时目录里跑：仓库里的 data/ 只读，不动。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "merge_memory_libraries.py"
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

# --------------------------------------------------------------------------
# 测试数据（跟仓库里那两个真库一模一样）
# --------------------------------------------------------------------------
LIVE_ROWS = [
    ("m93a8381559", "主人生日在2月4号", "event", 0.8),
    ("m8234a9dca7", "主人的开发者代号是cxxy，是小汐的创造者", "fact", 0.9),
]
LEGACY_ROWS = [
    ("mf5735140d8", "主人的生日是2005年2月4号", "fact", 0.5),
    ("m2520d36e97", "主人非常喜欢韭菜猪肉馅的饺子", "preference", 0.8),
    ("m15d21aa86c", "主人是创造小汐的开发者，名字叫初晓旬依", "person", 1.0),
    ("me6653335c1", "共享记忆: 主人叫 X", "fact", 0.5),
]

BIRTHDAY_LEGACY = "主人的生日是2005年2月4号"
BIRTHDAY_LIVE = "主人生日在2月4号"
CREATOR_LEGACY = "主人是创造小汐的开发者，名字叫初晓旬依"
CREATOR_LIVE = "主人的开发者代号是cxxy，是小汐的创造者"
DUMPLING = "主人非常喜欢韭菜猪肉馅的饺子"
PLACEHOLDER = "共享记忆: 主人叫 X"

#: 老 schema（跟仓库里那两个真库一样，只有 8 列）
OLD_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id              TEXT PRIMARY KEY,
    content         TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT 'other',
    importance      REAL NOT NULL DEFAULT 0.5,
    created_at      REAL NOT NULL,
    last_access_ts  REAL NOT NULL,
    access_count    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_memories_created_at ON memories(created_at);
CREATE INDEX IF NOT EXISTS idx_memories_category    ON memories(category);
"""


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_db(path: Path, rows) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(OLD_SCHEMA)
        for i, (mid, content, cat, imp) in enumerate(rows):
            ts = 1700000000.0 + i
            conn.execute(
                "INSERT INTO memories (id, content, category, importance, "
                "created_at, last_access_ts, access_count) "
                "VALUES (?,?,?,?,?,?,0)",
                (mid, content, cat, imp, ts, ts))
        conn.commit()
    finally:
        conn.close()


def _read_db(path: Path) -> list:
    """读 memories 表（只读）：返回 [(content, topic)]。"""
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)")}
        want = ["content"] + (["topic"] if "topic" in cols else [])
        rows = conn.execute("SELECT " + ", ".join(want)
                            + " FROM memories ORDER BY content").fetchall()
        if "topic" in cols:
            return [(r[0], r[1]) for r in rows]
        return [(r[0], None) for r in rows]
    finally:
        conn.close()


def _conflicts(path: Path) -> list:
    """读 memory_conflicts 配对表。"""
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT legacy_id, live_id, legacy_content, live_content "
            "FROM memory_conflicts ORDER BY legacy_id, live_id").fetchall()
    finally:
        conn.close()


def _run_cli(*args) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, encoding="utf-8",
        env=env, cwd=str(ROOT))


def _request(app, method: str, url: str, **kwargs):
    """在 ASGI 层打一个请求（httpx ASGITransport，不发网络）。"""
    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://panel.test") as client:
            return await client.request(method, url, **kwargs)
    return asyncio.run(_go())


def _get_json(app, url: str):
    r = _request(app, "GET", url)
    assert r.status_code == 200, "%s -> %s" % (url, r.status_code)
    return r.json()


def _make_data_dir(root: Path) -> dict:
    """造一份临时 data/：两个记忆库 + 一个假的 checkpoint 库。"""
    root.mkdir(parents=True, exist_ok=True)
    live = root / "memory_小汐.db"
    legacy = root / "memory.db"
    _write_db(live, LIVE_ROWS)
    _write_db(legacy, LEGACY_ROWS)
    # 假 checkpoint 库：面板原来的备份会把它一起打包（289MB 的真身）
    checkpoints = root / "langchain_checkpoints.db"
    checkpoints.write_bytes(os.urandom(2 * 1024 * 1024))
    return {"root": root, "live": live, "legacy": legacy,
            "checkpoints": checkpoints}


def _build_panel(root: Path):
    """造一份临时 data/ 并返回 (app, 元信息)。"""
    from app.brain.trace import TraceRecorder
    rec = TraceRecorder(root / "traces.db")
    rec.close()

    meta = _make_data_dir(root)
    from app.web.dashboard import create_app
    app = create_app(root / "traces.db", root / "memory.json")
    return app, meta


# --------------------------------------------------------------------------
# fixture
# --------------------------------------------------------------------------
@pytest.fixture()
def data_dir(tmp_path):
    """一份临时 data/（CLI 那条路用）。"""
    return _make_data_dir(tmp_path / "data")


@pytest.fixture()
def panel(tmp_path):
    """一份临时 data/ + 面板（面板那条路用）。"""
    return _build_panel(tmp_path / "data")


# --------------------------------------------------------------------------
# A1 · CLI 与面板：同一份输入 → 同一份判定 + 同一份结果
# --------------------------------------------------------------------------
class TestOneImplementation:
    def test_cli_and_panel_classify_identically(self, tmp_path):
        """A1：两边的判定逐行一致（relation / action / similarity）。"""
        cli_dir = _make_data_dir(tmp_path / "cli")
        pan_dir = _make_data_dir(tmp_path / "panel")

        proc = _run_cli("--live", str(cli_dir["live"]),
                        "--legacy", str(cli_dir["legacy"]),
                        "--data-dir", str(cli_dir["root"]), "--json")
        assert proc.returncode == 0, proc.stderr
        cli = json.loads(proc.stdout)
        cli_groups = {g["legacy_content"]: g for g in cli["groups"]}

        from app.web.dashboard import _classify_legacy, _read_memory_db
        live_items = _read_memory_db(pan_dir["live"])
        legacy_items = _read_memory_db(pan_dir["legacy"])
        pan_groups = {p["legacy_content"]: p
                      for p in _classify_legacy(live_items, legacy_items)}

        assert set(cli_groups) == set(pan_groups), "两边覆盖的行不一样"
        for content, g in cli_groups.items():
            p = pan_groups[content]
            assert p["relation"] == g["relation"], (content, g, p)
            assert p["action"] == g["action"], (content, g, p)
            assert p["similarity"] == g["similarity"], (content, g, p)
            assert p["live_id"] == g["live_id"], (content, g, p)

    def test_cli_and_panel_end_result_identically(self, tmp_path):
        """A1：两边**真的合并一遍**，live 库逐行一样、归档件也一样。"""
        cli_dir = _make_data_dir(tmp_path / "cli")
        pan_dir = _make_data_dir(tmp_path / "panel")
        cli_legacy_hash = _sha256(cli_dir["legacy"])
        pan_legacy_hash = _sha256(pan_dir["legacy"])

        # --- CLI 那条路 ---
        proc = _run_cli("--live", str(cli_dir["live"]),
                        "--legacy", str(cli_dir["legacy"]),
                        "--data-dir", str(cli_dir["root"]), "--apply", "--json")
        assert proc.returncode == 0, proc.stderr
        cli = json.loads(proc.stdout)

        # --- 面板那条路 ---
        app, meta = _build_panel(tmp_path / "panel_data")
        r = _request(app, "POST",
                     "/api/maintenance/merge-memory"
                     "?maintenance=1&confirm=merge-memory")
        assert r.status_code == 200, r.text
        pan = r.json()

        # 判定数量一致
        assert len(cli["groups"]) == len(pan["pairs"])

        # live 库逐行一致（内容 + 冲突标记）
        cli_rows = sorted(_read_db(cli_dir["live"]))
        pan_rows = sorted(_read_db(meta["live"]))
        assert cli_rows == pan_rows, (cli_rows, pan_rows)
        assert sorted(c for c, _ in cli_rows) == sorted(
            [BIRTHDAY_LIVE, CREATOR_LIVE, CREATOR_LEGACY, DUMPLING])
        assert BIRTHDAY_LEGACY not in [c for c, _ in cli_rows]
        assert PLACEHOLDER not in [c for c, _ in cli_rows]

        # 冲突配对表一致
        assert _conflicts(cli_dir["live"]) == _conflicts(meta["live"])

        # 归档件一致：都是**移动**走的，字节不变、还能打开
        cli_arch = sorted(cli_dir["root"].glob("backups/memory_legacy-*.db"))
        pan_arch = sorted(meta["root"].glob("backups/memory_legacy-*.db"))
        assert len(cli_arch) == 1 and len(pan_arch) == 1
        assert _sha256(cli_arch[0]) == cli_legacy_hash
        assert _sha256(pan_arch[0]) == pan_legacy_hash
        assert len(_read_db(cli_arch[0])) == len(_read_db(pan_arch[0])) == 4

        # 两边都把 legacy 从原位移走了
        assert not cli_dir["legacy"].exists()
        assert not meta["legacy"].exists()

    def test_both_paths_use_the_shared_module(self):
        """A1：CLI 脚本与面板都只是**壳**，判定在共享模块里。"""
        from app.brain import memory_merge as mm
        import merge_memory_libraries as cli_mod  # noqa: F401
        assert cli_mod.classify is mm.classify
        assert cli_mod.apply_merge is mm.apply_merge
        assert cli_mod.is_placeholder is mm.is_placeholder


# --------------------------------------------------------------------------
# A2 · 冲突行必须插进 live 库并打标记
# --------------------------------------------------------------------------
class TestConflictRowIsKeptAndMarked:
    def test_conflict_row_is_inserted_into_live(self, panel):
        """A2：「主人是创造小汐的开发者，名字叫初晓旬依」必须进 live 库。"""
        app, meta = panel
        r = _request(app, "POST",
                     "/api/maintenance/merge-memory"
                     "?maintenance=1&confirm=merge-memory")
        assert r.status_code == 200, r.text
        data = r.json()

        contents = [c for c, _ in _read_db(meta["live"])]
        assert CREATOR_LEGACY in contents, "冲突的那条被弄丢了"
        assert CREATOR_LIVE in contents
        assert data["inserted_count"] == 2, data["inserted"]
        assert DUMPLING in contents

    def test_conflict_row_is_marked(self, panel):
        """A2：topic='conflict' + memory_conflicts 配对表（2.4 supersede 的输入）。"""
        app, meta = panel
        r = _request(app, "POST",
                     "/api/maintenance/merge-memory"
                     "?maintenance=1&confirm=merge-memory")
        assert r.status_code == 200, r.text

        rows = dict(_read_db(meta["live"]))
        assert "conflict" in (rows[CREATOR_LIVE] or "")
        assert "conflict" in (rows[CREATOR_LEGACY] or "")
        # 非冲突的那条不该带标记
        assert "conflict" not in (rows[BIRTHDAY_LIVE] or "")
        assert "conflict" not in (rows[DUMPLING] or "")

        pairs = _conflicts(meta["live"])
        assert len(pairs) == 1, pairs
        assert pairs[0][2] == CREATOR_LEGACY
        assert pairs[0][3] == CREATOR_LIVE


# --------------------------------------------------------------------------
# A3 · legacy 归档 = **移动**（不是复制，更不是删除）
# --------------------------------------------------------------------------
class TestLegacyIsArchivedByMoving:
    def test_legacy_moves_into_backups_and_data_keeps_one_library(self, panel):
        app, meta = panel
        before_bytes = meta["legacy"].read_bytes()
        before_hash = _sha256(meta["legacy"])

        r = _request(app, "POST",
                     "/api/maintenance/merge-memory"
                     "?maintenance=1&confirm=merge-memory")
        assert r.status_code == 200, r.text
        data = r.json()

        # 原位没了（被**移走**了）
        assert not meta["legacy"].exists()
        # 归档件在 backups/ 里，字节一模一样、还能打开、行数一条不少
        archived = Path(data["legacy_archived_to"])
        assert archived.is_file()
        assert archived.parent == meta["root"] / "backups"
        assert archived.name.startswith("memory_legacy-")
        assert _sha256(archived) == before_hash
        assert archived.read_bytes() == before_bytes
        rows = _read_db(archived)
        assert len(rows) == len(LEGACY_ROWS)
        assert sorted(c for c, _ in rows) == sorted(r[1] for r in LEGACY_ROWS)

        # data/ 顶层只剩**一个**记忆库
        dbs = sorted(p.name for p in meta["root"].glob("memory*.db"))
        assert dbs == ["memory_小汐.db"], dbs

    def test_merge_is_idempotent(self, panel):
        """跑第二遍不会多插任何一行（幂等）。"""
        app, meta = panel
        r = _request(app, "POST",
                     "/api/maintenance/merge-memory"
                     "?maintenance=1&confirm=merge-memory")
        assert r.status_code == 200, r.text
        after_first = sorted(_read_db(meta["live"]))

        r2 = _request(app, "POST",
                      "/api/maintenance/merge-memory"
                      "?maintenance=1&confirm=merge-memory")
        assert r2.status_code == 200, r2.text
        assert sorted(_read_db(meta["live"])) == after_first


# --------------------------------------------------------------------------
# A4 · 备份只打包它动的文件
# --------------------------------------------------------------------------
class TestBackupOnlyTouchesWhatItChanges:
    def test_backup_contains_only_the_memory_libraries(self, panel):
        app, meta = panel
        r = _request(app, "POST",
                     "/api/maintenance/merge-memory"
                     "?maintenance=1&confirm=merge-memory")
        assert r.status_code == 200, r.text
        data = r.json()
        backup = data["backup"]
        assert backup["ok"] is True

        archive = Path(backup["archive"])
        assert archive.is_file()
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
        assert sorted(names) == ["memory.db", "memory_小汐.db"], names
        # 那个 289MB 的 checkpoint 库**不在**备份里
        assert "langchain_checkpoints.db" not in names
        assert backup["bytes"] < meta["checkpoints"].stat().st_size
        assert {f["name"] for f in backup["packed"]} == {
            "memory.db", "memory_小汐.db"}


# --------------------------------------------------------------------------
# A5 · 预览 = CLI 的判定
# --------------------------------------------------------------------------
class TestPreviewMatchesCli:
    def test_preview_shows_the_four_relations(self, panel):
        app, meta = panel
        data = _get_json(app, "/api/memory/merge-preview")
        assert data["live_count"] == len(LIVE_ROWS)
        assert data["legacy_count"] == len(LEGACY_ROWS)
        rel = {p["legacy_content"]: p["relation"] for p in data["pairs"]}
        act = {p["legacy_content"]: p["action"] for p in data["pairs"]}
        assert rel[BIRTHDAY_LEGACY] == "duplicate"
        assert act[BIRTHDAY_LEGACY] == "keep_live"
        assert rel[CREATOR_LEGACY] == "conflict"
        assert act[CREATOR_LEGACY] == "keep_both"
        assert rel[DUMPLING] == "unique"
        assert act[DUMPLING] == "insert"
        assert rel[PLACEHOLDER] == "invalid"
        assert act[PLACEHOLDER] == "discard"
        assert len(data["will_insert"]) == 1
        assert len(data["will_keep_live"]) == 1
        assert len(data["will_keep_both"]) == 1
        assert len(data["will_discard"]) == 1

    def test_preview_is_read_only(self, panel):
        app, meta = panel
        before = {p: p.stat().st_mtime_ns for p in meta["root"].iterdir()
                  if p.is_file()}
        _get_json(app, "/api/memory/merge-preview")
        after = {p: p.stat().st_mtime_ns for p in meta["root"].iterdir()
                 if p.is_file()}
        assert before == after, "预览不该动任何文件"


# --------------------------------------------------------------------------
# A6 · 危险操作的安全闸门不许被削弱
# --------------------------------------------------------------------------
class TestSafetyGates:
    def test_merge_requires_post_and_confirm(self, panel):
        app, meta = panel
        assert _request(app, "GET",
                        "/api/maintenance/merge-memory").status_code == 405
        assert _request(app, "POST",
                        "/api/maintenance/merge-memory").status_code == 403
        assert _request(app, "POST",
                        "/api/maintenance/merge-memory"
                        "?maintenance=1").status_code == 400
        # 没动过任何文件
        assert meta["legacy"].is_file()
        assert len(_read_db(meta["live"])) == len(LIVE_ROWS)


# --------------------------------------------------------------------------
# A7 · 仓库里的 data/ 一个字节都不动
# --------------------------------------------------------------------------
def _snapshot(root: Path) -> dict:
    out = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_mtime_ns, st.st_size,
                                              _sha256(p))
    return out


@pytest.fixture(scope="module", autouse=True)
def _repo_data_guard():
    """A7：整个模块跑完，仓库里的 data/ 必须一字节都没变。"""
    before = _snapshot(ROOT / "data")
    yield
    after = _snapshot(ROOT / "data")
    assert after == before, (
        "仓库里的 data/ 被测试改动了："
        + str({k: v for k, v in after.items() if before.get(k) != v}))
