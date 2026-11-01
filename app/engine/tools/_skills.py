# -*- coding: utf-8 -*-
"""use_skill —— 模型自己取一个技能（行为覆盖包）来用。

为什么要有这个工具：
  skills/ 目录里的技能（poem / systematic-debugging / domain-modeling …）
  以前**只能靠主人手动打 /skills/<名字>** 才会生效 —— 模型自己看不到、
  也不会用。有了这个工具，模型看到任务对味就自己去取。

技能正文就是「行为覆盖包」：取回来之后**这一轮**按它说的做。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ._core import ToolRegistry
from ._decorator import tool

log = logging.getLogger(__name__)

#: 项目里的 skills 目录（默认由主程序注入）
_SKILLS_DIR: Optional[Path] = None


def set_skills_dir(path) -> None:
    """设置 skills 目录（主程序启动时调一次）。"""
    global _SKILLS_DIR
    _SKILLS_DIR = Path(path) if path else None


def register(reg: ToolRegistry, skills_dir=None) -> None:
    """注册 use_skill 工具。"""
    if skills_dir is not None:
        set_skills_dir(skills_dir)

    @tool(
        description="取一个技能（行为覆盖包）来用。 skills/ 目录里每个 .md 就是"
                    "一个技能（例如 domain-modeling 建模、systematic-debugging "
                    "排障、poem 用诗回话）。 觉得任务对味就调用它，工具会把这轮"
                    "该守的规矩原样返回给你。"
    )
    def use_skill(name: str) -> str:
        """按名字取一个技能。"""
        from app.brain.skills import list_skills, load_skill

        d = _SKILLS_DIR
        if d is None:
            return "错误：技能目录没配好（主程序没注入 skills_dir）。"
        nm = str(name or "").strip().strip("/")
        if not nm:
            avail = list_skills(d)
            return "错误：要用哪个技能？可用：%s" % ("、".join(avail) or "（没有）")
        try:
            text = load_skill(d, nm)
        except Exception as e:  # noqa: BLE001
            return "错误：取技能失败：%s" % (str(e) or type(e).__name__)
        if not text:
            avail = list_skills(d)
            return ("错误：没有叫 %s 的技能。可用：%s"
                    % (nm, "、".join(avail) or "（没有）"))
        return text

    reg.register(use_skill)
