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
        # v23-10b: dashboard 读 sqlite memory.db, 改写 sqlite 而不是 json
        self.memory_file = os.path.join(self.tmpdir, "memory.db")
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
        # 准备 memory (sqlite schema, 2 条 fake 用于测重要性排序)
        import sqlite3
        conn = sqlite3.connect(self.memory_file)
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
                "INSERT INTO memories (id, content, category, importance, "
                "created_at, last_access_ts, access_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("m1", "主人喜欢咖啡", "preference", 0.8,
                     1700000000.0, 1700000000.0, 1),
                    ("m2", "主人不喜欢香菜", "preference", 0.7,
                     1700000100.0, 1700000100.0, 0),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        from app.web.dashboard import create_app
        self.app = create_app(self.trace_db, self.memory_file)
        self.client = fastapi_testclient.TestClient(self.app)

    def teardown_method(self):
        import shutil
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
        # v23-10b: 读 sqlite memory.db (2 条 fake 用于测试)
        assert data["count"] == 2
        # 重要性高的排前
        items = data["items"]
        for i in range(1, len(items)):
            assert items[i-1]["importance"] >= items[i]["importance"]
