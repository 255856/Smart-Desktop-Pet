"""2026-10-20 性能审计修复的回归测试。

覆盖这一批里「行为容易悄悄变掉」的改动：
    M5  设置面板：set_many 单次落盘 + 300 ms 去抖
    M7  本地图片：data URL 记忆化（不重复读盘 / base64）
    M10 companion_log：尾部读 + 轮转
    M11 trace：stats 走 SQL 聚合 + list_runs 消除 N+1
    L1  app_registry：key→显示名 预计算
    L2  installed_apps：COM 对象按线程缓存
    L5  TTS 朗读队列有上限
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".local-packages"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


# ============================================================
#  M5：SettingsStore.set_many + 设置面板去抖
# ============================================================

class TestSettingsStoreBatch:
    def test_set_many_writes_once(self, tmp_path):
        from app.core.settings_store import SettingsStore
        store = SettingsStore(tmp_path / "s.json")
        calls = []
        orig = SettingsStore._save

        def _spy(self):
            calls.append(1)
            return orig(self)

        SettingsStore._save = _spy  # type: ignore[assignment]
        try:
            store.set_many({
                "model_base_url": "https://x/v1",
                "model_api_key": "k",
                "model_name": "m",
                "model_temperature": 0.8,
                "model_max_tokens": 4096,
                "model_timeout": 60,
                "model_stream": True,
            })
        finally:
            SettingsStore._save = orig  # type: ignore[assignment]

        assert len(calls) == 1, f"7 个键应该只落盘 1 次，实际 {len(calls)} 次"
        assert store.get("model_name") == "m"

    def test_set_many_persists_to_disk(self, tmp_path):
        from app.core.settings_store import SettingsStore
        p = tmp_path / "s.json"
        store = SettingsStore(p)
        store.set_many({"a": 1, "b": 2})
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["a"] == 1 and data["b"] == 2

    def test_model_config_write_is_debounced(self, tmp_path, qapp):
        """_on_model_config_changed 不应立刻落盘（要等去抖）。"""
        from app.core.settings_store import SettingsStore
        from app.ui.settings_window import SettingsWindow
        store = SettingsStore(tmp_path / "s.json")
        sw = SettingsWindow(
            parent=None, char_cfg=None, renderer=None,
            sticker_enabled=False, always_on_top=False, live2d_state=None,
        )
        sw.settings_store = store
        try:
            sw.edt_api_key.setText("sk-test-1234")
            sw._on_model_config_changed()
            # 还没到去抖时间 → 不应写盘
            assert store.get("model_api_key") is None, "去抖期间不应写盘"
            # 手动 flush（模拟 300 ms 后触发 / 关窗口）
            sw._flush_model_config()
            assert store.get("model_api_key") == "sk-test-1234"
        finally:
            sw.close()
            sw.deleteLater()


# ============================================================
#  M7：本地图片 data URL 记忆化
# ============================================================

class TestImageDataUrlCache:
    def _fake_window(self, tmp_path):
        from app.ui.chat_window import ChatWindow
        uploads = tmp_path / "assets" / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)

        class _Fake:
            _IMG_CACHE_MAX_BYTES = ChatWindow._IMG_CACHE_MAX_BYTES
            _IMG_CACHE_MAX_ENTRIES = ChatWindow._IMG_CACHE_MAX_ENTRIES

            def __init__(self):
                self._img_data_url_cache = {}
                self._doc_has_inline_images = False
                self.reads = 0

            def _uploads_dir(self):
                return uploads

        return _Fake()

    def test_second_render_does_not_reread_file(self, tmp_path):
        from app.ui.chat_window import ChatWindow
        fake = self._fake_window(tmp_path)
        img = tmp_path / "assets" / "uploads" / "a.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        text = f"![图](file:///{img.as_posix()})"

        real_read = Path.read_bytes
        reads = []

        def _spy(self, *a, **kw):
            if self.suffix.lower() == ".png":
                reads.append(1)
            return real_read(self, *a, **kw)

        Path.read_bytes = _spy  # type: ignore[assignment]
        try:
            out1 = ChatWindow._inline_local_images_to_data_url(fake, text)
            out2 = ChatWindow._inline_local_images_to_data_url(fake, text)
        finally:
            Path.read_bytes = real_read  # type: ignore[assignment]

        assert out1 == out2, "同样的输入必须得到同样的输出"
        assert "data:image/png;base64," in out1
        assert len(reads) == 1, f"第二次应命中缓存，实际读了 {len(reads)} 次"
        assert fake._doc_has_inline_images is True

    def test_changed_file_invalidates_cache(self, tmp_path):
        from app.ui.chat_window import ChatWindow
        fake = self._fake_window(tmp_path)
        img = tmp_path / "assets" / "uploads" / "b.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 32)
        text = f"![图](file:///{img.as_posix()})"
        out1 = ChatWindow._inline_local_images_to_data_url(fake, text)
        # 换内容 + 换 mtime
        time.sleep(0.01)
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"2" * 64)
        out2 = ChatWindow._inline_local_images_to_data_url(fake, text)
        assert out1 != out2, "文件变了缓存必须失效"


# ============================================================
#  M10：companion_log 尾部读 + 轮转
# ============================================================

class TestCompanionLogTailRead:
    @staticmethod
    def _log_path_under_cwd():
        """日志路径：**相对 CWD**（= monkeypatch.chdir 之后的 tmp_path）。

        tests/conftest.py 里把日志整体重定向到临时目录（不许写仓库 data/），
        这里要测「相对路径 + 轮转」的行为，所以把默认路径恢复成相对 CWD。
        """
        p = Path("data") / "companion_log.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def test_read_recent_returns_latest(self, tmp_path, monkeypatch):
        from app.brain import _companion_log as cl
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(cl, "_default_log_path", self._log_path_under_cwd)
        for i in range(300):
            cl.trace_decision(f"t{i}", "open_app", 1, "ALLOW", "ok", "happy", "Lv1")
        recs = cl.read_recent(limit=10)
        assert len(recs) == 10
        assert recs[0]["thread_id"] == "t299", "最新的一条要排在最前"

    def test_kind_filter(self, tmp_path, monkeypatch):
        from app.brain import _companion_log as cl
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(cl, "_default_log_path", self._log_path_under_cwd)
        for i in range(50):
            cl.trace_decision(f"d{i}", "open_app", 1, "ALLOW", "ok", "happy", "Lv1")
            cl.trace_outcome(f"o{i}", "done", "open_app", 0.1, 0.0, 0.0, 1.0, 1, 2, True)
        outs = cl.read_recent(limit=5, kind="outcome")
        assert len(outs) == 5
        assert all(r.get("kind") == "outcome" for r in outs)
        decs = cl.read_recent(limit=5, kind="decision")
        assert all("decision" in r for r in decs)

    def test_rotation_keeps_file_bounded(self, tmp_path, monkeypatch):
        from app.brain import _companion_log as cl
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(cl, "_default_log_path", self._log_path_under_cwd)
        monkeypatch.setattr(cl, "_MAX_LOG_BYTES", 4096)
        for i in range(200):
            cl.trace_decision(f"t{i}", "open_app", 1, "ALLOW", "ok", "happy", "Lv1")
        p = tmp_path / "data" / "companion_log.txt"
        assert p.stat().st_size <= 8192, "轮转后主文件不应该无限增长"
        assert (tmp_path / "data" / "companion_log.txt.1").is_file()


# ============================================================
#  M11：trace stats / list_runs
# ============================================================

class TestTraceQueries:
    def _recorder(self, tmp_path):
        from app.brain.trace import TraceRecorder
        rec = TraceRecorder(tmp_path / "trace.db")
        for i in range(20):
            rid = rec.begin_run(f"goal{i}", "react")
            rec.record(rid, "tool", {"name": "open_app", "args": "{}"})
            rec.record(rid, "text", {"content": "hi"})
            rec.end_run(rid, "done", "success")
        return rec

    def test_stats_tool_distribution(self, tmp_path):
        rec = self._recorder(tmp_path)
        try:
            s = rec.stats()
            assert s["total_runs"] == 20
            assert s["tool_distribution"].get("open_app") == 20
        finally:
            rec.close()

    def test_list_runs_includes_events(self, tmp_path):
        rec = self._recorder(tmp_path)
        try:
            runs = rec.list_runs(limit=5)
            assert len(runs) == 5
            assert all(len(r["events"]) == 2 for r in runs)
        finally:
            rec.close()

    def test_get_run_still_works(self, tmp_path):
        rec = self._recorder(tmp_path)
        try:
            runs = rec.list_runs(limit=1)
            one = rec.get_run(runs[0]["id"])
            assert one is not None
            assert len(one["events"]) == 2
        finally:
            rec.close()


# ============================================================
#  L1 / L2 / L5
# ============================================================

class TestLowItems:
    def test_key_to_display_precomputed(self):
        """_key_to_display 走预计算表（不再每次跑正则）。"""
        from app.core.app_registry import AppRegistry, _KEY_TO_DISPLAY
        reg = AppRegistry()
        assert reg._key_to_display("chrome") == "chrome"
        assert reg._key_to_display("不存在的键") == "不存在的键"
        assert _KEY_TO_DISPLAY, "预计算表不能为空"

    def test_com_shell_is_thread_local(self):
        """COM 对象必须按线程缓存（跨线程用会 0xC0000005 崩）。"""
        import threading
        from app.core import installed_apps as ia
        ia._SHELL_LOCAL = threading.local()
        shells = {}

        def _worker(name):
            shells[name] = ia._get_shell()

        threads = []
        names = [f"t{i}" for i in range(3)]
        for n in names:
            t = threading.Thread(target=_worker, args=(n,))
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        # 每个线程各自拿一份（不能共用同一个 COM 对象）
        seen = [shells[n] for n in names]
        for s in seen[1:]:
            assert s is None or s is not seen[0]
        # 主线程没建过 → 应为 None（不受其它线程影响）
        assert getattr(ia._SHELL_LOCAL, "shell", None) is None

    def test_tts_queue_is_bounded(self, tmp_path):
        """朗读队列有上限（满了丢最老的，不无限积压）。"""
        from app.voice.voice import TTS
        v = TTS(cache_dir=tmp_path / "cache")
        assert v._speak_queue.maxsize > 0, "队列必须有上限"
        for i in range(v._SPEAK_QUEUE_MAX + 10):
            v._enqueue_speak(f"第 {i} 句", None)
        assert v._speak_queue.qsize() <= v._SPEAK_QUEUE_MAX
        # 丢的是最老的：队首应该是较新的那条
        first = v._speak_queue.get_nowait()[0]
        assert first != "第 0 句", "队列满了应该丢最老的一条"
