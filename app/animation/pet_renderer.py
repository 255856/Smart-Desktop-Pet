"""PetRenderer 抽象基类 —— sprite / live2d 双渲染器共享接口。

设计目标：把现有的 PetAnimator（sprite 实现）抽成接口，让 Live2DRenderer（QWebEngineView
+ Cubism Web SDK）实现同一套方法。PetWindow 持有 PetRenderer 引用，对两种实现无感知。

接口覆盖：
    - set_idle / set_emotion / set_sleep / set_wake / set_thinking
    - play_animation / play_reaction / play_eat / play_file / play_spin / play_stretch / play_jump / play_swim
    - start_drag / end_drag / set_walk
    - get_widget() —— 返回要嵌入到 PetWindow 的子 QWidget
    - shutdown() —— 清理资源

调用方 (PetWindow) 只用 PetRenderer 类型，不 import 具体实现。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from app.core.qt_compat import QWidget


class PetRenderer(ABC):
    """所有桌宠渲染器的统一接口。"""

    # ----- 状态切换 -----
    @abstractmethod
    def set_idle(self) -> None:
        """切到主待机（呼吸/眨眼由渲染器内部处理）。"""

    @abstractmethod
    def set_sleep(self) -> None:
        """进入睡觉状态（闭嘴 + 趴下）。"""

    @abstractmethod
    def set_wake(self) -> None:
        """从睡觉醒来。"""

    @abstractmethod
    def set_thinking(self) -> None:
        """进入思考状态（抬眉 + 眼珠下移）。"""

    @abstractmethod
    def set_emotion(self, name: str) -> None:
        """切换情绪（happy/sad/angry/shy/think/pride/fear/doubt/surprise）。"""

    def set_emotion_reaction(self, emotion: str) -> None:
        """播放一次性情绪反应 + 回到 idle（默认实现 = set_emotion）。"""
        # sprite 默认实现调 set_emotion；Live2D 可重写
        self.set_emotion(emotion)

    # ----- 渲染器能力查询（菜单自适应用） -----
    def get_emotion_options(self) -> list[tuple[str, str]]:
        """返回当前渲染器支持的 (id, 标签) 情绪选项。

        sprite: ('happy', '开心') 等固定项
        live2d: 从模型 expressions 自动枚举 + 通用别名映射
        """
        return [
            ('happy', '开心'), ('sad', '悲伤'), ('angry', '生气'),
            ('shy', '害羞'), ('think', '思考'),
        ]

    def reset_emotion(self) -> None:
        """恢复自然表情（清除当前持续情绪）。

        sprite 没有持续叠加的情绪参数，默认 no-op；Live2D 重写为表情参数归零。
        """

    def supports_hairstyles(self) -> bool:
        """是否支持模型自带发型切换（Live2D 视模型而定，sprite 不支持）。"""
        return False

    def get_hairstyle_options(self) -> list[tuple[str, str]]:
        """返回 (id, 标签) 发型选项；不支持时返回空（菜单据此隐藏“发型”子菜单）。"""
        return []

    def set_hairstyle(self, name: str) -> None:
        """切换发型（Live2D 模型自带发型预设）。sprite 默认 no-op。"""

    # ----- 分类外观菜单（Live2D 专属能力，sprite 返回空 = 菜单不渲染这部分） -----
    def get_menu_groups(self) -> list[dict]:
        """分类外观子菜单数据：[{"id","label","mode","items":[(id,label)]}, ...]。"""
        return []

    def activate_menu_item(self, group_id: str, item_id: str) -> None:
        """菜单/设置页点击一个分类条目（sprite 默认 no-op）。"""

    def get_active_items(self) -> set:
        """当前激活的分类条目名集合（供设置页 chip 初始化选中态）。sprite 默认空。"""
        return set()

    def reset_all_appearance(self) -> None:
        """复位全部外观（sprite 默认等价于 reset_emotion）。"""
        self.reset_emotion()

    def get_play_options(self) -> list[tuple[str, str]]:
        """「玩一下」菜单数据：[(动作名, 标签)]。sprite 无默认实现（菜单自己列）。"""
        return []

    # ----- 触发场景动作（Live2D 可视化配置；sprite 仅把情绪场景映射到 set_emotion） -----
    # 聊天情绪场景 → sprite 情绪键
    _SPRITE_CHAT_EMOTION = {
        "chat_happy": "happy", "chat_sad": "sad", "chat_angry": "angry",
        "chat_shy": "shy", "chat_surprised": "surprised", "chat_thinking": "think",
    }

    def trigger_scene(self, scene_id: str, hold_ms: Optional[int] = None) -> None:
        """触发一个固定场景（情绪/开机/提醒/闲置/深夜等）。

        Live2D 会按设置面板里配置的外观组合播放；sprite 仅支持把聊天情绪映射到
        set_emotion，其余状态/互动场景由原有专用方法承担，这里 no-op。
        """
        emo = self._SPRITE_CHAT_EMOTION.get(scene_id)
        if emo:
            self.set_emotion(emo)

    def get_custom_actions(self) -> list:
        """自定义动作列表（仅 Live2D 场景配置支持，sprite 恒为空）。"""
        return []

    def restore_scene_appearance(self) -> None:
        """退出环境场景（闲置/深夜）后恢复自然（sprite 回自然表情）。"""
        self.reset_emotion()


    def play_custom_action(self, cid: str) -> None:
        """播放一个自定义动作（sprite 无此能力，no-op）。"""

    # ----- 挂机随机表情（Live2D 专属，sprite 默认不支持） -----
    def set_random_expressions(self, enabled: bool) -> None:
        """开关挂机随机表情。sprite 默认 no-op。"""

    def is_random_expressions_enabled(self) -> bool:
        return False

    def note_activity(self) -> None:
        """外部交互后通知渲染器（随机表情计时重置）。默认 no-op。"""

    def set_talking(self, on: bool) -> None:
        """口型同步开关（说话时嘴开合）。sprite 默认 no-op。"""

    def get_renderer_type(self) -> str:
        """当前渲染器类型（'sprite' | 'live2d'）——给菜单显示。"""
        return type(self).__name__.replace('Renderer', '').lower()

    # ----- 一次性动作 -----
    @abstractmethod
    def play_reaction(self, where: str) -> None:
        """对触摸反应（'head' 或 'body'）。播完后回到之前状态。"""

    @abstractmethod
    def play_animation(self, anim_name: str) -> None:
        """播放一次性动作动画（'jump'/'stretch'/'spin'/'swim'/'eat'/'file' 等）。"""

    @abstractmethod
    def play_eat(self) -> None:
        """吃饭（嘴张开 + 头点）。"""

    @abstractmethod
    def play_file(self) -> None:
        """吃文件（播放一次后回到待机）。"""

    @abstractmethod
    def play_spin(self) -> None:
        """转一圈。"""

    @abstractmethod
    def play_stretch(self) -> None:
        """伸懒腰。"""

    @abstractmethod
    def play_jump(self) -> None:
        """起跳。"""

    @abstractmethod
    def play_swim(self) -> None:
        """游泳。"""

    # ----- 拖动 / 行走 -----
    @abstractmethod
    def start_drag(self) -> None:
        """开始拖动（持续中循环显示拖动动画）。"""

    @abstractmethod
    def end_drag(self) -> None:
        """结束拖动（播放 Drag_2 收尾动作）。"""

    @abstractmethod
    def set_walk(self, direction: str) -> None:
        """切到走路动画（direction='left'/'right'）。"""

    # ----- 表情参数（Live2D 专用，sprite 用 set_emotion 即可） -----
    def set_parameter(self, name: str, value: float, duration_ms: int = 0) -> None:
        """设置 Live2D 参数。sprite 实现可以 no-op。

        Args:
            name: 参数 ID（如 'ParamAngleX'）
            value: 0.0~1.0
            duration_ms: 渐变到目标值的时长；0 表示立即
        """
        # 默认 no-op；Live2DRenderer 重写

    def set_expression(self, name: str) -> None:
        """Live2D 模型专用：直接切到 expression 文件。"""
        # 默认 no-op

    def set_emotion_reaction(self, emotion: str) -> None:
        """播放一次性情绪反应 + 回到 idle（默认实现 = set_emotion）。"""
        # sprite 默认实现调 set_emotion；Live2D 可重写
        self.set_emotion(emotion)

    # ----- Qt 集成 -----
    @abstractmethod
    def get_widget(self) -> QWidget:
        """返回要嵌入到 PetWindow 的子 QWidget。"""

    # ----- 生命周期 -----
    @abstractmethod
    def shutdown(self) -> None:
        """清理资源（停止动画 timer / 释放 WebView 等）。"""

    # ----- 状态查询 / 控制（默认实现；子类按需重写） -----
    def is_sleeping(self) -> bool:
        """是否在睡觉（用于 state.tick 跳过衰减等逻辑）。默认 False。"""
        return False

    def is_lock_first_idle(self) -> bool:
        """是否锁定第一帧 idle（默认 False）。"""
        return False

    def set_lock_first_idle(self, on: bool) -> None:
        """锁定第一帧 idle。默认 no-op（sprite 的 PetAnimator 实际 no-op）。"""
        pass

    # ----- 可选能力 -----
    def supports_expression_listing(self) -> bool:
        """是否支持列出模型可用 expression（Live2D 是，sprite 一般不是）。"""
        return False

    def list_expressions(self) -> list[str]:
        """列出模型可用 expression 名字（Live2D 专用）。"""
        return []


__all__ = ["PetRenderer"]