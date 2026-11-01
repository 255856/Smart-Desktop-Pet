"""Stage 5 · 项目总览面板的离线自检（PLAN.md 5.4）。

只用 httpx 的 ASGITransport 直接打 ASGI 应用：
**不发网络、不起浏览器、不调真实 API、不起子进程**。
所有数据都落在 pytest 的临时目录里，仓库里的 data/ 一个字节都不动。

覆盖：
  A1  /api/memory 只注册一次，且**只读 sidecar**（HTTP），不再读本地记忆库
  A2  每个 Tab 的端点都 200 且字段齐全
  A3  API key 不出现在任何响应体里
  A4  危险操作只走 POST + confirm 标志（重建记忆索引由 sidecar 自己干，卡片不动）
  A5  金额标 estimated（真实价目表 Stage 4.1 才落地，这里留了挂载点）
  A6  工具 Tab 报 68 = 61 内置 + 7 MCP（4 ActivityWatch + 3 filesystem）
  A7  测试全程不碰仓库里的 data/
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))   # tests/fake_sidecar.py

# --------------------------------------------------------------------------
# 测试数据（都落在临时目录里）
# --------------------------------------------------------------------------
# 记忆 sidecar 里的卡片（面板只读它，不读任何本地记忆库文件）
MEMORY_CARDS = [
    # (content, category, importance, hit_count)
    ("主人生日在2月4号", "event", 0.8, 3),
    ("主人的开发者代号是cxxy，是小汐的创造者", "fact", 0.9, 0),
    ("主人非常喜欢韭菜猪肉馅的饺子", "preference", 0.8, 0),
]
#: 面板必须能看见的那几条（内容级证明）
MEMORY_CONTENTS = [c[0] for c in MEMORY_CARDS]

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
    "记忆": ["/api/memory"],
    "决策": ["/api/companion"],
    "工具": ["/api/tools", "/api/reconciliation"],
    "配置": ["/api/config"],
    "状态": ["/api/state"],
    "语音": ["/api/voice"],
    "健康与维护": ["/api/health", "/api/maintenance/status"],
}

# 端点 -> 必须存在的字段路径
EXPECTED_KEYS = {
    "/api/stats": (("total_runs",), ("success_rate",), ("avg_steps",),
                   ("avg_duration",), ("total_tool_calls",), ("tool_distribution",)),
    "/api/memory": (("ok",), ("count",), ("source",), ("items",),
                    ("by_kind",), ("by_importance",), ("access_hits",),
                    ("stats",), ("sidecar",), ("url",)),
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
    "/api/voice": (("ok",), ("tts", "url"), ("tts", "up"), ("tts", "detail"),
                   ("rvc", "python"), ("rvc", "python_ok"), ("rvc", "sing_script"),
                   ("rvc", "sing_script_ok"), ("rvc", "weight"), ("rvc", "weight_ok"),
                   ("rvc", "fetch_script"), ("rvc", "fetch_script_ok"),
                   ("models",), ("model_count",), ("emotions",), ("emotion_count",),
                   ("emotion_dir",), ("out_dir",), ("search_backend",)),
    "/api/maintenance/status": (("maintenance_required",), ("operations",),
                                ("last_results",)),
}

# 危险操作端点（PLAN.md 5.3）
DANGEROUS_OPS = {
    "/api/maintenance/backup": "backup",
    "/api/maintenance/reset-ledger": "reset-ledger",
    "/api/maintenance/reconciliation": "rerun-reconciliation",
}


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _memory_client_cards(fake):
    """假 sidecar 里预置几张记忆卡（面板应当原样列出来）。"""
    from app.brain.memory_client import card_id, category_to_kind

    for n, (text, category, importance, hits) in enumerate(MEMORY_CARDS):
        cid = card_id(text, kind=category_to_kind(category), key="")
        fake.cards[cid] = {
            "id": cid, "key": "", "kind": category_to_kind(category),
            "category": category, "text": text, "mood": "",
            "importance": importance, "status": "active",
            "createdAt": 1700000000000 + n, "updatedAt": 1700000000000 + n,
            "hitCount": hits,
        }
    return fake


def _install_fake_sidecar():
    """把 MemoryClient 的网络换成进程内的假 sidecar。

    面板是独立进程、记忆只走 HTTP —— 测试里既不能连真 sidecar（会写 data/
    或用真模型），也不能让它去连 8788（机器上可能真有一个在跑）。
    """
    from app.brain import memory_client as mc
    from fake_sidecar import FakeSidecar

    fake = _memory_client_cards(FakeSidecar())
    mp = pytest.MonkeyPatch()
    mp.setattr(mc, "_urllib_http", fake.handle)
    return fake, mp


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

    (root / "token-usage.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in LEDGER_ROWS) + "\n",
        encoding="utf-8")
    (root / "pet_state.json").write_text(
        json.dumps({"strength_food": 62.5, "feeling": 71.0, "level": 6,
                    "exp": 120.0, "likability": 33.5, "mode": "normal"},
                   ensure_ascii=False),
        encoding="utf-8")

    from app.web.dashboard import create_app
    app = create_app(root / "traces.db", "http://127.0.0.1:8788")
    meta = {"root": root, "memory_count": len(MEMORY_CARDS)}
    return app, meta


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


#: 桌宠**运行时自己会追加写**的文件（app 正在跑就会变）。
#: 这条守卫要抓的是「**测试/面板**往仓库 data/ 里写东西」，不是「主程序在写自己的日志」——
#: 否则只要桌宠开着跑，日志文件每长一字节这条测试就误伤。
_VOLATILE_DATA_FILES = frozenset({
    "companion_log.txt",
    "token-usage.jsonl",
    # 桌宠运行时自己写的（app 开着就会变）
    "pet_state.json",
    "pet_state.bak.json",
    "chat_history.db",
    "settings.json",
    "reminders.json",
    "langchain_checkpoints.db",
    "langchain_checkpoints.db-shm",
    "langchain_checkpoints.db-wal",
    # 记忆 sidecar 的真值卡（sidecar 在跑就会写）
    "cards.json",
})

# 同一目录下的前缀也算运行时易变（sidecar 的向量索引）。
_VOLATILE_DATA_PREFIXES = ("memory_sidecar/chroma/",)


def _is_volatile(path: Path) -> bool:
    """这个文件是不是运行时自己会变的（日志 / 账本 / sidecar 产物）。"""
    if path.suffix == ".log":
        return True
    if path.name in _VOLATILE_DATA_FILES:
        return True
    rel = path.as_posix()
    return any(rel.endswith(p) or ("/" + p) in rel
               for p in _VOLATILE_DATA_PREFIXES)


def _snapshot(path: Path):
    """目录快照：相对路径 -> (mtime_ns, size)。**跳过运行时易变文件**。"""
    out = {}
    if not path.exists():
        return out
    for p in sorted(path.rglob("*")):
        if p.is_file() and not _is_volatile(p):
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
def _fake_sidecar():
    """整个模块用**进程内的假 sidecar** 顶替真记忆服务（离线、可断言）。

    真 sidecar 会加载两个 transformer 模型、还会把卡片写进 data/；
    机器上也可能正好有一个在 8788 上跑着 —— 两种情况都会让面板测试不稳。
    """
    fake, mp = _install_fake_sidecar()
    yield fake
    mp.undo()


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
# A1 · /api/memory 只注册一次，且**只读 sidecar**
# --------------------------------------------------------------------------
class TestMemoryEndpoint:
    def test_memory_route_registered_exactly_once(self, panel):
        paths = [getattr(r, "path", None) for r in panel["app"].routes]
        assert paths.count("/api/memory") == 1, (
            "/api/memory 注册了 %d 次（FastAPI 里先注册的那个赢）"
            % paths.count("/api/memory"))

    def test_memory_answers_from_the_sidecar(self, panel):
        data = _get_json(panel["app"], "/api/memory")
        assert data["source"] == "sidecar"
        assert data["ok"] is True
        assert data["count"] == panel["memory_count"]
        assert data["url"].startswith("http://")

    def test_memory_items_come_from_the_sidecar(self, panel):
        """内容级证明：卡片是 sidecar 给的那几张。"""
        data = _get_json(panel["app"], "/api/memory")
        contents = [i["content"] for i in data["items"]]
        for text in MEMORY_CONTENTS:
            assert text in contents, text
        # 每条的字段是面板要的形状
        first = data["items"][0]
        for key in ("id", "content", "category", "kind", "importance", "created_at"):
            assert key in first, key

    def test_memory_reports_hits_and_distribution(self, panel):
        data = _get_json(panel["app"], "/api/memory")
        # hitCount > 0 的只有第一条
        assert data["access_hits"] == 1
        # kind / importance 分布
        assert sum(data["by_kind"].values()) == panel["memory_count"]
        assert sum(data["by_importance"].values()) == panel["memory_count"]
        assert isinstance(data["stats"], dict), "/stats 必须原样带回"

    def test_memory_reports_sidecar_health(self, panel):
        """/api/memory 顺带把 sidecar 的 /health 报回来（活着没 / 模型就绪没）。"""
        data = _get_json(panel["app"], "/api/memory")
        sc = data["sidecar"]
        assert sc["reachable"] is True
        assert sc["cards"] == panel["memory_count"]
        assert sc["models"]["embed"] == "ready"

    def test_memory_is_empty_and_honest_when_the_sidecar_is_down(self, panel):
        """sidecar 不在 → 200 + ok=False + 空列表（面板显示「没连上」，不是 500）。"""
        from app.brain import memory_client as mc

        def _refused(method, url, payload, timeout):
            raise ConnectionRefusedError("nothing on 8788")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mc, "_urllib_http", _refused)
            data = _get_json(panel["app"], "/api/memory")
        assert data["ok"] is False
        assert data["items"] == []
        assert data["count"] == 0
        assert "sidecar" in data["note"]


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
                "/api/companion",
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

    def test_maintenance_status_lists_post_only_operations(self, panel):
        data = _get_json(panel["app"], "/api/maintenance/status")
        ops = {o["endpoint"]: o for o in data["operations"]}
        for url, token in DANGEROUS_OPS.items():
            assert url in ops
            assert ops[url]["method"] == "POST"
            assert ops[url]["confirm"] == token
        assert data["maintenance_required"] is True


# --------------------------------------------------------------------------
# A5 · 金额：**人民币**计价 + **诚实**的估算标记
#
# 本项目实际调的是 MiniMax 国产平台（https://api.minimaxi.com/v1），
# 账单按**元**结算 —— 面板以前写死 "$"（美元），是错的。
# 估算标记也必须诚实：价格已知的模型（MiniMax-M3）不能标「估算」，
# 价目表里没有的模型必须继续标「估算」。
# --------------------------------------------------------------------------
class TestEstimatedAmounts:
    def test_summary_cost_is_cny(self, panel):
        s = _get_json(panel["app"], "/api/tokens/summary")
        cost = s["cost"]
        assert isinstance(cost["estimated"], bool)
        assert cost["amount"] >= 0
        assert cost["currency"] == "CNY", "本项目按人民币结算，不能报美元"
        assert cost["symbol"] == "¥"
        # 价目表来源必须写出来（不能让人以为是国际站的美元账单）
        assert cost.get("price_table"), "缺价目表来源"

    def test_known_model_is_not_estimated(self, panel):
        """台账里的模型是 MiniMax-M3 —— 价目表里查得到 -> 不是估算。"""
        s = _get_json(panel["app"], "/api/tokens/summary")
        by_model = s["by_model"] or {}
        assert "MiniMax-M3" in by_model, by_model
        cost = by_model["MiniMax-M3"]["cost"]
        assert cost["estimated"] is False, "价格已知还标估算 = 假诚实"
        assert cost["currency"] == "CNY"

    def test_unknown_model_stays_estimated(self, panel):
        """价目表里没有的模型必须继续标「估算」（不许假装是准数）。"""
        from app.web.dashboard import _estimate_cost
        known = _estimate_cost({"prompt_tokens": 1000}, model="MiniMax-M3")
        assert known["estimated"] is False
        unknown = _estimate_cost({"prompt_tokens": 1000}, model="No-Such-Model-XYZ")
        assert unknown["estimated"] is True
        assert unknown["currency"] == "CNY"
        # 压根没传模型 -> 也是估算（拿兜底价猜的）
        assert _estimate_cost({"prompt_tokens": 1000})["estimated"] is True

    def test_overview_cost_card_is_cny(self, panel):
        cards = _get_json(panel["app"], "/api/overview")["cards"]
        cost_card = [c for c in cards if c["label"] == "累计花费"]
        assert cost_card
        card = cost_card[0]
        assert card["currency"] == "CNY"
        assert card["symbol"] == "¥"
        # 台账里只有已知模型 -> 汇总不是估算
        assert card["estimated"] is False

    def test_turns_and_series_costs_are_not_estimated_for_known_model(self, panel):
        """逐请求 / 时间桶：MiniMax-M3 那一行价格已知 -> 不标估算。"""
        for url in ("/api/tokens/series", "/api/tokens/turns"):
            data = _get_json(panel["app"], url)
            for item in data["items"]:
                assert item["cost"]["currency"] == "CNY"
                assert item["cost"]["estimated"] is False

    def test_cost_panel_uses_backend_currency(self, panel):
        """前端不许再写死 "$" —— 币种跟后端走。"""
        html = _request(panel["app"], "GET", "/").text
        assert "costSymbol" in html
        assert "costPriceNote" in html
        assert '"$"+Number(cost.amount' not in html


# --------------------------------------------------------------------------
# 审查 · F2 Live2D「当前模型」只有一个真值
#
# 桌宠进程那边有两个来源：渲染器实际加载的 model_dir（权威）跟
# config.yaml 里写的 pet.live2d.model_dir（切换时写、启才生效）。
# 面板以前只标 cfg 那一个 -> 换了模型没重启时会把「没生效的」标成当前。
# --------------------------------------------------------------------------
class TestLive2dCurrentModel:
    def test_renderer_model_is_the_single_source_of_truth(self):
        from app.web.dashboard import _live2d_model_state
        ap = _live2d_model_state({
            "available": True,
            "model": "assets/live2d/real",
            "models": [
                {"path": "assets/live2d/real", "current": False},
                {"path": "assets/live2d/other", "current": True},
            ],
        })
        assert ap["currentModel"] == "assets/live2d/real"
        assert ap["configuredModel"] == "assets/live2d/other"
        assert ap["modelMismatch"] is True

    def test_no_mismatch_when_they_agree(self):
        from app.web.dashboard import _live2d_model_state
        ap = _live2d_model_state({
            "model": "assets/live2d/real",
            "models": [{"path": "assets/live2d/real", "current": True}],
        })
        assert ap["modelMismatch"] is False
        assert ap["currentModel"] == ap["configuredModel"]

    def test_settings_endpoint_exposes_the_truth(self, panel):
        """桌宠没连上也不能 500；连上时要带出唯一真值。"""
        data = _get_json(panel["app"], "/api/settings")
        ap = data["appearance"]
        for key in ("currentModel", "configuredModel", "modelMismatch"):
            assert key in ap, key

    def test_frontend_marks_the_renderer_model(self, panel):
        html = _request(panel["app"], "GET", "/").text
        assert "currentModel" in html
        assert "configuredModel" in html
        assert "modelMismatch" in html


# --------------------------------------------------------------------------
# 审查 · F3 记忆图必须说清「看的是哪一种图」
# --------------------------------------------------------------------------
class TestMemoryGraphLabel:
    def test_page_says_which_graph_it_shows(self, panel):
        html = _request(panel["app"], "GET", "/").text
        assert "_graphReason" in html
        assert "语义图" in html
        assert "分类图" in html
        assert "语义图不可用" in html

    def test_graph_endpoint_explains_why_it_is_empty(self, panel):
        """语义图拿不到时，端点要说清楚为什么（不能默默换个图）。"""
        data = _get_json(panel["app"], "/api/memory/graph")
        assert data["ok"] is False
        assert data["reason"], "必须说明为什么没有语义图"

    def test_memory_payload_carries_the_reason(self, panel):
        data = _get_json(panel["app"], "/api/memory")
        graph = data["graph"]
        assert graph["ok"] is False
        assert graph["reason"]


# --------------------------------------------------------------------------
# 审查 · F4 sidecar 停掉时，面板**每个**端点都得 200 + 说清楚
# --------------------------------------------------------------------------
class TestSidecarDownDegrades:
    ENDPOINTS = [
        ("GET", "/api/memory"),
        ("GET", "/api/memory/graph"),
        ("GET", "/api/memory/sleep"),
        ("POST", "/api/memory/sleep/schedule"),
        ("POST", "/api/memory/sleep/run"),
        ("GET", "/api/health"),
        ("GET", "/api/overview"),
    ]

    @staticmethod
    def _refused(method, url, payload, timeout):
        raise ConnectionRefusedError("nothing on 8788")

    def test_every_endpoint_answers_200(self, panel):
        from app.brain import memory_client as mc
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mc, "_urllib_http", self._refused)
            for method, url in self.ENDPOINTS:
                r = _request(panel["app"], method, url)
                assert r.status_code == 200, "%s %s -> %s" % (
                    method, url, r.status_code)

    def test_memory_tab_says_the_service_is_down(self, panel):
        from app.brain import memory_client as mc
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mc, "_urllib_http", self._refused)
            data = _get_json(panel["app"], "/api/memory")
        assert data["ok"] is False
        assert data["count"] == 0
        assert data["items"] == []
        assert "sidecar" in data["note"]
        assert data["sidecar"]["reachable"] is False
        assert data["sidecar"]["error"]

    def test_sleep_plan_says_why(self, panel):
        from app.brain import memory_client as mc
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mc, "_urllib_http", self._refused)
            data = _get_json(panel["app"], "/api/memory/sleep")
        assert data["ok"] is True
        assert data["plan"]["ok"] is False
        assert data["plan"]["reason"]

    def test_health_and_overview_still_work(self, panel):
        from app.brain import memory_client as mc
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mc, "_urllib_http", self._refused)
            health = _get_json(panel["app"], "/api/health")
            ov = _get_json(panel["app"], "/api/overview")
        assert health["memory_sidecar"]["reachable"] is False
        assert ov["memory"]["reachable"] is False
        assert ov["memory"]["count"] == 0

    def test_rebuild_index_answers_200_when_sidecar_is_down(self, panel):
        from app.brain import memory_client as mc
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(mc, "_urllib_http", self._refused)
            r = _request(panel["app"], "POST",
                         "/api/maintenance/rebuild-vector-index"
                         "?maintenance=1&confirm=rebuild-vector-index")
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["error"]


# --------------------------------------------------------------------------
# 审查 · 工具 Tab 要能看见执行期白名单（放行 / 拦截）
# --------------------------------------------------------------------------
class TestToolAllowlist:
    def test_allowlist_status_is_reported(self, panel):
        t = _get_json(panel["app"], "/api/tools")
        al = t["allowlist"]
        assert al["available"] is True, "仓库的 companion_config.yaml 里有该段"
        assert al["exact_count"] > 0
        assert al["prefixes"]
        names = {x["name"] for x in t["tools"]}
        assert set(al["allowed"]) | set(al["blocked"]) == names
        assert not (set(al["allowed"]) & set(al["blocked"]))
        assert "open_app" in al["allowed"]
        # 注册表里的 68 个工具都登记过（exact 或前缀）-> 不该有拦截项
        assert al["blocked"] == []
        assert al["dead_entries"] == []

    def test_every_tool_row_says_allowlisted(self, panel):
        t = _get_json(panel["app"], "/api/tools")
        allowed = set(t["allowlist"]["allowed"])
        for tool in t["tools"]:
            assert tool["allowlisted"] is (tool["name"] in allowed), tool["name"]

    def test_tools_page_shows_the_allowlist(self, panel):
        html = _request(panel["app"], "GET", "/").text
        assert "toolAllowlist" in html
        assert "执行期白名单" in html


# --------------------------------------------------------------------------
# 审查 · A5 面板不许再描述**已经删掉**的架构
# --------------------------------------------------------------------------
class TestNoStaleArchitectureText:
    STALE = ("live 库", "access_count", "merge-preview", "memory.db",
             "data/vectors", "sqlite")

    def test_page_has_no_stale_text(self, panel):
        html = _request(panel["app"], "GET", "/").text
        for word in self.STALE:
            assert word not in html, word

    def test_endpoints_have_no_stale_text(self, panel):
        for url in ("/api/memory", "/api/health", "/api/overview", "/api/tools"):
            body = json.dumps(_get_json(panel["app"], url), ensure_ascii=False)
            for word in ("live 库", "access_count", "memory.db", "data/vectors"):
                assert word not in body, "%s: %s" % (url, word)


# --------------------------------------------------------------------------
# A6 · 工具 Tab：68 = 61 内置 + 7 MCP
# --------------------------------------------------------------------------
class TestToolsTab:
    def test_tool_inventory_counts(self, panel):
        t = _get_json(panel["app"], "/api/tools")
        assert t["count"] == 76, t["count"]
        assert t["builtin_count"] == 66, t["builtin_count"]
        assert t["mcp_count"] == 10, t["mcp_count"]
        assert t["count"] == t["builtin_count"] + t["mcp_count"]
        assert len(t["tools"]) == 76

    def test_mcp_split_is_activitywatch_filesystem_and_voice(self, panel):
        t = _get_json(panel["app"], "/api/tools")
        aw = [x for x in t["tools"] if x["name"].startswith("mcp_aw_")]
        fs = [x for x in t["tools"] if x["name"].startswith("mcp_fs_")]
        voice = [x for x in t["tools"] if x["name"].startswith("mcp_voice_")]
        assert len(aw) == 4, [x["name"] for x in aw]
        assert len(fs) == 3, [x["name"] for x in fs]
        assert len(voice) == 3, [x["name"] for x in voice]
        assert len(aw) + len(fs) + len(voice) == t["mcp_count"]

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
        assert rec["live_count"] == 76
        assert "sources" in rec and rec["sources"]


# --------------------------------------------------------------------------
# 语音 Tab：本机音色 / 情绪参考 / 服务状态（不可达时要降级，不能 500）
# --------------------------------------------------------------------------
class TestVoiceEndpoint:
    def test_voice_endpoint_lists_local_assets(self, panel):
        data = _get_json(panel["app"], "/api/voice")
        assert data["ok"] is True, data.get("error")
        tts = data["tts"]
        assert tts["url"].startswith("http")
        assert isinstance(tts["up"], bool)
        # 音色模型：每个都有名字和大小
        for m in data["models"]:
            assert m["name"].endswith(".pth"), m
            assert m["size"] >= 0
            assert m["size_kb"]
        # 情绪参考：名字 + 中文标签
        for e in data["emotions"]:
            assert e["name"]
            assert e["label"]
        # RVC 解释器 / 唱歌脚本 / 翻唱脚本 在不在
        rvc = data["rvc"]
        assert isinstance(rvc["python_ok"], bool)
        assert isinstance(rvc["sing_script_ok"], bool)
        assert isinstance(rvc["fetch_script_ok"], bool)
        assert isinstance(rvc["weight_ok"], bool)

    def test_voice_endpoint_reuses_the_tool_status(self, panel):
        """控制台的数据来自**工具那一份**盘点（不是另写一套）。

        主人原话：「大肥鱼有自己的音色文件啊，你好像没有同步到控制台里面」。
        """
        from app.mcp import voice_server
        data = _get_json(panel["app"], "/api/voice")
        tool_text = voice_server.handle_status()["content"][0]["text"]
        assert "本机语音服务状态" in tool_text
        for m in data["models"]:
            assert m["name"] in tool_text, m["name"]
        for e in data["emotions"]:
            assert e["label"] in tool_text, e["label"]

    def test_voice_endpoint_degrades_when_unreachable(self, panel, monkeypatch):
        """语音服务不可达 → 200 + ok=false + 说明（**绝不 500**）。"""
        from app.mcp import voice_server

        def _boom(*a, **k):
            raise RuntimeError("语音服务没起来")

        monkeypatch.setattr(voice_server, "voice_status_payload", _boom)
        r = _request(panel["app"], "GET", "/api/voice")
        assert r.status_code == 200, r.status_code
        data = r.json()
        assert data["ok"] is False
        assert "语音服务没响应" in data["error"]
        assert data["models"] == [] and data["emotions"] == []


# --------------------------------------------------------------------------
# A7 · data/ 一个字节都没动
# --------------------------------------------------------------------------
class TestDataDirUntouched:
    def test_repo_data_dir_unchanged_during_tests(self):
        assert _snapshot(ROOT / "data") == _DATA_SNAPSHOT["before"]

    def test_panel_reads_only_from_temp_data_dir(self, panel):
        """面板的数据源都在临时目录里，不依赖主进程的内存态。"""
        mem = _get_json(panel["app"], "/api/memory")
        # 记忆只来自 sidecar（HTTP），面板不再读任何本地记忆库文件
        assert mem["source"] == "sidecar"
        assert str(panel["root"]) not in json.dumps(mem["items"], ensure_ascii=False)
        state = _get_json(panel["app"], "/api/state")
        assert str(panel["root"]) in state["path"]
        summary = _get_json(panel["app"], "/api/tokens/summary")
        assert str(panel["root"]) in summary["source"]


# --------------------------------------------------------------------------
# 前端：9 个 Tab
# --------------------------------------------------------------------------
class TestFrontendTabs:
    def test_page_has_ten_tabs(self, panel):
        html = _request(panel["app"], "GET", "/").text
        tabs = re.findall(r'data-tab="([^"]+)"', html)
        assert tabs == ["runs", "cost", "memory", "decision", "tools", "voice",
                        "settings", "config", "state", "health"], tabs

    def test_page_has_tab_labels(self, panel):
        html = _request(panel["app"], "GET", "/").text
        for label in ("运行", "成本", "记忆", "决策", "工具", "语音", "设置",
                      "配置", "状态", "健康与维护"):
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


class TestMemoryPartitions:
    """记忆分区（身份 / 价值 / 程序性）—— 常量块，要在控制台的网络视图里看得见。"""

    def test_partitions_endpoint_returns_three_blocks(self, panel):
        d = _get_json(panel["app"], "/api/memory/partitions")
        assert d["ok"] is True, d
        keys = [b["key"] for b in d["blocks"]]
        assert keys == ["identity", "values", "procedures"], keys
        for b in d["blocks"]:
            assert b["header"], b
            assert b["path"], b
            assert b["count"] == len(b["items"]), b

    def test_partitions_show_the_real_content(self, panel):
        d = _get_json(panel["app"], "/api/memory/partitions")
        by_key = {b["key"]: b for b in d["blocks"]}
        # 身份：文件不存在时用代码内兜底值，也要看得见
        assert by_key["identity"]["count"] >= 1, by_key["identity"]
        assert by_key["identity"]["items"][0]["text"], by_key["identity"]
        # 价值：分层（底线 → 偏好 → 风格）
        levels = [it.get("level") for it in by_key["values"]["items"]]
        assert levels and all(levels), by_key["values"]
        # 程序性：步骤 + 成功率
        proc = by_key["procedures"]["items"]
        assert proc and proc[0]["steps"], proc

    def test_partitions_are_shown_in_the_network_pane(self, panel):
        """前端：网络视图里得有分区面板（不然主人看不到）。"""
        r = _request(panel["app"], "GET", "/")
        assert r.status_code == 200
        html = r.text
        assert 'id="memPartitions"' in html, "网络视图里没有分区面板"
        assert 'data-pane="net"' in html
        assert "/api/memory/partitions" in html, "前端没去取分区数据"

