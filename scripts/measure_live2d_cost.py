#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Live2D / sprite 渲染器启动与运行成本测量（隔离 harness，不启动桌宠主程序）。

用法：
    python scripts/measure_live2d_cost.py                       # live2d + sprite 对照
    python scripts/measure_live2d_cost.py --renderer live2d
    python scripts/measure_live2d_cost.py --label before --json data/measure/before.json

测什么：
* **进程树**：QtWebEngineProcess.exe（Chromium GPU / 渲染进程）数量、总工作集 (MB)
* **CPU**：待机 10s 的树内总 CPU%（主进程与子进程分开列）
* **启动时延**：渲染器构造 → 模型 ready → 第一帧（JS 侧 ticker 真的跑起来）
* **动画证据**：ticker 时间推进 + 核心参数（ParamAngleX / 口型）随时间变化

清理：每个用例跑在独立子进程里，父进程负责 kill 整棵进程树
（含残留的 QtWebEngineProcess —— 用唯一 --user-data-dir 认领），跑完不留进程。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# JS 探针
JS_READY = (
    "(function(){var l=window.live2d;if(!l||!l.ready||!l.app||!l.app.ticker)"
    "return 0;return l.app.ticker.elapsedMS>0?1:0;})()"
)
JS_PARAM = (
    "(function(){var l=window.live2d;if(!l||!l.model)return null;"
    "var c=l.model.internalModel&&l.model.internalModel.coreModel;"
    "if(!c)return null;try{return c.getParameterValueById(%s);}catch(e){return null;}})()"
)
JS_STATE = (
    "(function(){var l=window.live2d;if(!l||!l.model)return null;"
    "var c=l.model.internalModel&&l.model.internalModel.coreModel;"
    "var em=(l._expressionManager&&l._expressionManager())?l._expressionManager():null;"
    "return {ticker:l.app?l.app.ticker.elapsedMS:0,"
    "frame:(l.app&&l.app.ticker)?l.app.ticker.frame:0,"
    "fps:l.app?l.app.ticker.FPS:0,maxFPS:l.app?l.app.ticker.maxFPS:0,"
    "angleX:c?c.getParameterValueById('ParamAngleX'):null,"
    "mouth:c?c.getParameterValueById('ParamMouthOpenY'):null,"
    "lastMouth:(l._lastMouth===undefined?null:l._lastMouth),"
    "talking:!!l._talking,idle:!!l._isIdle,"
    "expr:(em&&em.currentExpression)?(em.currentExpression.name||null):null};})()"
)



def _pick_emotion_item(renderer):
    """从模型 profile 里挑一个带**非零参数**的情绪条目。

    用于验证“表情真的生效”：切一个情绪后，对应的 core 参数应该从 0 变成那个值。
    """
    try:
        cat = renderer.profile.emotion_category()
        if cat is None:
            return None
        fallback = None
        for it in cat.items:
            if not it.params:
                continue
            if fallback is None:
                fallback = it
            for _pid, val in it.params.items():
                try:
                    if float(val) != 0.0:
                        return it
                except Exception:  # noqa: BLE001
                    continue
        return fallback
    except Exception:  # noqa: BLE001
        return None


def _pix_diff(pm1, pm2):
    """两张截图的差异像素占比（抽样统计）——模型在动 → 画面在变。"""
    if pm1 is None or pm2 is None:
        return None
    i1 = pm1.toImage()
    i2 = pm2.toImage()
    w = min(i1.width(), i2.width())
    h = min(i1.height(), i2.height())
    if w <= 0 or h <= 0:
        return None
    diff = 0
    total = 0
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            total += 1
            if i1.pixel(x, y) != i2.pixel(x, y):
                diff += 1
    return {"diff_pixels": diff, "sampled_pixels": total,
            "diff_ratio": round(diff / total, 4) if total else 0.0}


def _save_shot(pixmap, name):
    """保存截图到 data/measure（测量证据）。"""
    try:
        d = Path(__file__).resolve().parent.parent / "data" / "measure"
        d.mkdir(parents=True, exist_ok=True)
        pixmap.save(str(d / name))
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------
# 进程树采样
# --------------------------------------------------------------------------
MAIN_PID = os.getpid()


