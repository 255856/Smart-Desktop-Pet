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
        self.memory_file = os.path.join(self.tmpdir, "memory.json")
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
        # 准备 memory
        self.memory_data = [
            {"id": "m1", "content": "主人喜欢咖啡", "category": "preference",
             "importance": 0.8, "created_at": 1700000000.0,
             "last_access_ts": 1700000000.0, "access_count": 1},
            {"id": "m2", "content": "主人不喜欢香菜", "category": "preference",
             "importance": 0.7, "created_at": 1700000100.0,
             "last_access_ts": 1700000100.0, "access_count": 0},
        ]
        Path = __import__("pathlib").Path
        Path(self.memory_file).write_text(
            json.dumps(self.memory_data, ensure_ascii=False),
            encoding="utf-8",
        )

        from app.web.dashboard import create_app
        self.app = create_app(self.trace_db, self.memory_file)
        self.client = fastapi_testclient.TestClient(self.app)

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_index_returns_html(self):
        r = self.client.get("/")
        assert r.status_code == 200
        assert "Dashboard" in r.text

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

    def test_memory_endpoint(self):
        r = self.client.get("/api/memory")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 2
        # 重要性高的排前
        assert data["items"][0]["importance"] >= data["items"][1]["importance"]
