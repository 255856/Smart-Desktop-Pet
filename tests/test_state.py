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


class TestCheckin:
    def test_checkin_once_per_day(self):
        s = PetState()
        s.money = 0.0
        ok, reward = s.daily_checkin(100.0)
        assert ok is True
        assert reward == 100.0
        assert s.money == 100.0
        assert s.has_checked_in_today() is True
        ok2, reward2 = s.daily_checkin(100.0)
        assert ok2 is False
        assert reward2 == 0.0
        assert s.money == 100.0

    def test_checkin_resets_next_day(self):
        s = PetState()
        s.checkin_date = "2000-01-01"
        assert s.has_checked_in_today() is False
        ok, _ = s.daily_checkin(100.0)
        assert ok is True

    def test_checkin_persisted(self):
        s = PetState()
        s.daily_checkin(100.0)
        s2 = PetState.from_dict(s.to_dict())
        assert s2.checkin_date == s.checkin_date


class TestGameReward:
    def test_daily_cap(self):
        s = PetState()
        s.money = 0.0
        total = 0.0
        for _ in range(6):
            total += s.add_game_reward(20)
        assert total == 100.0
        assert s.money == 100.0
        assert s.game_coin_remaining() == 0.0
        assert s.add_game_reward(20) == 0.0

    def test_partial_reward_when_near_cap(self):
        s = PetState()
        s.game_coin_daily_cap = 100.0
        s.add_game_reward(90)
        assert s.add_game_reward(20) == 10.0
        assert s.game_coin_remaining() == 0.0

    def test_resets_next_day(self):
        s = PetState()
        s.add_game_reward(100)
        s.game_coin_date = "2000-01-01"
        s.game_coin_count = 100.0
        assert s.game_coin_remaining() == 100.0
        assert s.add_game_reward(20) == 20.0

    def test_persisted(self):
        s = PetState()
        s.add_game_reward(40)
        s2 = PetState.from_dict(s.to_dict())
        assert s2.game_coin_count == 40.0
        assert s2.game_coin_date == s.game_coin_date
