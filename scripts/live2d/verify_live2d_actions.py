#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证本轮改动：三轴转头 / 脚钉住不动 / 动作能播。

用法：
    python scripts/live2d/verify_live2d_actions.py --model-dir assets/live2d/DeepSeek/DeepSeek
输出：data/measure/actions_verify/*.png + result.json
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
    "var im=l.model.internalModel,c=im.coreModel;"
    "function pv(id){try{var v=c.getParameterValueById(id);"
    "return (typeof v==='number')?Math.round(v*1000)/1000:null;}catch(e){return 'ERR';}}"
    "return {AngleX:pv('AngleX'),AngleY:pv('AngleY'),AngleZ:pv('AngleZ'),"
    "ParamAngleX:pv('ParamAngleX'),ParamAngleY:pv('ParamAngleY'),"
    "ParamAngleZ:pv('ParamAngleZ'),ParamBodyAngleX:pv('ParamBodyAngleX'),"
    "ParamBodyAngleY:pv('ParamBodyAngleY'),ParamBodyAngleZ:pv('ParamBodyAngleZ'),"
    "HipAngleX:pv('HipAngleX'),HipAngleZ:pv('HipAngleZ'),LEGAngleZ2:pv('LEGAngleZ2'),"
    "SkirtX1:pv('SkirtX1'),ChestX1:pv('ChestX1'),ShoulderZ:pv('ShoulderZ'),"
    "LArm:pv('LArm'),Param60:pv('Param60'),Param61:pv('Param61'),"
    "ParamEyeBallX:pv('ParamEyeBallX'),ParamEyeBallY:pv('ParamEyeBallY'),"
    "PupilX:pv('PupilX'),PupilY:pv('PupilY'),"
    "Movement2:pv('Movement2'),Movement3:pv('Movement3')};})()"
)

JS_INSTALL_SNAP = (
    "(function(){var l=window.live2d,im=l.model.internalModel,c=im.coreModel;"
    "if(im.__snapHooked)return 1;im.__snapHooked=true;"
    "im.on('beforeModelUpdate',function(){"
    "function pv(id){try{var v=c.getParameterValueById(id);"
    "return (typeof v==='number')?Math.round(v*1000)/1000:null;}catch(e){return 'ERR';}}"
    "window.__snap={AngleX:pv('AngleX'),AngleY:pv('AngleY'),AngleZ:pv('AngleZ'),"
    "ParamAngleX:pv('ParamAngleX'),ParamAngleY:pv('ParamAngleY'),"
    "ParamAngleZ:pv('ParamAngleZ'),ParamBodyAngleX:pv('ParamBodyAngleX'),"
    "ParamBodyAngleY:pv('ParamBodyAngleY'),ParamBodyAngleZ:pv('ParamBodyAngleZ'),"
    "HipAngleX:pv('HipAngleX'),HipAngleZ:pv('HipAngleZ'),LEGAngleZ2:pv('LEGAngleZ2'),"
    "SkirtX1:pv('SkirtX1'),ChestX1:pv('ChestX1'),ShoulderZ:pv('ShoulderZ'),"
    "LArm:pv('LArm'),Param60:pv('Param60'),Param61:pv('Param61'),"
    "Movement2:pv('Movement2'),Movement3:pv('Movement3')};});return 1;})()"
)


def _region_diff(pm1, pm2, y0: float = 0.0, y1: float = 1.0):
    """指定纵向区间的像素差（0~1 相对坐标）。"""
    if pm1 is None or pm2 is None:
        return None
    i1, i2 = pm1.toImage(), pm2.toImage()
    w, h = min(i1.width(), i2.width()), min(i1.height(), i2.height())
    if w <= 0 or h <= 0:
        return None
    ya, yb = int(h * y0), int(h * y1)
    diff = total = 0
    for y in range(ya, yb, 2):
        for x in range(0, w, 2):
            total += 1
            if i1.pixel(x, y) != i2.pixel(x, y):
                diff += 1
    return {"diff_ratio": round(diff / total, 4) if total else 0.0,
            "region": [y0, y1]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--out", default=str(ROOT / "data" / "measure" / "actions_verify"))
    args = ap.parse_args()

    model_dir = Path(args.model_dir)
    if not model_dir.is_absolute():
        model_dir = ROOT / model_dir
    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("OMP_NUM_THREADS", "1")
    with tempfile.TemporaryDirectory(prefix="dp_actions_") as tmp:
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

    # (名字, look, 等待ms, 额外JS)
    steps = [
        ("install", None, 300, JS_INSTALL_SNAP),
        ("center", (0.0, 0.0), 2200, None),
        ("look_left", (-1.0, 0.0), 2200, None),
        ("look_right", (1.0, 0.0), 2200, None),
        ("look_up", (0.0, -1.0), 2200, None),
        ("look_down", (0.0, 1.0), 2200, None),
        ("look_right_down", (0.8, 0.8), 2200, None),
        ("look_diag", (0.8, -0.8), 2200, None),
        ("back_center", (0.0, 0.0), 2500, None),
        # 动作：挥手（自写 motion）+ 模型自带竖食指
        ("wave_a", None, 700, "window.live2d.playMotion('挥手');"),
        ("wave_b", None, 700, None),
        ("wave_c", None, 900, None),
        ("beckon_a", None, 700, "window.live2d.playMotion('招手');"),
        ("beckon_b", None, 600, None),
        ("point_beckon_a", None, 800, "window.live2d.playMotion('指过来');"),
        ("point_beckon_b", None, 700, None),
        ("point_a", None, 800, "window.live2d.playMotion('竖手指');"),
        ("point_b", None, 1200, None),
        ("nod_a", None, 300, "window.live2d.playPoseAnim('nod', 1200);"),
        ("nod_b", None, 400, None),
    ]
    idx = {"i": 0}

    def _run_step(nm, look, wait, extra):
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
            nm2, look2, wait2, extra2 = steps[idx["i"]]
            if look2 is not None:
                renderer.look_at(look2[0], look2[1])
            if extra2:
                _js(extra2, lambda _v: None)
            QTimer.singleShot(50, lambda: _run_step(nm2, look2, wait2, extra2))

        if look is not None:
            renderer.look_at(look[0], look[1])
        if extra:
            _js(extra, lambda _v: None)
        _after(None)

    def _finish():
        base = shots.get("back_center") or shots.get("center")
        result["diffs"] = {}
        for k, pm in shots.items():
            if k == "back_center" or base is None:
                continue
            result["diffs"][k] = {
                "full": _region_diff(base, pm),
                # 脚部区域：画面下方 22%（角色脚所在）—— 应该几乎不变
                "feet": _region_diff(base, pm, 0.78, 1.0),
                # 上半身：应该明显变化
                "upper": _region_diff(base, pm, 0.14, 0.60),
            }
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
                                  else QTimer.singleShot(300,
                                                        lambda: _run_step(*steps[0]))))

    QTimer.singleShot(50, _poll)
    QTimer.singleShot(180000, lambda: _finish())
    app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