def _tree(proc):
    """pid -> psutil.Process（含自身与全部子孙）。"""
    out = {proc.pid: proc}
    try:
        for kid in proc.children(recursive=True):
            out[kid.pid] = kid
    except Exception:  # noqa: BLE001
        pass
    return out


def _sample(proc):
    """采样进程树：每个 pid 的 (name, rss, cpu_seconds)。"""
    rows = {}
    for pid, p in _tree(proc).items():
        try:
            with p.oneshot():
                mi = p.memory_info()
                ct = p.cpu_times()
                priv = 0
                try:
                    mi2 = p.memory_full_info()
                    priv = int(getattr(mi2, "private", 0) or 0)
                except Exception:  # noqa: BLE001
                    priv = 0
            rows[pid] = {
                "name": p.name(),
                "rss": int(mi.rss),
                "priv": priv,
                "cpu": float(ct.user + ct.system),
            }
        except Exception:  # noqa: BLE001
            continue
    return rows


def _delta(before, after, wall_s):
    """两次采样之间的 CPU% / 内存。"""
    total_cpu = 0.0
    main_cpu = 0.0
    rss = 0
    priv = 0
    per = []
    for pid, row in after.items():
        old = before.get(pid)
        rss += row["rss"]
        priv += row.get("priv", 0)
        d = 0.0
        if old is not None:
            d = max(0.0, row["cpu"] - old["cpu"])
        total_cpu += d
        if pid == MAIN_PID:
            main_cpu = d
        per.append({
            "pid": pid,
            "name": row["name"],
            "rss_mb": round(row["rss"] / 1048576, 1),
            "private_mb": round(row.get("priv", 0) / 1048576, 1),
            "cpu_pct": round(d / wall_s * 100, 2) if wall_s > 0 else 0.0,
        })
    return {
        "process_count": len(after),
        "rss_mb": round(rss / 1048576, 1),
        "private_mb": round(priv / 1048576, 1),
        "cpu_pct_total": round(total_cpu / wall_s * 100, 2) if wall_s > 0 else 0.0,
        "cpu_pct_main": round(main_cpu / wall_s * 100, 2) if wall_s > 0 else 0.0,
        "per_process": sorted(per, key=lambda x: -x["rss_mb"]),
    }


