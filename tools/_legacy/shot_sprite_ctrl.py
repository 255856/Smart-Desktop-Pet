# -*- coding: utf-8 -*-
"""边界：sprite/无 renderer 时控制 tab 仍是 5 个情绪按钮（无黑条）。"""
import sys, os
sys.path.insert(0, r"E:\study\desktop-pet")
from PyQt5.QtCore import Qt, QCoreApplication, QTimer, QEventLoop
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWidgets import QApplication, QTabWidget, QPushButton
app = QApplication(sys.argv)
from app.ui.settings_window import SettingsWindow
w = SettingsWindow(renderer=None, sticker_enabled=True)
w.resize(620,800); w.show()
def wait(ms):
    loop=QEventLoop(); QTimer.singleShot(ms,loop.quit); loop.exec_()
wait(200)
tw=w.findChildren(QTabWidget)[0]
for i in range(tw.count()):
    if tw.tabText(i)=="控制": tw.setCurrentIndex(i)
wait(200)
print("tabs:", [tw.tabText(i) for i in range(tw.count())])
print("emo_btns:", len(w.emo_btns), [b.text() for b,_ in w.emo_btns])
out=r"E:\study\desktop-pet\tools\preview"; os.makedirs(out,exist_ok=True)
p=w.grab(); print("save",p.save(os.path.join(out,"v2_control_sprite.png")))
app.quit()
