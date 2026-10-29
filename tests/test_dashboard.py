"""Dashboard API 单元测试（用 FastAPI TestClient）。"""
import json
import os
import tempfile

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")


class TestDashboard:
    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.trace_db = os.path.join(self.tmpdir, "traces.db")
        # 准备一些 trace 数据
        from app.brain.trace import TraceRecorder
        rec = TraceRecorder(self.trace_db)
        rid = rec.begin_run("设置喝水提醒", mode="react")
        rec.record(rid, "plan", {"steps": [{"id": "1", "kind": "tool"}]})
        rec.record(rid, "tool", {"name": "add_reminder",
                                 "args": {"text": "喝水"},
                                 "result": "已设置"})
        rec.record(rid, "reflection", {"verdict": "ok", "comment": ""})
        rec.end_run(rid, final_answer="已为你设好提醒～", status="success")

        # 记忆只在 sidecar 里：面板只读 HTTP，不再读任何本地记忆库文件。
        # 这里把客户端网络换成一个进程内假 sidecar（离线、可断言）。
        import sys as _sys
        from pathlib import Path as _Path
        _tests = str(_Path(__file__).resolve().parent)
        if _tests not in _sys.path:
            _sys.path.insert(0, _tests)
        from fake_sidecar import FakeSidecar
        from app.brain import memory_client as _mc
        from app.brain.memory_client import card_id, category_to_kind

        fake = FakeSidecar()
        for n, (text, importance, hits) in enumerate((
                ("主人喜欢咖啡", 0.8, 1),
                ("主人不喜欢香菜", 0.7, 0))):
            cid = card_id(text, kind=category_to_kind("preference"), key="")
            fake.cards[cid] = {
                "id": cid, "key": "", "kind": category_to_kind("preference"),
                "category": "preference", "text": text, "mood": "",
                "importance": importance, "status": "active",
                "createdAt": 1700000000000 + n, "updatedAt": 1700000000000 + n,
                "hitCount": hits,
            }
        self.fake_sidecar = fake
        self._orig_http = _mc._urllib_http
        _mc._urllib_http = fake.handle

        from app.web.dashboard import create_app
        self.app = create_app(self.trace_db, "http://127.0.0.1:8788")
        self.client = fastapi_testclient.TestClient(self.app)

    def teardown_method(self):
        import shutil
        from app.brain import memory_client as _mc
        _mc._urllib_http = self._orig_http
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_index_returns_html(self):
        r = self.client.get("/")
        assert r.status_code == 200
        # 新控制台页面（中文）应包含核心结构标识
        assert "桌宠智能体控制台" in r.text
        assert "会话记录" in r.text
        assert "长期记忆" in r.text
        assert "工具统计" in r.text

    def test_list_runs(self):
        r = self.client.get("/api/runs?limit=10")
        assert r.status_code == 200
        data = r.json()
        assert len(data) >= 1
        assert data[0]["goal"] == "设置喝水提醒"
        assert data[0]["status"] == "success"

    def test_run_detail(self):
        runs = self.client.get("/api/runs?limit=1").json()
        rid = runs[0]["id"]
        r = self.client.get(f"/api/runs/{rid}")
        assert r.status_code == 200
        data = r.json()
        assert data["goal"] == "设置喝水提醒"
        kinds = [e["kind"] for e in data["events"]]
        assert "plan" in kinds
        assert "tool" in kinds
        assert "reflection" in kinds

    def test_run_detail_404(self):
        r = self.client.get("/api/runs/nonexistent")
        assert r.status_code == 404

    def test_stats(self):
        r = self.client.get("/api/stats")
        assert r.status_code == 200
        data = r.json()
        assert data["total_runs"] >= 1
        assert data["success_runs"] >= 1
        assert "add_reminder" in data["tool_distribution"]
        # 增强字段
        for key in ("failed_runs", "running_runs", "cancelled_runs",
                    "avg_duration", "total_tool_calls", "last_active_at"):
            assert key in data, f"stats 缺少字段 {key}"
        assert data["total_tool_calls"] >= 1
        assert data["last_active_at"] is not None

    def test_memory_endpoint(self):
        r = self.client.get("/api/memory")
        assert r.status_code == 200
        data = r.json()
        assert data["source"] == "sidecar"
        assert data["count"] == 2
        items = data["items"]
        assert {i["content"] for i in items} == {"主人喜欢咖啡", "主人不喜欢香菜"}
        # 更新时间倒序（sidecar /list 的口径）
        for i in range(1, len(items)):
            assert items[i-1]["updated_at"] >= items[i]["updated_at"]
