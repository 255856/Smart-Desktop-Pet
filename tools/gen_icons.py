# -*- coding: utf-8 -*-
"""一次性生成 QSS 图标 PNG（下拉箭头 / 复选框勾号），输出到 assets/icons。"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, ".local-packages"))
sys.path.insert(0, ROOT)
from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QPixmap, QPainter, QColor, QPen, QPainterPath
from PyQt5.QtCore import Qt

app = QApplication(sys.argv)
OUT = os.path.join(ROOT, "assets", "icons")
os.makedirs(OUT, exist_ok=True)

ACCENT = QColor("#7c6cf0")

# 下拉箭头：24x24，紫色描边三角
pm = QPixmap(24, 24)
pm.fill(QColor(0, 0, 0, 0))
p = QPainter(pm)
p.setRenderHint(QPainter.Antialiasing)
pen = QPen(ACCENT)
pen.setWidth(2)
pen.setCapStyle(Qt.RoundCap)
pen.setJoinStyle(Qt.RoundJoin)
p.setPen(pen)
path = QPainterPath()
path.moveTo(6, 9.5)
path.lineTo(12, 15)
path.lineTo(18, 9.5)
p.drawPath(path)
p.end()
pm.save(os.path.join(OUT, "arrow_down.png"))
print("saved arrow_down.png")

# 复选框勾号：32x32（透明底，白色勾），供 18px indicator 缩放
pm = QPixmap(32, 32)
pm.fill(QColor(0, 0, 0, 0))
p = QPainter(pm)
p.setRenderHint(QPainter.Antialiasing)
pen = QPen(QColor("white"))
pen.setWidth(4)
pen.setCapStyle(Qt.RoundCap)
pen.setJoinStyle(Qt.RoundJoin)
p.setPen(pen)
path = QPainterPath()
path.moveTo(7, 17)
path.lineTo(13.5, 23)
path.lineTo(25.5, 9.5)
p.drawPath(path)
p.end()
pm.save(os.path.join(OUT, "check.png"))
print("saved check.png")
