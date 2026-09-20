"""验证（第二轮修复）：粉色背景隐藏 / 动作持久生效 / 表情包配置 / idle 循环。

用法：python -u tools/verify_chaopin_v2.py
输出：tools/preview_chaopin/v2_*.png
"""
import sys
import json
import logging
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, r"E:\study\desktop-pet")

logging.basicConfig(level=logging.INFO,
                    format="%(levelname)s %(name)s: %(message)s", stream=sys.stdout)

from PyQt5.QtCore import Qt, QCoreApplication, QTimer
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWidgets import QApplication

app = QApplication(sys.argv)

from app.animation.live2d_renderer import Live2DRenderer

MODEL_DIR = r"E:\study\live2d\超频猫猫完整版\超频猫猫"
OUT = Path(r"E:\study\desktop-pet\tools\preview_chaopin")
OUT.mkdir(parents=True, exist_ok=True)

renderer = Live2DRenderer(MODEL_DIR, widget_size=(400, 500), scale=0.4)
view = renderer.get_widget()
view.show()

# --- 表情包配置 ---
scfg = renderer.get_sticker_config()
print("[STICKERS]", "None" if scfg is None else
      f"{len(scfg['files'])} files, {scfg['min_s']}-{scfg['max_s']}s, "
      f"{scfg['duration_s']}s, size={scfg['size']}")
assert scfg is not None and len(scfg["files"]) == 36, "表情包应解析到 36 张"

# --- hide_parts 配置 ---
print("[HIDE_PARTS]", renderer.profile.hide_parts)
assert renderer.profile.hide_parts == ["Part10"]

state = {"ticks": 0, "phase": 0}


def grab(tag):
    pix = view.grab()
    out = OUT / f"v2_{tag}.png"
    pix.save(str(out))
    print("[SHOT]", tag, "->", out.name, flush=True)


def finish():
    try:
        renderer.shutdown()
    finally:
        app.quit()


def run_steps():
    idx = {"i": 0}

    def next_step():
        i = idx["i"]
        if i > 0:
            grab(STEPS[i - 1][0])
        if i >= len(STEPS):
            print("ALL DONE", flush=True)
            finish()
            return
        print(f"[STEP {i}] {STEPS[i][0]}", flush=True)
        STEPS[i][1]()
        idx["i"] += 1

    next_step()
    step_timer = QTimer()
    step_timer.timeout.connect(next_step)
    step_timer.start(2000)
    globals()["_step_timer"] = step_timer


# 步骤：base（无粉色背景）→ 唱歌手势持久 → 3 秒后仍在 → 换动作替换 → 复位
STEPS = [
    ("base", lambda: None),
    ("act_唱歌手势", lambda: renderer.play_animation("eat")),   # 右嗷呜
    ("act_换打招呼", lambda: renderer.play_animation("jump")),  # 应替换掉右嗷呜
    ("reset", lambda: (renderer.reset_all_appearance(), renderer.set_idle())),
]


def wait_tick():
    state["ticks"] += 1
    if renderer.is_ready() and state["phase"] == 0:
        state["phase"] = 1
        run_steps()
    elif renderer._error:
        print("ERROR:", renderer._error, flush=True)
        finish()
    elif state["ticks"] > 60:
        print("TIMEOUT", flush=True)
        finish()


wt = QTimer()
wt.timeout.connect(wait_tick)
wt.start(500)
globals()["_wt"] = wt
app.exec_()
print("VERIFY2 DONE")
