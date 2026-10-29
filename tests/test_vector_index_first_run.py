# -*- coding: utf-8 -*-
"""首次运行 / 离线降级 的验收测试（Stage 2.11）。

覆盖（对应任务书的验收标准）：

  F1 **首次聊天绝不等模型下载**
     - 模型没下载时，聊天路径毫秒级返回内容（不等 180 秒的加载超时）；
     - 聊天路径**不发起任何下载**（连模型对象都不构造）；
     - 显式重建时立刻失败，并给出**可执行**的补救命令。
  F2 **降级对主人可见**（不是一行日志）
     - 状态字段（store / 面板都能看到「降级 + 该跑什么」）；
     - 主人收到一条系统通知（app/core/toast.py 那条既有通道）。
  F3 模型没下载时 store 照常工作，召回退回字面路径，**异常不许外泄**
  F4 scripts/prefetch_models.py：--check / 幂等 / 不写仓库 / 不乱联网

不需要 API key / 网络 / GUI。
"""
from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path

import pytest

from app.brain import vector_index as vi
from app.brain.memory import MemoryStore
from app.brain.vector_index import (
    EMBED_MODEL_ID,
    MODEL_FILES,
    MODEL_STATE_ABSENT,
    MODEL_STATE_IDLE,
    RERANK_MODEL_ID,
    VectorIndex,
    _ModelCache,
    model_files_present,
    models_status,
)

# 复用现成的 fixture（tests/test_vector_index.py）：
#   no_models   —— 「模型都没有」（模拟没下载 / 下载失败）
#   fake_models —— 装上假模型（确定性、零下载）
from tests.test_vector_index import fake_models, no_models  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def _rows(block: str) -> list:
    """从记忆块里抠出注入的行（跟 tests/test_vector_index.py 同一口径）。"""
    out = []
    for line in (block or "").splitlines():
        if line.startswith("- ["):
            out.append(line.split(") ", 1)[1])
    return out


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def _fake_models_status(missing=(EMBED_MODEL_ID, RERANK_MODEL_ID)) -> dict:
    """假的 models_status()（**不依赖本机缓存**，测试可复现）。"""
    missing = list(missing)
    out = {"cache_dir": "<fake>", "missing": missing,
           "complete": not missing, "embed": None, "rerank": None}
    for key, model_id in (("embed", EMBED_MODEL_ID), ("rerank", RERANK_MODEL_ID)):
        out[key] = {"model": model_id, "present": model_id not in missing,
                    "bytes": 1024 if model_id not in missing else 0}
    return out


@pytest.fixture()
def models_absent(monkeypatch):
    """模拟「这台机器从来没下过模型」。

    - 模型文件一律判为「不在」；
    - 运行时**不允许**下载（默认行为）；
    - 换一个干净的进程级模型缓存（别污染别的用例）；
    - 通知登记表清空（每个用例各自记自己的）。
    """
    monkeypatch.setattr(vi, "model_files_present", lambda mid: False)
    monkeypatch.setattr(vi, "allow_runtime_download", lambda: False)
    monkeypatch.setattr(vi, "models_status",
                        lambda *a, **k: _fake_models_status())
    cache = vi._ModelCache()
    monkeypatch.setattr(vi, "_MODEL_CACHE", cache)
    monkeypatch.setattr(vi, "_OWNER_NOTICES", {})
    # 测试里**不真的弹窗**：把提醒记下来（orig_sink 留着给「真通道」那条用例）
    notices = []
    orig_sink = vi._owner_notice_sink
    monkeypatch.setattr(vi, "_owner_notice_sink",
                        lambda title, message: notices.append((title, message)))
    cache.notices = notices
    cache.orig_sink = orig_sink
    return cache


@pytest.fixture()
def no_model_loading(monkeypatch):
    """模型对象**一个都不许构造**（= 不许触发任何加载 / 下载）。"""
    import sentence_transformers

    def boom(*a, **k):
        raise AssertionError("聊天路径不该去构造模型（更不该下载）")

    monkeypatch.setattr(sentence_transformers, "SentenceTransformer", boom)
    monkeypatch.setattr(sentence_transformers, "CrossEncoder", boom)


