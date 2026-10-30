#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""桌宠**启动阶段** CPU 成本测量（统一口径：Live2D / sprite / 记忆 sidecar）。

回答 owner 的那个问题：「启动时到底是谁在吃 CPU？」

用法：
    python scripts/measure_startup_cost.py --case live2d --seconds 25
    python scripts/measure_startup_cost.py --case sidecar --seconds 45
    python scripts/measure_startup_cost.py --case sidecar --no-warmup --seconds 45
    python scripts/measure_startup_cost.py --case all --json data/measure/startup.json

口径（与 scripts/measure_live2d_cost.py 一致，见 scripts/measure_common.py）：
* CPU% = 进程树里 (user+system 增量)/(时间增量)，100% = 跑满一个核心。
* 时间轴：250ms 一格，记录每格 CPU% / 内存 / 进程数，并打「谁在第几秒就绪」的标记。
* 内存：rss = 工作集，private = 私有提交。

清理：每个用例都在本进程里跑，结束时杀掉自己拉起的全部子进程
（Chromium / sidecar），跑完不留任何进程。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parent))
from measure_common import Timeline  # noqa: E402


class Sampler(threading.Thread):
    """后台采样线程：主线程就算卡在构造渲染器里，CPU 曲线也不会丢。"""

    def __init__(self, proc, interval=0.25):
        super().__init__(daemon=True)
        self.tl = Timeline(proc, interval=interval)
        self._stop_evt = threading.Event()

    def run(self):
        self.tl.start()
        while not self._stop_evt.is_set():
            self.tl.tick()
            self._stop_evt.wait(self.tl.interval)

    def stop(self):
        self._stop_evt.set()


def _kill_tree(proc):
    """杀掉被测进程拉起的全部子进程（不留残留）。"""
    import psutil

    try:
        kids = proc.children(recursive=True)
    except Exception:  # noqa: BLE001
        kids = []
    for k in kids:
        try:
            k.kill()
        except Exception:  # noqa: BLE001
            pass
    _, alive = psutil.wait_procs(kids, timeout=5)
    for a in alive:
        try:
            a.kill()
        except Exception:  # noqa: BLE001
            pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------
# case: live2d
# --------------------------------------------------------------------------
def case_live2d(args) -> dict:
    import psutil

    os.environ.setdefault("OMP_NUM_THREADS", "1")
    from app.core.webengine_env import configure_webengine_env

    configure_webengine_env(trim=(args.chromium_trim == "on"))
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] += (
        f" --user-data-dir={args.user_data_dir}")

    from PyQt5.QtCore import Qt, QCoreApplication, QTimer

    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    from app.core.qt_compat import QApplication

    app = QApplication([sys.argv[0]])
    proc = psutil.Process()
    sampler = Sampler(proc)
    sampler.start()

    from app.animation.live2d_renderer import Live2DRenderer

    state = {"ready": False, "t_ready": None, "deadline": time.perf_counter() + 60}
    t0 = time.perf_counter()
    renderer = Live2DRenderer(
        model_dir=args.model_dir,
        widget_size=(500, 500),
        scale=0.5,
        max_fps=args.max_fps,
    )
    t_construct = time.perf_counter() - t0
    # 首帧时机以 Python 侧的 _on_model_ready 回调为准（JS 轮询的回调会排队）
    _orig_ready = renderer._on_model_ready

    def _ready(expressions_json):
        if not state.get("ready"):
            state["ready"] = True
            state["t_ready"] = round(time.perf_counter() - t0, 3)
            sampler.tl.mark("模型 ready（首帧）")
        _orig_ready(expressions_json)

    renderer._on_model_ready = _ready
    widget = renderer.get_widget()
    widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    widget.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
    widget.show()

    def _poll():
        if state["ready"] or time.perf_counter() > state["deadline"]:
            _on_ready()
            return
        try:
            renderer._page.runJavaScript(
                "(function(){var l=window.live2d;if(!l||!l.ready||!l.app||!l.app.ticker)"
                "return 0;return l.app.ticker.elapsedMS>0?1:0;})()", _on_js)
        except Exception:  # noqa: BLE001
            _on_ready()

    def _on_js(v):
        if v in (1, True, 1.0):
            _on_ready()
        else:
            QTimer.singleShot(30, _poll)

    def _on_ready():
        if state.get("quit_scheduled"):
            return
        state["quit_scheduled"] = True
        if not state.get("ready"):
            state["ready"] = True
            state["t_ready"] = round(time.perf_counter() - t0, 3)
            sampler.tl.mark("模型 ready（首帧）（JS 轮询兜底）")
        remain = max(0.0, args.seconds - (time.perf_counter() - t0))
        QTimer.singleShot(int(remain * 1000), app.quit)

    QTimer.singleShot(20, _poll)
    QTimer.singleShot(int(args.seconds * 1000) + 8000, app.quit)
    app.exec()

    sampler.stop()
    sampler.join(timeout=3)
    out = {
        "case": "live2d",
        "model_dir": args.model_dir,
        "t_construct_s": round(t_construct, 3),
        "t_ready_s": state["t_ready"],
        "summary": sampler.tl.summary(),
        "timeline": sampler.tl.rows,
    }
    try:
        renderer.shutdown()
    except Exception:  # noqa: BLE001
        pass
    _kill_tree(proc)
    return out


