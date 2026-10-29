# -*- coding: utf-8 -*-
"""危险操作的备份范围：**只备份这次操作真正会动的文件**。

原来的 §app/web/dashboard.py§ 里三个维护端点（backup / reset-ledger /
reconciliation）都调 §_backup_data_dir()§ 把**整个** data/ 打成 zip ——
里面混着 ~289MB 的 §langchain_checkpoints.db§（应用自己都按 512KB 上限 prune 它）。
于是「重置一下 token 账本」也要先付 289MB 的代价。

这里把三条都钉死（对应验收标准）：
  A1  reset-ledger 的备份**只有**账本（+ 归档本身），而且很小（< 1MB）
  A2  reconciliation 不写 data/ → **不产生**任何归档（更不会有 289MB 的包）
  A3  全量 backup 默认**排除** langchain_checkpoints.db；
      §?include_checkpoints=1§ 才把它打进去
  A4  每个响应都报归档路径 + 大小 + 文件名单
  A5  安全属性没丢：GET 不动手、confirm 不对被拒、被拒时 data/ 一个字节都没变
  A6  仓库里的 data/ 全程 mtime + size + sha256 一模一样
      （所有用例都跑在 pytest 临时目录里，一个字节都不碰仓库）

只用 httpx 的 ASGITransport 直接打 ASGI 应用：不发网络、不起浏览器、不起子进程。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import zipfile
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent

# 假 checkpoint 库的大小：3MB 随机字节（压不动，才看得出归档大小的差别）。
FAKE_CKPT_BYTES = 3 * 1024 * 1024
# 「小归档」的门槛：1MB。
SMALL_ARCHIVE_MAX = 1024 * 1024

LEDGER_ROWS = [
    {"v": 1, "kind": "request", "session_id": "char:test", "request_index": 1,
     "backend": "standard", "retry": 0, "ts": 1700000000.0, "model": "MiniMax-M3",
     "prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
     "cache_hit_tokens": 100, "cache_miss_tokens": 300, "cache_write_tokens": 0},
    {"v": 1, "kind": "request", "session_id": "char:test", "request_index": 2,
     "backend": "standard", "retry": 0, "ts": 1700000060.0, "model": "MiniMax-M3",
     "prompt_tokens": 800, "completion_tokens": 100, "total_tokens": 900,
     "cache_hit_tokens": 200, "cache_miss_tokens": 100, "cache_write_tokens": 0},
]

# 危险操作端点 -> confirm 标志
DANGEROUS_OPS = {
    "/api/maintenance/backup": "backup",
    "/api/maintenance/reset-ledger": "reset-ledger",
    "/api/maintenance/reconciliation": "rerun-reconciliation",
}


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _fingerprint(path: Path) -> dict:
    """目录指纹：相对路径 -> (mtime_ns, size, sha256)。

    比 mtime+size 更严 —— 同样大小、同样 mtime 的替换也能被抓出来。
    """
    out: dict = {}
    if not path.exists():
        return out
    for p in sorted(path.rglob("*")):
        if p.is_file():
            st = p.stat()
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            out[str(p.relative_to(path))] = (st.st_mtime_ns, st.st_size, digest)
    return out


def _request(app, method: str, url: str, **kwargs):
    """在 ASGI 层打一个请求（httpx ASGITransport，不发网络）。"""
    async def _go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport,
                                     base_url="http://panel.test") as client:
            return await client.request(method, url, **kwargs)
    return asyncio.run(_go())


def _zip_names(archive: Path) -> list:
    with zipfile.ZipFile(archive) as zf:
        return zf.namelist()


def _build_panel(root: Path):
    """造一份临时 data/ 并返回 (app, root)。

    data/ 里放：账本、假 checkpoint 库（3MB）、一份 txt。
    仓库里的 data/ 一个字节都不动。
    """
    root.mkdir(parents=True, exist_ok=True)
    (root / "token-usage.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in LEDGER_ROWS) + "\n",
        encoding="utf-8")
    # 假 checkpoint 库：确定性随机字节（压不动）
    (root / "langchain_checkpoints.db").write_bytes(
        random.Random(20261029).randbytes(FAKE_CKPT_BYTES))
    (root / "companion_log.txt").write_text("fake companion log\n", encoding="utf-8")

    from app.web.dashboard import create_app
    app = create_app(root / "traces.db", root / "memory.json")
    return app, root


# --------------------------------------------------------------------------
# fixture
# --------------------------------------------------------------------------
@pytest.fixture(scope="module", autouse=True)
def _repo_data_guard():
    """A6：整个模块跑完，仓库里的 data/ 必须一个字节都没变。"""
    before = _fingerprint(ROOT / "data")
    yield
    after = _fingerprint(ROOT / "data")
    changed = {k: (before.get(k), after.get(k))
               for k in set(before) | set(after)
               if before.get(k) != after.get(k)}
    assert not changed, "仓库里的 data/ 被测试改动了：%s" % changed


@pytest.fixture()
def panel(tmp_path):
    """每个用例一份全新的临时 data/。"""
    app, root = _build_panel(tmp_path / "data")
    return {"app": app, "root": root}


# --------------------------------------------------------------------------
# A1 · reset-ledger：只备份账本（+ 归档本身）
# --------------------------------------------------------------------------
class TestResetLedgerScope:
    def test_backup_contains_only_the_ledger(self, panel):
        app, root = panel["app"], panel["root"]
        r = _request(app, "POST",
                     "/api/maintenance/reset-ledger"
                     "?maintenance=1&confirm=reset-ledger")
        assert r.status_code == 200, r.text
        data = r.json()
        backup = data["backup"]
        assert backup["ok"] is True

        # 归档里**只有**账本
        archive = Path(backup["archive"])
        assert archive.is_file(), archive
        names = _zip_names(archive)
        assert names == ["token-usage.jsonl"], names
        assert backup["files"] == ["token-usage.jsonl"], backup["files"]
        # 响应里说清楚「到底备份了哪个文件」
        assert data["backup_files"] == ["token-usage.jsonl"], data["backup_files"]
        assert "token-usage.jsonl" in (backup.get("reason") or "")

        # 假 checkpoint 库没被卷进去
        assert "langchain_checkpoints.db" not in names

    def test_archive_is_small(self, panel):
        """归档远小于 1MB（原来会把 289MB 的 checkpoint 库一起打包）。"""
        app = panel["app"]
        r = _request(app, "POST",
                     "/api/maintenance/reset-ledger"
                     "?maintenance=1&confirm=reset-ledger")
        assert r.status_code == 200, r.text
        data = r.json()
        archive = Path(data["backup"]["archive"])
        size = archive.stat().st_size
        assert size < SMALL_ARCHIVE_MAX, "归档太大了：%d 字节" % size
        assert data["backup_bytes"] == size
        # 备份目录里也没有大文件
        for p in Path(data["backup"]["dir"]).rglob("*"):
            if p.is_file():
                assert p.stat().st_size < SMALL_ARCHIVE_MAX, p

    def test_old_ledger_is_archived_not_deleted(self, panel):
        """旧账本的内容还在备份目录里（备份只复制，不删）。"""
        app, root = panel["app"], panel["root"]
        ledger = root / "token-usage.jsonl"
        old = ledger.read_text(encoding="utf-8")
        r = _request(app, "POST",
                     "/api/maintenance/reset-ledger"
                     "?maintenance=1&confirm=reset-ledger")
        assert r.status_code == 200, r.text
        data = r.json()
        archived = Path(data["archived_to"])
        assert archived.is_file()
        assert archived.read_text(encoding="utf-8") == old
        assert data["rows_before"] == len(LEDGER_ROWS)
        assert ledger.read_text(encoding="utf-8") == ""


# --------------------------------------------------------------------------
# A2 · reconciliation：不写 data/ → 不需要备份
# --------------------------------------------------------------------------
class TestReconciliationNoBackup:
    def test_no_archive_is_created(self, panel):
        app, root = panel["app"], panel["root"]
        before = _fingerprint(root)
        r = _request(app, "POST",
                     "/api/maintenance/reconciliation"
                     "?maintenance=1&confirm=rerun-reconciliation")
        assert r.status_code == 200, r.text
        data = r.json()

        # 明确说清「这次没备份」
        backup = data["backup"]
        assert backup["noop"] is True, backup
        assert backup["archive"] is None
        assert backup["bytes"] == 0
        assert backup["files"] == []
        assert "无需备份" in (backup.get("reason") or "")

        # 一个归档都没造出来（更不会有 289MB 的包）
        assert list(root.rglob("*.zip")) == []
        # 也没有任何「比 1MB 大」的新文件被造出来
        after = _fingerprint(root)
        assert set(after) == set(before), (set(after) - set(before))
        for name in after:
            assert after[name][1] == before[name][1], name

    def test_data_is_untouched(self, panel):
        """对账只读 —— data/ 里一个字节都不能变。"""
        app, root = panel["app"], panel["root"]
        before = _fingerprint(root)
        r = _request(app, "POST",
                     "/api/maintenance/reconciliation"
                     "?maintenance=1&confirm=rerun-reconciliation")
        assert r.status_code == 200, r.text
        assert _fingerprint(root) == before, "对账不该动 data/ 里的任何文件"


# --------------------------------------------------------------------------
# A3 · 全量备份：默认排除 checkpoint 库，显式 opt-in 才包含
# --------------------------------------------------------------------------
class TestFullBackupScope:
    def test_excludes_checkpoints_by_default(self, panel):
        app = panel["app"]
        r = _request(app, "POST",
                     "/api/maintenance/backup?maintenance=1&confirm=backup")
        assert r.status_code == 200, r.text
        data = r.json()
        backup = data["backup"]
        assert backup["ok"] is True

        archive = Path(backup["archive"])
        names = _zip_names(archive)
        assert "langchain_checkpoints.db" not in names, names
        assert backup["files"] == names
        assert data["backup_files"] == names

        # 响应里明说：排除了、为什么、怎么才能包含
        ck = data["checkpoints"]
        assert ck["included"] is False
        assert ck["requested"] is False
        assert ck["excluded"] == ["langchain_checkpoints.db"]
        assert "include_checkpoints=1" in ck["reason"]
        assert "checkpoint" in ck["reason"]

        # 该在的文件一个不少（PLAN 5.3：*.db + *.jsonl + *.txt）
        assert {"traces.db", "token-usage.jsonl", "companion_log.txt"} <= set(names)

        # 归档里没有那个 3MB 的假 checkpoint 库 → 归档很小
        assert archive.stat().st_size < SMALL_ARCHIVE_MAX, archive.stat().st_size

    def test_includes_checkpoints_when_opted_in(self, panel):
        app = panel["app"]
        r = _request(app, "POST",
                     "/api/maintenance/backup"
                     "?maintenance=1&confirm=backup&include_checkpoints=1")
        assert r.status_code == 200, r.text
        data = r.json()
        backup = data["backup"]

        archive = Path(backup["archive"])
        names = _zip_names(archive)
        assert "langchain_checkpoints.db" in names, names
        assert backup["files"] == names
        assert data["backup_files"] == names

        ck = data["checkpoints"]
        assert ck["included"] is True
        assert ck["requested"] is True
        assert ck["excluded"] == []

        # 3MB 随机字节压不动 → 归档明显大于 1MB（证明它真的被打进去了）
        assert archive.stat().st_size > SMALL_ARCHIVE_MAX, archive.stat().st_size
        assert data["backup_bytes"] == archive.stat().st_size

    def test_backup_does_not_modify_data(self, panel):
        """备份只是复制：data/ 里的原文件一个都没变。"""
        app, root = panel["app"], panel["root"]
        before = _fingerprint(root)
        r = _request(app, "POST",
                     "/api/maintenance/backup?maintenance=1&confirm=backup")
        assert r.status_code == 200, r.text
        after = _fingerprint(root)
        # 只有 backups/ 是新出现的，其余文件一字不改
        for name, info in before.items():
            assert after.get(name) == info, name


# --------------------------------------------------------------------------
# A4 · 每个响应都报：归档路径 + 大小 + 文件名单
# --------------------------------------------------------------------------
class TestResponseShape:
    def test_backup_response_reports_path_size_and_files(self, panel):
        app = panel["app"]
        r = _request(app, "POST",
                     "/api/maintenance/backup?maintenance=1&confirm=backup")
        data = r.json()
        backup = data["backup"]
        archive = Path(backup["archive"])
        assert archive.is_file()
        assert backup["bytes"] == archive.stat().st_size
        assert backup["files"] == _zip_names(archive)
        # 顶层也有一份（方便前端/脚本直接读）
        assert data["backup_archive"] == backup["archive"]
        assert data["backup_bytes"] == backup["bytes"]
        assert data["backup_files"] == backup["files"]

    def test_reset_ledger_response_reports_path_size_and_files(self, panel):
        app = panel["app"]
        r = _request(app, "POST",
                     "/api/maintenance/reset-ledger"
                     "?maintenance=1&confirm=reset-ledger")
        data = r.json()
        backup = data["backup"]
        archive = Path(backup["archive"])
        assert archive.is_file()
        assert backup["bytes"] == archive.stat().st_size
        assert backup["files"] == _zip_names(archive) == ["token-usage.jsonl"]
        assert data["backup_archive"] == backup["archive"]
        assert data["backup_bytes"] == backup["bytes"]
        assert data["backup_files"] == backup["files"]

    def test_reconciliation_response_reports_no_archive(self, panel):
        app = panel["app"]
        r = _request(app, "POST",
                     "/api/maintenance/reconciliation"
                     "?maintenance=1&confirm=rerun-reconciliation")
        data = r.json()
        assert data["backup_archive"] is None
        assert data["backup_bytes"] == 0
        assert data["backup_files"] == []

    def test_old_fields_are_still_there(self, panel):
        """面板 JS 读的老字段不能被删掉（backup.dir / packed / ok ...）。"""
        app = panel["app"]
        for url, token in DANGEROUS_OPS.items():
            r = _request(app, "POST",
                         url + "?maintenance=1&confirm=" + token)
            assert r.status_code == 200, (url, r.text)
            data = r.json()
            backup = data["backup"]
            for key in ("ok", "dir", "archive", "bytes", "packed", "files"):
                assert key in backup, (url, key)


# --------------------------------------------------------------------------
# A5 · 安全属性：GET 不动手 / confirm 不对被拒 / 被拒时 data/ 一字不改
# --------------------------------------------------------------------------
class TestSafetyProperties:
    def test_get_does_nothing(self, panel):
        app, root = panel["app"], panel["root"]
        before = _fingerprint(root)
        for url in DANGEROUS_OPS:
            r = _request(app, "GET", url)
            assert r.status_code in (404, 405), (url, r.status_code)
        assert _fingerprint(root) == before, "GET 不该动任何文件"

    def test_rejected_requests_do_not_touch_data(self, panel):
        app, root = panel["app"], panel["root"]
        before = _fingerprint(root)
        for url, token in DANGEROUS_OPS.items():
            cases = [
                "",                                   # 没开维护模式
                "?maintenance=1",                     # 开了但没 confirm
                "?maintenance=1&confirm=wrong",      # confirm 写错
                "?confirm=" + token,                 # confirm 对但没开维护模式
            ]
            for q in cases:
                r = _request(app, "POST", url + q)
                assert 400 <= r.status_code < 500, (url + q, r.status_code)
                assert _fingerprint(root) == before, (url + q)
