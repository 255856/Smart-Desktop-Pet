"""Trace recorder 单元测试。"""
import os
import tempfile

import pytest

from app.brain.trace import TraceRecorder


class TestTraceRecorder:
    def setup_method(self):
        self.tmp = tempfile.NamedTemporaryFile(
            suffix=".db", delete=False)
        self.tmp.close()
        self.rec = TraceRecorder(self.tmp.name)

    def teardown_method(self):
        os.unlink(self.tmp.name)

    def test_begin_and_end_run(self):
        rid = self.rec.begin_run("测试目标", mode="react")
        self.rec.end_run(rid, final_answer="回答", status="success")
        run = self.rec.get_run(rid)
        assert run is not None
        assert run["goal"] == "测试目标"
        assert run["final_answer"] == "回答"
        assert run["status"] == "success"

    def test_record_events(self):
        rid = self.rec.begin_run("g", mode="react")
        self.rec.record(rid, "plan", {"steps": [{"id": "1"}]})
        self.rec.record(rid, "tool", {"name": "add_reminder",
                                       "args": {"text": "x"}})
        self.rec.record(rid, "reflection",
                        {"verdict": "ok", "comment": ""})
        self.rec.end_run(rid, final_answer="done")
        run = self.rec.get_run(rid)
        assert len(run["events"]) == 3
        assert run["events"][0]["kind"] == "plan"
        assert run["events"][1]["kind"] == "tool"
        assert run["tool_calls"] == 1

    def test_stats(self):
        for i in range(3):
            rid = self.rec.begin_run(f"g{i}", mode="react")
            self.rec.record(rid, "tool", {"name": "add_reminder"})
            self.rec.record(rid, "tool", {"name": "list_reminders"})
            self.rec.end_run(rid, final_answer="ok", status="success")
        # 加一个失败
        rid = self.rec.begin_run("fail", mode="react")
        self.rec.end_run(rid, "err", status="failed")
        s = self.rec.stats()
        assert s["total_runs"] == 4
        assert s["success_runs"] == 3
        assert s["success_rate"] == 0.75
        assert "add_reminder" in s["tool_distribution"]

    def test_trace_context_manager(self):
        with self.rec.trace("g", mode="react") as t:
            t.event("plan", {"steps": []})
            t.event("tool", {"name": "x"})
            t.set_final("done")
        # context manager 应该自动 end_run
        runs = self.rec.list_runs(limit=1)
        assert len(runs) == 1
        assert runs[0]["status"] == "success"

    def test_trace_context_manager_on_exception(self):
        try:
            with self.rec.trace("g") as t:
                t.event("plan", {})
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        runs = self.rec.list_runs(limit=1)
        assert runs[0]["status"] == "failed"

    def test_list_runs_ordering(self):
        for i in range(5):
            rid = self.rec.begin_run(f"goal_{i}", mode="react")
            self.rec.end_run(rid, f"answer_{i}", "success")
        runs = self.rec.list_runs(limit=3)
        assert len(runs) == 3
        # 倒序：最新在前
        assert runs[0]["goal"] == "goal_4"
