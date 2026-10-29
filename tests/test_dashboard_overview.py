"""Stage 5 · 项目总览面板的离线自检（PLAN.md 5.4）。

只用 httpx 的 ASGITransport 直接打 ASGI 应用：
**不发网络、不起浏览器、不调真实 API、不起子进程**。
所有数据都落在 pytest 的临时目录里，仓库里的 data/ 一个字节都不动。

覆盖：
  A1  /api/memory 只注册一次，且答的是 live 库 data/memory_<角色名>.db
  A2  每个 Tab 的端点都 200 且字段齐全
  A3  API key 不出现在任何响应体里
  A4  危险操作只走 POST + confirm 标志 + 动手前自动备份，记忆合并只归档不删除
  A5  金额标 estimated（真实价目表 Stage 4.1 才落地，这里留了挂载点）
  A6  工具 Tab 报 68 = 61 内置 + 7 MCP（4 ActivityWatch + 3 filesystem）
  A7  测试全程不碰仓库里的 data/
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# 测试数据（都落在临时目录里）
# --------------------------------------------------------------------------
LIVE_ROWS = [
    # (id, content, category, importance, access_count)
    ("mlive01", "主人生日在2月4号", "event", 0.8, 3),
    ("mlive02", "主人的开发者代号是cxxy，是小汐的创造者", "fact", 0.9, 0),
]
LEGACY_ROWS = [
    ("mleg01", "主人的生日是2005年2月4号", "fact", 0.5, 0),
    ("mleg02", "主人非常喜欢韭菜猪肉馅的饺子", "preference", 0.8, 0),
    ("mleg03", "主人是创造小汐的开发者，名字叫初晓旬依", "person", 1.0, 0),
    ("mleg04", "共享记忆: 主人叫 X", "fact", 0.5, 0),
]
# legacy 里独有、live 里没有的那一条（合并时会被并进 live）
LEGACY_ONLY_CONTENT = "主人非常喜欢韭菜猪肉馅的饺子"
# 语义重复（取值兼容）→ 保留 live 那条
BIRTHDAY_LEGACY = "主人的生日是2005年2月4号"
BIRTHDAY_LIVE = "主人生日在2月4号"
# 同主题但**取值不同** → 两条都保留 + 打 conflict 标记
CREATOR_LEGACY = "主人是创造小汐的开发者，名字叫初晓旬依"
CREATOR_LIVE = "主人的开发者代号是cxxy，是小汐的创造者"
# 没填完的模板 → 丢弃
PLACEHOLDER = "共享记忆: 主人叫 X"

LEDGER_ROWS = [
    {"v": 1, "kind": "request", "session_id": "char:小汐:test", "request_index": 1,
     "backend": "standard", "retry": 0, "ts": 1700000000.0, "model": "MiniMax-M3",
     "prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
     "cache_hit_tokens": 100, "cache_miss_tokens": 300, "cache_write_tokens": 0},
    {"v": 1, "kind": "request", "session_id": "char:小汐:test", "request_index": 2,
     "backend": "standard", "retry": 0, "ts": 1700000060.0, "model": "MiniMax-M3",
     "prompt_tokens": 800, "completion_tokens": 100, "total_tokens": 900,
     "cache_hit_tokens": 200, "cache_miss_tokens": 100, "cache_write_tokens": 0},
]

# Tab -> 它吃的端点（PLAN.md 5.1 / 5.2）
TAB_ENDPOINTS = {
    "运行": ["/api/stats", "/api/runs"],
    "成本": ["/api/tokens/summary", "/api/tokens/series", "/api/tokens/turns"],
    "记忆": ["/api/memory", "/api/memory/merge-preview"],
    "决策": ["/api/companion"],
    "工具": ["/api/tools", "/api/reconciliation"],
    "配置": ["/api/config"],
    "状态": ["/api/state"],
    "健康与维护": ["/api/health", "/api/maintenance/status"],
}

# 端点 -> 必须存在的字段路径
EXPECTED_KEYS = {
    "/api/stats": (("total_runs",), ("success_rate",), ("avg_steps",),
                   ("avg_duration",), ("total_tool_calls",), ("tool_distribution",)),
    "/api/memory": (("count",), ("store",), ("source",), ("items",),
                    ("live", "count"), ("live", "by_kind"),
                    ("live", "by_importance"), ("live", "access_hits"),
                    ("legacy", "count"), ("duplicates",), ("duplicate_count",)),
    "/api/memory/merge-preview": (("live_count",), ("legacy_count",),
                                  ("will_insert",), ("will_keep_live",),
                                  ("will_keep_both",), ("will_discard",), ("pairs",)),
    "/api/companion": (("state",), ("config",), ("decisions",), ("outcomes",)),
    "/api/tokens/summary": (("available",), ("total_tokens",), ("prompt_tokens",),
                            ("completion_tokens",), ("cache_hit_tokens",),
                            ("cache_miss_tokens",), ("cache_hit_rate",),
                            ("avg_tokens_per_turn",), ("requests",), ("sessions",),
                            ("by_model",), ("by_backend",), ("by_session",),
                            ("cost", "amount"), ("cost", "estimated")),
    "/api/tokens/series": (("items",), ("bucket_ms",)),
    "/api/tokens/turns": (("items",), ("session_id",)),
    "/api/tools": (("count",), ("builtin_count",), ("mcp_count",), ("tools",),
                   ("mcp_by_server",), ("reconciliation",)),
    "/api/reconciliation": (("available",), ("live_count",), ("error_count",),
                            ("sources",), ("errors",)),
    "/api/config": (("model",), ("base_url",), ("temperature",), ("max_tokens",),
                    ("api_key", "configured"), ("api_key", "length"),
                    ("api_key", "valid"), ("unlock",)),
    "/api/state": (("strength_food",), ("feeling",), ("level",), ("likability",),
                   ("unlock_progress",)),
    "/api/health": (("rate_limited",), ("crashes",), ("ledger",), ("files",)),
    "/api/overview": (("stats",), ("tokens",), ("memory",), ("tools",), ("cards",)),
    "/api/maintenance/status": (("maintenance_required",), ("operations",),
                                ("last_results",)),
}

# 危险操作端点（PLAN.md 5.3）
DANGEROUS_OPS = {
    "/api/maintenance/backup": "backup",
    "/api/maintenance/reset-ledger": "reset-ledger",
    "/api/maintenance/reconciliation": "rerun-reconciliation",
    "/api/maintenance/merge-memory": "merge-memory",
}


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _write_memory_db(path: Path, rows) -> None:
    """写一个最小可用的 memories 表（列名对齐 app/brain/memory.py）。"""
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'other',
                importance REAL NOT NULL DEFAULT 0.5,
                created_at REAL NOT NULL,
                last_access_ts REAL NOT NULL,
                access_count INTEGER NOT NULL DEFAULT 0
            );
        """)
        conn.executemany(
            "INSERT INTO memories (id, content, category, importance, created_at,"
            " last_access_ts, access_count) VALUES (?,?,?,?,?,?,?)",
            [(r[0], r[1], r[2], r[3], 1700000000.0, 1700000000.0, r[4])
             for r in rows],
        )
        conn.commit()
    finally:
        conn.close()


