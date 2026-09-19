# -*- coding: utf-8 -*-
"""用项目自带 .local-packages 依赖构造真实 ChatWindow，验证底部按钮。"""
import sys, os
ROOT = r"E:\study\desktop-pet"
sys.path.insert(0, os.path.join(ROOT, ".local-packages"))
sys.path.insert(0, ROOT)
from PyQt5.QtCore import Qt, QCoreApplication, QTimer, QEventLoop
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWidgets import QApplication
app = QApplication(sys.argv)
import yaml
cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
sprite_dir = cfg.get("sprite", {}).get("directory", "assets/sprites")
from app.core.config import LLMConfig, CharacterConfig
from app.ui.chat_window import ChatWindow
win = ChatWindow(LLMConfig(), CharacterConfig(name="鲸鱼娘"),
                 os.path.join(ROOT, sprite_dir), asr_enabled=False)
win.resize(720, 540); win.show()
def wait(ms):
    loop=QEventLoop(); QTimer.singleShot(ms,loop.quit); loop.exec_()
wait(300)
win.stop_btn.setEnabled(True)
wait(150)
out=os.path.join(ROOT,"tools","preview"); os.makedirs(out,exist_ok=True)
for nm in ("mic_btn","stop_btn","send_btn"):
    b=getattr(win,nm)
    print(f"{nm}: actual={b.width()}x{b.height()} minH={b.minimumHeight()} "
          f"fontH={b.fontMetrics().height()} pad=see-QSS")
pix=win.grab()
print("save full", pix.save(os.path.join(out,"chat_real_after.png")))
# 底部按钮行特写
ic=win.input_container
pic=ic.grab()
print("save input", pic.save(os.path.join(out,"chat_real_input.png")))
app.quit()
