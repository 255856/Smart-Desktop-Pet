# -*- coding: utf-8 -*-
"""记忆闸门的「价目表」：**每一类记忆各有一条阈值线**（按类别标定，不是拍脑袋）。

为什么要有这张表
----------------
闸门原来只有**一套全局阈值**：余弦门禁（绝对）+ 相对补位门槛 + 语义保命符。
问题出在「近似陷阱」上：语料里有 3 条「看着像、答的不是这回事」的卡
（scripts/eval_memory_recall.py 的 d01 / d02 / m06 / m23）。

把全局门槛收紧是最直接的路，但**会连坐**：评测台实测把全局 min_score_ratio
抬到 0.6，陷阱是避开了，recall@6 却从 1.000 掉到 0.762 —— 被裁掉的是
复合问句的另外一半（「我同事和老板分别是谁」里的同事、「我身体最近怎么样」
里的睡眠），它们跟陷阱**不在同一类**里。

按类别分线就能两全：陷阱住在 fact 这一类，就把 fact 这一类收紧；
impression / topic 留着宽松的值（复合问句的另外一半大多住在这儿）。

表里的数怎么来的（**量出来的**）
--------------------------------
尺子：scripts/eval_memory_recall.py（32 条语料 / 20 条标注问句 / 3 条陷阱）。
每一类分别量两个比值（都以「这一轮的最高分 / 最高余弦」为 1）：

    分数比 = 该行最终分 / 这一轮最高分
    语义比 = 该行余弦   / 这一轮最高余弦

然后取：

    min_score_ratio  ：要压过这一类的陷阱分数比，同时给这一类的相关行
                       留 >= 15% 余量（相关行靠**分数**就能活，不靠保命符救）
    dense_keep_ratio ：语义保命符。**只有当这一类的陷阱会被它救回来时才关掉**
                       （开着就等于把陷阱一起救回来）

实测记录（2026-11-04，bge-small-zh-v1.5 + bge-reranker-v2-m3）：

    kind         相关行分数比         陷阱分数比  陷阱语义比  取值
    ------------ -------------------- ---------- ---------- -----------------
    fact         m14 0.358 / m30      d02 0.224   1.000       0.28 / 0（关保命符）
                 0.658，其余 1.000
    impression   m12 0.103 / m03      m23 0.004   0.653       0.08 / 0.75
                 0.258，其余 1.000
    topic        m27 0.420，其余      m06 0.0003  0.690       0.30 / 0.75
                 1.000
    event        语料里没有这一类     —           —           用全局默认
    thought      语料里没有这一类     —           —           用全局默认

余弦门禁（min_similarity）**不分类别**：它是「这一问跟记忆库有没有关系」的
**整轮**判断。实测相关行的余弦下限是 0.349（fact 的 m14）/ 0.436（impression）
/ 0.486（topic），比 0.42 低 —— 分类别设门禁必然切掉真答案，所以它留在全局。

怎么覆盖（优先级从低到高）
--------------------------
    1. 默认表 KIND_GATE_TABLE
    2. 环境变量 DESKTOP_PET_MEMORY_KIND_GATE（JSON）里的 "*"（通配，覆盖所有类别）
    3. 环境变量里的具体类别
    4. 构造参数 MemoryStore(..., kind_gate={...}) 里的 "*"
    5. 构造参数里的具体类别
    6. 全局旋钮（--min-score-ratio / DESKTOP_PET_MEMORY_MIN_SCORE_RATIO /
       DESKTOP_PET_MEMORY_DENSE_KEEP_RATIO）**显式给了值**时接管对应的那一列
       —— 想「一把梭」收紧全部类别，用它们就行。
"""
from __future__ import annotations

import json
import os

#: 这张表的版本号（改动阈值时顺手改，方便 /stats 里看出跑的是哪一版）
KIND_GATE_TABLE_VERSION = "2026-11-05"

#: 环境变量：整张表用 JSON 覆盖
KIND_GATE_ENV = "DESKTOP_PET_MEMORY_KIND_GATE"