def _build_panel(root: Path):
    """造一份临时 data/ 并返回 (app, 元信息)。

    面板是文件观测器：它只看 data/ 与 config.yaml。
    这里把 data/ 整份搬到临时目录，仓库里的 data/ 一个字节都不动。
    """
    root.mkdir(parents=True, exist_ok=True)

    from app.brain.trace import TraceRecorder
    rec = TraceRecorder(root / "traces.db")
    rid = rec.begin_run("测试会话：喝水提醒", mode="react")
    rec.record(rid, "plan", {"steps": [{"id": "1", "kind": "tool"}]})
    rec.record(rid, "tool", {"name": "add_reminder", "args": {"text": "喝水"},
                             "result": "错误：参数不对"})
    rec.record(rid, "error", {"message": "HTTP 429 rate_limit tpm/rpm"})
    rec.end_run(rid, final_answer="已设好提醒", status="success")
    rec.close()

    from app.web.dashboard import _character_name, _project_root
    char = _character_name(_project_root())
    live_name = ("memory_%s.db" % char) if char else "memory_live.db"

    _write_memory_db(root / live_name, LIVE_ROWS)
    _write_memory_db(root / "memory.db", LEGACY_ROWS)
    (root / "token-usage.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in LEDGER_ROWS) + "\n",
        encoding="utf-8")
    (root / "pet_state.json").write_text(
        json.dumps({"strength_food": 62.5, "feeling": 71.0, "level": 6,
                    "exp": 120.0, "likability": 33.5, "mode": "normal"},
                   ensure_ascii=False),
        encoding="utf-8")

    from app.web.dashboard import create_app
    app = create_app(root / "traces.db", root / "memory.json")
    meta = {
        "root": root,
        "live_name": live_name,
        "live_count": len(LIVE_ROWS),
        "legacy_count": len(LEGACY_ROWS),
    }
    return app, meta


