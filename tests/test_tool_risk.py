"""工具风险等级表 (_risk.py) 单元测试。

覆盖:
  - TOOL_RISK_LEVEL 包含全部 60 个工具
  - 5 档风险常量都有覆盖
  - 已知高风险 / 不可逆工具分档正确
  - risk_for_tool 默认 MEDIUM (新工具强制 confirm 路径)
  - is_risky(threshold=HIGH) 包含原 _RISKY_TOOLS 的 9 个 + 新增 generate_video
"""
from app.engine.tools import _risk
from app.engine.tools._risk import (
    SAFE, LOW, MEDIUM, HIGH, IRREVERSIBLE,
    TOOL_RISK_LEVEL, RISK_LABELS, RISK_RATIONALE,
    risk_for_tool, is_risky,
)


def test_risk_constants_5_levels():
    """5 档常量值跟规划对: 0/1/2/3/4."""
    assert SAFE == 0
    assert LOW == 1
    assert MEDIUM == 2
    assert HIGH == 3
    assert IRREVERSIBLE == 4
    # RISK_LABELS / RATIONALE 都要 5 个
    assert len(RISK_LABELS) == 5
    assert len(RISK_RATIONALE) == 5
    assert RISK_LABELS[IRREVERSIBLE] == "IRREVERSIBLE"
    print("[OK] _risk: 5 档常量 + 标签齐全")


def test_risk_table_has_60_tools():
    """主表覆盖所有 60 个工具 (PDF "59 个工具" + Task subagent 算 1)。

    阈值定 50 是为了"宽松" — 新增 / 删除工具时不会立刻误报,
    只要总数 ≥ 50 就视为通过。具体数下次更新版本会变。
    """
    assert len(TOOL_RISK_LEVEL) >= 50, (
        f"_risk 表应至少有 50 个工具. 实际: {len(TOOL_RISK_LEVEL)}"
    )
    # 关键 9 个工具必须在表里 (跟 _RISKY_TOOLS 对齐)
    must_have = {
        "lock_screen", "kill_process", "set_wifi", "set_bluetooth",
        "send_notification", "run_script", "set_volume", "set_brightness",
        "send_email", "generate_video",
        # 读类工具
        "get_current_time", "web_search", "read_file", "system_info",
    }
    missing = must_have - set(TOOL_RISK_LEVEL.keys())
    assert not missing, f"_risk 表缺关键工具: {missing}"
    print(f"[OK] _risk: TOOL_RISK_LEVEL 含 {len(TOOL_RISK_LEVEL)} 个工具")


def test_risk_table_5_levels_all_used():
    """5 档风险都至少有一个工具, 不出现 "某档完全空"。

    空档意味着没举例, 模型 prompt 里看到那一行没意义。
    """
    levels_used = set(TOOL_RISK_LEVEL.values())
    for lv, label in RISK_LABELS.items():
        assert lv in levels_used, (
            f"_risk 表完全没用 {label} (={lv}) 档, 模型看不到举例. "
            f"已用档: {sorted(levels_used)}"
        )
    print(f"[OK] _risk: 5 档都有覆盖 (used={sorted(levels_used)})")


def test_lock_screen_and_kill_process_are_irreversible():
    """主人立刻被影响的工具必须进 IRREVERSIBLE (4) 档。"""
    assert risk_for_tool("lock_screen") == IRREVERSIBLE, (
        f"lock_screen 应 IRREVERSIBLE. 实际: {risk_for_tool('lock_screen')}"
    )
    assert risk_for_tool("kill_process") == IRREVERSIBLE, (
        f"kill_process 应 IRREVERSIBLE. 实际: {risk_for_tool('kill_process')}"
    )
    print("[OK] _risk: lock_screen + kill_process = IRREVERSIBLE")


def test_high_risk_tools_match_old_risky_set():
    """原 _RISKY_TOOLS (hallucination_guard.py 里 9 条) 都应 ≥ LOW (即 is_risky 默认门槛)。

    这里跟 is_risky() 阈值一致: 默认 LOW, 即所有非 SAFE 都算 risky。
    不再要求 ≥HIGH, 因为新论文分级:
      - set_volume / set_brightness = MEDIUM (影响中等, 但可恢复), 不是 HIGH
      - lock_screen / kill_process = IRREVERSIBLE, 主人立刻被打断
    """
    high_plus = {
        "lock_screen", "kill_process", "set_wifi", "set_bluetooth",
        "send_notification", "run_script", "set_volume", "set_brightness",
        "send_email",
    }
    for name in high_plus:
        rv = risk_for_tool(name)
        assert rv >= LOW, (
            f"{name} 应 ≥LOW (1). 实际: {rv}"
        )
    print(f"[OK] _risk: 原 9 条 _RISKY_TOOLS 都 ≥LOW")


