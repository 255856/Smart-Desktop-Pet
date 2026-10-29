"""merge_similar 的索引刷新回归测试（Stage 2.8 agent 报的 bug）。

症状：`merge_similar()` 用向量索引算相似度，但**不刷新**它 ——
`add()` 只把索引标脏，`_ensure_backend_fresh()` 只在**检索**路径上调。
于是合并时后端的词表还是旧的（新记忆的词不在词表里）⇒
相似度恒为 0 ⇒ **一条都合并不了**。

修法：在 `merge_similar` 内部自己刷新（调用方不用绕过）。
"""
from __future__ import annotations

from app.brain.memory import MemoryStore


def test_merge_similar_refreshes_the_index_itself(tmp_path):
    """没先检索过（索引标脏）时，合并也必须真的合得掉。"""
    s = MemoryStore(tmp_path / "memory.db", backend_name="tfidf")
    s.add("主人喜欢喝拿铁", "preference", importance=0.6)
    s.add("主人喜欢喝拿铁咖啡", "preference", importance=0.8)
    s.add("主人讨厌香菜", "preference", importance=0.5)

    merged = s.merge_similar(threshold=0.5)

    assert merged >= 1, (
        "合并前没刷新向量索引 ⇒ 相似度恒为 0 ⇒ 什么都合并不了")
    assert s.count() == 2, s.count()
    # 保留的是 importance 最高的那条
    assert any(i.importance == 0.8 for i in s.all()), [i.content for i in s.all()]


def test_merge_similar_is_a_noop_when_nothing_is_similar(tmp_path):
    """不相似的不该被合并（刷新索引也不能乱合）。"""
    s = MemoryStore(tmp_path / "memory.db", backend_name="tfidf")
    s.add("主人喜欢喝拿铁", "preference", importance=0.6)
    s.add("明天要去医院复查", "fact", importance=0.5)
    merged = s.merge_similar(threshold=0.95)
    assert merged == 0
    assert s.count() == 2
