# -*- coding: utf-8 -*-
"""探针：手臂参数到底绑没绑在网格上（直接打满 LArm/LForearm/LWrist 看画面变不变）。

用法: python scripts/probe_live2d_arm_binding.py --model-dir assets/live2d/DeepSeek/DeepSeek
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
sys.path.insert(0, str(ROOT))

JS_READY = "(function(){var l=window.live2d;return (l&&l.ready)?1:0;})()"

JS_SET = ("window.live2d.setParam('LArm',%s,0);"
          "window.live2d.setParam('LForearm',%s,0);"
          "window.live2d.setParam('LWrist',%s,0);"
          "window.live2d.setParam('RArm',%s,0);"
          "window.live2d.setParam('RForearm',%s,0);"
          "window.live2d.setParam('RWrist',%s,0);")

JS_READ = (
    "(function(){var l=window.live2d;if(!l||!l.model)return null;"
    "var c=l.model.internalModel.coreModel;"
    "function pv(id){try{var v=c.getParameterValueById(id);"
    "return (typeof v==='number')?Math.round(v*1000)/1000:null;}catch(e){return 'ERR';}}"
    "return {LArm:pv('LArm'),LForearm:pv('LForearm'),LWrist:pv('LWrist'),"
    "RArm:pv('RArm'),RForearm:pv('RForearm'),RWrist:pv('RWrist'),"
    "ParamBodyAngleX:pv('ParamBodyAngleX')};})()"
)


def _diff(pm1, pm2):
    i1, i2 = pm1.toImage(), pm2.toImage()
    w, h = min(i1.width(), i2.width()), min(i1.height(), i2.height())
    d = t = 0
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            t += 1
            if i1.pixel(x, y) != i2.pixel(x, y):
                d += 1
                minx, miny = min(minx, x), min(miny, y)
                maxx, maxy = max(maxx, x), max(maxy, y)
    box = None
    if maxx >= 0:
        box = [round(minx / w, 3), round(miny / h, 3), round(maxx / w, 3), round(maxy / h, 3)]
    return {"diff": round(d / t, 4) if t else 0, "box": box}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--out", default=str(ROOT / "data" / "measure" / "arm_binding"))
    args = ap.parse_args()
    md = Path(args.model_dir)
    if not md.is_absolute():
        md = ROOT / md
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("OMP_NUM_THREADS", "1")
    with tempfile.TemporaryDirectory(prefix="dp_armbind_") as tmp:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
            os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
            + f" --user-data-dir={Path(tmp) / 'chromium'}")
        from app.core.webengine_env import configure_webengine_env

        configure_webengine_env(trim="on")
        from PyQt5.QtCore import Qt, QTimer, QCoreApplication

        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
        from app.core.qt_compat import QApplication

        app = QApplication([sys.argv[0]])
        from app.animation.live2d_renderer import Live2DRenderer

        renderer = Live2DRenderer(model_dir=str(md), widget_size=(500, 500), scale=0.5)
        out = {"states": []}
        shots = {}

        def _js(code, cb):
            try:
                renderer._page.runJavaScript(code, cb)
            except Exception:
                cb(None)

        steps = [("rest", 0.0), ("arm_plus", 0.45), ("arm_minus", -0.45), ("rest2", 0.0)]
        idx = {"i": 0}

        def _step():
            if idx["i"] >= len(steps):
                _finish()
                return
            nm, val = steps[idx["i"]]
            idx["i"] += 1
            _js(JS_SET % ((val,) * 6), lambda v: None)
            QTimer.singleShot(900, lambda: _grab(nm))

        def _grab(nm):
            pm = renderer.get_widget().grab()
            pm.save(str(out_dir / ("%s.png" % nm)))
            shots[nm] = pm
            _js(JS_READ, lambda v: (out["states"].append({"state": nm, "v": v}),
                                     QTimer.singleShot(50, _step)))

        def _finish():
            base = shots.get("rest")
            out["diffs"] = {k: _diff(base, v) for k, v in shots.items()
                            if k != "rest" and base is not None}
            (out_dir / "result.json").write_text(
                json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps(out, ensure_ascii=False, indent=2))
            sys.stdout.flush()
            try:
                renderer.shutdown()
            except Exception:
                pass
            app.quit()
            os._exit(0)

        widget = renderer.get_widget()
        widget.show()
        deadline = time.perf_counter() + 120

        def _poll():
            if time.perf_counter() > deadline:
                _finish()
                return
            _js(JS_READY, lambda v: (QTimer.singleShot(60, _poll)
                                     if v not in (1, True, 1.0)
                                     else QTimer.singleShot(200, _step)))

        QTimer.singleShot(50, _poll)
        QTimer.singleShot(150000, lambda: _finish())
        app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
