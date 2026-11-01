# -*- coding: utf-8 -*-
"""探针：手臂/手参数的范围 + 跟随鼠标时实际摆了多少（读渲染瞬间的值）。

用法: python scripts/probe_live2d_arms.py --model-dir assets/live2d/DeepSeek/DeepSeek
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

WATCH = ["LArm", "LForearm", "LWrist", "RArm", "RForearm", "RWrist",
         "ParamBodyAngleX", "ParamBodyAngleY", "ParamBodyAngleZ",
         "ParamAngleX", "ParamBreath", "ChestX1", "ChestY1", "ShoulderZ",
         "ShoulderY", "SkirtX1", "SkirtY1", "HipAngleX", "HipAngleZ",
         "Param_Angle_Rotation84", "PupilX", "PupilY"]

JS_IDS = json.dumps(WATCH)

JS_RANGES = (
    "(function(){var l=window.live2d;if(!l||!l.model)return null;"
    "var c=l.model.internalModel.coreModel;"
    "var ids=%s;var out={};"
    "for(var i=0;i<ids.length;i++){var id=ids[i];try{"
    "var idx=c.getParameterIndex(id);"
    "out[id]={min:c.getParameterMinimumValue(idx),max:c.getParameterMaximumValue(idx),"
    "def:c.getParameterDefaultValue(idx)};}catch(e){out[id]='ERR:'+e;}}"
    "return out;})()"
) % JS_IDS

JS_SNAP = (
    "(function(){var l=window.live2d;if(!l||!l.model)return null;"
    "return window.__snap||null;})()"
)

JS_INSTALL_SNAP = (
    "(function(){var l=window.live2d,im=l.model.internalModel,c=im.coreModel;"
    "if(im.__snapHooked)return 1;im.__snapHooked=true;"
    "im.on('beforeModelUpdate',function(){"
    "function pv(id){try{var v=c.getParameterValueById(id);"
    "return (typeof v==='number')?Math.round(v*1000)/1000:null;}catch(e){return 'ERR';}}"
    "var o={};for(var i=0;i<%s.length;i++){o[%s[i]]=pv(%s[i]);}"
    "window.__snap=o;});return 1;})()"
) % (JS_IDS, JS_IDS, JS_IDS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--out", default=str(ROOT / "data" / "measure" / "arms_probe"))
    args = ap.parse_args()
    md = Path(args.model_dir)
    if not md.is_absolute():
        md = ROOT / md
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("OMP_NUM_THREADS", "1")
    with tempfile.TemporaryDirectory(prefix="dp_arms_") as tmp:
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
        out = {"ranges": {}, "snaps": []}
        shots = {}

        def _js(code, cb):
            try:
                renderer._page.runJavaScript(code, cb)
            except Exception:
                cb(None)

        steps = [("center", (0.0, 0.0), 1800), ("right", (1.0, 0.0), 1800),
                 ("left", (-1.0, 0.0), 1800), ("up", (0.0, -1.0), 1800),
                 ("center2", (0.0, 0.0), 1800)]
        idx = {"i": 0}

        def _step():
            if idx["i"] >= len(steps):
                _finish()
                return
            nm, look, wait = steps[idx["i"]]
            idx["i"] += 1
            renderer.look_at(look[0], look[1])
            QTimer.singleShot(wait, lambda: _grab(nm))

        def _grab(nm):
            try:
                pm = renderer.get_widget().grab()
                pm.save(str(out_dir / ("%s.png" % nm)))
                shots[nm] = pm
            except Exception as e:  # noqa: BLE001
                out.setdefault("errors", []).append(repr(e))
            _js(JS_SNAP, lambda v: (out["snaps"].append({"state": nm, "v": v}),
                                     QTimer.singleShot(50, _step)))

        def _finish():
            _js(JS_RANGES, lambda v: out.__setitem__("ranges", v))
            QTimer.singleShot(600, _report)

        def _report():
            rng = out.get("ranges") or {}
            (out_dir / "result.json").write_text(
                json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            print("参数范围:")
            for k, v in rng.items():
                if isinstance(v, dict):
                    print("  %-22s [%s .. %s] default=%s"
                          % (k, v.get("min"), v.get("max"), v.get("def")))
            print("\n各状态下的实际值（渲染瞬间）:")
            for s in out["snaps"]:
                v = s["v"] or {}
                print("  --", s["state"])
                for k in ("ParamBodyAngleX", "ParamBodyAngleY", "LArm", "LForearm",
                          "LWrist", "RArm", "RForearm", "RWrist", "ChestY1",
                          "ShoulderZ", "SkirtX1", "ParamBreath",
                          "Param_Angle_Rotation84"):
                    print("     %-22s %s" % (k, v.get(k)))
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
            _js("(function(){var l=window.live2d;return (l&&l.ready)?1:0;})()",
                lambda v: (QTimer.singleShot(60, _poll) if v not in (1, True, 1.0)
                            else QTimer.singleShot(200, lambda: _js(
                                JS_INSTALL_SNAP, lambda _x: _step()))))

        QTimer.singleShot(50, _poll)
        QTimer.singleShot(150000, lambda: _finish())
        app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
