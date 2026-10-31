#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""探针：Live2D 模型的「身体到底能不能动」。

回答三个问题（纯读数据 + 抓图对比，不改模型文件）：
  1. 物理链有没有把「头」传到「身体」参数（ParamBodyAngleX → 胸/盆骨/裙/手臂）；
  2. 关掉物理后，直接把身体参数打满，画面到底变不变
     —— 变了 = 网格真的绑在身体参数上（程序侧能救）；
     不变 = 身体是刚性网格/没绑定（得回 Cubism 重绑，PS 只能改图不能加绑定）；
  3. 差异集中在画面哪一块（判断动的是身体还是只有头发）。

用法：
    python scripts/live2d/probe_live2d_body.py --model-dir assets/live2d/DeepSeek/DeepSeek
    python scripts/live2d/probe_live2d_body.py --model-dir <dir> --out data/measure/body_probe
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 关注的参数：头 → 身体 → 末端（胸/盆骨/裙/手臂/尾巴）
WATCH = [
    "ParamAngleX", "ParamAngleY", "ParamAngleZ",
    "ParamBodyAngleX", "ParamBodyAngleY", "ParamBodyAngleZ",
    "ParamBodyAngleX2", "ParamBodyAngleY2", "ParamBodyAngleRX", "ParamBodyAngleRZ",
    "ParamBreath",
    "ChestX1", "ChestY1", "ChestY4",
    "ShoulderY", "ShoulderZ",
    "HipAngleX", "HipAngleY", "HipAngleZ",
    "SkirtX1", "SkirtY1",
    "LArm", "RArm",
    "Param_Angle_Rotation84",
    "FrontHairX1",
]

JS_READ_PARAMS = (
    "(function(){var l=window.live2d;if(!l||!l.model)return null;"
    "var c=l.model.internalModel&&l.model.internalModel.coreModel;if(!c)return null;"
    "var ids=%s;var o={};for(var i=0;i<ids.length;i++){try{"
    "var v=c.getParameterValueById(ids[i]);"
    "o[ids[i]]=(typeof v==='number')?Math.round(v*1000)/1000:null;}"
    "catch(e){o[ids[i]]='ERR';}}return o;})()"
) % json.dumps(WATCH)

JS_READY = (
    "(function(){var l=window.live2d;if(!l||!l.ready||!l.app||!l.app.ticker)return 0;"
    "return l.app.ticker.elapsedMS>0?1:0;})()"
)


def _pix_diff(pm1, pm2):
    """两张图的差异像素占比 + 差异区域包围盒（相对坐标 0~1）。"""
    if pm1 is None or pm2 is None:
        return None
    i1, i2 = pm1.toImage(), pm2.toImage()
    w, h = min(i1.width(), i2.width()), min(i1.height(), i2.height())
    if w <= 0 or h <= 0:
        return None
    diff = total = 0
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            total += 1
            if i1.pixel(x, y) != i2.pixel(x, y):
                diff += 1
                if x < minx: minx = x
                if y < miny: miny = y
                if x > maxx: maxx = x
                if y > maxy: maxy = y
    box = None
    if maxx >= 0:
        box = {
            "x0": round(minx / w, 3), "y0": round(miny / h, 3),
            "x1": round(maxx / w, 3), "y1": round(maxy / h, 3),
        }
    return {"diff_ratio": round(diff / total, 4) if total else 0.0,
            "diff_pixels": diff, "sampled": total, "box": box}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--out", default=str(ROOT / "data" / "measure" / "body_probe"))
    ap.add_argument("--size", type=int, default=600)
    args = ap.parse_args()

    model_dir = Path(args.model_dir)
    if not model_dir.is_absolute():
        model_dir = ROOT / model_dir
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("OMP_NUM_THREADS", "1")
    with tempfile.TemporaryDirectory(prefix="dp_bodyprobe_") as tmp:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
            os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
            + f" --user-data-dir={Path(tmp) / 'chromium'}")
        return _run(model_dir, out_dir, args.size)


