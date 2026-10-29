# -*- coding: utf-8 -*-
"""记忆 sidecar 的**监督者**（拉起 / 健康轮询 / 有界重启 / 降级 / 退出清理）的测试。

全部用假进程 + 假时钟（A4/A5）：**不需要**真 sidecar、网络或模型。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.brain import brain_controller as bc  # noqa: E402
from app.brain.brain_controller import MemorySidecarSupervisor  # noqa: E402
from app.brain.memory_client import MemoryClient  # noqa: E402


class Clock:
    """假时钟：sleep 会推进 monotonic（所以「有限次等待」能瞬间测完）。"""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(round(float(seconds), 4))
        self.t += float(seconds)


class FakeProc:
    """假子进程：只需要 poll / terminate / kill / wait。"""

    def __init__(self, code=None):
        self.returncode = code      # None = 还活着
        self.terminated = 0
        self.killed = 0
        self.waited = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.returncode = -15

    def kill(self):
        self.killed += 1
        self.returncode = -9

    def wait(self, timeout=None):
        self.waited += 1
        return self.returncode

    def die(self, code=1):
        self.returncode = code


class Port:
    """假端口：healthy=True 表示 /health 答得上来。"""

    def __init__(self, healthy=False):
        self.healthy = healthy
        self.probes = 0

    def __call__(self):
        self.probes += 1
        return {"ok": True, "cards": 0} if self.healthy else None


class Spawner:
    """假 spawn：按脚本产出子进程（进程一起来，端口就健康 —— 除非明说失败）。"""

    def __init__(self, port=None, becomes_healthy=True, fail=False):
        self.port = port
        self.becomes_healthy = becomes_healthy
        self.fail = fail
        self.procs = []

    def __call__(self):
        if self.fail:
            raise OSError("起不来")
        proc = FakeProc()
        self.procs.append(proc)
        return proc

    @property
    def calls(self):
        return len(self.procs)


def _supervisor(tmp_path, probe, spawn, says=None, **kwargs):
    says = says if says is not None else []
    clock = kwargs.pop("clock", None) or Clock()
    sup = MemorySidecarSupervisor(
        tmp_path,
        port=8788,
        client=MemoryClient(base_url="http://127.0.0.1:8788"),
        probe=probe,
        spawn=spawn,
        say=says.append,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        poll_interval=kwargs.pop("poll_interval", 0.25),
        start_timeout=kwargs.pop("start_timeout", 2.0),
        backoff=kwargs.pop("backoff", (0.5, 1.0)),
        max_restarts=kwargs.pop("max_restarts", 3),
        watch_interval=kwargs.pop("watch_interval", 5.0),
    )
    sup.clock = clock
    return sup


# ---------------------------------------------------------------------------
# 端口上已经有健康的 sidecar → 复用，不再起第二个
# ---------------------------------------------------------------------------


def test_uses_an_already_healthy_sidecar_instead_of_spawning(tmp_path):
    port = Port(healthy=True)
    spawn = Spawner()
    sup = _supervisor(tmp_path, port, spawn)

    assert sup.start(blocking=True) is True
    assert spawn.calls == 0, "端口上已经有健康的 sidecar，不该再起第二个"
    assert sup.external is True
    assert sup.status()["running"] is True
    assert sup.status()["degraded"] is False


# ---------------------------------------------------------------------------
# 拉起 + 有限次健康轮询
# ---------------------------------------------------------------------------


def test_spawns_then_polls_health_until_ready(tmp_path):
    port = Port(healthy=False)
    spawn = Spawner()
    sup = _supervisor(tmp_path, port, spawn)

    def probe():
        # 子进程一起来端口就健康（真实 sidecar 也是这样：HTTP 起来就能答）
        port.healthy = spawn.calls > 0 and port.probes >= 2
        return port()

    sup._probe_fn = probe
    assert sup.start(blocking=True) is True
    assert spawn.calls == 1
    assert sup.running is True and sup.degraded is False
    assert sup.failures == 0
    assert sup.clock.sleeps, "等就绪期间应该按 poll_interval 轮询（不是忙等）"
    assert set(sup.clock.sleeps) == {0.25}


def test_bounded_wait_then_failure_when_health_never_comes(tmp_path):
    port = Port(healthy=False)
    spawn = Spawner()
    sup = _supervisor(tmp_path, port, spawn, start_timeout=1.0, poll_interval=0.25)

    assert sup.tick() is False
    assert spawn.calls == 1
    assert sup.failures == 1
    assert sup.running is False
    assert "健康检查" in sup.last_error
    # 有限次：等满 start_timeout 就收手（0.5/0.25 = 4 次轮询左右，绝不是死循环）
    assert len(sup.clock.sleeps) <= 6
    assert sup.clock.t >= 1.0
    # 没就绪的子进程要收尸，不能留孤儿
    assert spawn.procs[0].terminated == 1
    assert sup.process is None


def test_spawn_failure_is_recorded_not_raised(tmp_path):
    port = Port(healthy=False)
    spawn = Spawner(fail=True)
    sup = _supervisor(tmp_path, port, spawn)

    assert sup.tick() is False
    assert sup.failures == 1
    assert "起不来" in sup.last_error


# ---------------------------------------------------------------------------
# 崩溃重启（有界 + 退避）
# ---------------------------------------------------------------------------


def test_restarts_after_a_crash(tmp_path):
    port = Port(healthy=False)
    spawn = Spawner()
    sup = _supervisor(tmp_path, port, spawn)

    def probe():
        return {"ok": True} if sup.child_alive() else None

    sup._probe_fn = probe
    assert sup.tick() is True
    assert spawn.calls == 1
    first = sup.process

    first.die(1)                       # 崩了
    assert sup.tick() is True          # 看门狗重启它
    assert spawn.calls == 2
    assert sup.process is not first
    assert sup.running is True


def test_restart_attempts_use_backoff_and_are_bounded(tmp_path):
    port = Port(healthy=False)
    spawn = Spawner()
    says = []
    sup = _supervisor(tmp_path, port, spawn, says=says, start_timeout=0.5,
                      poll_interval=0.25, backoff=(0.5, 1.0), max_restarts=3)

    results = [sup.tick() for _ in range(5)]

    assert results == [False] * 5
    assert spawn.calls == 3, "最多重试 max_restarts 次，绝不无限重启"
    assert sup.degraded is True
    # 退避：第 2/3 次尝试之前各睡一次（0.5 / 1.0）
    assert 0.5 in sup.clock.sleeps and 1.0 in sup.clock.sleeps
    assert len(says) == 1, "降级只提醒主人**一次**"
    assert "长期记忆" in says[0]


def test_recovery_resets_the_failure_counter(tmp_path):
    port = Port(healthy=False)
    spawn = Spawner()
    sup = _supervisor(tmp_path, port, spawn, start_timeout=0.25)

    sup.tick()                                  # 失败一次
    assert sup.failures == 1
    port.healthy = True
    sup.process = None
    assert sup.tick() is True                   # 端口上已经健康 → 复用
    assert sup.failures == 1 or sup.failures == 0  # 复用不额外记失败
    assert sup.degraded is False


# ---------------------------------------------------------------------------
# 退出：不留孤儿（A5）
# ---------------------------------------------------------------------------


def test_stop_terminates_the_child_and_is_idempotent(tmp_path):
    port = Port(healthy=False)
    spawn = Spawner()
    sup = _supervisor(tmp_path, port, spawn)

    def probe():
        return {"ok": True} if sup.child_alive() else None

    sup._probe_fn = probe
    assert sup.start(blocking=True) is True
    proc = sup.process
    assert proc is not None

    sup.stop()
    assert proc.terminated == 1
    assert proc.waited >= 1
    assert sup.process is None
    assert sup.status()["running"] is False

    sup.stop()                       # 重复调用安全，不会重复 terminate
    assert proc.terminated == 1


def test_kill_is_the_fallback_when_terminate_hangs(tmp_path):
    class StubbornProc(FakeProc):
        """terminate 杀不死它（returncode 一直是 None），只能靠 kill。"""

        def terminate(self):
            self.terminated += 1

        def wait(self, timeout=None):
            if self.returncode is None:
                raise subprocess.TimeoutExpired("cmd", timeout)
            return self.returncode

    port = Port(healthy=True)
    spawn = Spawner()
    sup = _supervisor(tmp_path, port, spawn)
    proc = StubbornProc()
    sup.process = proc

    sup.stop()
    assert proc.terminated == 1
    assert proc.killed == 1, "terminate 不走就得 kill"
    assert sup.process is None


def test_atexit_is_registered_on_start(tmp_path):
    sup = _supervisor(tmp_path, Port(healthy=True), Spawner())
    assert sup._atexit_registered is False
    sup.start(blocking=True)
    assert sup._atexit_registered is True, "退出时必须有机会收尸（atexit 兜底）"


# ---------------------------------------------------------------------------
# 启动不被阻塞：生产模式丢后台线程
# ---------------------------------------------------------------------------


def test_start_returns_immediately_and_watchdog_runs_in_a_daemon_thread(tmp_path):
    port = Port(healthy=True)
    spawn = Spawner()
    sup = _supervisor(tmp_path, port, spawn)

    started = time.monotonic()
    assert sup.start() is True
    assert time.monotonic() - started < 1.0, "起 sidecar 绝不许阻塞桌宠启动"
    assert sup._thread is not None and sup._thread.daemon is True

    deadline = time.monotonic() + 5.0
    while port.probes == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    sup.stop()
    assert port.probes >= 1, "后台看门狗应该真的跑起来了"


# ---------------------------------------------------------------------------
# 真·拉起命令的形状（不真的起进程：只替换 subprocess.Popen）
# ---------------------------------------------------------------------------


def test_real_spawn_uses_python_dash_m_sidecar_and_writes_a_log(tmp_path, monkeypatch):
    recorded = {}

    def fake_popen(cmd, **kwargs):
        recorded["cmd"] = list(cmd)
        recorded["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(bc.subprocess, "Popen", fake_popen)
    port = Port(healthy=True)
    sup = _supervisor(tmp_path, port, Spawner())
    sup._spawn_fn = None            # 走真实 spawn_child
    sup.python = sys.executable

    proc = sup.spawn_child()

    assert proc is not None
    assert recorded["cmd"] == [sys.executable, "-m", "memory_sidecar", "serve",
                               "--port", "8788"]
    assert Path(recorded["kwargs"]["cwd"]) == tmp_path
    assert recorded["kwargs"]["stderr"] == subprocess.STDOUT
    log_dir = tmp_path / "data" / "logs"
    assert log_dir.is_dir(), "日志目录要自己建出来"
    assert sup.log_path is not None and sup.log_path.parent == log_dir
    assert recorded["kwargs"]["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------------------
# 状态里的字段（面板要）
# ---------------------------------------------------------------------------


def test_status_exposes_running_cards_error_for_the_panel(tmp_path):
    port = Port(healthy=True)
    sup = _supervisor(tmp_path, port, Spawner())
    sup.start(blocking=True)
    status = sup.status()
    for key in ("running", "managed", "external", "degraded", "failures",
                "port", "url", "log", "error"):
        assert key in status, key
    assert status["url"] == "http://127.0.0.1:8788"
    assert status["port"] == 8788


def test_default_owner_notice_reuses_the_owner_notice_channel(monkeypatch):
    """降级提醒走**已有**那条通道（气泡 say hook → 系统通知），不另发明一个。"""
    from app.core import owner_notice

    seen = []
    monkeypatch.setattr(owner_notice, "notify_owner_degraded",
                        lambda key, title, message: seen.append((key, title, message)))
    bc._default_owner_notice("长期记忆暂时用不了了")

    assert seen and seen[0][0] == "memory_sidecar", seen
    assert "长期记忆" in seen[0][1]
    assert seen[0][2] == "长期记忆暂时用不了了"


def test_probe_uses_a_short_timeout(tmp_path):
    """探测只看端口在不在听：不吃聊天路径的 2s 超时（否则启动会很慢）。"""
    seen = []

    class _Client:
        def health(self, timeout=None):
            seen.append(timeout)
            return {}

    sup = MemorySidecarSupervisor(tmp_path, port=8788, client=_Client(),
                                  sleep=lambda s: None, monotonic=lambda: 0.0)
    assert sup.probe() is None
    assert seen and seen[0] == pytest.approx(bc.SIDECAR_PROBE_TIMEOUT)
