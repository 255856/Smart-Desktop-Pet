#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""端到端验证：走真实渲染器 + 真实 look_at()，检查身体是不是真的跟着动。

截图：look_left / look_right / center / idle_a / idle_b
输出：data/measure/body_verify/result.json（含每帧真实参数 + 像素差异）
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

JS_READY = (
    "(function(){var l=window.live2d;if(!l||!l.ready||!l.app||!l.app.ticker)return 0;"
    "return l.app.ticker.elapsedMS>0?1:0;})()"
)

JS_SNAP = (
    "(function(){var l=window.live2d;if(!l||!l.model)return null;"
    "if(window.__snap)return window.__snap;"     # 渲染瞬间的真实值（物理链刚算完）
    "var im=l.model.internalModel,c=im.coreModel;"
    "function pv(id){try{var v=c.getParameterValueById(id);"
    "return (typeof v==='number')?Math.round(v*1000)/1000:null;}catch(e){return 'ERR';}}"
    "return {AngleX:pv('AngleX'),AngleY:pv('AngleY'),AngleZ:pv('AngleZ'),"
    "ParamAngleX:pv('ParamAngleX'),ParamBodyAngleX:pv('ParamBodyAngleX'),"
    "ParamBodyAngleY:pv('ParamBodyAngleY'),ParamBodyAngleZ:pv('ParamBodyAngleZ'),"
    "ChestX1:pv('ChestX1'),ChestY1:pv('ChestY1'),ShoulderZ:pv('ShoulderZ'),"
    "HipAngleZ:pv('HipAngleZ'),SkirtX1:pv('SkirtX1'),LArm:pv('LArm'),"
    "Param_Angle_Rotation84:pv('Param_Angle_Rotation84')};})()"
)

JS_INSTALL_SNAP = (
    "(function(){var l=window.live2d,im=l.model.internalModel,c=im.coreModel;"
    "if(im.__snapHooked)return 1;im.__snapHooked=true;"
    "im.on('beforeModelUpdate',function(){"
    "function pv(id){try{var v=c.getParameterValueById(id);"
    "return (typeof v==='number')?Math.round(v*1000)/1000:null;}catch(e){return 'ERR';}}"
    "window.__snap={AngleX:pv('AngleX'),AngleY:pv('AngleY'),"
    "ParamAngleX:pv('ParamAngleX'),ParamBodyAngleX:pv('ParamBodyAngleX'),"
    "ParamBodyAngleY:pv('ParamBodyAngleY'),ParamBodyAngleZ:pv('ParamBodyAngleZ'),"
    "ChestX1:pv('ChestX1'),ChestY1:pv('ChestY1'),ShoulderZ:pv('ShoulderZ'),"
    "HipAngleZ:pv('HipAngleZ'),SkirtX1:pv('SkirtX1'),LArm:pv('LArm'),"
    "Param_Angle_Rotation84:pv('Param_Angle_Rotation84')};});return 1;})()"
)


def _pix_diff(pm1, pm2):
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
                minx, miny = min(minx, x), min(miny, y)
                maxx, maxy = max(maxx, x), max(maxy, y)
    box = None
    if maxx >= 0:
        box = {"x0": round(minx / w, 3), "y0": round(miny / h, 3),
               "x1": round(maxx / w, 3), "y1": round(maxy / h, 3)}
    return {"diff_ratio": round(diff / total, 4) if total else 0.0,
            "box": box}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--out", default=str(ROOT / "data" / "measure" / "body_verify"))
    args = ap.parse_args()

    model_dir = Path(args.model_dir)
    if not model_dir.is_absolute():
        model_dir = ROOT / model_dir
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("OMP_NUM_THREADS", "1")
    with tempfile.TemporaryDirectory(prefix="dp_verify_") as tmp:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
            os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
            + f" --user-data-dir={Path(tmp) / 'chromium'}")
        return _run(model_dir, out_dir)


def _run(model_dir: Path, out_dir: Path) -> int:
    from app.core.webengine_env import configure_webengine_env  # noqa: PLC0415

    configure_webengine_env(trim="on")
    from PyQt5.QtCore import Qt, QTimer, QCoreApplication  # noqa: PLC0415

    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
    from app.core.qt_compat import QApplication  # noqa: PLC0415

    app = QApplication([sys.argv[0]])
    from app.animation.live2d_renderer import Live2DRenderer  # noqa: PLC0415

    renderer = Live2DRenderer(model_dir=str(model_dir),
                              widget_size=(600, 600), scale=0.5)
    result: dict = {"model_dir": str(model_dir), "states": {}}
    shots: dict = {}

    def _js(code, cb):
        try:
            renderer._page.runJavaScript(code, cb)
        except Exception:  # noqa: BLE001
            cb(None)

    def _shot(name):
        try:
            pm = renderer.get_widget().grab()
            pm.save(str(out_dir / f"{name}.png"))
            shots[name] = pm
        except Exception as e:  # noqa: BLE001
            result.setdefault("errors", []).append(repr(e))

    steps = [
        ("install_snap", None, 300),
        ("center", (0.0, 0.0), 2500),
        ("look_left", (-1.0, 0.0), 2500),
        ("look_right", (1.0, 0.0), 2500),
        ("look_up", (0.0, -1.0), 2500),
        ("back_center", (0.0, 0.0), 2500),
        ("idle_a", None, 200),
        ("idle_b", None, 2500),
    ]
    idx = {"i": 0}

    def _run_step(nm, look, wait):
        def _after(_v):
            QTimer.singleShot(wait, lambda: _after_wait(nm))

        def _after_wait(n):
            _shot(n)
            _js(JS_SNAP, lambda v: (result["states"].__setitem__(n, v),
                                     _after_state()))

        def _after_state():
            idx["i"] += 1
            if idx["i"] >= len(steps):
                QTimer.singleShot(100, _finish)
                return
            nm2, look2, wait2 = steps[idx["i"]]
            if look2 is not None:
                renderer.look_at(look2[0], look2[1])
            QTimer.singleShot(50, lambda: _run_step(nm2, look2, wait2))

        if nm == "install_snap":
            _js(JS_INSTALL_SNAP, lambda _v: None)
        if look is not None:
            renderer.look_at(look[0], look[1])
        _after(None)

    def _finish():
        base = shots.get("center")
        result["diffs"] = {k: _pix_diff(base, v) for k, v in shots.items()
                            if k != "center" and base is not None}
        (out_dir / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.stdout.flush()
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

    deadline = time.perf_counter() + 120

    def _poll():
        if time.perf_counter() > deadline:
            result["error"] = "模型未在 120s 内 ready"
            _finish()
            return
        _js(JS_READY, lambda v: (QTimer.singleShot(60, _poll)
                                  if v not in (1, True, 1.0)
                                  else QTimer.singleShot(300, lambda: _run_step(*steps[0]))))

    QTimer.singleShot(50, _poll)
    QTimer.singleShot(180000, lambda: _finish())
    app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