def _store(tmp_path, name="memory.db", contents=()):
    store = MemoryStore(tmp_path / name, backend_name="tfidf")
    for content in contents:
        store.add(content, "fact", importance=0.5)
    return store


# --------------------------------------------------------------------------
# F1 · 首次聊天绝不等模型下载
# --------------------------------------------------------------------------
class TestFirstChatNeverWaitsForDownload:
    def test_chat_path_returns_content_when_models_are_absent(self, tmp_path,
                                                              models_absent):
        """模型没下载 → 聊天路径照常返回内容（**毫秒级**）。"""
        store = _store(tmp_path, contents=("主人明天要交房租",))
        started = time.time()
        block = store.system_block("明天要交房租")
        elapsed = time.time() - started
        assert block, "降级不等于不返回内容"
        assert "房租" in block, block
        assert elapsed < 5.0, "聊天路径等了 %.2fs（不该等下载）" % elapsed

    def test_chat_path_never_starts_a_download(self, tmp_path, models_absent,
                                                no_model_loading):
        """模型没下载 → **连模型对象都不构造**（no_model_loading 会直接炸）。"""
        store = _store(tmp_path, contents=("主人明天要交房租",))
        # 建一个空的索引目录 → store 会真的去「打开索引 + 同步」这条路径
        store.vector_index_dir.mkdir(parents=True, exist_ok=True)
        started = time.time()
        block = store.system_block("明天要交房租")
        elapsed = time.time() - started
        assert block
        assert elapsed < 5.0, elapsed
        cache = models_absent
        assert cache.embed_state == MODEL_STATE_ABSENT, cache.embed_state
        assert cache._embed_thread is None, "不该起加载线程"
        assert cache.embed_error is None

    def test_rebuild_without_models_fails_fast_and_says_what_to_do(
            self, tmp_path, models_absent):
        """显式重建（面板按钮 / 脚本）→ **立刻**失败 + 给出补救命令。"""
        store = _store(tmp_path, contents=("主人明天要交房租",))
        started = time.time()
        report = store.rebuild_vector_index(blocking=True)
        elapsed = time.time() - started
        assert report["ok"] is False, report
        assert elapsed < 30.0, "等了 %.1fs（模型加载超时会等 180s）" % elapsed
        assert "prefetch_models.py" in str(report["error"]), report

    def test_absent_state_is_rechecked_so_prefetch_needs_no_restart(self):
        """模型被 prefetch 补上之后，桌宠**不用重启**也能用上。"""
        cache = _ModelCache()
        cache._embed_state = MODEL_STATE_ABSENT
        cache._embed_absent_at = time.time()
        assert cache._absent_may_retry("embed") is False, "不该马上重查"
        cache._embed_absent_at = time.time() - 9999
        assert cache._absent_may_retry("embed") is True
        assert cache._embed_state == MODEL_STATE_IDLE