# --------------------------------------------------------------------------
# case: sprite
# --------------------------------------------------------------------------
def case_sprite(args) -> dict:
    import psutil

    from PyQt5.QtCore import Qt, QCoreApplication, QTimer

    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    from app.core.qt_compat import QApplication, QSize

    app = QApplication([sys.argv[0]])
    proc = psutil.Process()
    sampler = Sampler(proc)

    from app.animation.sprite_renderer import SpriteRenderer

    sampler.start()
    t0 = time.perf_counter()
    renderer = SpriteRenderer(
        sprite_dir=Path(args.sprite_dir),
        fallback_image=(Path(args.sprite_dir) / "fallback.png"),
        window_size=QSize(500, 500),
        scale=0.5,
    )
    t_construct = time.perf_counter() - t0
    renderer.get_widget().show()
    sampler.tl.mark("sprite 资源加载完成")

    QTimer.singleShot(int(args.seconds * 1000), app.quit)
    app.exec()
    sampler.stop()
    sampler.join(timeout=3)
    out = {
        "case": "sprite",
        "t_construct_s": round(t_construct, 3),
        "summary": sampler.tl.summary(),
        "timeline": sampler.tl.rows,
    }
    try:
        renderer.shutdown()
    except Exception:  # noqa: BLE001
        pass
    return out


# --------------------------------------------------------------------------
# case: sidecar（记忆 sidecar 预热）
# --------------------------------------------------------------------------
def case_sidecar(args) -> dict:
    import psutil

    proc = psutil.Process()
    # 数据目录：拷一份到临时目录，**不动真实的 data/memory_sidecar**
    tmpdir = Path(tempfile.mkdtemp(prefix="dp_sidecar_"))
    src = ROOT / "data" / "memory_sidecar"
    data_dir = tmpdir / "memory_sidecar"
    if src.is_dir():
        shutil.copytree(src, data_dir)
    else:
        data_dir.mkdir(parents=True, exist_ok=True)

    port = _free_port()
    cmd = [sys.executable, "-m", "memory_sidecar", "serve",
           "--port", str(port), "--data-dir", str(data_dir)]
    if args.no_warmup:
        cmd.append("--no-warmup")
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    child = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace")

    sampler = Sampler(proc)
    sampler.start()
    t0 = time.perf_counter()
    log_lines: list[str] = []
    health_at = None
    health = None
    warmup_at = None
    search_at = None
    search_ms = None

    def _drain():
        """读 sidecar 的输出（找「预热完成」那行）。"""
        try:
            line = child.stdout.readline()
        except Exception:  # noqa: BLE001
            return
        if not line:
            return
        log_lines.append(line.rstrip())
        if "预热完成" in line and warmup_at is None:
            warmup_at = round(time.perf_counter() - t0, 2)
            sampler.tl.mark("sidecar 预热完成")

    threading.Thread(target=_drain, daemon=True).start()

    deadline = time.perf_counter() + args.seconds
    while time.perf_counter() < deadline:
        if health_at is None:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{port}/health", timeout=1) as resp:
                    body = resp.read().decode("utf-8")
                health_at = round(time.perf_counter() - t0, 2)
                sampler.tl.mark("sidecar /health 可用")
                health = json.loads(body)
            except Exception:  # noqa: BLE001
                pass
        if (args.search_after and health_at and search_at is None
                and time.perf_counter() - t0 > health_at + 1):
            search_at = round(time.perf_counter() - t0, 2)
            t_s = time.perf_counter()
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/search",
                    data=json.dumps({"query": "今天做了什么", "top_k": 3}).encode(),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=90) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                search_ms = round((time.perf_counter() - t_s) * 1000, 1)
                sampler.tl.mark(
                    f"首次 /search 返回（{search_ms}ms，path={payload.get('path')}）")
            except Exception as e:  # noqa: BLE001
                sampler.tl.mark(f"首次 /search 失败：{e!r}")
        time.sleep(0.25)

    sampler.stop()
    sampler.join(timeout=3)
    out = {
        "case": "sidecar_no_warmup" if args.no_warmup else "sidecar",
        "warmup_flag": not args.no_warmup,
        "t_health_s": health_at,
        "t_warmup_done_s": warmup_at,
        "t_search_s": search_at,
        "search_ms": search_ms,
        "health": health,
        "summary": sampler.tl.summary(),
        "timeline": sampler.tl.rows,
        "log_tail": log_lines[-8:],
    }
    # 收尾：sidecar 及其子进程全部杀掉
    try:
        child.terminate()
        child.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            child.kill()
        except Exception:  # noqa: BLE001
            pass
    _kill_tree(proc)
    shutil.rmtree(tmpdir, ignore_errors=True)
    return out