# --------------------------------------------------------------------------
# 子进程：真正跑渲染器
# --------------------------------------------------------------------------
def _child_main(args) -> int:
    import psutil  # noqa: PLC0415

    os.environ.setdefault("OMP_NUM_THREADS", "1")
    from app.core.webengine_env import configure_webengine_env  # noqa: PLC0415

    configure_webengine_env(trim=(args.chromium_trim == "on"))
    # 唯一 user-data-dir：父进程据此认领并清理残留的 Chromium 进程
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] += (
        f" --user-data-dir={args.user_data_dir}")

    from PyQt5.QtCore import Qt, QCoreApplication  # noqa: PLC0415

    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    from app.core.qt_compat import QApplication  # noqa: PLC0415

    app = QApplication([sys.argv[0]])

    from PyQt5.QtCore import QTimer  # noqa: PLC0415

    result = {
        "renderer": args.renderer,
        "label": args.label,
        "model_dir": args.model_dir,
        "ok": False,
        "error": None,
    }
    me = psutil.Process()
    renderer = None

    def _js(expr, cb):
        try:
            renderer._page.runJavaScript(expr, cb)
        except Exception:  # noqa: BLE001
            cb(None)

    def _write_result():
        try:
            Path(args.out).write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass

    def _finish(code=0):
        # ⚠️ 必须先写盘再 app.quit()：quit() 之后事件循环退出，
        # 挂在队列里的定时器不会再触发（延迟写盘会丢结果）。
        result["ok"] = (code == 0)
        _write_result()
        try:
            if renderer is not None:
                renderer.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            app.quit()
        except Exception:  # noqa: BLE001
            pass
        os._exit(code)

    try:
        t0 = time.perf_counter()
        if args.renderer == "live2d":
            from app.animation.live2d_renderer import Live2DRenderer  # noqa: PLC0415

            _kwargs = dict(
                model_dir=args.model_dir,
                widget_size=(500, 500),
                scale=0.5,
                max_fps=args.max_fps,
            )
            if args.idle_fps:
                try:   # 旧版渲染器没有 idle_fps 参数（改造前基线量官方时跳过）
                    renderer = Live2DRenderer(idle_fps=args.idle_fps, **_kwargs)
                except TypeError:
                    renderer = Live2DRenderer(**_kwargs)
            else:
                renderer = Live2DRenderer(**_kwargs)
        else:
            from app.animation.sprite_renderer import SpriteRenderer  # noqa: PLC0415
            from app.core.qt_compat import QSize  # noqa: PLC0415

            renderer = SpriteRenderer(
                sprite_dir=Path(args.sprite_dir),
                fallback_image=(Path(args.sprite_dir) / "fallback.png"),
                window_size=QSize(500, 500),
                scale=0.5,
            )
        result["t_construct_ms"] = round((time.perf_counter() - t0) * 1000, 1)

        # 首帧时机以 Python 侧的 _on_model_ready 回调为准（权威事件）：
        # JS 轮询的回调在页面加载期间会排队（模型加载阻塞 JS 线程），
        # 回调可能比真实就绪晚一个多秒才回来。
        if args.renderer == "live2d":
            _orig_ready = renderer._on_model_ready

            def _ready(expressions_json):
                if not result.get("ready"):
                    result["t_ready_ms"] = round(
                        (time.perf_counter() - t0) * 1000, 1)
                    result["t_first_frame_ms"] = result["t_ready_ms"]
                    result["ready"] = True
                _orig_ready(expressions_json)

            renderer._on_model_ready = _ready
    
        widget = renderer.get_widget()
        if widget is not None:
            try:
                from PyQt5.QtCore import Qt as _Qt

                widget.setAttribute(_Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
                widget.setWindowFlag(_Qt.WindowType.WindowDoesNotAcceptFocus, True)
            except Exception:  # noqa: BLE001
                pass
            widget.show()
    
        # ---- 等 ready（live2d 等 JS 侧 ticker 真的跑起来）----
        state = {"ready": False, "t_ready": None, "deadline": time.perf_counter() + 45}

        def _poll():
            if state["ready"] or time.perf_counter() > state["deadline"]:
                _on_ready()
                return
            _js(JS_READY, lambda v: _on_js_ready(v))

        def _on_js_ready(v):
            if v in (1, True, 1.0):
                _on_ready()
            else:
                QTimer.singleShot(30, _poll)

        def _on_ready():
            if state["ready"]:
                return
            state["ready"] = True
            if not result.get("ready"):
                # 兜底：模型还没报 ready（JS 轮询先看到了）
                result["t_ready_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                result["t_first_frame_ms"] = result["t_ready_ms"]
                result["ready"] = True
            QTimer.singleShot(300, _start_idle)

        if args.renderer == "live2d":
            QTimer.singleShot(30, _poll)
        else:
            QTimer.singleShot(300, _on_ready)

        def _phase(name, seconds, next_fn):
            """采样一个阶段（seconds 秒）的进程树占用。"""
            snap0 = _sample(me)
            t_start = time.perf_counter()

            def _tick():
                if time.perf_counter() - t_start >= seconds:
                    snap1 = _sample(me)
                    wall = time.perf_counter() - t_start
                    result[name] = _delta(snap0, snap1, wall)
                    result[name]["seconds"] = round(wall, 2)
                    next_fn()
                    return
                QTimer.singleShot(250, _tick)

            QTimer.singleShot(50, _tick)

        def _start_idle():
            _phase("idle", args.seconds, _idle_done)

        def _idle_done():
            # 动画证据 A：截两帧图做像素差分（模型真的在动 = 画面在变）
            shot1 = None
            try:
                shot1 = renderer.get_widget().grab()
            except Exception as e:  # noqa: BLE001
                result["shot_error"] = repr(e)

            def _shot2():
                try:
                    shot2 = renderer.get_widget().grab()
                    result["anim_pixels"] = _pix_diff(shot1, shot2)
                    _save_shot(shot1, "anim_a.png")
                    _save_shot(shot2, "anim_b.png")
                except Exception as e:  # noqa: BLE001
                    result["shot_error"] = repr(e)

                def _js_a(v):
                    result.setdefault("js", {})["a"] = v
                    QTimer.singleShot(1500, lambda: _js(JS_STATE, _js_b))

                def _js_b(v):
                    result.setdefault("js", {})["b"] = v
                    _start_interact()

                _js(JS_STATE, _js_a)

            QTimer.singleShot(1000, _shot2)

        def _start_interact():
            if args.renderer == "live2d":
                try:
                    renderer.set_talking(True)
                    renderer.look_at(0.6, 0.2)
                    exprs = renderer.list_expressions()
                    if exprs:
                        renderer.set_emotion(exprs[0])
                    result["emotion_applied"] = getattr(renderer, "_current_emotion", None)
                    result["expressions_count"] = len(exprs)
                    # 表情证据：切一个 profile 情绪条目，看核心参数是否真的变了
                    item = _pick_emotion_item(renderer)
                    if item is not None:
                        pid_ = next(iter(item.params.keys()))
                        result["emotion_item"] = item.name
                        result["emotion_param"] = pid_

                        def _after_set(v):
                            result["emotion_param_before"] = v
                            renderer.set_emotion(item.name)
                            QTimer.singleShot(700, lambda: _js(
                                JS_PARAM % json.dumps(pid_),
                                lambda v2: result.__setitem__("emotion_param_after", v2)))

                        _js(JS_PARAM % json.dumps(pid_), _after_set)
                except Exception as e:  # noqa: BLE001
                    result["interact_error"] = repr(e)
                # 口型证据：说话期间抓一次口型峰值
                QTimer.singleShot(900, lambda: _js(
                    JS_STATE,
                    lambda v: result.setdefault("js", {}).__setitem__("talking", v)))
            _phase("interact", max(3.0, args.seconds / 2), _interact_done)

        def _interact_done():
            if args.renderer == "live2d":
                try:
                    renderer.set_talking(False)
                except Exception:  # noqa: BLE001
                    pass
                def _mouth(v):
                    result.setdefault("js", {})["mouth"] = v
                    _finish(0)
                QTimer.singleShot(200, lambda: _js(JS_STATE, _mouth))
            else:
                _finish(0)

        QTimer.singleShot(90000, lambda: _finish(3))
        # 跑事件循环（_finish 里会写结果并 os._exit）
        app.exec()

    except Exception as e:  # noqa: BLE001
        result["error"] = repr(e)
        import traceback

        result["traceback"] = traceback.format_exc()
        _write_result()
        return 1

    return 0


# --------------------------------------------------------------------------
# 父进程：调度 + 清理
# --------------------------------------------------------------------------
def _kill_tree(proc, psutil_mod) -> None:
    """杀掉子进程及其全部子孙。"""
    try:
        kids = proc.children(recursive=True)
    except Exception:  # noqa: BLE001
        kids = []
    for k in kids:
        try:
            k.kill()
        except Exception:  # noqa: BLE001
            pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass
    gone, alive = psutil_mod.wait_procs(kids + [proc], timeout=5)
    for a in alive:
        try:
            a.kill()
        except Exception:  # noqa: BLE001
            pass


def _kill_leftovers(marker: str) -> list[str]:
    """按 --user-data-dir 标记清理残留的 Chromium 进程。"""
    import psutil

    killed = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmd = " ".join(p.info.get("cmdline") or [])
            if marker in cmd:
                killed.append(f"{p.info['name']}({p.info['pid']})")
                p.kill()
        except Exception:  # noqa: BLE001
            continue
    return killed


def _default_model_dir() -> str:
    """从 config.yaml 读 pet.live2d.model_dir（只读 pet 段，不碰 llm/api_key）。"""
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


def _run_case(renderer: str, args) -> dict:
    import psutil
    import tempfile

    with tempfile.TemporaryDirectory(prefix="dp_measure_") as tmp:
        tmpdir = Path(tmp)
        out = tmpdir / f"{renderer}.json"
        udd = tmpdir / "chromium"
        cmd = [
            sys.executable, str(Path(__file__).resolve()),
            "--child", "--renderer", renderer, "--out", str(out),
            "--user-data-dir", str(udd),
            "--seconds", str(args.seconds),
            "--max-fps", str(args.max_fps),
            "--idle-fps", str(args.idle_fps),
            "--label", args.label,
            "--chromium-trim", args.chromium_trim,
        ]
        if renderer == "live2d":
            cmd += ["--model-dir", args.model_dir]
        else:
            cmd += ["--sprite-dir", args.sprite_dir]
        env = dict(os.environ)
        env.pop("QTWEBENGINE_CHROMIUM_FLAGS", None)
        proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env)
        try:
            proc.wait(timeout=args.timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc, psutil)
            proc.wait(timeout=10)
        data = {}
        if out.is_file():
            try:
                data = json.loads(out.read_text(encoding="utf-8"))
            except Exception as e:  # noqa: BLE001
                data = {"error": f"读取结果失败: {e!r}"}
        else:
            data = {"error": f"子进程无结果（退出码 {proc.returncode}）"}
        # 清理：本用例的 Chromium 残留
        killed = _kill_leftovers(str(udd))
        if killed:
            data["leftovers_killed"] = killed
        return data


def _fmt_row(name: str, d: dict) -> str:
    idle = d.get("idle") or {}
    inter = d.get("interact") or {}
    return (f"{name:<10} {d.get('process_count', '?'):>3} "
            f"{d.get('rss_mb', '?'):>8} {d.get('cpu_pct_total', '?'):>8} "
            f"{d.get('t_ready_ms', '?'):>9}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Live2D / sprite 成本测量")
    ap.add_argument("--renderer", choices=["live2d", "sprite", "both"], default="both")
    ap.add_argument("--model-dir", default=_default_model_dir())
    ap.add_argument("--sprite-dir", default=str(ROOT / "assets" / "sprites"))
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--max-fps", type=int, default=30)
    ap.add_argument("--idle-fps", type=int, default=0,
                    help="静置帧率（0 = 用默认；旧版渲染器会自动忽略）")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--label", default="run")
    ap.add_argument("--chromium-trim", choices=["on", "off"], default="on",
                    help="off = 只用 GPU flag（改造前基线）；on = 加 Chromium 瘦身 flag")
    ap.add_argument("--json", default="")
    ap.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--out", default="", help=argparse.SUPPRESS)
    ap.add_argument("--user-data-dir", default="", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.child:
        return _child_main(args)

    targets = ["live2d", "sprite"] if args.renderer == "both" else [args.renderer]
    out = {}
    for t in targets:
        print(f"[measure] 测量 {t} ...", flush=True)
        d = _run_case(t, args)
        out[t] = d
        idle = d.get("idle") or {}
        inter = d.get("interact") or {}
        print(f"[measure] {t}: ready={d.get('ready')} "
              f"t_ready={d.get('t_ready_ms')}ms "
              f"进程={idle.get('process_count')} "
              f"内存={idle.get('rss_mb')}MB "
              f"待机CPU={idle.get('cpu_pct_total')}% "
              f"交互CPU={inter.get('cpu_pct_total')}%", flush=True)

    if args.json:
        p = Path(args.json)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[measure] 结果写入 {p}")

    print()
    print("| 渲染器 | 进程数 | 内存MB | 待机CPU% | 交互CPU% | 首帧ms |")
    print("|---|---|---|---|---|---|")
    for t in targets:
        d = out.get(t) or {}
        idle = d.get("idle") or {}
        inter = d.get("interact") or {}
        print(f"| {t} | {idle.get('process_count','?')} | {idle.get('rss_mb','?')} "
              f"| {idle.get('cpu_pct_total','?')} | {inter.get('cpu_pct_total','?')} "
              f"| {d.get('t_ready_ms','?')} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
