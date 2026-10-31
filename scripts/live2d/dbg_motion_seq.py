# -*- coding: utf-8 -*-
"""调试：连续播放多个动作组，看哪个没生效。

用法: python scripts/live2d/dbg_motion_seq.py 挥手 招手 指过来 竖手指
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

JS_READY = "(function(){var l=window.live2d;if(!l||!l.ready)return 0;return 1;})()"

JS_PLAY = ("(function(){try{var l=window.live2d;var r=l.model.motion(%s);"
           "return {ret:(r===undefined?'undefined':String(r))};}"
           "catch(e){return {err:String(e&&(e.stack||e.message||e))};}})()")

JS_PARAMS = (
    "(function(){var l=window.live2d,c=l.model.internalModel.coreModel;"
    "function pv(id){try{var v=c.getParameterValueById(id);"
    "return (typeof v==='number')?Math.round(v*1000)/1000:null;}catch(e){return 'ERR';}}"
    "var mm=l.model.internalModel.motionManager;var st=[];"
    "try{var ms=mm._motions||{};for(var k in ms){var m=ms[k];"
    "st.push(k+':'+(m&&m._playing?'play':'idle')+'/'+(m&&m._weight!==undefined?Math.round(m._weight*100)/100:'-'));}}"
    "catch(e){}"
    "return {Movement2:pv('Movement2'),Movement3:pv('Movement3'),Param40:pv('Param40'),"
    "Param41:pv('Param41'),Param60:pv('Param60'),Param61:pv('Param61'),states:st};})()"
)


def main() -> int:
    model_dir = ROOT / "assets" / "live2d" / "DeepSeek" / "DeepSeek"
    groups = sys.argv[1:] or ["挥手", "招手", "指过来"]

    os.environ.setdefault("OMP_NUM_THREADS", "1")
    with tempfile.TemporaryDirectory(prefix="dp_seq_") as tmp:
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

        renderer = Live2DRenderer(model_dir=str(model_dir),
                                  widget_size=(500, 500), scale=0.5)
        out = {"steps": []}

        def _js(code, cb):
            try:
                renderer._page.runJavaScript(code, cb)
            except Exception as e:
                cb({"js_err": repr(e)})

        def _play(i):
            if i >= len(groups):
                _finish()
                return
            g = groups[i]

            def _after_play(v):
                out["steps"].append({"group": g, "play": v})
                QTimer.singleShot(1200, lambda: _read(i, g))

            _js(JS_PLAY % json.dumps(g), _after_play)

        def _read(i, g):
            def _after(v):
                out["steps"][-1]["params"] = v
                QTimer.singleShot(100, lambda: _play(i + 1))

            _js(JS_PARAMS, _after)

        def _finish():
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
                                     else QTimer.singleShot(300, lambda: _play(0))))

        QTimer.singleShot(50, _poll)
        QTimer.singleShot(120000, lambda: _finish())
        app.exec()
    return 0


if __name__ == "__main__":
    sys.exit(main())