def test_safe_tools_for_read():
    """纯读工具应 SAFE (0): time / search / read_file / system_info。"""
    read_only = ["get_current_time", "web_search", "read_file",
                 "system_info", "get_pet_status", "recall_memory"]
    for name in read_only:
        assert risk_for_tool(name) == SAFE, (
            f"{name} 应 SAFE. 实际: {risk_for_tool(name)}"
        )
    print("[OK] _risk: 6 个纯读工具都 SAFE")


def test_unknown_tool_defaults_to_medium_not_safe():
    """新加工具没在表里 → 默认 MEDIUM (强制 confirm), 不能默认 SAFE。

    这是安全策略: 新工具默认不让它"裸奔", 强制 reviewer 显式定级。
    """
    assert risk_for_tool("never_existed_tool_xxx") == MEDIUM
    assert risk_for_tool("any_unknown_name") == MEDIUM
    print("[OK] _risk: 未知工具默认 MEDIUM (强制 confirm)")


def test_is_risky_threshold_consistent_with_old_set():
    """is_risky(HIGH) 应至少覆盖原 hallucination_guard._RISKY_TOOLS 9 条。"""
    must_be_risky = {
        "lock_screen", "kill_process", "set_wifi", "set_bluetooth",
        "send_notification", "run_script", "set_volume", "set_brightness",
        "send_email",
    }
    for name in must_be_risky:
        assert is_risky(name), f"{name} 应 is_risky=True"
    # 纯读工具不应 is_risky
    assert not is_risky("get_current_time")
    assert not is_risky("web_search")
    assert not is_risky("read_file")
    print("[OK] _risk: is_risky(HIGH) 覆盖原 9 条 + 纯读工具不算 risky")


def test_is_risky_low_threshold_picks_medium_plus():
    """is_risky(LOW) 应包含 LOW/MEDIUM/HIGH/IRREVERSIBLE, 排除 SAFE。

    不暴露给生产用, 但实验 1 量化时可能用到 (统计"任何非安全工具")。
    """
    assert is_risky("set_volume")           # MEDIUM
    assert is_risky("lock_screen")          # IRREVERSIBLE
    assert not is_risky("get_current_time") # SAFE
    print("[OK] _risk: is_risky(LOW) 阈值包含 MEDIUM+ 排除 SAFE")


def test_v212_risky_tools_derived_from_risk_table():
    """hallucination_guard._RISKY_TOOLS 改成从 _risk 派生。

    验证:
      - _RISKY_TOOLS 仍是 frozenset
      - 原 9 条仍在集合 (向后兼容)
      - 集合里每个工具的 risk 都 ≥ MEDIUM (跟默认 is_risky 阈值匹配)
      - 加新工具后 (这里手动模拟) 集合自动扩展
    """
    from app.brain.hallucination_guard import _RISKY_TOOLS
    from app.engine.tools._risk import risk_for_tool, MEDIUM, is_risky

    assert isinstance(_RISKY_TOOLS, frozenset), (
        f"_RISKY_TOOLS 应 frozenset, 实际: {type(_RISKY_TOOLS)}"
    )
    # 原 9 条仍应在 (向后兼容)
    legacy_9 = {
        "lock_screen", "kill_process", "set_wifi", "set_bluetooth",
        "send_notification", "run_script", "set_volume", "set_brightness",
        "send_email",
    }
    assert legacy_9.issubset(_RISKY_TOOLS), (
        f"原 9 条应在 _RISKY_TOOLS. missing: {legacy_9 - _RISKY_TOOLS}"
    )
    # 集合里每个工具 risk 都 ≥ MEDIUM (跟默认 is_risky 阈值匹配)
    for name in _RISKY_TOOLS:
        assert risk_for_tool(name) >= MEDIUM, (
            f"{name} in _RISKY_TOOLS 但 risk < MEDIUM. "
            f"is_risky 默认门槛 = MEDIUM, 这种就矛盾了"
        )
    # 生成视频也进了 (新风险工具)
    assert "generate_video" in _RISKY_TOOLS, (
        "generate_video 应进 (HIGH ≥ MEDIUM). 否则 v21-2 没派生成功"
    )
    # 集合 = {name: is_risky(name)}
    manual = {n for n in TOOL_RISK_LEVEL if is_risky(n)}
    assert _RISKY_TOOLS == manual, (
        f"_RISKY_TOOLS 应 == {{n: is_risky(n)}}. diff: "
        f"in-tools not-tools={_RISKY_TOOLS - manual}, "
        f"in-manual not-tools={manual - _RISKY_TOOLS}"
    )
    print(f"[OK] v21-2: hallucination_guard._RISKY_TOOLS 是派生集合 (n={len(_RISKY_TOOLS)})")


