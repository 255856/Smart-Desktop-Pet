# -*- coding: utf-8 -*-
"""「拍一拍」：**只**绑双击模型（owner 定：不再按摸头 / 身体分开）。

  A1 双击模型 → 一次拍一拍事件（部位=model）
  A2 单击热区 → 只是触摸反应，**不**算拍一拍
  A3 有可见反应：表情 + 一句台词（走现有气泡通道）
  A4 被记下来：互动记录（state）+ 长期记忆卡（sidecar）
  A5 连点被节流（不刷屏）
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # tests/fake_sidecar.py

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.core.pet_control import (  # noqa: E402
    DEFAULT_PAT_COOLDOWN_S, PAT_PARTS, PetPokeService,
    register_poke_handler, trigger_poke, unregister_poke_handler,
)
from app.engine.state import PetState  # noqa: E402


class _Clock:
    """假时钟：让「间隔」在测试里可控。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float = 10.0) -> None:
        self.t += dt


class _FakeMouseEvent:
    """够用的假鼠标事件：mouseDoubleClickEvent 只用 button() / accept()。"""

    def __init__(self, button=None) -> None:
        from app.core.qt_compat import Qt
        self._button = button if button is not None else Qt.MouseButton.LeftButton
        self.accepted = False

    def button(self):
        return self._button

    def accept(self):
        self.accepted = True


def _click(pet, part: str) -> None:
    """按真实路径点热区（命中检测 → on_click 回调）。"""
    from app.core.qt_compat import QPoint
    area = next(a for a in pet.touch_areas if a.name == part)
    sx = (area.locate[0] + area.size[0] / 2) * pet.SPRITE_SIZE.width()
    sy = (area.locate[1] + area.size[1] / 2) * pet.SPRITE_SIZE.height()
    x = int(sx / pet.SPRITE_SIZE.width() * pet.width())
    y = int(sy / pet.SPRITE_SIZE.height() * pet.height())
    hit = pet._hit_zone(QPoint(x, y))
    assert hit is not None, "热区 %s 没命中" % part
    assert hit.name == part, "点到了 %s，不是 %s" % (hit.name, part)
    hit.on_click()


def _double_click(pet) -> None:
    """双击模型（真实路径：mouseDoubleClickEvent）。"""
    pet.mouseDoubleClickEvent(_FakeMouseEvent())


@pytest.fixture
def pet(qapp, tmp_path):
    """真桌宠窗口（空 sprite 目录 → 渲染器降级，不碰真实素材）。"""
    from app.ui.pet_window import PetWindow
    w = PetWindow(tmp_path, fallback_image=None, scale=0.4)
    w.attach_state(PetState())
    w.poke_service._clock = _Clock()
    yield w
    unregister_poke_handler(w._emit_pat_from_console)
    w.close()


# --- A1：双击模型 = 一次拍一拍 ---------------------------------------------


def test_double_click_sends_exactly_one_pat_event_with_part_model(pet):
    events = []
    pet.pat_requested.connect(lambda part: events.append(part))

    _double_click(pet)

    assert events == ["model"], events


def test_pat_and_touch_are_two_different_signals(pet):
    """拍一拍（双击）跟触摸反应（单击）是两个事件，别混在一起。"""
    assert pet.pat_requested is not pet.reaction_requested

    pats, reactions = [], []
    pet.pat_requested.connect(pats.append)
    pet.reaction_requested.connect(reactions.append)

    _double_click(pet)

    assert pats == ["model"], pats
    assert reactions == [], reactions


def test_pat_carries_the_model_part(pet):
    """拍一拍的部位固定是 model（不再有摸头 / 身体两套）。"""
    parts = []
    pet.pat_requested.connect(parts.append)
    _double_click(pet)
    assert parts == ["model"], parts
    assert "model" in PAT_PARTS, PAT_PARTS


# --- A2：单击热区只是触摸反应，不算拍一拍 ---------------------------------


def test_single_click_on_a_hot_zone_is_not_a_pat(pet):
    pats = []
    pet.pat_requested.connect(pats.append)

    for part in ("head", "body", "raise"):
        _click(pet, part)

    assert pats == [], pats
    assert pet._attached_state.pat_total == 0, pet._attached_state.pat_total


