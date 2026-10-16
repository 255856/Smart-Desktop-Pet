"""桌宠养成数值配置 (v23-2 创新点 2 数值参数化)。

主人 2026-10-16 反馈: 数值 + 范围 + 公式 + 钩子权重都要走 yaml, 不要 hardcode.

设计原则:
  - 单源真相: `app/core/companion_config.yaml` 是默认表, 仓库里提交
  - 用户覆盖: `data/companion_config.yaml` 存在时优先 (不提交, 用户本地调试用)
  - dataclass 强类型: 读 yaml 后用 dataclass 包, 业务代码不要直接读 yaml
  - 不会抛: 文件不存在 / 字段缺失走默认值, log.warning 而不是 raise

加载顺序 (优先级 高 → 低):
    1. `data/companion_config.yaml` (用户本地覆盖)
    2. `app/core/companion_config.yaml` (仓库默认)
    3. 内置 _DEFAULTS (兜底, 万一 yaml 都没了)

用法:
    from app.core.companion_config import companion_config
    delta = companion_config.event_hooks.task_success.mood  # = 8.0
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 内置默认值 (兜底) — 跟主人 E:\study\日志.txt + 日志.txt 数值方案一致
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Range:
    """单维度数值范围 + 初始值。"""
    minimum: float = 0.0
    maximum: float = 100.0
    initial: float = 50.0

    def clamp(self, v: float) -> float:
        return max(self.minimum, min(self.maximum, v))


@dataclass(frozen=True)
class _QuantizeBand:
    """量化阈值。"""
    low_below: float = 33.0    # <low_below → LOW
    mid_below: float = 67.0    # <mid_below → MID, 否则 HIGH
    hysteresis_low_to_mid: float = 40.0  # prev=LOW → 涨到这值才回 MID
    hysteresis_high_to_mid: float = 60.0  # prev=HIGH → 跌到这值才回 MID


@dataclass(frozen=True)
class _LevelingCfg:
    """等级 + 经验公式。

    公式: exp_to_next(level) = base * (level ** exp)
      主人方案: 100 * (level ** 1.5)
    """
    base: float = 100.0
    exp: float = 1.5
    max_level: int = 10
    exp_per_success: float = 20.0
    exp_per_accepted: float = 15.0
    exp_per_positive_feedback: float = 15.0
    exp_per_continuous_7d: float = 50.0


@dataclass(frozen=True)
class _TimeDecayCfg:
    """时间驱动衰减 (主人 2026-10-16 要求每小时减饱食度)。"""
    satiety_per_hour: float = -0.833    # 24h -20 折算 per hour ≈ -0.833
    mood_no_interact_48h: float = -5.0
    mood_low_satiety_extra_per_24h: float = -5.0  # sat<30 时额外扣
    check_interval_seconds: int = 600              # 每 10 分钟检查一次


@dataclass(frozen=True)
class _HookDeltas:
    """单条事件钩子的 4 维数值增量 (per-run 颗粒, 主人 2026-10-16 选)。"""
    satiety: float = 0.0
    mood: float = 0.0
    favor: float = 0.0
    exp: float = 0.0


@dataclass(frozen=True)
class _EventHooks:
    """事件驱动钩子 — 主人 2026-10-16 确认 4 类 (含 task_success/fail + 用户 accept/reject + 喂食/互动)。

    数值权重默认取 自 E:\\study\\日志.txt:
      - 任务成功: 心情 +8, 经验 +20, 好感 +1
      - 任务失败: 心情 -12, 好感 -3
      - 用户拒绝: 心情 -10, 好感 -2
      - 用户采纳: 心情 +6, 好感 +3
      - 用户喂食: 饱食 +15, 心情 +5
      - 用户互动: 心情 +3, 好感 +2
    """
    task_success: _HookDeltas = field(default_factory=lambda: _HookDeltas(mood=8.0, exp=20.0, favor=1.0))
    task_failure: _HookDeltas = field(default_factory=lambda: _HookDeltas(mood=-12.0, favor=-3.0))
    user_rejected: _HookDeltas = field(default_factory=lambda: _HookDeltas(mood=-10.0, favor=-2.0))
    user_accepted: _HookDeltas = field(default_factory=lambda: _HookDeltas(mood=6.0, favor=3.0))
    user_feed: _HookDeltas = field(default_factory=lambda: _HookDeltas(satiety=15.0, mood=5.0))
    user_interact: _HookDeltas = field(default_factory=lambda: _HookDeltas(mood=3.0, favor=2.0))


@dataclass(frozen=True)
class _FavorRules:
    """好感维度额外决策规则 (主人 2026-10-16 加, 替换原 trust 维度)。

    - high_favor (≥ high_threshold) → 高风险工具可额外 ALLOW (主人长期信任)
    - low_favor  (< low_threshold)  → 任意 MEDIUM+ 工具都走 CONFIRM (桌宠
                                       没建立信任关系, 不要放行)
    """
    high_threshold: float = 80.0
    low_threshold: float = 30.0
    high_allows_irreversible: bool = False   # high_favor 即使 ≥80, 不可逆仍需 confirm
    low_forces_confirm_medium: bool = True   # low_favor 时, MEDIUM 也要 confirm


@dataclass(frozen=True)
class _PersistenceCfg:
    """持久化。"""
    file: str = "data/pet_state.json"
    write_on_update: bool = True
    write_min_interval_seconds: int = 5      # 防抖: 短时间内多次更新只写一次


@dataclass(frozen=True)
class _TraceCfg:
    """trace 日志 (v23-1 接好, v23-7 加 UI)。"""
    enabled: bool = True
    log_file: str = "data/companion_log.txt"
    max_lines_in_memory: int = 500           # trace 页读盘后保留多少行


@dataclass(frozen=True)
class CompanionConfig:
    """桌宠养成数值配置顶层 dataclass。"""
    satiety: _Range = field(default_factory=lambda: _Range(0, 100, 80))
    mood: _Range = field(default_factory=lambda: _Range(0, 100, 70))
    level: _Range = field(default_factory=lambda: _Range(1, 10, 1))
    favor: _Range = field(default_factory=lambda: _Range(0, 100, 50))   # 好感 (替换 trust)
    quantize: _QuantizeBand = field(default_factory=_QuantizeBand)
    leveling: _LevelingCfg = field(default_factory=_LevelingCfg)
    time_decay: _TimeDecayCfg = field(default_factory=_TimeDecayCfg)
    event_hooks: _EventHooks = field(default_factory=_EventHooks)
    favor_rules: _FavorRules = field(default_factory=_FavorRules)
    persistence: _PersistenceCfg = field(default_factory=_PersistenceCfg)
    trace: _TraceCfg = field(default_factory=_TraceCfg)


# ---------------------------------------------------------------------------
# yaml 加载
# ---------------------------------------------------------------------------

def _try_yaml_import():
    try:
        import yaml  # type: ignore
        return yaml
    except ImportError:
        return None


def _default_yaml_path() -> Path:
    """仓库内默认 yaml。"""
    return Path(__file__).resolve().parent / "companion_config.yaml"


def _user_yaml_path() -> Path:
    """用户本地覆盖 yaml (data/ 目录, 不提交)。"""
    return Path("data") / "companion_config.yaml"


def _read_yaml(path: Path) -> Optional[dict]:
    """读 yaml 文件, 不存在 / 出错返 None。"""
    if not path.is_file():
        return None
    yaml = _try_yaml_import()
    if yaml is None:
        log.warning("PyYAML 没装, 跳过 yaml 配置读取: %s", path)
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001
        log.warning("读 yaml 配置失败 %s: %s", path, e)
        return None


def _merge(base: dict, override: dict) -> dict:
    """递归合并 dict (override 覆盖 base)。

    浅拷贝, 不修改原 dict。list / scalar 直接 override 替换。
    """
    out = dict(base)
    for k, v in (override or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _build_from_dict(d: dict) -> CompanionConfig:
    """dict → CompanionConfig dataclass。

    每个段都建独立 dataclass, 段内字段缺失走 dataclass 自己的 default.
    字段名不匹配时静默忽略 (yaml 用户写错字段不抛, 走内置默认)。

    嵌套 dataclass (e.g. _EventHooks.task_success 是 _HookDeltas) 走手工
    构造, 不能光 _EventHooks(**sub), 不然子字段会落成 dict.
    """
    if not d:
        return CompanionConfig()

    def _filter_kwargs(cls, kwargs):
        """过滤掉 cls 不认识的字段, 避免 TypeError。"""
        import dataclasses
        field_names = {f.name for f in dataclasses.fields(cls)}
        known = {k: v for k, v in kwargs.items() if k in field_names}
        unknown = set(kwargs) - field_names
        if unknown:
            log.debug("companion_config: 忽略未知字段 %s", unknown)
        return known

    def _r(key, cls):
        sub = d.get(key) or {}
        return cls(**_filter_kwargs(cls, sub))

    def _hook(name):
        sub = (d.get("event_hooks") or {}).get(name) or {}
        return _HookDeltas(**_filter_kwargs(_HookDeltas, sub))

    return CompanionConfig(
        satiety=_r("satiety", _Range),
        mood=_r("mood", _Range),
        level=_r("level", _Range),
        favor=_r("favor", _Range),
        quantize=_r("quantize", _QuantizeBand),
        leveling=_r("leveling", _LevelingCfg),
        time_decay=_r("time_decay", _TimeDecayCfg),
        event_hooks=_EventHooks(
            task_success=_hook("task_success"),
            task_failure=_hook("task_failure"),
            user_rejected=_hook("user_rejected"),
            user_accepted=_hook("user_accepted"),
            user_feed=_hook("user_feed"),
            user_interact=_hook("user_interact"),
        ),
        favor_rules=_r("favor_rules", _FavorRules),
        persistence=_r("persistence", _PersistenceCfg),
        trace=_r("trace", _TraceCfg),
    )


def load_companion_config() -> CompanionConfig:
    """读默认 yaml → 应用用户覆盖 → 构造 dataclass。

    缓存到模块级 singleton, 避免每次都 parse yaml。
    """
    global _cached
    if _cached is not None:
        return _cached

    base = _read_yaml(_default_yaml_path()) or {}
    user_override = _read_yaml(_user_yaml_path()) or {}
    merged = _merge(base, user_override)
    cfg = _build_from_dict(merged)
    _cached = cfg
    return cfg


_cached: Optional[CompanionConfig] = None


# ---------------------------------------------------------------------------
# 顶层 singleton: 业务代码直接 import `companion_config` 即可
# ---------------------------------------------------------------------------

def _reset_cache_for_tests() -> None:
    """测试用: 重新读 yaml。"""
    global _cached
    _cached = None


companion_config: CompanionConfig = load_companion_config()


__all__ = [
    "CompanionConfig", "_Range", "_QuantizeBand", "_LevelingCfg",
    "_TimeDecayCfg", "_HookDeltas", "_EventHooks", "_FavorRules",
    "_PersistenceCfg", "_TraceCfg",
    "load_companion_config", "_reset_cache_for_tests",
]
