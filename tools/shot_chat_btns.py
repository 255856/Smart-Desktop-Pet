# -*- coding: utf-8 -*-
"""截聊天窗底部按钮（停止/发送），核对文字是否被裁剪。"""
import sys, os, types
sys.path.insert(0, r"E:\study\desktop-pet")
# 该环境未装 pydantic_settings；截图只需 BaseModel，stub 掉 BaseSettings 即可
from pydantic import BaseModel
_stub = types.ModuleType("pydantic_settings")
_stub.BaseSettings = BaseModel
sys.modules["pydantic_settings"] = _stub
from PyQt5.QtCore import Qt, QCoreApplication, QTimer, QEventLoop
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWidgets import QApplication
app = QApplication(sys.argv)
from app.core.config import LLMConfig, CharacterConfig
from app.ui.chat_window import ChatWindow
import yaml
_cfg = yaml.safe_load(open(r"E:\study\desktop-pet\config.yaml", encoding="utf-8"))
sprite_dir = _cfg["pet"].get("sprite_dir", r"E:\study\desktop-pet\assets\sprites")
win = ChatWindow(LLMConfig(), CharacterConfig(), sprite_dir,
                 asr_enabled=False)
win.resize(720, 540)
win.show()
def wait(ms):
    loop=QEventLoop(); QTimer.singleShot(ms,loop.quit); loop.exec_()
wait(300)
# 让停止按钮进入可点态，便于同时观察两个按钮文字
win.stop_btn.setEnabled(True)
wait(150)
out=r"E:\study\desktop-pet\tools\preview"; os.makedirs(out,exist_ok=True)
for nm in ("send_btn","stop_btn","mic_btn"):
    b=getattr(win,nm)
    print(nm, "sizeHint=",b.sizeHint().width(),b.sizeHint().height(),
          "actual=",b.width(),b.height(),
          "text=",repr(b.text()))
# 只截输入区底部那一横条
ic = win.input_container
pix = ic.grab()
print("input_container size", ic.width(), ic.height())
print("save", pix.save(os.path.join(out,"v2_chat_buttons_before.png")))
app.quit()