def test_single_click_still_reacts_to_the_touch(pet):
    """单击的手感不变：摸头 / 身体有反应，点脚丫开聊天。"""
    reactions, chats = [], []
    pet.reaction_requested.connect(reactions.append)
    pet.chat_requested.connect(lambda: chats.append(1))

    _click(pet, "head")
    _click(pet, "body")
    _click(pet, "raise")

    assert reactions == ["head", "body"], reactions
    assert chats == [1], chats


# --- A3：看得见的反应 ------------------------------------------------------


def test_pat_shows_an_emotion_and_a_line(pet):
    played = []
    pet.animator.play_reaction = lambda where: played.append(("anim", where))
    pet.play_emotion = lambda name: played.append(("emo", name))

    _double_click(pet)

    assert ("emo", "happy") in played, played
    assert pet._bubble_text, "拍一拍要说一句台词"
    assert not pet._bubble_label.isHidden(), "台词要显示在气泡上"


def test_pat_does_not_cut_the_double_click_animation(pet):
    """双击自己会播动作（转圈圈 / 场景），拍一拍别用触摸反应把它打断。"""
    played = []
    pet.animator.play_reaction = lambda where: played.append(where)
    pet.play_emotion = lambda name: played.append(name)

    _double_click(pet)

    assert "model" not in played, played


def test_model_part_has_its_own_line(pet):
    from app.core.pet_control import PAT_LINES
    assert "model" in PAT_LINES, PAT_LINES
    assert len(set(PAT_LINES["model"])) > 1, "台词轮换，不能只有一句"


# --- A4：被记下来 ----------------------------------------------------------


def test_pat_is_recorded_in_the_interaction_log(pet):
    state = pet._attached_state
    _double_click(pet)
    assert state.pat_total == 1, state.pat_total
    assert state.pat_today == 1, state.pat_today
    assert state.pat_parts_today == {"model": 1}, state.pat_parts_today
    assert "今天被拍1次" in state.stats_summary(), state.stats_summary()
    assert "主人今天拍了我 1 次" in state.pat_summary(), state.pat_summary()


def test_interaction_log_survives_a_save_load_round_trip():
    state = PetState()
    state.record_pat("model")
    state.record_pat("raise")
    again = PetState.from_dict(state.to_dict())
    assert again.pat_total == 2, again.pat_total
    assert again.pat_parts_today == {"model": 1, "raise": 1}, again.pat_parts_today
    assert again.pat_last_part == "raise", again.pat_last_part


def test_pat_counts_reset_when_the_day_changes():
    state = PetState()
    state.record_pat("model")
    state.pat_date = "2000-01-01"          # 假装是昨天拍的
    assert state.pat_today == 0
    state.record_pat("model")
    assert state.pat_today == 1, state.pat_today
    assert state.pat_total == 2, state.pat_total


# --- A5：连点被节流 --------------------------------------------------------


def test_rapid_double_clicking_is_throttled(pet):
    events = []
    pet.pat_requested.connect(lambda part: events.append(part))
    _double_click(pet)
    _double_click(pet)
    assert events == ["model"], events
    assert pet._attached_state.pat_total == 1, pet._attached_state.pat_total


def test_a_pat_is_accepted_again_after_the_cooldown(pet):
    events = []
    pet.pat_requested.connect(lambda part: events.append(part))
    _double_click(pet)
    pet.poke_service._clock.advance(DEFAULT_PAT_COOLDOWN_S + 0.5)
    _double_click(pet)
    assert events == ["model", "model"], events


def test_the_service_rejects_an_unknown_part():
    svc = PetPokeService(clock=_Clock())
    out = svc.poke("tail")
    assert out["ok"] is False
    assert out["reason"] == "unknown_part", out


# --- 控制台也能拍它 -------------------------------------------------------


