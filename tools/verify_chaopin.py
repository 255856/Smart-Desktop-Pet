"""端到端验证（超频猫猫）：HTTP 注入 → ready → 菜单分组 → 表情/发型/手势/睡觉 → 截图。

用法：python tools/verify_chaopin.py
截图输出到 tools/preview_chaopin/。
"""
import sys
import json
import logging
import urllib.request
from pathlib import Path

sys.path.insert(0, r"E:\study\desktop-pet")

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)

from PyQt5.QtCore import Qt, QCoreApplication, QTimer
QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts, True)
from PyQt5.QtWidgets import QApplication

app = QApplication(sys.argv)

from app.animation.live2d_renderer import Live2DRenderer

MODEL_DIR = r"E:\study\live2d\超频猫猫完整版\超频猫猫"
SIZE = (400, 500)
OUT_DIR = Path(r"E:\study\desktop-pet\tools\preview_chaopin")
OUT_DIR.mkdir(parents=True, exist_ok=True)

renderer = Live2DRenderer(MODEL_DIR, widget_size=SIZE, scale=0.4)
view = renderer.get_widget()
view.setWindowTitle("chaopin verify")
view.show()

# --- 验证 1：HTTP 注入的 model3.json ---
import urllib.parse
settings_url = f"{renderer._http_base}/model/{urllib.parse.quote(renderer._settings_name)}"
http = urllib.request.urlopen(settings_url, timeout=5)
injected = json.loads(http.read().decode("utf-8"))
fr = injected["FileReferences"]
print("[HTTP] Expressions:", len(fr.get("Expressions", [])),
      "Motions:", {k: v[0]["File"] for k, v in fr.get("Motions", {}).items()})
assert len(fr.get("Expressions", [])) == 52, "注入表情数应为 52"
assert "Idle" in fr.get("Motions", {}) and "Sleep" in fr.get("Motions", {})

# --- 验证 2：profile / 菜单分组 ---
groups = renderer.get_menu_groups()
print("[MENU]", [(g["label"], len(g["items"]), g["kind"]) for g in groups])
print("[PLAY]", renderer.get_play_options())
print("[EMO]", len(renderer.get_emotion_options()), "个情绪选项")
print("[RANDOM]", renderer.is_random_expressions_enabled())

# --- 顺序步骤：(标签, 执行函数)；每步 1.8s，先截图上一步结果 ---
STEPS = [
    ("idle",          lambda: None),
    ("happy_心心眼",   lambda: renderer.set_emotion("happy")),
    ("hair_gesture",  lambda: (renderer.activate_menu_item("hair", "双马尾"),
                               renderer.activate_menu_item("gesture", "比心"))),
    ("gear",          lambda: (renderer.activate_menu_item("gear", "外套"),
                               renderer.activate_menu_item("gear", "耳机"))),
    ("sleep",         lambda: renderer.set_sleep()),
    ("wake_thinking", lambda: (renderer.set_wake(), renderer.set_thinking())),
    ("reset",         lambda: (renderer.reset_all_appearance(), renderer.set_idle())),
]


def grab_shot(tag):
    pix = view.grab()
    img = pix.toImage()
    non_transparent = sum(
        1 for y in range(0, img.height(), 2) for x in range(0, img.width(), 2)
        if img.pixelColor(x, y).alpha() > 10)
    out = OUT_DIR / f"chaopin_{tag}.png"
    pix.save(str(out))
    print(f"[SHOT {tag}] non_transparent={non_transparent} saved={out}")


def finish():
    try:
        renderer.shutdown()
    finally:
        app.quit()


def run_steps():
    print("READY expressions:", len(renderer.list_expressions()))
    idx = {"i": 0}

    def next_step():
        i = idx["i"]
        if i > 0:
            grab_shot(STEPS[i - 1][0])
        if i >= len(STEPS):
            print("ALL STEPS DONE")
            finish()
            return
        print(f"[STEP {i}] {STEPS[i][0]}")
        STEPS[i][1]()
        idx["i"] += 1

    next_step()
    # 注意：QTimer 必须保留引用，否则被 GC 后永不触发
    step_timer = QTimer()
    step_timer.timeout.connect(next_step)
    step_timer.start(1800)
    globals()["_step_timer"] = step_timer


tick_state = {"ticks": 0}


def wait_ready():
    tick_state["ticks"] += 1
    if renderer.is_ready():
        wait_timer.stop()
        run_steps()
    elif renderer._error:
        print("ERROR:", renderer._error)
        finish()
    elif tick_state["ticks"] >= 40:
        print("TIMEOUT ready=", renderer.is_ready())
        grab_shot("timeout")
        finish()


wait_timer = QTimer()
wait_timer.timeout.connect(wait_ready)
wait_timer.start(500)
app.exec_()
print("VERIFY DONE")
