# -*- coding: utf-8 -*-
"""use_skill：模型能自己取技能；技能正文**不许**漏进 prompt。"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')


def _tool(skills_dir='skills'):
    from app.engine.tools import build_default_tools

    class _Stub:
        def get(self, *a, **k):
            return None

        def stats_summary(self):
            return 'stub'

    reg = build_default_tools(state=_Stub(), reminders=None, memory=None,
                              items=None, skills_dir=skills_dir)
    return reg._tools['use_skill']


def test_use_skill_returns_the_skill_body(qapp, tmp_path):
    """调它 → 拿到技能正文（带 [Skill: name] 头）。"""
    t = _tool()
    out = t.fn(name='poem')
    assert '[Skill: poem]' in out, out[:200]
    assert '七言绝句' in out, out[:200]


def test_use_skill_lists_available_when_name_missing(qapp, tmp_path):
    t = _tool()
    out = t.fn(name='')
    assert '可用：' in out, out[:200]
    assert 'poem' in out


def test_use_skill_rejects_unknown_name(qapp, tmp_path):
    t = _tool()
    out = t.fn(name='没有这个技能')
    assert '没有叫' in out, out[:200]


def test_prompt_catalogue_has_names_but_not_skill_bodies(qapp, tmp_path):
    """提示里只列**名字** —— poem 的正文一旦漏进去就会改掉人设。"""
    from app.brain.prompt_layer import build_dynamic_block
    block = build_dynamic_block(skills_dir='skills')
    assert 'use_skill' in block, block[-300:]
    assert 'poem' in block, block[-300:]
    # 关键：技能**正文**不许出现
    assert '七言绝句' not in block, block[-300:]
    assert '用一首' not in block, block[-300:]
