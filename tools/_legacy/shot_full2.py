# -*- coding: utf-8 -*-
"""截 Live2D 页：收起态整页 + 全展开态整页。"""
import sys, os, logging
sys.path.insert(0, r"E:\study\desktop-pet")
logging.basicConfig(level=logging.ERROR)
import yaml
_cfg = yaml.safe_load(open(r"E:\study\desktop-pet\config.yaml", encoding="utf-8"))
MODEL_DIR = _cfg["pet"]["live2d"]["model_dir"]
from PyQt5.QtCore import Qt, QCoreApplication, QTimer, QEventLoop
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWidgets import QApplication, QTabWidget, QPushButton, QScrollArea
app = QApplication(sys.argv)
from app.animation.live2d_renderer import Live2DRenderer
from app.ui.settings_window import SettingsWindow
r = Live2DRenderer(MODEL_DIR, widget_size=(400,400), scale=0.4, hide_watermark=True)
w = SettingsWindow(renderer=r, sticker_enabled=True)
w.resize(620, 800); w.show()
def wait(ms):
    loop=QEventLoop(); QTimer.singleShot(ms,loop.quit); loop.exec_()
wait(300)
out_dir=r"E:\study\desktop-pet\tools\preview"; os.makedirs(out_dir,exist_ok=True)
tw=w.findChildren(QTabWidget)[0]
for i in range(tw.count()):
    if tw.tabText(i)=="Live2D": tw.setCurrentIndex(i)
wait(300)
sa = tw.currentWidget().findChildren(QScrollArea)[0]
inner = sa.widget()
def grab_inner(name):
    inner.adjustSize(); pix=inner.grab()
    fn=os.path.join(out_dir,name); print("save",pix.save(fn),pix.width(),pix.height(),name)
grab_inner("v2_full_collapsed.png")
for b in w.findChildren(QPushButton):
    if b.objectName()=="more_btn":
        b.click()
wait(200)
grab_inner("v2_full_expanded.png")
app.quit()