# --------------------------------------------------------------------------
def _timeline_table(rows, bucket=1.0) -> str:
    """把 250ms 的采样聚成 1s 一格，打印「第几秒烧了多少 CPU」。"""
    if not rows:
        return "（无数据）"
    out = ["| 秒 | 峰值CPU% | 均值CPU% | 主进程CPU% | 内存MB | 进程数 |",
           "|---|---|---|---|---|---|"]
    buckets: dict[int, list] = {}
    for r in rows:
        buckets.setdefault(int(r["t"] // bucket), []).append(r)
    for sec in sorted(buckets):
        g = buckets[sec]
        peak = max(x["cpu_pct"] for x in g)
        mean = sum(x["cpu_pct"] for x in g) / len(g)
        main = max(x["cpu_pct_main"] for x in g)
        out.append(f"| {sec}-{sec + int(bucket)} | {peak:.1f} | {mean:.1f} | {main:.1f} "
                   f"| {g[-1]['rss_mb']:.0f} | {g[-1]['process_count']} |")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="启动阶段 CPU 成本测量")
    ap.add_argument("--case", choices=["live2d", "sprite", "sidecar", "all"],
                    default="all")
    ap.add_argument("--seconds", type=float, default=25.0)
    ap.add_argument("--max-fps", type=int, default=30)
    ap.add_argument("--chromium-trim", choices=["on", "off"], default="on")
    ap.add_argument("--no-warmup", action="store_true",
                    help="sidecar 不预热（--no-warmup）")
    ap.add_argument("--search-after", action="store_true",
                    help="sidecar 就绪后发一次 /search（量首次语义检索）")
    ap.add_argument("--model-dir", default="")
    ap.add_argument("--sprite-dir", default=str(ROOT / "assets" / "sprites"))
    ap.add_argument("--user-data-dir", default="")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    if not args.model_dir:
        args.model_dir = _default_model_dir()
    if not args.user_data_dir:
        args.user_data_dir = tempfile.mkdtemp(prefix="dp_udd_")

    if args.case == "all":
        cases = ["live2d", "sprite", "sidecar"]
    else:
        cases = [args.case]

    results = {}
    for c in cases:
        print(f"[startup] 测量 {c} ...", flush=True)
        if c == "live2d":
            r = case_live2d(args)
        elif c == "sprite":
            r = case_sprite(args)
        else:
            r = case_sidecar(args)
        results[c] = r
        s = r.get("summary", {})
        print(f"[startup] {c}: 峰值CPU={s.get('peak_cpu_pct')}% @{s.get('peak_at_s')}s "
              f"均值={s.get('mean_cpu_pct')}% CPU秒={s.get('cpu_seconds_total')} "
              f"内存={s.get('end_rss_mb')}MB 进程数={s.get('max_process_count')}",
              flush=True)
        for m in s.get("marks", []):
            print(f"           · {m['t']}s {m['label']}", flush=True)
        print(_timeline_table(r.get("timeline", [])), flush=True)

    if args.json:
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(results, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        print(f"[startup] 结果写入 {p}")
    shutil.rmtree(args.user_data_dir, ignore_errors=True)
    return 0


def _default_model_dir() -> str:
    """从 config.yaml 读 pet.live2d.model_dir（只读 pet 段）。"""
    cfg = ROOT / "config.yaml"
    if cfg.is_file():
        try:
            import yaml

            raw = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            pet = raw.get("pet") or {}
            l2d = pet.get("live2d") or {}
            d = l2d.get("model_dir") or ""
            if d:
                p = Path(d)
                if not p.is_absolute():
                    p = ROOT / d
                if p.is_dir():
                    return str(p)
        except Exception:  # noqa: BLE001
            pass
    return str(ROOT / "assets" / "live2d" / "hiyori_zh-Hans" / "hiyori_free" / "runtime")


if __name__ == "__main__":
    sys.exit(main())