def _read_memories(path: Path):
    """读 memories 表（只读）：返回 [(content, topic)]。"""
    conn = sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True)
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


def _dig(data, path):
    node = data
    for key in path:
        assert isinstance(node, dict), "不是字典：%s" % (path,)
        assert key in node, "缺少字段 %s（实际有 %s）" % (key, sorted(node))
        node = node[key]
    return node


def _snapshot(path: Path):
    """目录快照：相对路径 -> (mtime_ns, size)。"""
    out = {}
    if not path.exists():
        return out
    for p in sorted(path.rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p.relative_to(path))] = (st.st_mtime_ns, st.st_size)
    return out


def _real_api_key() -> str:
    """从仓库的 config.yaml 里取真实的 llm.api_key（拿它当"不能出现"的字符串）。"""
    cfg = ROOT / "config.yaml"
    if not cfg.is_file():
        return ""
    try:
        import yaml
        raw = yaml.safe_load(cfg.read_text(encoding="utf-8-sig")) or {}
    except Exception:  # noqa: BLE001
        return ""
    return str(((raw.get("llm") or {}).get("api_key")) or "").strip()


# --------------------------------------------------------------------------
# fixture
# --------------------------------------------------------------------------
_DATA_SNAPSHOT: dict = {}


@pytest.fixture(scope="module", autouse=True)
def _data_dir_guard():
    """A7：整个模块跑完，仓库里的 data/ 必须一字节都没变。"""
    _DATA_SNAPSHOT["before"] = _snapshot(ROOT / "data")
    yield
    after = _snapshot(ROOT / "data")
    assert after == _DATA_SNAPSHOT["before"], (
        "仓库里的 data/ 被测试改动了："
        + str({k: v for k, v in after.items()
               if _DATA_SNAPSHOT["before"].get(k) != v}))


@pytest.fixture(scope="module")
def panel(tmp_path_factory):
    """只读的模块级面板（除了单独建实例的合并测试，谁都不许改它）。"""
    root = tmp_path_factory.mktemp("stage5_panel")
    app, meta = _build_panel(root)
    return {"app": app, **meta}


@pytest.fixture()
def fresh_panel(tmp_path):
    """每个用例一份全新的临时 data/（给会写文件的危险操作测试用）。"""
    root = tmp_path / "data"
    app, meta = _build_panel(root)
    return {"app": app, **meta}


