# -*- coding: utf-8 -*-
"""独立复刻聊天窗底部按钮行（不依赖聊天/agent 业务模块），验证文字裁剪。"""
import sys, os
sys.path.insert(0, r"E:\study\desktop-pet")
from PyQt5.QtCore import Qt, QCoreApplication, QTimer, QEventLoop
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWidgets import (QApplication, QWidget, QVBoxLayout, QHBoxLayout,
                             QPushButton, QLabel, QFrame)
app = QApplication(sys.argv)
from app.ui import ui_style

def build(extra_qss: str = "", tag: str = ""):
    win = QWidget()
    win.setStyleSheet(ui_style.CHAT_QSS + extra_qss)
    win.setObjectName("chat_root")
    outer = QVBoxLayout(win); outer.setContentsMargins(12, 12, 12, 12)
    ic = QFrame(); ic.setObjectName("input_container")
    row = QHBoxLayout(ic); row.setContentsMargins(12, 8, 12, 8); row.setSpacing(4)
    mic = QPushButton("语音"); mic.setObjectName("ghost_btn"); mic.setFixedHeight(28)
    st = QLabel("0 / 2000"); st.setStyleSheet("color:#aaa; font-size:8pt;")
    stop = QPushButton("停止"); stop.setObjectName("stop_btn"); stop.setFixedHeight(30)
    send = QPushButton("发送"); send.setObjectName("send_btn"); send.setFixedHeight(30)
    for b in (mic, stop, send): b.setCursor(Qt.CursorShape.PointingHandCursor)
    row.addWidget(mic); row.addWidget(st); row.addStretch(); row.addWidget(stop); row.addWidget(send)
    outer.addWidget(ic)
    # 标题栏同款 ghost 按钮（自适应高度，padding 同步改小）
    bar = QHBoxLayout(); bar.setContentsMargins(0,0,0,0); bar.setSpacing(6)
    bar.addStretch()
    for t in ("清空","调试"):
        b = QPushButton(t); b.setObjectName("ghost_btn")
        b.setCursor(Qt.CursorShape.PointingHandCursor); bar.addWidget(b)
    outer.addLayout(bar)
    win.resize(720, 130); win.show()
    def wait(ms):
        loop=QEventLoop(); QTimer.singleShot(ms,loop.quit); loop.exec_()
    wait(150)
    for nm,b in (("stop",stop),("send",send),("mic",mic)):
        fm=b.fontMetrics()
        print(f"[{tag}] {nm}: actual={b.width()}x{b.height()} "
              f"textW={fm.horizontalAdvance(b.text())} fontH={fm.height()} "
              f"needsW~{fm.horizontalAdvance(b.text())+2*12}")
    out=r"E:\study\desktop-pet\tools\preview"
    pix=win.grab(); fn=os.path.join(out,f"chatbtns_{tag}.png")
    print("save",pix.save(fn))
    return win

w1 = build(tag="after")
app.quit()