# --------------------------------------------------------------------------
# F2 · 降级对主人可见
# --------------------------------------------------------------------------
class TestDegradedStateIsVisible:
    def test_store_status_reports_degraded_and_how_to_fix_it(self, tmp_path,
                                                             models_absent):
        store = _store(tmp_path, contents=("主人明天要交房租",))
        status = store.vector_index_status()
        assert status["degraded"] is True, status
        assert status["degraded_reason"] == "models_missing", status
        assert "prefetch_models.py" in status["notice"], status
        assert EMBED_MODEL_ID in status["notice"], status
        assert status["models_missing"], status

    def test_owner_gets_a_real_notification_not_just_a_log_line(
            self, tmp_path, models_absent):
        """主人要收到**一条看得见的提醒**（通知），不是日志行。"""
        seen = models_absent.notices
        store = _store(tmp_path, contents=("主人明天要交房租",))
        store.vector_index_dir.mkdir(parents=True, exist_ok=True)
        store.system_block("明天要交房租")
        assert _wait_for(lambda: bool(seen)), "主人应该收到一条提醒"
        title, message = seen[0]
        assert "记忆检索" in title or "降级" in title, title
        assert "prefetch_models.py" in message, message
        # 同一个原因**只提醒一次**（不刷屏）
        assert vi.notify_owner_degraded("models_missing", "x", "y") is False

    def test_pet_says_it_to_the_owner(self, tmp_path, models_absent,
                                      monkeypatch):
        """桌宠**自己冒泡**告诉主人（项目里既有的「说一声」通道）。

        通道：app/engine/tools/_timer.py 的 say hook ——
        主程序启动时注入一个把文字冒泡给主人的函数。
        """
        import app.engine.tools._timer as timer_mod
        said = []
        monkeypatch.setattr(timer_mod, "_global_say_hook",
                            lambda text: said.append(text))
        # 用**真的**提醒出口（fixture 里那个是录制的替身）
        monkeypatch.setattr(vi, "_owner_notice_sink", models_absent.orig_sink)
        store = _store(tmp_path, contents=("主人明天要交房租",))
        store.vector_index_dir.mkdir(parents=True, exist_ok=True)
        store.system_block("明天要交房租")
        assert _wait_for(lambda: bool(said)), "桌宠应该冒泡告诉主人"
        assert "prefetch_models.py" in said[0], said[0]

    def test_dashboard_payload_shows_the_degraded_state(self, tmp_path,
                                                        models_absent):
        from app.web import dashboard
        payload = dashboard._vector_index_status(tmp_path, ROOT)
        assert payload["degraded"] is True, payload
        assert payload["degraded_reason"] == "models_missing", payload
        assert "prefetch_models.py" in payload["notice"], payload
        assert payload["models_missing"], payload

    def test_status_never_raises_even_if_the_probe_breaks(self, tmp_path,
                                                           monkeypatch):
        """状态探测坏掉 → 只影响状态，不许把聊天路径带崩。"""
        def boom(*a, **k):
            raise RuntimeError("探测炸了")

        monkeypatch.setattr(vi, "models_status", boom)
        idx = VectorIndex(tmp_path / "vectors" / "memory")
        status = idx.status()
        assert status["error"], status
        assert status["degraded"] is True, status


# --------------------------------------------------------------------------
# F3 · 降级时 store 照常工作，异常不许外泄
# --------------------------------------------------------------------------
class TestDegradedStoreStillWorks:
    def test_recall_falls_back_to_the_lexical_path(self, tmp_path, no_models):
        """模型都没有 → 召回退回今天的数字（substring + TF-IDF）。"""
        contents = ("主人明天要交房租", "主人的猫叫团子", "主人喜欢喝冰美式")
        query = "明天要交房租"
        store = _store(tmp_path, "a.db", contents)
        store.rebuild_vector_index(blocking=True)      # 没模型 → 建不起来
        got = _rows(store.system_block(query))
        ref = MemoryStore(tmp_path / "ref.db", backend_name="tfidf",
                          vector_index=False)
        for content in contents:
            ref.add(content, "fact", importance=0.5)
        want = _rows(ref.system_block(query))
        assert got == want, (got, want)

    def test_index_exception_never_escapes_into_the_chat_path(
            self, tmp_path, fake_models, monkeypatch):
        """索引内部任何异常都**不许冒到聊天路径**。"""
        def boom(*a, **k):
            raise RuntimeError("索引炸了")

        for name in ("_sync_impl", "_query_impl", "_rerank_impl"):
            monkeypatch.setattr(VectorIndex, name, boom)
        store = _store(tmp_path, contents=("主人明天要交房租",))
        store.vector_index_dir.mkdir(parents=True, exist_ok=True)
        block = store.system_block("明天要交房租")      # 不许抛
        assert block, "降级之后仍然要返回内容"
        assert "房租" in block, block