# --------------------------------------------------------------------------
# A1 · /api/memory 只注册一次，答的是 live 库
# --------------------------------------------------------------------------
class TestMemoryEndpoint:
    def test_memory_route_registered_exactly_once(self, panel):
        paths = [getattr(r, "path", None) for r in panel["app"].routes]
        assert paths.count("/api/memory") == 1, (
            "/api/memory 注册了 %d 次（FastAPI 里先注册的那个赢）"
            % paths.count("/api/memory"))

    def test_memory_answers_from_live_library(self, panel):
        data = _get_json(panel["app"], "/api/memory")
        # 答的是 live 库：行数 == live 库行数
        assert data["store"] == "live"
        assert data["count"] == panel["live_count"]
        assert data["live"]["count"] == panel["live_count"]
        # legacy 库只是对照
        assert data["legacy"]["count"] == panel["legacy_count"]
        # 路径确实是 live 库而不是 legacy 库
        assert Path(data["live"]["path"]).name == panel["live_name"]
        assert Path(data["live"]["path"]).name != "memory.db"
        assert Path(data["live"]["path"]) != Path(data["legacy"]["path"])
        assert Path(data["live"]["path"]).is_file()

    def test_memory_items_come_from_live_not_legacy(self, panel):
        """内容级证明：live 的内容在，legacy 独有的内容不在。"""
        data = _get_json(panel["app"], "/api/memory")
        contents = [i["content"] for i in data["items"]]
        assert LIVE_ROWS[0][1] in contents
        assert LIVE_ROWS[1][1] in contents
        assert LEGACY_ONLY_CONTENT not in contents
        # legacy 的内容只在 legacy 段里
        legacy_contents = [i["content"] for i in data["legacy"]["items"]]
        assert LEGACY_ONLY_CONTENT in legacy_contents

    def test_memory_reports_access_count_hits_and_distribution(self, panel):
        data = _get_json(panel["app"], "/api/memory")
        # access_count 命中数：只有第一条被召回过
        assert data["live"]["access_hits"] == 1
        # kind / importance 分布
        assert sum(data["live"]["by_kind"].values()) == panel["live_count"]
        assert sum(data["live"]["by_importance"].values()) == panel["live_count"]

    def test_memory_reports_legacy_duplicates(self, panel):
        data = _get_json(panel["app"], "/api/memory")
        groups = set(data["known_groups"])
        # 已知的 3 组（docs/baseline-before-alignment.md 第 5 节）
        assert "生日" in groups
        assert "创造者身份" in groups
        assert data["duplicate_count"] >= 2
        # 共享记忆占位符被标成 invalid / discard
        invalid = [p for p in data["duplicates"] if p["relation"] == "invalid"]
        assert invalid and invalid[0]["action"] == "discard"


# --------------------------------------------------------------------------
# A2 · 每个 Tab 的端点都 200 且字段齐全
# --------------------------------------------------------------------------
class TestAllEndpoints:
    def test_every_endpoint_returns_200(self, panel):
        for tab, urls in TAB_ENDPOINTS.items():
            for url in urls:
                r = _request(panel["app"], "GET", url)
                assert r.status_code == 200, "%s（%s tab）-> %s" % (
                    url, tab, r.status_code)

    def test_every_endpoint_has_expected_keys(self, panel):
        for url, paths in EXPECTED_KEYS.items():
            data = _get_json(panel["app"], url)
            for path in paths:
                _dig(data, path)

    def test_runs_endpoint_returns_list(self, panel):
        runs = _get_json(panel["app"], "/api/runs")
        assert isinstance(runs, list) and runs
        detail = _get_json(panel["app"], "/api/runs/" + runs[0]["id"])
        assert "events" in detail and "goal" in detail

    def test_run_detail_404(self, panel):
        r = _request(panel["app"], "GET", "/api/runs/nope")
        assert r.status_code == 404

    def test_overview_has_eight_cards(self, panel):
        cards = _get_json(panel["app"], "/api/overview")["cards"]
        assert len(cards) == 8
        labels = [c["label"] for c in cards]
        for wanted in ("总会话数", "成功率", "工具调用次数", "平均步骤", "平均耗时",
                       "累计 token", "累计花费", "缓存命中率"):
            assert wanted in labels, "顶部卡片少了：%s" % wanted

    def test_index_page_served(self, panel):
        r = _request(panel["app"], "GET", "/")
        assert r.status_code == 200
        assert "桌宠智能体控制台" in r.text