#: 每类记忆的闸门阈值（见模块文档的实测记录）
#:   min_score_ratio  ：最终分低于「这一轮最高分 × 该值」的行不返回
#:   dense_keep_ratio ：语义保命符 —— 余弦 >= 「这一轮最高余弦 × 该值」的行
#:                      即使分数低也保留（0 = 关掉）
KIND_GATE_TABLE = {
    "fact": {"min_score_ratio": 0.28, "dense_keep_ratio": 0.0},
    "impression": {"min_score_ratio": 0.08, "dense_keep_ratio": 0.75},
    "topic": {"min_score_ratio": 0.30, "dense_keep_ratio": 0.75},
    # 语料里没有 event / thought —— 先沿用全局默认，等有数据再标定
    "event": {"min_score_ratio": 0.12, "dense_keep_ratio": 0.75},
    "thought": {"min_score_ratio": 0.12, "dense_keep_ratio": 0.75},
    # 程序性记忆（工具序列）：新类别，语料里还没有 —— 同样先沿用全局默认
    "procedural": {"min_score_ratio": 0.12, "dense_keep_ratio": 0.75},
}


def _clean_entry(entry, fallback_ratio, fallback_dense):
    """把一条表项洗成两个合法浮点数（认 min_score_ratio / dense_keep_ratio）。"""
    if not isinstance(entry, dict):
        entry = {}
    ratio = entry.get("min_score_ratio", fallback_ratio)
    dense = entry.get("dense_keep_ratio", fallback_dense)
    try:
        ratio = float(ratio)
    except (TypeError, ValueError):
        ratio = float(fallback_ratio)
    try:
        dense = float(dense)
    except (TypeError, ValueError):
        dense = float(fallback_dense)
    ratio = max(0.0, min(1.0, ratio))
    dense = max(0.0, min(1.0, dense))
    return {"min_score_ratio": ratio, "dense_keep_ratio": dense}


def _load_env_table(env=None):
    """读环境变量里的 JSON 表。**填坏了就当没设**（不许把服务搞挂）。"""
    raw = os.environ.get(KIND_GATE_ENV) if env is None else env
    raw = str(raw or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def resolve_kind_gate(table=None, env=None,
                      ratio_override=None, dense_override=None,
                      fallback_ratio=0.12, fallback_dense=0.75):
    """合成「最终生效的按类别闸门表」。

    返回 {kind: {"min_score_ratio": float, "dense_keep_ratio": float}}。
    表里没有的类别，闸门会退回全局旋钮（min_score_ratio / dense_keep_ratio）。
    """
    merged = {}
    for kind, entry in KIND_GATE_TABLE.items():
        merged[str(kind)] = _clean_entry(entry, fallback_ratio, fallback_dense)

    for source in (_load_env_table(env), table):
        if not isinstance(source, dict):
            continue
        wildcard = source.get("*")
        if isinstance(wildcard, dict):
            # 通配：覆盖所有类别（连默认表里的也一起）
            for kind in merged:
                entry = dict(wildcard)
                entry.update(merged[kind])
                merged[kind] = _clean_entry(entry, fallback_ratio, fallback_dense)
        for kind, entry in source.items():
            kind = str(kind).strip()
            if not kind or kind == "*":
                continue
            merged[kind] = _clean_entry(entry, fallback_ratio, fallback_dense)

    if ratio_override is not None:
        try:
            ratio = max(0.0, min(1.0, float(ratio_override)))
            for entry in merged.values():
                entry["min_score_ratio"] = ratio
        except (TypeError, ValueError):
            pass
    if dense_override is not None:
        try:
            dense = max(0.0, min(1.0, float(dense_override)))
            for entry in merged.values():
                entry["dense_keep_ratio"] = dense
        except (TypeError, ValueError):
            pass
    return merged


__all__ = [
    "KIND_GATE_TABLE",
    "KIND_GATE_TABLE_VERSION",
    "KIND_GATE_ENV",
    "resolve_kind_gate",
]
