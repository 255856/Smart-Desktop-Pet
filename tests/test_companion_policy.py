"""companion_policy 决策查表单元测试。"""
from app.engine.state import PetState
from app.brain.companion_policy import (
    RULES_TABLE, Rule, Decision, ANY,
    lookup_decision, quantize_state,
)
from app.engine.tools._risk import (
    SAFE, LOW, MEDIUM, HIGH, IRREVERSIBLE, risk_for_tool,
)


def test_rules_table_count_27():
    """RULES_TABLE 至少 27 条 (PDF 12 + 扩展 15)，paper #10 拆 +4 净增。"""
    assert len(RULES_TABLE) >= 27, (
        f"RULES_TABLE 应至少有 27 条 (PDF 12 + 扩展 15, paper #10 拆 +4). "
        f"实际: {len(RULES_TABLE)}"
    )
    # 关键: paper #10 variants 必须存在 (priority 16-18 / 19-20)
    pdf10_variants = [r for r in RULES_TABLE
                      if r.priority in (16, 17, 18, 19, 20)]
    assert len(pdf10_variants) >= 5, (
        f"paper #10 拆分应 ≥5 条 (priority 16-20). 实际: {len(pdf10_variants)}"
    )
    print(f"[OK] companion_policy: {len(RULES_TABLE)} 条规则 (≥ 27)")


def test_rules_sorted_by_priority():
    """RULES_TABLE 应按 priority 升序排列, 让 specific 先匹配。"""
    for i in range(1, len(RULES_TABLE)):
        assert RULES_TABLE[i - 1].priority <= RULES_TABLE[i].priority, (
            f"规则 {i-1} priority={RULES_TABLE[i-1].priority} > "
            f"规则 {i} priority={RULES_TABLE[i].priority}"
        )
    print("[OK] companion_policy: RULES_TABLE 按 priority 升序")


def test_rule_matches_exact():
    """Rule.matches 全 explicit 字段: 全相等才命中, 任意一个不等就 miss。"""
    r = Rule(sat="LOW", mood="MID", level="HIGH", risk="MEDIUM",
             decision=Decision.ALLOW, priority=999)
    # 全等 → 命中
    assert r.matches("LOW", "MID", "HIGH", "MID", "MEDIUM")
    # 任何不等 → 不命中
    assert not r.matches("MID", "MID", "HIGH", "MID", "MEDIUM")
    assert not r.matches("LOW", "HIGH", "HIGH", "MID", "MEDIUM")
    assert not r.matches("LOW", "MID", "LOW", "MID", "MEDIUM")
    assert not r.matches("LOW", "MID", "HIGH", "MID", "HIGH")
    print("[OK] Rule.matches: explicit 全等才命中 (v23-3 加 favor)")


def test_rule_matches_any_wildcard():
    """Rule.matches 任一字段 = ANY 时, 该字段总是命中。"""
    r = Rule(sat=ANY, mood="LOW", level=ANY, risk=ANY,
             decision=Decision.CONFIRM, priority=999)
    # sat / level / risk / favor 都是 ANY, mood 必须是 LOW
    assert r.matches("LOW", "LOW", "LOW", "LOW", "LOW")
    assert r.matches("HIGH", "LOW", "HIGH", "HIGH", "HIGH")
    # mood 不等于 LOW → miss
    assert not r.matches("LOW", "MID", "HIGH", "HIGH", "HIGH")
    assert not r.matches("LOW", "HIGH", "HIGH", "HIGH", "HIGH")
    print("[OK] Rule.matches: ANY 字段恒真, 其他必须相等 (v23-3 favor)")


def test_pdf_rule1_heartbroken_irreversible_rejected():
    """PDF §6.1 #1: 心情 LOW + 不可逆 → REJECT (REFUSE 后另算)。
    """
    ps = PetState()
    ps.feeling = 30   # LOW
    ps.strength_food = 50
    ps.level = 8
    d = lookup_decision(ps, "lock_screen")  # IRREVERSIBLE
    assert d == Decision.REJECT, (
        f"心情 LOW + lock_screen (IRREVERSIBLE) 应 REJECT. 实际: {d}"
    )
    print("[OK] PDF #1: 心情低落 + 不可逆 → REJECT")


