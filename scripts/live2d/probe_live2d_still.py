# -*- coding: utf-8 -*-
"""验证：鼠标不动时模型是否真的静止（抓两帧比像素差）。

用法: python scripts/live2d/probe_live2d_still.py --model-dir assets/live2d/DeepSeek/DeepSeek
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_here = Path(__file__).resolve()
ROOT = next((p for p in (_here.parent, _here.parent.parent,
                         _here.parent.parent.parent)
             if (p / "app" / "animation").is_dir()), _here.parent.parent)
sys.path.insert(0, str(ROOT))

JS_READY = ("(function(){var l=window.live2d;if(!l||!l.ready||!l.app||!l.app.ticker)"
            "return 0;return l.app.ticker.elapsedMS>0?1:0;})()")

JS_SNAP = (
    "(function(){var l=window.live2d;if(!l||!l.model)return null;"
    "var im=l.model.internalModel,c=im.coreModel;"
    "function pv(id){try{var v=c.getParameterValueById(id);"
    "return (typeof v==='number')?Math.round(v*1000)/1000:null;}catch(e){return 'ERR';}}"
    "var s=window.__snap||{};"
    "return {AngleX:pv('AngleX'),AngleY:pv('AngleY'),"
    "r_ParamAngleX:s.ParamAngleX,r_ParamAngleY:s.ParamAngleY,"
    "r_ParamBodyAngleX:s.ParamBodyAngleX,r_ParamBodyAngleY:s.ParamBodyAngleY,"
    "r_ParamBodyAngleZ:s.ParamBodyAngleZ,r_ParamBreath:s.ParamBreath,"
    "r_ChestY1:s.ChestY1,r_ChestX1:s.ChestX1,"
    "r_SkirtY1:s.SkirtY1,r_SkirtX1:s.SkirtX1,"
    "r_FrontHairX1:s.FrontHairX1,r_SideHairX1:s.SideHairX1,"
    "r_Tail84:s.Tail84,r_LArm:s.LArm,r_HipAngleZ:s.HipAngleZ};})()"
)

# 渲染瞬间的参数快照（物理刚算完，还没被帧末复位）
JS_INSTALL_SNAP = (
    "(function(){var l=window.live2d,im=l.model.internalModel,c=im.coreModel;"
    "if(im.__snapHooked)return 1;im.__snapHooked=true;"
    "im.on('beforeModelUpdate',function(){"
    "function pv(id){try{var v=c.getParameterValueById(id);"
    "return (typeof v==='number')?Math.round(v*1000)/1000:null;}catch(e){return 'ERR';}}"
    "window.__snap={ParamAngleX:pv('ParamAngleX'),ParamAngleY:pv('ParamAngleY'),"
    "ParamBodyAngleX:pv('ParamBodyAngleX'),ParamBodyAngleY:pv('ParamBodyAngleY'),"
    "ParamBodyAngleZ:pv('ParamBodyAngleZ'),ParamBreath:pv('ParamBreath'),"
    "ChestY1:pv('ChestY1'),ChestX1:pv('ChestX1'),SkirtY1:pv('SkirtY1'),"
    "SkirtX1:pv('SkirtX1'),FrontHairX1:pv('FrontHairX1'),SideHairX1:pv('SideHairX1'),"
    "Tail84:pv('Param_Angle_Rotation84'),LArm:pv('LArm'),HipAngleZ:pv('HipAngleZ')};"
    "});return 1;})()"
)


def _diff(pm1, pm2):
    i1, i2 = pm1.toImage(), pm2.toImage()
    w, h = min(i1.width(), i2.width()), min(i1.height(), i2.height())
    d = t = 0
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            t += 1
            if i1.pixel(x, y) != i2.pixel(x, y):
                d += 1
    return round(d / t, 4) if t else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--out", default=str(ROOT / "data" / "measure" / "still_probe"))
    ap.add_argument("--no-breath", action="store_true",
                    help="关掉 pixi 的 breath 管理器（看剩下的运动是不是模型自带呼吸）")
    args = ap.parse_args()
    md = Path(args.model_dir)
    if not md.is_absolute():
        md = ROOT / md
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("OMP_NUM_THREADS", "1")
    with tempfile.TemporaryDirectory(prefix="dp_still_") as tmp:
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
        out = {"pairs": []}
        shots = []

        def _js(code, cb):
            try:
                renderer._page.runJavaScript(code, cb)
            except Exception:
                cb(None)

        def _grab(i):
            try:
                pm = renderer.get_widget().grab()
                pm.save(str(out_dir / f"still_{i}.png"))
                shots.append(pm)
            except Exception as e:
                out.setdefault("errors", []).append(repr(e))

        def _step(i):
            # 每隔 1.5s 抓一帧，共 5 帧；鼠标全程不动
            if i >= 5:
                _finish()
                return
            _js(JS_INSTALL_SNAP, lambda v: None)
            QTimer.singleShot(1500, lambda: _grab(i))
            QTimer.singleShot(1600, lambda: (_js(JS_SNAP, lambda v: out.setdefault(
                "snaps", []).append(v)), _step(i + 1)))

        def _finish():
            pairs = []
            for i in range(len(shots) - 1):
                pairs.append({"gap_s": 1.5, "diff_ratio": _diff(shots[i], shots[i + 1])})
            out["pairs"] = pairs
            out["max_diff"] = max((p["diff_ratio"] for p in pairs), default=0)
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
        if args.no_breath:
            _js("(function(){var l=window.live2d;if(l&&l.model)"
                "{l.model.internalModel.breath=null;}return 1;})()", lambda v: None)
        deadline = time.perf_counter() + 120

        def _poll():
            if time.perf_counter() > deadline:
                _finish()
                return
            _js(JS_READY, lambda v: (QTimer.singleShot(60, _poll)
                                     if v not in (1, True, 1.0)
                                     else (QTimer.singleShot(300, lambda: _step(0)))))

        QTimer.singleShot(50, _poll)
        QTimer.singleShot(150000, lambda: _finish())
        app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
