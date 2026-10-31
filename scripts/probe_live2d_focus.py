#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""探针 2：两个关键机制问题。

Q1  Cubism 物理的输出会不会**覆盖**参数？
    → 物理开着时直接写 ParamAngleX / ParamBodyAngleX，读回来看数值。
Q2  为什么 focus() 之后 ParamAngleX 还是 0？
    → 直接调 model.focus()，把异常、focusController 的 x/targetX 都读出来。

用法：
    python scripts/probe_live2d_focus.py --model-dir assets/live2d/DeepSeek/DeepSeek
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

JS_STATE = (
    "(function(){var l=window.live2d;if(!l||!l.model)return null;"
    "var im=l.model.internalModel,c=im.coreModel,fc=im.focusController;"
    "function pv(id){try{var v=c.getParameterValueById(id);"
    "return (typeof v==='number')?Math.round(v*1000)/1000:null;}catch(e){return 'ERR';}}"
    "var mm=im.motionManager;var act=[];"
    "try{var gs=mm._motions||{};for(var k in gs){if(gs[k]&&gs[k]._playing)act.push(k);}}catch(e){}"
    "return {screen:[l.app?l.app.screen.width:0,l.app?l.app.screen.height:0],"
    "orig:[im.originalWidth,im.originalHeight],"
    "focusCtrl:fc?{x:fc.x,y:fc.y,tx:fc.targetX,ty:fc.targetY}:null,"
    "hasPhysics:!!im.physics,hasBreath:!!im.breath,"
    "motionGroups:act,"
    "ParamAngleX:pv('ParamAngleX'),ParamAngleY:pv('ParamAngleY'),"
    "ParamBodyAngleX:pv('ParamBodyAngleX'),ParamBodyAngleY:pv('ParamBodyAngleY'),"
    "ParamBodyAngleZ:pv('ParamBodyAngleZ'),ParamBreath:pv('ParamBreath'),"
    "AngleX:pv('AngleX'),AngleY:pv('AngleY'),AngleZ:pv('AngleZ'),"
    "PositionX:pv('PositionX'),PositionY:pv('PositionY'),PositionZ:pv('PositionZ'),"
    "Param52:pv('Param52'),Param53:pv('Param53'),"
    "snap:window.__snap||null};})()"
)

# 在渲染前抓一帧真实参数（beforeModelUpdate 就在 a.update() 之前）
JS_INSTALL_SNAP = (
    "(function(){var l=window.live2d,im=l.model.internalModel,c=im.coreModel;"
    "if(im.__snapHooked)return 1;im.__snapHooked=true;"
    "im.on('beforeModelUpdate',function(){"
    "function pv(id){try{var v=c.getParameterValueById(id);"
    "return (typeof v==='number')?Math.round(v*1000)/1000:null;}catch(e){return 'ERR';}}"
    "window.__snap={ParamAngleX:pv('ParamAngleX'),ParamAngleY:pv('ParamAngleY'),"
    "ParamBodyAngleX:pv('ParamBodyAngleX'),ParamBodyAngleY:pv('ParamBodyAngleY'),"
    "ParamBodyAngleZ:pv('ParamBodyAngleZ'),ParamBreath:pv('ParamBreath'),"
    "AngleX:pv('AngleX'),ChestX1:pv('ChestX1'),ChestY1:pv('ChestY1'),"
    "ShoulderY:pv('ShoulderY'),ShoulderZ:pv('ShoulderZ'),"
    "HipAngleX:pv('HipAngleX'),HipAngleZ:pv('HipAngleZ'),"
    "SkirtX1:pv('SkirtX1'),LArm:pv('LArm'),"
    "Param_Angle_Rotation84:pv('Param_Angle_Rotation84')};});return 1;})()"
)

# 直接调 model.focus，捕获异常
JS_FOCUS_DIRECT = (
    "(function(){var l=window.live2d;try{"
    "var w=l.app.screen.width,h=l.app.screen.height;"
    "l.model.focus(w/2+w/2,h/2);return {ok:1};}"
    "catch(e){return {ok:0,err:String(e&&(e.stack||e.message||e))};}})()"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--out", default=str(ROOT / "data" / "measure" / "focus_probe"))
    args = ap.parse_args()

    model_dir = Path(args.model_dir)
    if not model_dir.is_absolute():
        model_dir = ROOT / model_dir
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("OMP_NUM_THREADS", "1")
    with tempfile.TemporaryDirectory(prefix="dp_focusprobe_") as tmp:
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

    def _js(code, cb):
        try:
            renderer._page.runJavaScript(code, cb)
        except Exception:  # noqa: BLE001
            cb(None)

    def _grab(name):
        try:
            renderer.get_widget().grab().save(str(out_dir / f"{name}.png"))
        except Exception as e:  # noqa: BLE001
            result.setdefault("errors", []).append(f"grab {name}: {e!r}")

    steps = [
        ("default", JS_INSTALL_SNAP, 600),
        # Q1: 物理开着时直接写 ParamAngleX
        ("set_angleX_phys_on",
         "window.live2d.setParam('ParamAngleX',25,0);", 600),
        # 头到底绑在哪个参数上？试 AngleX(=OX)
        ("set_OX_phys_on",
         "window.live2d.setParam('ParamAngleX',0,0);"
         "window.live2d.setParam('AngleX',25,0);", 600),
        ("set_AngleYbs_phys_on",
         "window.live2d.setParam('AngleX',0,0);"
         "window.live2d.setParam('AngleYbs',25,0);", 600),
        # Q2: focus
        ("focus_direct", JS_FOCUS_DIRECT, 1500),
        ("focus_bridge", "window.live2d.focus(1,0);", 1500),
        # 关物理后再写，验证「物理在压制」
        ("phys_off",
         "(function(){var l=window.live2d,m=l.model.internalModel;"
         "m.__p=m.physics;m.physics=null;return 1;})()", 600),
        ("set_angleX_phys_off", "window.live2d.setParam('ParamAngleX',25,0);", 600),
        ("set_OX_phys_off",
         "window.live2d.setParam('ParamAngleX',0,0);"
         "window.live2d.setParam('AngleX',25,0);", 600),
        ("set_bodyX_phys_off",
         "window.live2d.setParam('AngleX',0,0);"
         "window.live2d.setParam('ParamBodyAngleX',25,0);", 600),
        ("phys_on",
         "(function(){var l=window.live2d,m=l.model.internalModel;"
         "if(m.__p)m.physics=m.__p;return 1;})()", 600),
        ("set_bodyX_phys_on",
         "window.live2d.setParam('ParamBodyAngleX',25,0);", 600),
    ]

    idx = {"i": 0}

    def _run_step(nm, js, wait):
        def _after(_v):
            QTimer.singleShot(wait, lambda: _after_wait(nm))

        def _after_wait(n):
            _grab(n)
            _js(JS_STATE, lambda v: _after_state(n, v))

        def _after_state(n, v):
            result["states"][n] = v
            if n == "focus_direct":
                _js(JS_FOCUS_DIRECT, lambda r: result.__setitem__("focus_direct_result", r))
                QTimer.singleShot(50, lambda: _next())
                return
            _next()

        if js:
            _js(js, _after)
        else:
            _after(None)

    def _next():
        if idx["i"] >= len(steps):
            _finish()
            return
        nm, js, wait = steps[idx["i"]]
        idx["i"] += 1
        _run_step(nm, js, wait)

    def _finish():
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

    deadline = time.perf_counter() + 120

    def _poll():
        if time.perf_counter() > deadline:
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