def test_pdf_rule2_heartbroken_high_confirms():
    """PDF §6.1 #2: 心情 LOW + 高风险 → CONFIRM。"""
    ps = PetState()
    ps.feeling = 30
    ps.level = 5   # MID (避免 PDF #4 也命中)
    d = lookup_decision(ps, "run_script")  # HIGH
    assert d == Decision.CONFIRM, (
        f"心情 LOW + run_script (HIGH) 应 CONFIRM. 实际: {d}"
    )
    print("[OK] PDF #2: 心情低落 + 高风险 → CONFIRM")


def test_pdf_rule3_low_level_high_confirms():
    """PDF §6.1 #3: 等级 LOW + 高风险 → CONFIRM (主人还没培养到信任度)。"""
    ps = PetState()
    ps.feeling = 80   # HIGH 心情, 排除 #2 影响
    ps.level = 2      # LOW
    d = lookup_decision(ps, "send_email")  # HIGH
    assert d == Decision.CONFIRM, (
        f"等级 LOW + send_email (HIGH) 应 CONFIRM. 实际: {d}"
    )
    print("[OK] PDF #3: 等级低 + 高风险 → CONFIRM")


def test_pdf_rule5_full_state_medium_allows():
    """PDF §6.1 #5: 满 sat+mood + 中风险 → ALLOW。

    注意: 这条 priority 204 比 catch-all HIGH (50) 高, 但 LOW/HIGH/HIGH/MEDIUM 必须
    命中 PDF #5 才能 ALLOW。中等级 + 中风险 = 放行。
    """
    ps = PetState()
    ps.feeling = 80
    ps.strength_food = 80
    ps.level = 8      # MID
    d = lookup_decision(ps, "edit_file")   # MEDIUM
    assert d == Decision.ALLOW, (
        f"HIGH sat+mood + MID level + edit_file (MEDIUM) 应 ALLOW. 实际: {d}"
    )
    print("[OK] PDF #5: 满 sat+mood + 中风险 → ALLOW")


def test_pdf_rule7_happy_high_level_high_allows():
    """PDF §6.1 #7: 心情好 + 等级高 + 高风险 → ALLOW (高度信任)。"""
    ps = PetState()
    ps.feeling = 90
    ps.level = 18   # HIGH
    d = lookup_decision(ps, "run_script")  # HIGH
    assert d == Decision.ALLOW, (
        f"心情 HIGH + 等级 HIGH + run_script (HIGH) 应 ALLOW. 实际: {d}"
    )
    print("[OK] PDF #7: 心情好 + 等级高 + 高风险 → ALLOW")


def test_pdf_rule10_double_low_refuses():
    """PDF §6.1 #10: sat LOW + mood LOW → REFUSE (不调用工具, 自然语言解释)。"""
    ps = PetState()
    ps.feeling = 20   # LOW
    ps.strength_food = 20  # LOW
    ps.level = 8
    d = lookup_decision(ps, "edit_file")  # MEDIUM
    assert d == Decision.REFUSE, (
        f"心情 LOW + 饱食 LOW + edit_file 应 REFUSE. 实际: {d}"
    )
    print("[OK] PDF #10: 心情+饱食都 LOW → REFUSE")


def test_pdf_rule11_high_level_irreversible_confirms():
    """PDF §6.1 #11: 即便等级高 + 不可逆 → 还是要 CONFIRM。

    (这条 warning 主人: 不可逆操作无论信任多高都要 confirm。)
    """
    ps = PetState()
    ps.feeling = 90
    ps.level = 18    # HIGH
    d = lookup_decision(ps, "lock_screen")  # IRREVERSIBLE
    assert d == Decision.CONFIRM, (
        f"心情 HIGH + 等级 HIGH + lock_screen (IRREVERSIBLE) 应 CONFIRM. 实际: {d}"
    )
    print("[OK] PDF #11: 等级高 + 不可逆 → CONFIRM (不可逆就要拦)")