def _run(model_dir: Path, out_dir: Path, size: int) -> int:
    from app.core.webengine_env import configure_webengine_env  # noqa: PLC0415

    configure_webengine_env(trim="on")
    from PyQt5.QtCore import Qt, QTimer, QCoreApplication  # noqa: PLC0415

    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    from app.core.qt_compat import QApplication  # noqa: PLC0415

    app = QApplication([sys.argv[0]])
    from app.animation.live2d_renderer import Live2DRenderer  # noqa: PLC0415

    renderer = Live2DRenderer(model_dir=str(model_dir),
                              widget_size=(size, size), scale=0.5)
    result: dict = {"model_dir": str(model_dir), "states": {}}
    shots: dict = {}

    def _js(code, cb):
        try:
            renderer._page.runJavaScript(code, cb)
        except Exception:  # noqa: BLE001
            cb(None)

    def _grab(name):
        try:
            pm = renderer.get_widget().grab()
            pm.save(str(out_dir / f"{name}.png"))
            shots[name] = pm
        except Exception as e:  # noqa: BLE001
            result.setdefault("errors", []).append(f"grab {name}: {e!r}")

    def _sample(name, js_setup=None, wait_ms=700):
        """执行一段 JS 设置 → 等 → 抓图 + 读参数。"""
        def _after_setup(_v):
            QTimer.singleShot(wait_ms, lambda: _after_wait(name))

        def _after_wait(nm):
            _grab(nm)
            _js(JS_READ_PARAMS, lambda v: _after_params(nm, v))

        def _after_params(nm, v):
            result["states"][nm] = {"params": v}
            if nm in ("rest",) and v:
                result["params_rest"] = v
            _next(nm)

        if js_setup:
            _js(js_setup, _after_setup)
        else:
            _after_setup(None)

    steps = []

    # 1) 静息：关掉呼吸噪声 + 焦点回中（物理保持开启）
    steps.append((
        "rest",
        "(function(){var l=window.live2d,m=l.model.internalModel;"
        "m.breath=null;l.focus(0,0);return 1;})()", 900))

    # 2) 头转右：物理链是否把头带进身体参数
    steps.append(("head_right", "(function(){window.live2d.focus(1,0);return 1;})()", 1800))
    # 3) 头转左
    steps.append(("head_left", "(function(){window.live2d.focus(-1,0);return 1;})()", 1800))
    # 4) 焦点回中
    steps.append(("head_center", "(function(){window.live2d.focus(0,0);return 1;})()", 1800))

    # 5~8) 关物理，单独把身体参数打满（网格是否真的绑在这些参数上）
    steps.append((
        "phys_off",
        "(function(){var l=window.live2d,m=l.model.internalModel;"
        "l.__phys=m.physics;m.physics=null;m.breath=null;l.focus(0,0);return 1;})()", 900))
    steps.append((
        "body_x",
        "(function(){window.live2d.setParam('ParamBodyAngleX',25,0);return 1;})()", 700))
    steps.append((
        "body_z",
        "(function(){window.live2d.setParam('ParamBodyAngleX',0,0);"
        "window.live2d.setParam('ParamBodyAngleZ',25,0);return 1;})()", 700))
    steps.append((
        "body_y",
        "(function(){window.live2d.setParam('ParamBodyAngleZ',0,0);"
        "window.live2d.setParam('ParamBodyAngleY',15,0);return 1;})()", 700))
    steps.append((
        "chest_direct",
        "(function(){window.live2d.setParam('ParamBodyAngleY',0,0);"
        "window.live2d.setParam('ChestX1',30,0);"
        "window.live2d.setParam('ShoulderZ',20,0);return 1;})()", 700))
    # 9) 恢复物理，验证物理开着时是否还在压制身体参数
    steps.append((
        "phys_on",
        "(function(){var l=window.live2d,m=l.model.internalModel;"
        "if(l.__phys)m.physics=l.__phys;return 1;})()", 900))

    idx = {"i": 0}

    def _next(done_name: str = "") -> None:
        if idx["i"] >= len(steps):
            _finish()
            return
        nm, js, wait = steps[idx["i"]]
        idx["i"] += 1
        _sample(nm, js, wait)

    def _finish():
        base = shots.get("rest")
        result["diffs"] = {}
        for nm, pm in shots.items():
            if nm == "rest" or base is None:
                continue
            result["diffs"][nm] = _pix_diff(base, pm)
        # 头转左右相对静息的差异（物理链驱动）
        result["out"] = str(out_dir)
        (out_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            renderer.shutdown()
        except Exception:  # noqa: BLE001
            pass
        app.quit()
        os._exit(0)

    widget = renderer.get_widget()
    try:
        widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        widget.setWindowFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
    except Exception:  # noqa: BLE001
        pass
    widget.show()

    state = {"deadline": time.perf_counter() + 120}

    def _poll():
        if time.perf_counter() > state["deadline"]:
            result["error"] = "模型未在 120s 内 ready"
            _finish()
            return
        _js(JS_READY, lambda v: (QTimer.singleShot(60, _poll)
                                  if v not in (1, True, 1.0)
                                  else QTimer.singleShot(300, _next)))

    QTimer.singleShot(50, _poll)
    QTimer.singleShot(180000, lambda: _finish())
    app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
