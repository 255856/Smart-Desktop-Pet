# -*- coding: utf-8 -*-
"""update_self —— 模型改自己的三个区域（身份 / 价值 / 程序性）。

为什么单独一个工具（不塞进 §remember_fact§）：

* 「记事实」和「改自己是谁」是两种完全不同的动作，混在一个工具里模型会乱用；
* 改自己的分区要**限速** —— 她的人格是慢慢长出来的，不是每轮重写一遍；
* 改写 = §supersede§（旧条标记过时），不是删 —— 主人能在面板上看到演变过程。

三个区域就是记忆卡的三种 kind（跟 话 / 话题 / 事件 / 事实·偏好 / 印象 平级）：
§identity§（自我认知）/ §value§（价值观）/ §procedural§（行事风格与目标）。
"""
from __future__ import annotations

import logging

from app.brain.self_memory import (
    MAX_EDITS_PER_DAY, REGION_HEADERS, SELF_KINDS, VALUE_LEVEL_LABELS,
    VALUE_LEVELS, edits_today, invalidate_cache, make_key, record_edit,
)

from ._core import ToolRegistry
from ._decorator import tool

log = logging.getLogger(__name__)


def _client_error(client, what: str) -> str:
    """客户端失败时的统一说辞（**不是异常**：模型要能如实告诉主人）。"""
    return "错误：记忆服务不可用，%s（%s）。" % (what,
                                            getattr(client, "last_error", "") or "未知原因")


def _update_self(client, region, content, action, target, level, reason) -> str:
    """update_self 的实现。"""
    region = str(region or "").strip().lower()
    if region not in SELF_KINDS:
        return "错误：region 只能是 identity / value / procedure。"
    action = str(action or "add").strip().lower()
    if action not in ("add", "revise", "remove"):
        return "错误：action 只能是 add / revise / remove。"
    text = str(content or "").strip()
    if action != "remove" and not text:
        return "错误：要写的内容是空的，什么都没改。"
    level = str(level or "").strip()
    if region == "value" and level and level not in VALUE_LEVELS:
        return "错误：value 的 level 只能是 %s。" % " / ".join(VALUE_LEVELS)

    # 限速：人格是慢慢长出来的
    done = edits_today(region)
    if len(done) >= MAX_EDITS_PER_DAY:
        return ("错误：%s 这个区域今天已经改了 %d 次（每天最多 %d 次）——"
                "你的人格是慢慢长出来的，这一次先不改。想调整的话明天再来，"
                "或者请主人帮你改。"
                % (REGION_HEADERS.get(region, region), len(done),
                   MAX_EDITS_PER_DAY))

    if client is None:
        return _client_error(None, "拿不到记忆客户端")

    try:
        if action == "remove":
            target_id = str(target or "").strip()
            if not target_id:
                return "错误：要删哪一条？把它的 id 填进 target。"
            n = client.delete([target_id])
            if not n:
                return _client_error(client, "没能删掉这条自我记忆")
            record_edit(region, target_id)
            invalidate_cache()
            return ("已删除（%s 里的 id=%s 不再生效）"
                    % (REGION_HEADERS.get(region, region), target_id))

        if action == "revise":
            target_id = str(target or "").strip()
            if not target_id:
                return "错误：要改哪一条？把它的 id 填进 target。"
            old = None
            try:
                for row in client.list_cards(limit=200) or []:
                    if isinstance(row, dict) and str(row.get("id") or "") == target_id:
                        old = row
                        break
            except Exception:  # noqa: BLE001
                old = None
            if old is None:
                return ("错误：找不到 id=%s 那条自我记忆 —— 先用 recall_memory "
                        "看看现在有哪些再改。" % target_id)
            key = str(old.get("key") or "") or make_key(region, text, level)
            card = client.make_card(text, key=key, kind=region, importance=0.75,
                                    category=level or "self")
            if not client.supersede(target_id, card):
                return _client_error(client, "没能改写旧条目")
            record_edit(region, card.get("id") or target_id)
            invalidate_cache()
            return ("已改写（%s：旧 id=%s 标记过时，新 id=%s）"
                    % (REGION_HEADERS.get(region, region), target_id,
                       card.get("id") or "?"))

        # add
        key = make_key(region, text, level)
        written = client.add(text, key=key, kind=region, importance=0.75,
                              category=level or "self")
        if not written:
            return _client_error(client, "这条没能存进记忆库")
        record_edit(region, written.get("id") or "")
        invalidate_cache()
        label = VALUE_LEVEL_LABELS.get(level, "") if region == "value" else ""
        return ("已记下（%s%s id=%s）：%s"
                % (REGION_HEADERS.get(region, region),
                   ("「%s」" % label) if label else "",
                   written.get("id") or "?", text))
    except Exception as e:  # noqa: BLE001
        log.exception("update_self 失败")
        return "错误：改自己的记忆时出错了（%s）。请不要告诉主人「已改」。" % e


def register(reg: ToolRegistry, *, memory=None) -> None:
    """注册 update_self（memory = sidecar 客户端；不给就用默认客户端）。"""
    from app.engine.tools._memory import _as_client

    @tool(
        description=(
            "修改**你自己**的三个区域（这是长期记忆，不是聊天记录）："
            "identity=自我认知（我是谁、我跟主人什么关系）；"
            "value=价值观（底线 / 偏好 / 风格，level 用 "
            "bottom_line/preference/style）；"
            "procedural=程序性（做某事的固定套路、你的行事风格与目标）。"
            "action: add（新增一条）/ revise（改写 target 那条）/ remove（删掉 target）。"
            "target: revise/remove 要改的那条的 id（先用 recall_memory 查）。"
            "每天每个区域最多改 2 次 —— 你是慢慢长出来的，别每轮重写自己。"
            "改写用 revise（旧条会被标记过时，不是删）。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "region": {"type": "string", "enum": list(SELF_KINDS),
                           "description": "要改哪个区域"},
                "content": {"type": "string",
                            "description": "新的内容（一句话；remove 时可空）"},
                "action": {"type": "string",
                           "enum": ["add", "revise", "remove"],
                           "description": "新增 / 改写 / 删除"},
                "target": {"type": "string",
                           "description": "revise/remove 的目标 id"},
                "level": {"type": "string", "enum": list(VALUE_LEVELS),
                          "description": "仅 value 用：底线 / 偏好 / 风格"},
                "reason": {"type": "string",
                            "description": "为什么改（你自己的说法，给人看）"},
            },
            "required": ["region", "content"],
        },
    )
    def update_self(region: str, content: str = "", action: str = "add",
                    target: str = "", level: str = "", reason: str = "") -> str:
        return _update_self(_as_client(memory), region, content, action,
                            target, level, reason)

    reg.register(update_self)