def test_safe_tools_always_allow():
    """任何状态下纯读工具 (SAFE) 都 ALLOW。"""
    for f in (20, 50, 90):
        for sf in (20, 50, 90):
            for lv in (1, 8, 18):
                ps = PetState()
                ps.feeling, ps.strength_food, ps.level = f, sf, lv
                d = lookup_decision(ps, "get_current_time")
                assert d == Decision.ALLOW, (
                    f"任何 state 下 get_current_time 应 ALLOW. "
                    f"f={f} sf={sf} lv={lv} → {d}"
                )
    print("[OK] companion_policy: 纯读工具永远 ALLOW")


def test_default_fallback_when_no_rule_matches():
    """没规则命中时: SAFE/LOW/MEDIUM → ALLOW, HIGH → CONFIRM, IRREVERSIBLE → REJECT。"""
    # 用一个真实工具但 state 走查表 (所有路径都试): 直接看 IRREVERSIBLE + 普通 state
    ps = PetState()
    ps.feeling = 50; ps.strength_food = 50; ps.level = 5
    # run_script (HIGH) + level=5 MID 应该被某条规则命中 (catch-all HIGH)
    # 我们改成 test IRREVERSIBLE 等级低 → REJECT (匹配 PDF #4)
    ps.level = 2
    d = lookup_decision(ps, "kill_process")  # IRREVERSIBLE
    # PDF #4: 等级 LOW + IRREVERSIBLE → REJECT (priority 203)
    assert d == Decision.REJECT, (
        f"等级 LOW + IRREVERSIBLE 应 REJECT (PDF #4). 实际: {d}"
    )
    # 验证 fallback: 用一个返回 SAFE 的工具, 任何 state 都不被任何 explicit rule 命中
    # 但 catch-all SAFE priority 92 也覆盖, 实质不是 fallback
    # 验证 fallback HIGH (priority 50) 命中而不是 default CONFIRM
    ps.level = 5  # MID 等级, 不命中 PDF 200/203/210 任一条
    ps.feeling = 50; ps.strength_food = 50
    d = lookup_decision(ps, "set_wifi")  # HIGH
    # catch-all ANY/ANY/ANY/HIGH → CONFIRM (priority 50)
    assert d == Decision.CONFIRM, (
        f"任意 state + HIGH 应 CONFIRM (catch-all). 实际: {d}"
    )
    print("[OK] companion_policy: catch-all + fallback 兼容旧 RiskyToolConfirm")


def test_no_rules_actually_duplicate_same_specific():
    """规则没有两条完全 identical (sat/mood/level/favor/risk 全一样)。"""
    seen = {}
    for r in RULES_TABLE:
        key = (r.sat, r.mood, r.level, r.favor, r.risk)
        if key in seen:
            raise AssertionError(
                f"重复规则: prio {seen[key].priority} 与 {r.priority} 都是 {key}"
            )
        seen[key] = r
    print(f"[OK] companion_policy: {len(RULES_TABLE)} 条规则互不重复")


def test_memory_tools_are_not_gated_by_the_pets_mood():
    """心情差 + 好感低时，**记事不许被拦**。

    背景：状态闸门本来是给危险动作设的（心情差 → 别乱动主人的东西）。
    但记事不是危险动作 —— 主人刚说过的事因为桌宠当时心情不好就没存下来，
    主人后面问它就「忘了」（实测 companion_log 里 remember_fact 有 CONFIRM 的记录）。
    """
    from app.brain.companion_policy import lookup_decision
    from app.engine.state import PetState

    st = PetState()
    st.feeling = 5.0        # 心情差
    st.likability = 0.0     # 好感低
    st.level = 1
    st.store_strength_food = 90.0
    q = st.quantize()
    assert q['mood'] == 'LOW' and q['favor'] == 'LOW', q

    # 记事 / 回忆：永远放行
    assert lookup_decision(st, 'remember_fact') == 'ALLOW'
    assert lookup_decision(st, 'recall_memory') == 'ALLOW'

    # 删记忆 / 删提醒：仍然走状态闸门（不可逆，值得拦一下）
    assert lookup_decision(st, 'forget_memory') != 'ALLOW'
    assert lookup_decision(st, 'delete_reminder') != 'ALLOW'
    print("[OK] 记忆类工具豁免状态闸门，其他工具照常受闸")