# --------------------------------------------------------------------------
# A3 · API key 不出现在任何响应体里
# --------------------------------------------------------------------------
class TestNoApiKeyLeak:
    def test_real_key_is_readable(self):
        key = _real_api_key()
        if not key:
            pytest.skip("仓库里没有 config.yaml / llm.api_key（CI 环境），"
                        "没法拿真 key 验证脱敏")
        assert len(key) >= 8

    def test_key_absent_from_every_response(self, panel):
        key = _real_api_key()
        if not key:
            pytest.skip("没有真实 key，跳过")
        urls = ["/", "/api/stats", "/api/runs", "/api/memory",
                "/api/memory/merge-preview", "/api/companion",
                "/api/tokens/summary", "/api/tokens/series", "/api/tokens/turns",
                "/api/tools", "/api/reconciliation", "/api/config", "/api/state",
                "/api/health", "/api/overview", "/api/maintenance/status"]
        for url in urls:
            r = _request(panel["app"], "GET", url)
            assert r.status_code == 200, url
            body = r.content.decode("utf-8", "ignore")
            assert key not in body, "%s 的响应体里出现了 API key" % url
            # 嵌套值里也不能有（响应体是 json 时再查一次）
            if body.startswith("{"):
                assert key not in json.dumps(r.json(), ensure_ascii=False), url

    def test_config_endpoint_only_reports_key_metadata(self, panel):
        key = _real_api_key()
        cfg = _get_json(panel["app"], "/api/config")
        assert set(cfg["api_key"]) <= {"configured", "length", "valid"}
        assert cfg["api_key"]["configured"] is True
        assert cfg["api_key"]["length"] == len(key)
        # 配置里的其它字段可以看，但 key 本体不行
        assert "api_key" not in json.dumps(
            {k: v for k, v in cfg.items() if k != "api_key"},
            ensure_ascii=False).lower()


