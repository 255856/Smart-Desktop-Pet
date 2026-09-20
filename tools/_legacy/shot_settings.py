# -*- coding: utf-8 -*-
"""Live2D 设置面板优化后：截图 + 交互验证。"""
import sys, os, logging, traceback
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

emitted = []
r = Live2DRenderer(MODEL_DIR,
                   widget_size=(400, 400), scale=0.4, hide_watermark=True)
w = SettingsWindow(renderer=r, sticker_enabled=True)
w.resize(620, 800)
w.live2d_item_activated.connect(lambda g, i: emitted.append((g, i)))
w.live2d_reset_requested.connect(lambda: emitted.append(("__reset__", "")))
w.show()

def wait(ms):
    loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec_()
wait(300)
scroll = None
for tw in w.findChildren(QTabWidget):
    for i in range(tw.count()):
        if tw.tabText(i) == "Live2D":
            tw.setCurrentIndex(i)
            page = tw.widget(i)
            scrolls = page.findChildren(QScrollArea)
            scroll = scrolls[0] if scrolls else None
wait(400)

def chips():
    return [b for b in w.findChildren(QPushButton) if b.objectName() == "chip"]

def chip_by_text(t):
    for b in chips():
        if b.text() == t:
            return b
    return None

def shot(name):
    pix = w.grab()
    out_dir = r"E:\study\desktop-pet\tools\preview"
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, name)
    pix.save(out)
    print("saved", out, pix.width(), pix.height())

# 1) 初始顶部
shot("settings_live2d_after_top.png")
print("初始 chip 数:", len(chips()))
print("初始选中:", sorted(b.text() for b in chips() if b.isChecked()))

# 2) 点配件「人耳」（toggle）、表情「心心眼」（exclusive）
chip_by_text("人耳").click()
chip_by_text("心心眼").click()
wait(100)
print("\n点击后选中:", sorted(b.text() for b in chips() if b.isChecked()))
print("emitted:", emitted)

# 3) 表情组互斥检查：自然应取消
nat = chip_by_text("自然表情")
print("自然表情选中态(应为False):", nat.isChecked())
# 再点一个表情「星星眼」
chip_by_text("星星眼").click()
wait(100)
print("再点星星眼后表情组选中:",
      sorted(b.text() for b in chips() if b.isChecked() and b.text() in ("自然表情","心心眼","星星眼")))

# 4) 再点一次「人耳」应取消（toggle）
chip_by_text("人耳").click()
wait(100)
print("再点人耳后选中(人耳应为False):", chip_by_text("人耳").isChecked())

# 5) 复位
w.btn_live2d_reset.click()
wait(100)
print("复位后选中:", sorted(b.text() for b in chips() if b.isChecked()))

# 6) 滚动到底部
if scroll is not None:
    vsb = scroll.verticalScrollBar()
    print("\n滚动区内容高:", vsb.maximum(), "页长:", vsb.pageStep())
    vsb.setValue(vsb.maximum())
    wait(300)
    shot("settings_live2d_after_bottom.png")
app.quit()
