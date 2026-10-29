# -*- coding: utf-8 -*-
"""Stage 2.6 · legacy / live 双库合并脚本的离线自检。

全部在临时目录里跑：仓库里的 data/ 一个字节都不动
（只有「对着真库试运行」那条测试会读真库，而它断言的就是「读完没变」）。

覆盖：
  A1  --dry-run 是默认；试运行不改任何文件
  A2  临时副本上 --apply 的结果：3 组自动发现 / 重复只留 live / 冲突两条都在且打了标记 /
      无效占位被丢弃并报告 / legacy 独有的有效行被并进 live / legacy 被归档（字节不变、可读）
  A3  动手之前先备份，且备份可还原
  A4  跑第二遍是空操作（幂等）
  A5  仓库里的 data/ 没被动过
  A7  对着真库的试运行报告
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "merge_memory_libraries.py"
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import merge_memory_libraries as M  # noqa: E402

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


def _snap(path: Path) -> dict:
    """文件指纹（mtime + size + sha256）。"""
    st = path.stat()
    return {"mtime": st.st_mtime, "size": st.st_size, "sha256": _sha256(path)}


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
    """读 memories 表（只读）：返回 (content, topic) 列表。"""
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


def _run(*args, cwd=None):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True, encoding="utf-8",
        env=env, cwd=str(cwd or ROOT))


def _run_json(*args) -> dict:
    proc = _run(*args, "--json")
    assert proc.returncode == 0, f"脚本失败：{proc.stderr}"
    return json.loads(proc.stdout)


# --------------------------------------------------------------------------
# fixture
# --------------------------------------------------------------------------
@pytest.fixture()
def dbs(tmp_path):
    """两个库的**临时副本**（仓库里的 data/ 只读，不动）。"""
    data = tmp_path / "data"
    data.mkdir()
    live = data / "memory_小汐.db"
    legacy = data / "memory.db"
    _write_db(live, LIVE_ROWS)
    _write_db(legacy, LEGACY_ROWS)
    return {"tmp": tmp_path, "data": data, "live": live, "legacy": legacy}


def _args(d, *extra):
    return ("--live", str(d["live"]), "--legacy", str(d["legacy"]),
            "--data-dir", str(d["data"]), *extra)


# --------------------------------------------------------------------------
# A1 · 试运行
# --------------------------------------------------------------------------
class TestDryRun:
    def test_dry_run_is_the_default_and_changes_nothing(self, dbs):
        """A1：不带 --apply 就是试运行，什么都不改。"""
        before = {p: _snap(p) for p in (dbs["live"], dbs["legacy"])}
        listing_before = sorted(p.name for p in dbs["data"].iterdir())

        proc = _run(*_args(dbs))
        assert proc.returncode == 0, proc.stderr
        assert "dry-run" in proc.stdout
        assert "什么也没改" in proc.stdout

        for path, snap in before.items():
            assert _snap(path) == snap, f"{path} 被改了"
        # 没有生成备份 / 归档目录
        assert sorted(p.name for p in dbs["data"].iterdir()) == listing_before
        assert not (dbs["data"] / "backups").exists()

    def test_dry_run_flag_is_accepted(self, dbs):
        proc = _run(*_args(dbs, "--dry-run"))
        assert proc.returncode == 0, proc.stderr
        assert "dry-run" in proc.stdout

    def test_dry_run_lists_the_three_known_groups(self, dbs):
        """A2/A4：3 组是**自动发现**的，脚本里没写死它们的内容。"""
        proc = _run(*_args(dbs))
        assert proc.returncode == 0, proc.stderr
        for name in ("生日", "创造者身份", "共同记忆占位"):
            assert name in proc.stdout, f"报告里没有列出分组「{name}」"
        # 重复 / 冲突 / 无效 / 独有 四类判定都在报告里
        assert "语义重复 duplicate" in proc.stdout
        assert "取值冲突 conflict" in proc.stdout
        assert "无效占位 invalid" in proc.stdout
        assert "仅 legacy 独有 unique" in proc.stdout

    def test_classification_json(self, dbs):
        data = _run_json(*_args(dbs))
        rel = {g["legacy_content"]: g["relation"] for g in data["groups"]}
        act = {g["legacy_content"]: g["action"] for g in data["groups"]}
        assert rel[BIRTHDAY_LEGACY] == "duplicate"
        assert act[BIRTHDAY_LEGACY] == "keep_live"
        assert rel[CREATOR_LEGACY] == "conflict"
        assert act[CREATOR_LEGACY] == "keep_both"
        assert rel[PLACEHOLDER] == "invalid"
        assert act[PLACEHOLDER] == "discard"
        assert rel[DUMPLING] == "unique"
        assert act[DUMPLING] == "insert"
        # 分组名（自动发现 + 规范化）
        labels = {g["label"] for g in data["groups"]}
        assert {"生日", "创造者身份", "共同记忆占位"} <= labels


# --------------------------------------------------------------------------
# A2 · 合并结果
# --------------------------------------------------------------------------
class TestApply:
    def _apply(self, dbs):
        proc = _run(*_args(dbs, "--apply"))
        assert proc.returncode == 0, proc.stderr
        return proc

    def test_apply_outcome(self, dbs):
        """A2：临时副本上 --apply 的结果完全符合预期。"""
        self._apply(dbs)
        contents = [c for c, _ in _read_db(dbs["live"])]

        # 重复的那条：只有 live 的形态
        assert BIRTHDAY_LIVE in contents
        assert BIRTHDAY_LEGACY not in contents
        # 冲突的那对：两条都在
        assert CREATOR_LIVE in contents
        assert CREATOR_LEGACY in contents
        # legacy 独有的有效行：并进了 live
        assert DUMPLING in contents
        # 无效占位：丢了
        assert PLACEHOLDER not in contents
        # live 原有的一条都没动
        assert len(contents) == 4

    def test_conflict_pair_is_marked(self, dbs):
        """A2：冲突的两条都要在，而且都要被标记。"""
        self._apply(dbs)
        rows = dict(_read_db(dbs["live"]))
        assert "conflict" in (rows[CREATOR_LIVE] or "")
        assert "conflict" in (rows[CREATOR_LEGACY] or "")
        # 非冲突的那条不该带标记
        assert "conflict" not in (rows[BIRTHDAY_LIVE] or "")
        assert "conflict" not in (rows[DUMPLING] or "")

        # 另外还有一张配对表，记着这一对冲突
        conn = sqlite3.connect(f"file:{dbs['live'].as_posix()}?mode=ro", uri=True)
        try:
            pairs = conn.execute(
                "SELECT legacy_id, live_id, legacy_content, live_content "
                "FROM memory_conflicts").fetchall()
        finally:
            conn.close()
        assert len(pairs) == 1
        assert pairs[0][2] == CREATOR_LEGACY
        assert pairs[0][3] == CREATOR_LIVE

    def test_legacy_is_archived_not_deleted(self, dbs):
        """A2：legacy 被**归档**（移动），字节不变、还能打开。"""
        legacy_hash = _sha256(dbs["legacy"])
        self._apply(dbs)

        assert not dbs["legacy"].exists(), "legacy 应该在原位消失（被归档走了）"
        archives = sorted((dbs["data"] / "backups").glob("memory_legacy-*.db"))
        assert len(archives) == 1, archives
        archived = archives[0]
        assert _sha256(archived) == legacy_hash, "归档文件必须跟原文件字节一致"
        # 还能打开、4 行一条不少
        rows = _read_db(archived)
        assert len(rows) == 4
        assert sorted(c for c, _ in rows) == sorted(c for _, c, _, _ in LEGACY_ROWS)

    def test_backup_is_taken_before_write_and_is_restorable(self, dbs):
        """A3：动手之前先备份，且备份能还原。"""
        legacy_hash = _sha256(dbs["legacy"])
        live_hash_before = _sha256(dbs["live"])
        self._apply(dbs)

        backups = sorted((dbs["data"] / "backups").glob("*/memory_live-*.db"))
        assert len(backups) == 1, backups
        live_backup = backups[0]
        # 备份的是**合并前**的 live 库（2 行），sha256 也对得上
        assert _sha256(live_backup) == live_hash_before
        assert len(_read_db(live_backup)) == 2

        legacy_backups = sorted((dbs["data"] / "backups").glob(
            "*/memory_legacy-*.db"))
        assert len(legacy_backups) == 1, legacy_backups
        assert _sha256(legacy_backups[0]) == legacy_hash
        assert len(_read_db(legacy_backups[0])) == 4

        # 拿备份还原 → 得到合并前的状态
        restored = dbs["tmp"] / "restored.db"
        shutil.copy2(live_backup, restored)
        assert len(_read_db(restored)) == 2
        assert _read_db(restored) == _read_db(live_backup)

    def test_apply_is_idempotent(self, dbs):
        """A4：跑第二遍不重复插。"""
        self._apply(dbs)
        after_first = _read_db(dbs["live"])

        # legacy 已经被归档走了 → 第二遍是空操作
        proc = _run(*_args(dbs, "--apply"))
        assert proc.returncode == 0, proc.stderr
        assert _read_db(dbs["live"]) == after_first

        # 最狠的情况：把 legacy 放回去再跑一遍
        archived = sorted((dbs["data"] / "backups").glob(
            "memory_legacy-*.db"))[0]
        shutil.copy2(archived, dbs["legacy"])
        proc = _run(*_args(dbs, "--apply"))
        assert proc.returncode == 0, proc.stderr
        rows = _read_db(dbs["live"])
        assert rows == after_first, "第二遍不能多插任何一行"
        contents = [c for c, _ in rows]
        assert len(contents) == len(set(contents)), "不能出现重复内容"

    def test_apply_reports_what_it_did(self, dbs):
        proc = self._apply(dbs)
        assert "已插入 live" in proc.stdout
        assert "已打 conflict 标记" in proc.stdout
        assert "已归档 legacy" in proc.stdout
        assert "字节一致=True" in proc.stdout
        assert "可还原=True" in proc.stdout


# --------------------------------------------------------------------------
# 分组是「自动发现」的，不是写死的内容
# --------------------------------------------------------------------------
class TestDiscovery:
    def test_unknown_content_is_also_grouped(self, tmp_path):
        """换一套完全不同的内容，脚本照样能自动分组 + 判定。"""
        data = tmp_path / "data"
        data.mkdir()
        live = data / "memory_x.db"
        legacy = data / "memory.db"
        _write_db(live, [("mnew01", "主人养了一只叫豆豆的猫", "fact", 0.6)])
        _write_db(legacy, [
            ("mnew02", "主人有一只猫叫豆豆", "fact", 0.6),
            ("mnew03", "共同记忆: 宠物叫 X", "fact", 0.5),
        ])
        data_out = _run_json("--live", str(live), "--legacy", str(legacy),
                             "--data-dir", str(data))
        rel = {g["legacy_content"]: g["relation"] for g in data_out["groups"]}
        assert rel["主人有一只猫叫豆豆"] == "duplicate"
        assert rel["共同记忆: 宠物叫 X"] == "invalid"

    def test_placeholder_detection_is_generic(self):
        """占位符判定不是针对某一句话写死的。"""
        assert M.is_placeholder("共享记忆: 主人叫 X")
        assert M.is_placeholder("共同记忆: 宠物叫 X")
        assert M.is_placeholder("主人叫 X")
        assert M.is_placeholder("备注：")
        assert not M.is_placeholder("主人的生日是2005年2月4号")
        assert not M.is_placeholder("主人非常喜欢韭菜猪肉馅的饺子")

    def test_value_agreement_rule(self):
        """取值一致 / 不一致的判定。"""
        agree, _why, richer = M.values_agree(
            "主人的生日是2005年2月4号", "主人生日在2月4号", "legacy", "live")
        assert agree and richer == "a"
        agree, _why, _r = M.values_agree(
            "主人是创造小汐的开发者，名字叫初晓旬依",
            "主人的开发者代号是cxxy，是小汐的创造者", "legacy", "live")
        assert not agree
        # 日期不同 → 冲突
        agree, _why, _r = M.values_agree(
            "主人的生日是2005年2月4号", "主人生日在2月5号", "legacy", "live")
        assert not agree


# --------------------------------------------------------------------------
# A5 · 仓库里的 data/ 不能被动
# --------------------------------------------------------------------------
class TestRepoDataUntouched:
    def test_dry_run_on_real_databases_changes_nothing(self):
        """A5/A7：对真库试运行，仓库里的 data/ 一个字节都不动。"""
        live = ROOT / "data" / "memory_小汐.db"
        legacy = ROOT / "data" / "memory.db"
        if not (live.is_file() and legacy.is_file()):
            pytest.skip("仓库里没有那两个库（干净检出）")
        before = {p: _snap(p) for p in (live, legacy)}
        backups_dir = ROOT / "data" / "backups"
        backups_before = (sorted(p.name for p in backups_dir.iterdir())
                          if backups_dir.is_dir() else None)

        proc = _run("--dry-run")
        assert proc.returncode == 0, proc.stderr

        for path, snap in before.items():
            assert _snap(path) == snap, f"{path} 被改了"
        if backups_before is None:
            assert not backups_dir.exists(), "试运行不该创建 backups/ 目录"
        else:
            assert sorted(p.name for p in backups_dir.iterdir()) == backups_before

    def test_repo_data_has_no_archive_leftovers(self):
        """回归保护：仓库 data/ 里不该出现本脚本产生的归档 / 备份。"""
        data = ROOT / "data"
        assert not list(data.glob("memory_legacy-*.db")), \
            "仓库 data/ 里出现了 legacy 归档文件（不该有）"
        backups = data / "backups"
        if backups.is_dir():
            assert not list(backups.glob("memory_legacy-*.db")), \
                "仓库 data/backups/ 里出现了 legacy 归档文件（不该有）"


# --------------------------------------------------------------------------
# 其他
# --------------------------------------------------------------------------
class TestCli:
    def test_missing_live_db_exits_nonzero(self, tmp_path):
        proc = _run("--live", str(tmp_path / "nope.db"),
                    "--legacy", str(tmp_path / "memory.db"),
                    "--data-dir", str(tmp_path))
        assert proc.returncode == 1
        assert "live 库不存在" in proc.stderr

    def test_missing_legacy_db_is_a_noop(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        live = data / "memory_x.db"
        _write_db(live, LIVE_ROWS)
        proc = _run("--live", str(live), "--legacy", str(data / "memory.db"),
                    "--data-dir", str(data))
        assert proc.returncode == 0
        assert "没什么可做的" in proc.stdout

    def test_help_mentions_archive_not_delete(self):
        proc = _run("--help")
        assert proc.returncode == 0
        assert "归档不是删除" in proc.stdout
        assert "不删除" in proc.stdout