def test_the_control_service_can_trigger_a_pat():
    from app.core.pet_control import PetControlServer
    seen = []

    def _handler(part):
        seen.append(part)

    register_poke_handler(_handler)
    srv = PetControlServer(port=0)
    port = srv.start()
    try:
        assert port, "控制服务没起来"
        req = urllib.request.Request(
            "http://127.0.0.1:%d/action" % port,
            data=json.dumps({"action": "poke",
                             "params": {"part": "model"}}).encode("utf-8"),
            headers={"content-type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        assert out["ok"] is True, out
        assert seen == ["model"], seen
    finally:
        srv.stop()
        unregister_poke_handler(_handler)


def test_the_console_pat_lands_on_the_gui_thread(pet, qapp):
    """控制台在别的线程触发 → 处理必须回到 GUI 线程（不碰崩控件）。"""
    import threading
    import time as _time

    state = pet._attached_state
    thread = threading.Thread(target=trigger_poke, args=("model",))
    thread.start()
    thread.join(timeout=10)

    deadline = _time.time() + 5.0
    while state.pat_total == 0 and _time.time() < deadline:
        qapp.processEvents()
        _time.sleep(0.01)

    assert state.pat_total == 1, state.pat_total
    assert pet._bubble_text, "控制台拍的这一下也要有台词"


def test_trigger_poke_without_a_pet_says_so_instead_of_pretending():
    out = trigger_poke("model")
    assert out["ok"] is False, out
    assert out["handled"] is False, out


# --- 长期记忆：它真的记得「主人今天拍了我 N 次」 ---------------------------


@pytest.fixture(scope="module")
def brain(tmp_path_factory):
    """真 BrainController（临时 root；sidecar 用假 IO，不碰 data/）。"""
    from app.core.qt_compat import QApplication
    QApplication.instance() or QApplication(sys.argv[:1])

    from app.brain.brain_controller import BrainController
    from app.core.config import load_config
    from app.engine.state_manager import StateManager

    tmp = tmp_path_factory.mktemp("pet_poke_brain")
    (tmp / "config.yaml").write_text(
        "llm:\n"
        "  base_url: \"http://localhost:1/v1\"\n"
        "  api_key: \"test\"\n"
        "  model: \"test\"\n"
        "character:\n"
        "  name: \"测试\"\n"
        "window:\n"
        "  scale: 0.4\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp / "config.yaml")
    state_mgr = StateManager(tmp, cfg, type("Args", (), {"reset_state": False})())
    return BrainController(tmp, cfg, state_mgr.state, state_mgr.reminders)


def _reset_pat_counters(state) -> None:
    """互动记录清零（brain 是模块级 fixture，用例之间要隔离）。"""
    state.pat_total = 0
    state.pat_date = ""
    state.pat_count_today = 0
    state.pat_parts_today = {}


def test_the_pat_is_written_into_long_term_memory(brain):
    from app.core.qt_compat import QObject, Signal
    from fake_sidecar import install

    class _FakePet(QObject):
        pat_requested = Signal(str)

    fake = install(brain.memory_client)
    pet = _FakePet()
    brain.connect_pet_interaction(pet)
    _reset_pat_counters(brain.state)

    brain.state.record_pat("model")
    brain.state.record_pat("model")
    pet.pat_requested.emit("model")

    thread = brain._pat_memory_thread
    assert thread is not None, "拍一拍必须真的写一次记忆"
    thread.join(timeout=10)

    assert len(fake.cards) == 1, fake.cards
    card = list(fake.cards.values())[0]
    assert "主人今天拍了我 2 次" in card["text"], card
    assert "拍拍 2 次" in card["text"], card


def test_the_memory_card_is_updated_in_place_not_stacked(brain):
    """再拍一次 → 更新同一条（每天一条），不堆成一堆卡。"""
    from app.core.qt_compat import QObject, Signal
    from fake_sidecar import install

    class _FakePet(QObject):
        pat_requested = Signal(str)

    fake = install(brain.memory_client)
    pet = _FakePet()
    brain.connect_pet_interaction(pet)
    _reset_pat_counters(brain.state)

    brain.state.record_pat("model")
    pet.pat_requested.emit("model")
    thread = brain._pat_memory_thread
    thread.join(timeout=10)
    first = list(fake.cards.values())[0]["id"]

    brain.state.record_pat("model")
    pet.pat_requested.emit("model")
    thread = brain._pat_memory_thread
    thread.join(timeout=10)

    assert len(fake.cards) == 1, fake.cards
    assert list(fake.cards)[0] == first, "同一天必须是同一条记忆"
    assert "主人今天拍了我 2 次" in list(fake.cards.values())[0]["text"]