# --------------------------------------------------------------------------
# F4 · scripts/prefetch_models.py
# --------------------------------------------------------------------------
def _load_prefetch_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "prefetch_models_cli",
        ROOT / "scripts" / "prefetch_models.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_cache(root: Path, models=(EMBED_MODEL_ID, RERANK_MODEL_ID)) -> Path:
    """造一个「看起来已经有模型」的缓存目录（**不联网**）。"""
    hub = root / "hub"
    for model_id in models:
        base = hub / ("models--" + model_id.replace("/", "--"))
        snap = base / "snapshots" / "deadbeef"
        snap.mkdir(parents=True, exist_ok=True)
        for name in MODEL_FILES[model_id]:
            p = snap / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("fake", encoding="utf-8")
        (base / "refs").mkdir(parents=True, exist_ok=True)
        (base / "refs" / "main").write_text("deadbeef", encoding="utf-8")
    return hub


class TestPrefetchScript:
    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch):
        """脚本会改 HF_HOME —— 用 monkeypatch 兜住，teardown 还原。"""
        monkeypatch.setenv("HF_HOME", str(ROOT / ".pytest-tmp-hf-home"))
        monkeypatch.delenv("HF_HUB_CACHE", raising=False)

    def test_check_mode_reports_missing_models(self, tmp_path, capsys):
        mod = _load_prefetch_module()
        code = mod.main(["--check", "--cache-dir", str(tmp_path / "empty")])
        out = capsys.readouterr().out
        assert code == 1, out
        assert "不在" in out, out
        assert "prefetch_models.py" in out, out

    def test_check_mode_reports_present_models(self, tmp_path, capsys):
        mod = _load_prefetch_module()
        _fake_cache(tmp_path / "hf")
        code = mod.main(["--check", "--cache-dir", str(tmp_path / "hf")])
        out = capsys.readouterr().out
        assert code == 0, out
        assert "在" in out, out
        assert "不用下载" in out, out

    def test_running_with_models_present_is_a_noop(self, tmp_path, capsys,
                                                   monkeypatch):
        """模型已经在 → **不下载、不写任何东西**（幂等）。"""
        import huggingface_hub

        def boom(*a, **k):
            raise AssertionError("模型都在了还去下载？")

        monkeypatch.setattr(huggingface_hub, "snapshot_download", boom)
        mod = _load_prefetch_module()
        _fake_cache(tmp_path / "hf")
        code = mod.main(["--cache-dir", str(tmp_path / "hf"), "--skip-verify"])
        out = capsys.readouterr().out
        assert code == 0, out
        assert "跳过下载" in out, out

    def test_script_never_writes_into_the_repository(self, tmp_path, capsys):
        """模型**不进仓库**（只进 HF 缓存目录）。"""
        mod = _load_prefetch_module()
        _fake_cache(tmp_path / "hf")
        before = sorted(p.relative_to(ROOT).as_posix()
                        for p in (ROOT / "data").rglob("*") if p.is_file())
        code = mod.main(["--check", "--cache-dir", str(tmp_path / "hf")])
        after = sorted(p.relative_to(ROOT).as_posix()
                       for p in (ROOT / "data").rglob("*") if p.is_file())
        assert code == 0
        assert before == after, "脚本不该动仓库里的 data/"


# --------------------------------------------------------------------------
# 模型文件清单 / 缓存探测（纯本地，不联网）
# --------------------------------------------------------------------------
class TestModelCacheProbe:
    def test_model_files_present_reads_the_cache_dir(self, tmp_path,
                                                     monkeypatch):
        monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
        monkeypatch.delenv("HF_HUB_CACHE", raising=False)
        assert model_files_present(EMBED_MODEL_ID) is False
        _fake_cache(tmp_path / "hf", models=(EMBED_MODEL_ID,))
        assert model_files_present(EMBED_MODEL_ID) is True
        assert model_files_present(RERANK_MODEL_ID) is False

    def test_models_status_lists_what_is_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
        monkeypatch.delenv("HF_HUB_CACHE", raising=False)
        st = models_status()
        assert st["missing"] == [EMBED_MODEL_ID, RERANK_MODEL_ID], st
        assert st["complete"] is False
        _fake_cache(tmp_path / "hf")
        st = models_status()
        assert st["missing"] == [], st
        assert st["complete"] is True
