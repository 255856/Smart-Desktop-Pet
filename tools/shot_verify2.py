# -*- coding: utf-8 -*-
"""验证：外观/挂机置顶 + 每分类前3+更多 + 控制tab文字修复。"""
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
out_dir = r"E:\study\desktop-pet\tools\preview"
os.makedirs(out_dir, exist_ok=True)
def shot(name):
    p=w.grab(); fn=os.path.join(out_dir,name); print("save", p.save(fn), name); return fn
def chips():
    return [b for b in w.findChildren(QPushButton) if b.objectName()=="chip"]
def more_btns():
    return [b for b in w.findChildren(QPushButton) if b.objectName()=="more_btn"]

tw = w.findChildren(QTabWidget)[0]
for i in range(tw.count()):
    if tw.tabText(i)=="Live2D": tw.setCurrentIndex(i)
wait(300)
print("更多按钮数:", len(more_btns()), "->", [b.text() for b in more_btns()])
print("可见 chip 数(收起态):", sum(1 for c in chips() if c.isVisible()))
print("总 chip 数:", len(chips()))
shot("v2_live2d_top_collapsed.png")

# 展开手势组：找到第 4 个 more（special 无更多，hair/gear/gesture/face 有，顺序）
# 顺序：special(4项>3) 也有 more，hair,gear,gesture,face 共 5 个 more
mores = more_btns()
# 手势组是 gesture：按 more 顺序 special=0,hair=1,gear=2,gesture=3,face=4
mores[3].click()
wait(200)
print("展开手势后其文字:", mores[3].text())
print("可见 chip 数:", sum(1 for c in chips() if c.isVisible()))
shot("v2_live2d_gesture_expanded.png")

# 切控制 tab
for i in range(tw.count()):
    if tw.tabText(i)=="控制": tw.setCurrentIndex(i)
wait(250)
shot("v2_control_fixed.png")
app.quit()