# --------------------------------------------------------------------------
# A4 · 危险操作：只走 POST + confirm + 自动备份；记忆合并只归档不删除
# --------------------------------------------------------------------------
class TestDangerousOperations:
    def test_get_does_not_trigger_any_action(self, panel):
        before = _snapshot(panel["root"])
        for url in DANGEROUS_OPS:
            r = _request(panel["app"], "GET", url)
            assert r.status_code in (404, 405), (
                "GET %s 应该 405/404，实际 %s" % (url, r.status_code))
        assert _snapshot(panel["root"]) == before

    def test_post_without_confirm_is_rejected(self, panel):
        before = _snapshot(panel["root"])
        for url, token in DANGEROUS_OPS.items():
            r = _request(panel["app"], "POST", url)
            assert 400 <= r.status_code < 500, (url, r.status_code)
            # 维护模式开了但 confirm 不对 → 也要被拒
            r = _request(panel["app"], "POST", url + "?maintenance=1")
            assert 400 <= r.status_code < 500, (url, r.status_code)
            # confirm 写错 → 也要被拒
            r = _request(panel["app"], "POST",
                         url + "?maintenance=1&confirm=wrong-token")
            assert 400 <= r.status_code < 500, (url, r.status_code)
        assert _snapshot(panel["root"]) == before, "被拒的危险操作还是动了文件"

    def test_backup_creates_archive_without_touching_data(self, fresh_panel):
        app, meta = fresh_panel["app"], fresh_panel
        before = _snapshot(meta["root"])
        r = _request(app, "POST",
                     "/api/maintenance/backup?maintenance=1&confirm=backup")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["backup"]["ok"] is True
        archive = Path(data["backup"]["archive"])
        assert archive.is_file() and archive.stat().st_size > 0
        # 备份只是复制：data/ 里的原文件一个都没变
        after = _snapshot(meta["root"])
        for name, info in before.items():
            assert after[name] == info, name

    def test_reset_ledger_backs_up_first(self, fresh_panel):
        app, meta = fresh_panel["app"], fresh_panel
        ledger = meta["root"] / "token-usage.jsonl"
        old = ledger.read_text(encoding="utf-8")
        r = _request(app, "POST",
                     "/api/maintenance/reset-ledger?maintenance=1&confirm=reset-ledger")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["backup"]["ok"] is True
        archived = Path(data["archived_to"])
        # 旧账本被归档了（没删）
        assert archived.is_file()
        assert archived.read_text(encoding="utf-8") == old
        # 账本被清空
        assert ledger.read_text(encoding="utf-8") == ""
        assert data["rows_before"] == len(LEDGER_ROWS)

    def test_merge_preview_is_read_only(self, fresh_panel):
        app, meta = fresh_panel["app"], fresh_panel
        before = _snapshot(meta["root"])
        data = _get_json(app, "/api/memory/merge-preview")
        assert data["live_count"] == meta["live_count"]
        assert data["legacy_count"] == meta["legacy_count"]
        # 四类判定都在预览里（口径跟 CLI 脚本完全一致 —— 共享实现）
        assert len(data["will_insert"]) == 1
        assert data["will_insert"][0]["legacy_content"] == LEGACY_ONLY_CONTENT
        # 生日那条：取值兼容 → 语义重复 → 保留 live 那条
        assert len(data["will_keep_live"]) == 1
        assert data["will_keep_live"][0]["legacy_content"] == BIRTHDAY_LEGACY
        # 创造者那条：**取值不同** → 冲突 → 两条都保留
        # （原来面板按相似度阈值判，把它误判成「重复」，
        #   结果这条记忆永远进不了 live 库）
        assert len(data["will_keep_both"]) == 1
        assert data["will_keep_both"][0]["legacy_content"] == CREATOR_LEGACY
        assert data["will_keep_both"][0]["live_content"] == CREATOR_LIVE
        assert len(data["will_discard"]) == 1
        assert data["will_discard"][0]["legacy_content"] == PLACEHOLDER
        # 四种 relation 一个都不少
        assert {p["relation"] for p in data["pairs"]} == {
            "duplicate", "conflict", "unique", "invalid"}
        assert _snapshot(meta["root"]) == before, "预览不该动任何文件"

    def test_merge_archives_legacy_by_moving_it(self, fresh_panel):
        """legacy 库被**移动**到 backups/ 归档（不是复制，更不是删除）。

        原来这个用例断言「legacy 还在 data/ 里」—— 那正是缺陷本身：
        复制一份留底，data/ 里永远剩**两个**记忆库。
        """
        app, meta = fresh_panel["app"], fresh_panel
        legacy = meta["root"] / "memory.db"
        before_bytes = legacy.read_bytes()
        r = _request(app, "POST",
                     "/api/maintenance/merge-memory"
                     "?maintenance=1&confirm=merge-memory")
        assert r.status_code == 200, r.text
        data = r.json()
        # legacy 库从 data/ 里移走了（归档 = 移动）
        assert not legacy.exists(), "legacy 应该被归档走了"
        # 归档件：字节一模一样、还能打开、行数一条不少
        archived = Path(data["legacy_archived_to"])
        assert archived.is_file()
        assert archived.read_bytes() == before_bytes
        assert archived.parent == meta["root"] / "backups"
        assert archived.name.startswith("memory_legacy-")
        assert archived.name.endswith(".db")
        rows = _read_memories(archived)
        assert len(rows) == meta["legacy_count"]
        # data/ 顶层只剩**一个**记忆库
        dbs = sorted(p.name for p in meta["root"].glob("memory*.db"))
        assert dbs == [meta["live_name"]], dbs
        # live 库多了一条 legacy 独有的 + **一条冲突的**
        mem = _get_json(app, "/api/memory")
        contents = [i["content"] for i in mem["items"]]
        assert LEGACY_ONLY_CONTENT in contents
        assert CREATOR_LEGACY in contents, "冲突的那条必须进 live 库"
        assert mem["count"] == meta["live_count"] + 2
        # legacy 库已经不在 data/ 里了（所以 legacy 计数归零）
        assert mem["legacy"]["count"] == 0
        assert mem["legacy"]["exists"] is False

    def test_maintenance_status_lists_post_only_operations(self, panel):
        data = _get_json(panel["app"], "/api/maintenance/status")
        ops = {o["endpoint"]: o for o in data["operations"]}
        for url, token in DANGEROUS_OPS.items():
            assert url in ops
            assert ops[url]["method"] == "POST"
            assert ops[url]["confirm"] == token
        assert data["maintenance_required"] is True


