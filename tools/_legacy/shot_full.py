# -*- coding: utf-8 -*-
"""截 Live2D 滚动区全内容（inner widget 全高），核对全部分组。"""
import sys, os, logging
sys.path.insert(0, r"E:\study\desktop-pet")
logging.basicConfig(level=logging.ERROR)
import yaml
_cfg = yaml.safe_load(open(r"E:\study\desktop-pet\config.yaml", encoding="utf-8"))
MODEL_DIR = _cfg["pet"]["live2d"]["model_dir"]
from PyQt5.QtCore import Qt, QCoreApplication, QTimer, QEventLoop
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWidgets import QApplication, QTabWidget, QScrollArea
app = QApplication(sys.argv)
from app.animation.live2d_renderer import Live2DRenderer
from app.ui.settings_window import SettingsWindow
r = Live2DRenderer(MODEL_DIR, widget_size=(400,400), scale=0.4, hide_watermark=True)
w = SettingsWindow(renderer=r, sticker_enabled=True)
w.resize(int(sys.argv[1]) if len(sys.argv) > 1 else 620, 800); w.show()
def wait(ms):
    loop=QEventLoop(); QTimer.singleShot(ms,loop.quit); loop.exec_()
wait(300)
for tw in w.findChildren(QTabWidget):
    for i in range(tw.count()):
        if tw.tabText(i)=="Live2D":
            tw.setCurrentIndex(i); page=tw.widget(i)
wait(400)
sa = page.findChildren(QScrollArea)[0]
inner = sa.widget()
inner.adjustSize()
pix = inner.grab()
out_dir = r"E:\study\desktop-pet\tools\preview"
os.makedirs(out_dir, exist_ok=True)
out = os.path.join(out_dir, f"settings_live2d_full_{w.width()}.png")
print("save", pix.save(out), pix.width(), pix.height(), out)
app.quit()
