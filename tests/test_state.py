"""状态系统单元测试。"""
import pytest
import time
from app.engine.state import PetState, Mode, is_night


class TestPetState:
    def test_initial_state(self):
        s = PetState()
        assert s.strength == 100.0
        assert s.feeling == 80.0
        assert s.mode == Mode.NORMAL

    def test_feed(self):
        s = PetState()
        s.feed(strength=10, feeling=5)
        assert s.strength == 100.0  # 初始 100 + 10 = 110，裁剪到 100
        assert s.feeling == 85.0    # 初始 80 + 5 = 85，未超过 100

    def test_feed_clamp(self):
        s = PetState()
        s.feed(strength=200)
        assert s.strength == 100.0  # 裁剪到 100

    def test_feed_negative_clamp(self):
        s = PetState()
        s.feed(strength=-200)
        assert s.strength == 0.0  # 裁剪到 0

    def test_tick_decay(self):
        s = PetState()
        s.tick(dt_seconds=60.0)
        assert s.strength < 100.0  # 体力应该衰减

    def test_tick_sleeping_regen(self):
        s = PetState()
        s.strength = 50.0
        s.tick(dt_seconds=60.0, sleeping=True)
        assert s.strength > 50.0  # 睡觉应该回复

    def test_night_mode(self):
        s = PetState()
        s.tick(dt_seconds=60.0, is_night=True)
        # 夜间衰减更快
        assert s.strength < 100.0

    def test_level_up(self):
        s = PetState()
        s.exp = 0
        s.level = 1
        s.feed(exp=10000)
        assert s.level > 1

    def test_to_dict_from_dict(self):
        s = PetState()
        s.strength = 75.0
        d = s.to_dict()
        s2 = PetState.from_dict(d)
        assert s2.strength == 75.0
        assert s2.mode == s.mode

    def test_from_dict_old_mode(self):
        """兼容旧版 Nomal 拼写"""
        s = PetState.from_dict({"mode": "Nomal"})
        assert s.mode == Mode.NORMAL
