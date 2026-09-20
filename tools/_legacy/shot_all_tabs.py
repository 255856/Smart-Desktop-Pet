# -*- coding: utf-8 -*-
"""截设置面板所有 tab，排查其它 tab 的文字像素/显示问题。"""
import sys, os, logging
sys.path.insert(0, r"E:\study\desktop-pet")
logging.basicConfig(level=logging.ERROR)
import yaml
_cfg = yaml.safe_load(open(r"E:\study\desktop-pet\config.yaml", encoding="utf-8"))
MODEL_DIR = _cfg["pet"]["live2d"]["model_dir"]
from PyQt5.QtCore import Qt, QCoreApplication, QTimer, QEventLoop
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWidgets import QApplication, QTabWidget
app = QApplication(sys.argv)
from app.animation.live2d_renderer import Live2DRenderer
from app.ui.settings_window import SettingsWindow
r = Live2DRenderer(MODEL_DIR, widget_size=(400,400), scale=0.4, hide_watermark=True)
w = SettingsWindow(renderer=r, sticker_enabled=True)
w.resize(620, 800); w.show()
def wait(ms):
    loop=QEventLoop(); QTimer.singleShot(ms,loop.quit); loop.exec_()
wait(400)
out_dir = r"E:\study\desktop-pet\tools\preview\tabs"
os.makedirs(out_dir, exist_ok=True)
tw = w.findChildren(QTabWidget)[0]
for i in range(tw.count()):
    tw.setCurrentIndex(i)
    wait(180)
    name = tw.tabText(i)
    pix = w.grab()
    fn = os.path.join(out_dir, f"tab_{i}_{name}.png")
    pix.save(fn)
    print("saved", fn)
app.quit()
