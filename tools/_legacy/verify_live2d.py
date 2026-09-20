"""端到端验证：实例化 Live2DRenderer，等模型 ready，grab 截图并统计非透明像素。"""
import sys
import logging
from pathlib import Path

sys.path.insert(0, r"E:\study\desktop-pet")

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)

from PyQt5.QtCore import Qt, QCoreApplication, QTimer
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QColor

app = QApplication(sys.argv)

from app.animation.live2d_renderer import Live2DRenderer

MODEL_DIR = r"E:\study\live2d\bingtang\bingtang"
SIZE = (400, 400)
OUT_DIR = Path(r"E:\study\desktop-pet\tools\preview")
OUT_DIR.mkdir(parents=True, exist_ok=True)

renderer = Live2DRenderer(MODEL_DIR, widget_size=SIZE, scale=0.4)
view = renderer.get_widget()
# 给一个不透明父窗背景之外，先让 view 独立显示，便于观察
view.setWindowTitle("live2d verify")
view.show()

state = {"ticks": 0, "shot": False}

def grab_shot(tag):
    pix = view.grab()
    # 统计非透明像素
    img = pix.toImage()
    w, h = img.width(), img.height()
    non_transparent = 0
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            if (img.pixelColor(x, y).alpha() > 10):
                non_transparent += 1
    total = (w // 2) * (h // 2)
    ratio = non_transparent / total
    out = OUT_DIR / f"live2d_verify_{tag}.png"
    pix.save(str(out))
    print(f"[SHOT {tag}] size={w}x{h} non_transparent={ratio:.3f} saved={out}")
    return ratio

def tick():
    state["ticks"] += 1
    elapsed = state["ticks"]
    if renderer.is_ready() and not state["shot"]:
        state["shot"] = True
        print("READY expressions:", renderer.list_expressions())
        # 让模型渲染几帧
        QTimer.singleShot(1500, lambda: (grab_shot("idle"),
                                         renderer.set_emotion("angry"),
                                         QTimer.singleShot(1200, lambda: (grab_shot("angry"), finish()))))
        return
    if renderer._error:
        print("ERROR:", renderer._error)
        grab_shot("error")
        finish_now()
        return
    if elapsed >= 40:  # 40 秒
        print("TIMEOUT ready=", renderer.is_ready(), "error=", renderer._error)
        grab_shot("timeout")
        finish_now()

def finish():
    grab = None
    finish_now()

def finish_now():
    try:
        renderer.shutdown()
    except Exception as e:
        print("shutdown err", e)
    app.quit()

timer = QTimer()
timer.timeout.connect(tick)
timer.start(1000)

app.exec_()
print("DONE")