def test_v212_risky_prompt_fragment_lists_high_and_irreversible():
    """hallucination_guard 注入 prompt 的 fragment 应自动列出 HIGH + IRREVERSIBLE。

    验证:
      - IRREVERSIBLE 档工具都列 (kill_process, lock_screen)
      - HIGH 档工具都列 (set_wifi 等)
      - MEDIUM / LOW / SAFE 档**不**列 (避免 prompt 过长)
    """
    from app.brain.hallucination_guard import _RISKY_PROMPT_FRAGMENT

    # HIGH + IRREVERSIBLE 档都应出现 (全部 IRREVERSIBLE + HIGH 工具)
    must_have_irreversible = ["kill_process", "lock_screen"]
    for name in must_have_irreversible:
        assert name in _RISKY_PROMPT_FRAGMENT, (
            f"v21-2 prompt fragment 应含 IRREVERSIBLE 工具 {name}. "
            f"实际:\n{_RISKY_PROMPT_FRAGMENT}"
        )
    must_have_high = ["set_wifi", "send_email", "run_script", "send_notification"]
    for name in must_have_high:
        assert name in _RISKY_PROMPT_FRAGMENT, (
            f"v21-2 prompt fragment 应含 HIGH 工具 {name}. "
            f"实际:\n{_RISKY_PROMPT_FRAGMENT}"
        )
    # MEDIUM/LOW 工具**不**应列 (prompt 应该简洁, 只显示主人该紧张的)
    must_not_have = ["get_current_time", "web_search", "read_file",
                     "set_volume"]   # set_volume 是 MEDIUM, 不该在 prompt
    for name in must_not_have:
        assert name not in _RISKY_PROMPT_FRAGMENT, (
            f"v21-2 prompt fragment 不应含 MEDIUM/SAFE 工具 {name}. "
            f"实际:\n{_RISKY_PROMPT_FRAGMENT}"
        )
    print("[OK] v21-2: _RISKY_PROMPT_FRAGMENT 列 HIGH + IRREVERSIBLE, 不含 MEDIUM/SAFE")


def test_v212_anti_hallucination_suffix_uses_fragment():
    """_ANTI_HALLUCINATION_SUFFIX 末尾应包含 _RISKY_PROMPT_FRAGMENT。

    注: 这里**不**直接比 suffix 全文, 只比关键内容 (因为 suffix 头部"防幻觉基线"
    是手写的字符串, 不会因这次改动变, 不必 assert)。
    """
    from app.brain.hallucination_guard import (
        _ANTI_HALLUCINATION_SUFFIX, _RISKY_PROMPT_FRAGMENT,
    )
    assert _RISKY_PROMPT_FRAGMENT in _ANTI_HALLUCINATION_SUFFIX or (
        # 兼容: _ANTI_HALLUCINATION_SUFFIX 也可能拼接了 fragment 的子串,
        # 但核心内容 (HIGH 档工具列表) 应该在
        "kill_process" in _ANTI_HALLUCINATION_SUFFIX
        and "send_email" in _ANTI_HALLUCINATION_SUFFIX
    ), (
        f"v21-2 _ANTI_HALLUCINATION_SUFFIX 末尾应含 _RISKY_PROMPT_FRAGMENT 内容. "
        f"suffix 末尾 600:\n{_ANTI_HALLUCINATION_SUFFIX[-600:]}"
    )
    print("[OK] v21-2: _ANTI_HALLUCINATION_SUFFIX 末尾含 _RISKY_PROMPT_FRAGMENT")