# --------------------------------------------------------------------------
# A5 · 金额标 estimated
# --------------------------------------------------------------------------
class TestEstimatedAmounts:
    def test_summary_cost_is_estimated(self, panel):
        s = _get_json(panel["app"], "/api/tokens/summary")
        cost = s["cost"]
        assert isinstance(cost["estimated"], bool)
        if cost.get("price_source") == "placeholder":
            # 真实价目表还没落地（Stage 4.1），金额必须标 estimated
            assert cost["estimated"] is True
        assert cost["amount"] >= 0
        assert cost["currency"] == "USD"

    def test_overview_cost_card_is_estimated(self, panel):
        cards = _get_json(panel["app"], "/api/overview")["cards"]
        cost_card = [c for c in cards if c["label"] == "累计花费"]
        assert cost_card and cost_card[0].get("estimated") is True

    def test_turns_and_series_costs_are_estimated(self, panel):
        for url in ("/api/tokens/series", "/api/tokens/turns"):
            data = _get_json(panel["app"], url)
            for item in data["items"]:
                assert item["cost"]["estimated"] is True


# --------------------------------------------------------------------------
# A6 · 工具 Tab：68 = 61 内置 + 7 MCP
# --------------------------------------------------------------------------
class TestToolsTab:
    def test_tool_inventory_counts(self, panel):
        t = _get_json(panel["app"], "/api/tools")
        assert t["count"] == 68, t["count"]
        assert t["builtin_count"] == 61, t["builtin_count"]
        assert t["mcp_count"] == 7, t["mcp_count"]
        assert t["count"] == t["builtin_count"] + t["mcp_count"]
        assert len(t["tools"]) == 68

    def test_mcp_split_is_activitywatch_and_filesystem(self, panel):
        t = _get_json(panel["app"], "/api/tools")
        aw = [x for x in t["tools"] if x["name"].startswith("mcp_aw_")]
        fs = [x for x in t["tools"] if x["name"].startswith("mcp_fs_")]
        assert len(aw) == 4, [x["name"] for x in aw]
        assert len(fs) == 3, [x["name"] for x in fs]
        assert len(aw) + len(fs) == t["mcp_count"]

    def test_every_tool_has_risk_and_unlock_level(self, panel):
        t = _get_json(panel["app"], "/api/tools")
        for tool in t["tools"]:
            assert "risk_level" in tool
            assert "risk_label" in tool
            assert "unlock_level" in tool
            assert tool["source"] in ("builtin", "mcp")

    def test_reconciliation_reads_check_tool_docs(self, panel):
        rec = _get_json(panel["app"], "/api/reconciliation")
        assert rec["available"] is True
        assert rec["live_count"] == 68
        assert "sources" in rec and rec["sources"]


# --------------------------------------------------------------------------
# A7 · data/ 一个字节都没动
# --------------------------------------------------------------------------
class TestDataDirUntouched:
    def test_repo_data_dir_unchanged_during_tests(self):
        assert _snapshot(ROOT / "data") == _DATA_SNAPSHOT["before"]

    def test_panel_reads_only_from_temp_data_dir(self, panel):
        """面板的数据源都在临时目录里，不依赖主进程的内存态。"""
        mem = _get_json(panel["app"], "/api/memory")
        assert str(panel["root"]) in mem["live"]["path"]
        state = _get_json(panel["app"], "/api/state")
        assert str(panel["root"]) in state["path"]
        summary = _get_json(panel["app"], "/api/tokens/summary")
        assert str(panel["root"]) in summary["source"]


# --------------------------------------------------------------------------
# 前端：8 个 Tab
# --------------------------------------------------------------------------
class TestFrontendTabs:
    def test_page_has_eight_tabs(self, panel):
        html = _request(panel["app"], "GET", "/").text
        tabs = re.findall(r'data-tab="([^"]+)"', html)
        assert tabs == ["runs", "cost", "memory", "decision", "tools",
                        "config", "state", "health"], tabs

    def test_page_has_tab_labels(self, panel):
        html = _request(panel["app"], "GET", "/").text
        for label in ("运行", "成本", "记忆", "决策", "工具", "配置", "状态",
                      "健康与维护"):
            assert label in html, label

    def test_page_has_no_external_dependencies(self, panel):
        html = _request(panel["app"], "GET", "/").text
        assert "<script src=" not in html
        assert "cdn" not in html.lower()
        assert "https://" not in html
        assert "http://" not in html

    def test_page_has_top_card_slots(self, panel):
        html = _request(panel["app"], "GET", "/").text
        assert 'id="stats"' in html
        for label in ("累计 token", "累计花费", "缓存命中率"):
            assert label in html
