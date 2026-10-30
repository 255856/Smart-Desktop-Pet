# -*- coding: utf-8 -*-
"""价目表 + 金额换算（Stage 4.1）。

为什么要有这个模块
------------------
provider 只回 token 数，**不回钱**（qq-bridge 的注释写得很直白：
"DSH 自身不做任何货币换算"）。所以「花多少钱」只能由消费方用价目表算，
而价目表是会变的（官方调价、促销五折、上下文档位）—— 于是有一条铁律：

    **金额在读取时计算，不在写入时。**

台账（app/brain/token_ledger.py）只存 token 桶，一个「元」字都不许进文件；
改价目表不需要、也不允许重写历史。本模块就是那个「读取时算钱」的地方。

口径（与 qq-bridge src/model-prices.js 对齐）
--------------------------------------------
计费桶**四个、互不重叠**：
    cacheHit（缓存命中） / cacheMiss（缓存未命中） / cacheWrite（缓存写入） / output（输出）
扣减费用 = Σ(各桶 token 数 × 对应单价)。
空闲时段 / 高峰时段按北京时间判定：周一至周五 9:00-12:00、14:00-18:00 且不含
中国法定节假日为**高峰**，其余（含周末与法定节假日全天）为**空闲**。
高峰价 = 空闲价 × 倍率（倍率**按 provider 给**：DeepSeek 官方就是 2 倍；
MiniMax 官方没有分时定价 -> 1 倍）。

**一张表一个币种**（绝不把两种币种加在一起）
------------------------------------------
- `DEFAULT_PRICE_TABLE`：**美元**，MiniMax 国际站的官方价 —— 保留下来做**对照**
  （本项目不走这条路：账单不是按美元结的，面板现在报的是 **CNY / ¥**）。
- `CNY_PRICE_TABLE`：**人民币**，MiniMax 国产平台的官方价 + qq-bridge 的
  DeepSeek 那张表。本项目实际调的是 `https://api.minimaxi.com/v1`，
  账单按**元**结算 —— 想看「真账单」就用这张表
  （`summary(rows, table=CNY_PRICE_TABLE)`）。

价目来源（都从 provider 官方价目页抄的，不是猜的）
--------------------------------------------------
1. MiniMax 国际站（美元）：
   https://platform.minimax.io/docs/guides/pricing-paygo
   - MiniMax-M3（标准档，输入 ≤ 512k tokens，官方「永久五折」后的现价）：
     输入 $0.30/M、输出 $1.20/M、缓存读取 $0.06/M
   - MiniMax-M3（输入 > 512k tokens）：输入 $0.60、输出 $2.40、缓存读取 $0.12
   - MiniMax-M3.1-Flash：输入 $0.15、输出 $0.60、缓存读取 $0.015
2. MiniMax 国产平台（人民币，同一套价目表）：
   https://platform.minimaxi.com/docs/guides/pricing-paygo
   - MiniMax-M3（≤ 512k）：输入 2.10 元/M、输出 8.40、缓存读取 0.42
   - MiniMax-M3（> 512k）：输入 4.20、输出 16.80、缓存读取 0.84
   - MiniMax-M3.1-Flash：输入 1.00、输出 4.00、缓存读取 0.10
3. DeepSeek（qq-bridge 那张表，形状照抄；本项目当前不用）：
   https://api-docs.deepseek.com/zh-cn/quick_start/pricing
   - deepseek-flash（DeepSeek-V4.1-Flash-1017）：空闲 0.02 / 1 / 4 元/M
     （缓存命中 / 未命中 / 输出），高峰 2 倍
   - deepseek-v4-pro（DeepSeek-V4-Pro-0813）：空闲 0.15 / 4.5 / 13.5 元/M

三条通用规则
------------
1. 官方价目表只有「输入 / 输出 / 缓存读取」三档，**没有单独的缓存写入价**
   -> cacheWrite 按输入价计（DeepSeek 则是按未命中价计）。
2. 优先服务（service_tier=priority）是标准档的 1.5 倍；本项目没设
   service_tier，所以一律按标准档算。
3. 官方价目表没有分时定价 -> MiniMax 条目的 peakMultiplier = 1
   （高峰不加价；峰谷判定仍然算，byTier 照样拆，只是金额不变）。

「estimated」标记
----------------
**没核实过的价不许当成真的**。两种情况会标 estimated=true：
  - 模型不在表里（走 fallbackModel 兜底）—— 用别的模型的价格去猜，天然是估的；
    连「没说是哪个模型」（面板就是这么调的）也按估的算。
  - 表里的条目自己标了 estimated（价格来源不明或只是促销价）。
面板/报告看到 estimated=true 就知道「这个数字是估的，别当账单」。
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 价目表版本标记（改了价目表就改这里；只用于展示与排障）
PRICE_TABLE_VERSION = "2026-11-04"

#: 2026 年中国法定节假日（放假日，含调休连休段）。
#: 高峰时段判定要排除这些日期；周末本身已因「非周一至周五」被判为空闲。
#: 来源：国办发明电〔2025〕7 号。
CN_HOLIDAYS_2026 = [
    # 元旦：1/1(周四)-1/3(周六)
    "2026-01-01", "2026-01-02", "2026-01-03",
    # 春节：2/15(周日)-2/23(周一)
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
    # 清明节：4/4(周六)-4/6(周一)
    "2026-04-04", "2026-04-05", "2026-04-06",
    # 劳动节：5/1(周五)-5/5(周二)
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    # 端午节：6/19(周五)-6/21(周日)
    "2026-06-19", "2026-06-20", "2026-06-21",
    # 中秋节：9/25(周五)-9/27(周日)
    "2026-09-25", "2026-09-26", "2026-09-27",
    # 国庆节：10/1(周四)-10/7(周三)
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",
]

#: 2027 年法定节假日（国务院办公厅通知，放假调休段）。
#: 为什么要预置到 2027：价目表只认这些日期，缺失时的表现是**静默高估** ——
#: 节假日里的调用会被当高峰计价（2 倍），金额看着偏高但不报错。
CN_HOLIDAYS_2027 = [
    # 元旦：1/1(周五)-1/3(周日)
    "2027-01-01", "2027-01-02", "2027-01-03",
    # 春节：2/5(周五)-2/13(周六)
    "2027-02-05", "2027-02-06", "2027-02-07", "2027-02-08", "2027-02-09",
    "2027-02-10", "2027-02-11", "2027-02-12", "2027-02-13",
    # 清明节：4/4(周日)-4/6(周二)
    "2027-04-04", "2027-04-05", "2027-04-06",
    # 劳动节：5/1(周六)-5/5(周三)
    "2027-05-01", "2027-05-02", "2027-05-03", "2027-05-04", "2027-05-05",
    # 端午节：6/9(周三)-6/11(周五)
    "2027-06-09", "2027-06-10", "2027-06-11",
    # 中秋节：9/15(周三)-9/17(周五)
    "2027-09-15", "2027-09-16", "2027-09-17",
    # 国庆节：10/1(周五)-10/7(周四)
    "2027-10-01", "2027-10-02", "2027-10-03", "2027-10-04",
    "2027-10-05", "2027-10-06", "2027-10-07",
]

#: 内置的全部法定节假日（跨年份）
CN_HOLIDAYS = CN_HOLIDAYS_2026 + CN_HOLIDAYS_2027

#: 北京时间相对 UTC 的分钟偏移
BEIJING_OFFSET_MINUTES = 8 * 60

#: 空桶常量（避免各处重复字面量）
ZERO_BUCKETS: dict = {
    "cacheMiss": 0,
    "cacheHit": 0,
    "cacheWrite": 0,
    "output": 0,
    "reasoning": 0,
}


# ---------------------------------------------------------------------------
# 价目表
# ---------------------------------------------------------------------------

#: MiniMax 官方价目页（本项目实际调用的平台，账单按元结算）
MINIMAX_PRICE_SOURCE = "https://platform.minimaxi.com/docs/guides/pricing-paygo"
#: MiniMax 国际站价目页（美元口径，仅作对照，本模块不用）
MINIMAX_PRICE_SOURCE_USD = "https://platform.minimax.io/docs/guides/pricing-paygo"
#: DeepSeek 官方价目页
DEEPSEEK_PRICE_SOURCE = "https://api-docs.deepseek.com/zh-cn/quick_start/pricing"


#: 默认价目表：**美元**口径。
#: 为什么默认是美元：面板的契约是 `cost.currency == "USD"`
#: （app/web/dashboard.py 及其测试都这么要求），而 MiniMax 国际站的官方价目表
#: 就是美元 —— 不需要任何换算，也没有猜出来的汇率。
DEFAULT_PRICE_TABLE: dict = {
    "version": PRICE_TABLE_VERSION,
    "currency": "USD",
    "symbol": "$",
    "unit": 1e6,
    # 表级高峰倍率（DeepSeek 官方口径：空闲价 = 高峰价的一半）
    "peakMultiplier": 2,
    # 高峰时段窗口，按北京时间的小时数半开区间 [start, end)
    "peakWindows": [[9, 12], [14, 18]],
    # 高峰时段只在周一至周五；1=周一 … 7=周日
    "peakWeekdays": [1, 2, 3, 4, 5],
    "holidays": list(CN_HOLIDAYS),
    "models": {
        # ---- MiniMax（本项目实际用的 provider）----
        "minimax-m3": {
            "label": "MiniMax-M3",
            "currency": "USD",
            "symbol": "$",
            # MiniMax 官方没有分时定价 -> 高峰不加价（倍率 1）。
            # 峰谷判定仍然算（byTier 要能拆），只是金额不变。
            "peakMultiplier": 1,
            "estimated": False,
            "source": MINIMAX_PRICE_SOURCE_USD,
            "note": ("MiniMax-M3 标准档官方价（永久五折后现价）；官方只有"
                     "输入/输出/缓存读取三档，缓存写入按输入价计"),
            "aliases": ["minimaxm3", "minimax-m3-latest"],
            # 官方按输入长度分档：≤512k 与 >512k 两档
            "tiers": [
                {
                    "maxInputTokens": 512000,
                    "off": {
                        "cacheHit": 0.06,
                        "cacheMiss": 0.30,
                        "cacheWrite": 0.30,
                        "output": 1.20,
                    },
                },
                {
                    "maxInputTokens": None,
                    "off": {
                        "cacheHit": 0.12,
                        "cacheMiss": 0.60,
                        "cacheWrite": 0.60,
                        "output": 2.40,
                    },
                },
            ],
        },
        "minimax-m3.1-flash": {
            "label": "MiniMax-M3.1-Flash",
            "currency": "USD",
            "symbol": "$",
            "peakMultiplier": 1,
            "estimated": False,
            "source": MINIMAX_PRICE_SOURCE_USD,
            "note": "MiniMax-M3.1-Flash 官方价；缓存写入按输入价计",
            "off": {
                "cacheHit": 0.015,
                "cacheMiss": 0.15,
                "cacheWrite": 0.15,
                "output": 0.60,
            },
        },
    },
    # 未知模型的兜底价：按本项目实际用的模型计，并在返回值里标 estimated=true
    "fallbackModel": "minimax-m3",
}


#: 人民币口径的价目表：MiniMax 国产平台的官方价 + qq-bridge 的 DeepSeek 那张表。
#: 本项目实际调的是 https://api.minimaxi.com/v1，账单按**元**结算 ——
#: 想看「真账单」就用这张表：`summary(rows, table=CNY_PRICE_TABLE)`。
CNY_PRICE_TABLE: dict = {
    "version": PRICE_TABLE_VERSION,
    "currency": "CNY",
    "symbol": "¥",
    "unit": 1e6,
    "peakMultiplier": 2,
    "peakWindows": [[9, 12], [14, 18]],
    "peakWeekdays": [1, 2, 3, 4, 5],
    "holidays": list(CN_HOLIDAYS),
    "models": {
        "minimax-m3": {
            "label": "MiniMax-M3",
            "currency": "CNY",
            "symbol": "¥",
            "peakMultiplier": 1,
            "estimated": False,
            "source": MINIMAX_PRICE_SOURCE,
            "note": "MiniMax-M3 标准档国产平台官方价（永久五折后现价）",
            "aliases": ["minimaxm3", "minimax-m3-latest"],
            "tiers": [
                {
                    "maxInputTokens": 512000,
                    "off": {
                        "cacheHit": 0.42,
                        "cacheMiss": 2.10,
                        "cacheWrite": 2.10,
                        "output": 8.40,
                    },
                },
                {
                    "maxInputTokens": None,
                    "off": {
                        "cacheHit": 0.84,
                        "cacheMiss": 4.20,
                        "cacheWrite": 4.20,
                        "output": 16.80,
                    },
                },
            ],
        },
        "minimax-m3.1-flash": {
            "label": "MiniMax-M3.1-Flash",
            "currency": "CNY",
            "symbol": "¥",
            "peakMultiplier": 1,
            "estimated": False,
            "source": MINIMAX_PRICE_SOURCE,
            "note": "MiniMax-M3.1-Flash 国产平台官方价",
            "off": {
                "cacheHit": 0.10,
                "cacheMiss": 1.00,
                "cacheWrite": 1.00,
                "output": 4.00,
            },
        },
        # ---- DeepSeek（qq-bridge 那张表，形状照抄；本项目当前不用）----
        "deepseek-flash": {
            "label": "DeepSeek-V4.1-Flash-1017",
            "currency": "CNY",
            "symbol": "¥",
            "estimated": False,
            "source": DEEPSEEK_PRICE_SOURCE,
            "note": "空闲时段官方价；高峰时段 = 2 倍",
            "off": {
                "cacheHit": 0.02,
                "cacheMiss": 1,
                "cacheWrite": 1,
                "output": 4,
            },
        },
        "deepseek-v4-flash": {
            "label": "DeepSeek-V4.1-Flash（旧模型名）",
            "currency": "CNY",
            "symbol": "¥",
            "estimated": False,
            "source": DEEPSEEK_PRICE_SOURCE,
            "off": {
                "cacheHit": 0.02,
                "cacheMiss": 1,
                "cacheWrite": 1,
                "output": 4,
            },
        },
        "deepseek-v4-pro": {
            "label": "DeepSeek-V4-Pro-0813",
            "currency": "CNY",
            "symbol": "¥",
            "estimated": False,
            "source": DEEPSEEK_PRICE_SOURCE,
            "off": {
                "cacheHit": 0.15,
                "cacheMiss": 4.5,
                "cacheWrite": 4.5,
                "output": 13.5,
            },
        },
    },
    "fallbackModel": "minimax-m3",
}


def _copy_model_entry(entry: dict) -> dict:
    """拷贝一个模型条目（深一层：off / tiers 不能跟默认表共用同一份）。"""
    out = dict(entry or {})
    if isinstance(entry.get("off"), dict):
        out["off"] = dict(entry["off"])
    if isinstance(entry.get("tiers"), list):
        out["tiers"] = [
            {**tier, "off": dict(tier.get("off") or {})}
            if isinstance(tier, dict) else tier
            for tier in entry["tiers"]
        ]
    return out


def resolve_price_table(override: Optional[dict] = None,
                           base: Optional[dict] = None) -> dict:
    """合并用户配置的价目表覆盖项，失败时回落到默认表。

    覆盖项的形状跟 qq-bridge 一致：顶层字段直接覆盖，`models` 里可以只写
    其中几个桶（没写的沿用基础条目）。**只有条目自己没写 cacheWrite 时**
    才让它跟随 cacheMiss —— 否则会从兜底模型继承一个不相干的缓存写入价
    （静默算错钱）。

    base：改哪张表（默认 DEFAULT_PRICE_TABLE；想改人民币那张就传
    CNY_PRICE_TABLE）。**一张表一个币种**，绝不把两种币种合进一张。
    """
    base_table = base if isinstance(base, dict) and base else DEFAULT_PRICE_TABLE
    if not isinstance(override, dict) or not override:
        merged = dict(base_table)
        merged["models"] = {k: _copy_model_entry(v)
                            for k, v in base_table["models"].items()}
        merged["holidays"] = list(base_table["holidays"])
        return merged

    merged = dict(base_table)
    merged.update({k: v for k, v in override.items() if k != "models"})
    merged["models"] = {k: _copy_model_entry(v)
                         for k, v in base_table["models"].items()}
    merged["holidays"] = list(base_table["holidays"])
    fallback_off = base_table["models"][base_table["fallbackModel"]].get("off", {})

    for name, entry in (override.get("models") or {}).items():
        if not isinstance(entry, dict):
            continue
        prev = merged["models"].get(name) or {}
        given = entry.get("off") if isinstance(entry.get("off"), dict) else {}
        new_entry = dict(prev)
        new_entry.update({k: v for k, v in entry.items() if k != "off"})
        tiers = prev.get("tiers")
        if isinstance(tiers, list) and tiers:
            # 分档模型（MiniMax 按输入长度分档）：覆盖项按桶逐个盖到**每一档**上，
            # 不然改了价却还按老档算钱。
            new_tiers = []
            for tier in tiers:
                if not isinstance(tier, dict):
                    continue
                t = dict(tier)
                t_off = dict(t.get("off") or {})
                t_off.update(given)
                if not isinstance(given.get("cacheWrite"), (int, float)):
                    t_off["cacheWrite"] = t_off.get("cacheMiss", 0)
                t["off"] = t_off
                new_tiers.append(t)
            new_entry["tiers"] = new_tiers
            merged["models"][name] = new_entry
            continue
        off = dict(prev.get("off") or fallback_off)
        off.update({k: v for k, v in given.items()})
        if not isinstance(given.get("cacheWrite"), (int, float)):
            off["cacheWrite"] = off.get("cacheMiss", 0)
        new_entry["off"] = off
        new_entry.setdefault("label", name)
        merged["models"][name] = new_entry

    if not isinstance(merged.get("fallbackModel"), str) or \
            merged["fallbackModel"] not in merged["models"]:
        merged["fallbackModel"] = base_table["fallbackModel"]
    if not isinstance(merged.get("peakMultiplier"), (int, float)) or \
            merged["peakMultiplier"] <= 0:
        merged["peakMultiplier"] = base_table["peakMultiplier"]
    if not isinstance(merged.get("unit"), (int, float)) or merged["unit"] <= 0:
        merged["unit"] = base_table["unit"]
    if not isinstance(merged.get("version"), str) or not merged["version"]:
        merged["version"] = PRICE_TABLE_VERSION
    return merged


# ---------------------------------------------------------------------------
# 峰谷判定
# ---------------------------------------------------------------------------

def _table(table: Optional[dict]) -> dict:
    return table if isinstance(table, dict) and table else DEFAULT_PRICE_TABLE


def beijing_parts(at_ms: float) -> dict:
    """把时间戳换算成北京时间的日历分量（不依赖运行机器的本地时区）。"""
    try:
        ms = float(at_ms)
    except (TypeError, ValueError):
        ms = 0.0
    epoch = ms / 1000.0 + BEIJING_OFFSET_MINUTES * 60
    # 只用整数运算做日期换算，避免浮点误差把 23:59 判成第二天
    days = math.floor(epoch / 86400.0)
    sec_of_day = int(round(epoch - days * 86400.0))
    if sec_of_day >= 86400:
        sec_of_day -= 86400
        days += 1
    # 1970-01-01 是周四 -> weekday 索引按「1=周一 … 7=周日」折算
    weekday = ((days + 3) % 7) + 1
    y, m, d = _civil_from_days(int(days))
    hour = sec_of_day // 3600
    minute = (sec_of_day % 3600) // 60
    return {
        "weekday": weekday,
        "hour": hour,
        "date": f"{y:04d}-{m:02d}-{d:02d}",
        "clock": f"{hour:02d}:{minute:02d}",
    }


def _civil_from_days(z: int) -> tuple[int, int, int]:
    """天数 -> (年, 月, 日)（Howard Hinnant 的 civil_from_days 算法）。"""
    z += 719468
    era = (z if z >= 0 else z - 146096) // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    y = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    d = doy - (153 * mp + 2) // 5 + 1
    m = mp + 3 if mp < 10 else mp - 9
    return (y + (1 if m <= 2 else 0), m, d)


def is_peak_at(at_ms: float, table: Optional[dict] = None) -> bool:
    """判断某一时刻是否落在高峰时段（北京时间口径）。"""
    tbl = _table(table)
    try:
        ms = float(at_ms)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(ms):
        return False
    parts = beijing_parts(ms)
    weekdays = tbl.get("peakWeekdays") or []
    if parts["weekday"] not in weekdays:
        return False
    holidays = set(tbl.get("holidays") or [])
    if parts["date"] in holidays:
        return False
    for window in (tbl.get("peakWindows") or []):
        try:
            start, end = window[0], window[1]
        except (TypeError, IndexError):
            continue
        if isinstance(start, (int, float)) and isinstance(end, (int, float)) and \
                parts["hour"] >= start and parts["hour"] < end:
            return True
    return False


# ---------------------------------------------------------------------------
# 桶归一化
# ---------------------------------------------------------------------------

def _pick(usage: dict, *keys: str) -> int:
    """取第一个 >0 的整数桶值（认不出来的算 0）。"""
    for key in keys:
        value = usage.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
            return int(round(value))
    return 0


def normalize_usage(usage: Any) -> dict:
    """把各种 usage 形状归一化成四个**互不重叠**的计费桶。

    认两套键名（两套都要认，只认一套会在「写盘 → 重新载入」时把桶读成 0，
    花费凭空少一截）：
      - 台账落盘口径：cache_hit_tokens / cache_miss_tokens / cache_write_tokens /
        completion_tokens（app/brain/token_ledger.py 的 BUCKET_FIELDS）；
      - provider 原始口径：inputTokens / cacheReadTokens / cacheWriteTokens /
        outputTokens（qq-bridge 的 DSH 口径）。

    还处理「miss 里含 write」的那种口径（provider 把缓存写入算进未命中里）：
    这时四个桶会重叠，扣费就多算一份 —— 这里把 write 从 miss 里扣出来，
    保证四桶互斥。

    已经归一化过的桶（cacheHit / cacheMiss / cacheWrite / output）也能直接喂进来
    —— 本函数是幂等的，`price_usage()` 内部会再归一化一次。
    """
    if not isinstance(usage, dict) or not usage:
        return dict(ZERO_BUCKETS)
    prompt = _pick(usage, "prompt_tokens", "input_tokens", "prompt_token_count",
                   "inputTokens")
    hit = _pick(usage, "cache_hit_tokens", "cache_read_tokens",
                "cacheReadTokens", "cached_tokens", "cache_read_input_tokens",
                "prompt_cache_hit_tokens", "cacheHit")
    write = _pick(usage, "cache_write_tokens", "cacheWriteTokens",
                  "cache_creation_input_tokens", "prompt_cache_write_tokens",
                  "cacheWrite")
    miss = _pick(usage, "cache_miss_tokens", "uncached_input_tokens",
                 "uncachedInputTokens", "prompt_cache_miss_tokens", "cacheMiss")
    output = _pick(usage, "completion_tokens", "output_tokens", "outputTokens",
                   "output")
    reasoning = _pick(usage, "reasoning_tokens", "reasoningTokens", "reasoning")

    if prompt > 0 and hit <= prompt:
        rest = prompt - hit
        if miss <= 0:
            miss = rest
        elif write > 0 and miss > rest:
            # 口径 B：miss 里已经含了 write -> 扣掉，四桶才互斥
            miss = max(0, prompt - write)
        # provider 交代不清的那部分（prompt 比三个桶之和还大）算「未命中」：
        # prompt = 命中 + 未命中 + 写入，差额只能是没命中缓存的输入。
        gap = prompt - (hit + miss + write)
        if gap > 0:
            miss += gap

    return {
        "cacheMiss": miss,
        "cacheHit": hit,
        "cacheWrite": write,
        "output": output,
        "reasoning": reasoning,
    }


def total_tokens_of(buckets: Optional[dict]) -> int:
    """四个桶求和（token 总量，不含 reasoning）。"""
    b = buckets or {}
    return (int(b.get("cacheMiss") or 0) + int(b.get("cacheHit") or 0) +
            int(b.get("cacheWrite") or 0) + int(b.get("output") or 0))


def input_tokens_of(buckets: Optional[dict]) -> int:
    """输入侧 token 总量（三个输入桶；不含输出）。

    这就是「输入预算」比的那个数：provider 的 tpm 限额按输入算。
    """
    b = buckets or {}
    return (int(b.get("cacheMiss") or 0) + int(b.get("cacheHit") or 0) +
            int(b.get("cacheWrite") or 0))


def add_buckets(a: Optional[dict], b: Optional[dict]) -> dict:
    """桶相加。"""
    x, y = a or {}, b or {}
    return {
        "cacheMiss": int(x.get("cacheMiss") or 0) + int(y.get("cacheMiss") or 0),
        "cacheHit": int(x.get("cacheHit") or 0) + int(y.get("cacheHit") or 0),
        "cacheWrite": int(x.get("cacheWrite") or 0) + int(y.get("cacheWrite") or 0),
        "output": int(x.get("output") or 0) + int(y.get("output") or 0),
        "reasoning": int(x.get("reasoning") or 0) + int(y.get("reasoning") or 0),
    }


def sub_buckets(a: Optional[dict], b: Optional[dict]) -> dict:
    """桶相减（用于「基线之后」的增量），逐桶下限为 0。"""
    x, y = a or {}, b or {}

    def _s(key: str) -> int:
        return max(0, int(x.get(key) or 0) - int(y.get(key) or 0))

    return {
        "cacheMiss": _s("cacheMiss"),
        "cacheHit": _s("cacheHit"),
        "cacheWrite": _s("cacheWrite"),
        "output": _s("output"),
        "reasoning": _s("reasoning"),
    }


# ---------------------------------------------------------------------------
# 计价
# ---------------------------------------------------------------------------

def _model_key(model: Any) -> str:
    """模型名归一化（小写 + 去空白）。"""
    try:
        return str(model or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def _find_model(model: Any, table: Optional[dict] = None) -> tuple[Optional[str], Optional[dict]]:
    """找模型条目：先精确（归一化后）匹配，再试别名。"""
    tbl = _table(table)
    models = tbl.get("models") or {}
    key = _model_key(model)
    if not key:
        return None, None
    if key in models:
        return key, models[key]
    for name, entry in models.items():
        aliases = (entry or {}).get("aliases") or []
        if key in {_model_key(a) for a in aliases}:
            return name, entry
    return None, None


def _pick_tier(entry: dict, input_tokens: Optional[int]) -> dict:
    """按输入 token 数选价格档（官方价目表按输入长度分档时用）。

    没有分档就返回条目的 off。
    """
    tiers = entry.get("tiers")
    if not isinstance(tiers, list) or not tiers:
        return dict(entry.get("off") or {})
    used = input_tokens if isinstance(input_tokens, int) and input_tokens >= 0 else 0
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        limit = tier.get("maxInputTokens")
        off = tier.get("off")
        if not isinstance(off, dict):
            continue
        if limit is None:
            return dict(off)
        if isinstance(limit, (int, float)) and used <= limit:
            return dict(off)
    # 理论上不会走到这里（最后一档 maxInputTokens=None）；兜底用最后一档
    for tier in reversed(tiers):
        if isinstance(tier, dict) and isinstance(tier.get("off"), dict):
            return dict(tier["off"])
    return dict(entry.get("off") or {})


def rates_at(model: Any, at_ms: float = 0.0, table: Optional[dict] = None,
             input_tokens: Optional[int] = None) -> dict:
    """取某模型在某一时刻生效的单价（含峰谷判定）。

    找不到模型时走 fallbackModel，并把 `estimated` 置 True —— 用别的模型的
    价格去猜，天然是估的，不能当成账单。
    """
    tbl = _table(table)
    models = tbl.get("models") or {}
    key, entry = _find_model(model, tbl)
    estimated = True
    if entry is None:
        fallback = tbl.get("fallbackModel")
        entry = models.get(fallback) if fallback else None
        if entry is None and models:
            entry = list(models.values())[0]
        model_key = key or (tbl.get("fallbackModel") or "unknown")
        # 模型不在表里（连「没说是哪个模型」也算）-> 拿兜底价去猜，
        # 天然是「估的」，不能当成账单（qq-bridge 同一口径）。
    else:
        estimated = False
        model_key = key

    entry = entry or {}
    off = _pick_tier(entry, input_tokens)
    peak = is_peak_at(at_ms, tbl)
    multiplier = entry.get("peakMultiplier")
    if not isinstance(multiplier, (int, float)) or multiplier <= 0:
        multiplier = tbl.get("peakMultiplier") or 1
    factor = multiplier if peak else 1
    cache_miss = float(off.get("cacheMiss") or 0)
    cache_write = off.get("cacheWrite")
    if not isinstance(cache_write, (int, float)):
        cache_write = cache_miss
    if entry.get("estimated") is True:
        estimated = True
    currency = entry.get("currency") or tbl.get("currency") or "CNY"
    symbol = entry.get("symbol") or tbl.get("symbol") or "¥"
    return {
        "model": model_key or str(model or ""),
        "label": entry.get("label") or model_key,
        "tier": "peak" if peak else "off",
        "tierLabel": "高峰时段" if peak else "空闲时段",
        "currency": currency,
        "symbol": symbol,
        "unit": float(tbl.get("unit") or 1e6),
        "estimated": bool(estimated),
        "cacheHit": float(off.get("cacheHit") or 0) * factor,
        "cacheMiss": cache_miss * factor,
        "cacheWrite": float(cache_write) * factor,
        "output": float(off.get("output") or 0) * factor,
    }


def cost_of_buckets(buckets: Optional[dict], rates: dict) -> dict:
    """按单价把 token 桶折算成金额（单位同 rates.currency）。"""
    b = buckets or {}
    unit = float(rates.get("unit") or 1e6) or 1e6
    cache_hit = (int(b.get("cacheHit") or 0) / unit) * float(rates.get("cacheHit") or 0)
    cache_miss = (int(b.get("cacheMiss") or 0) / unit) * float(rates.get("cacheMiss") or 0)
    cache_write = (int(b.get("cacheWrite") or 0) / unit) * float(rates.get("cacheWrite") or 0)
    output = (int(b.get("output") or 0) / unit) * float(rates.get("output") or 0)
    return {
        "cacheHit": cache_hit,
        "cacheMiss": cache_miss,
        "cacheWrite": cache_write,
        "output": output,
        "total": cache_hit + cache_miss + cache_write + output,
    }


def price_usage(buckets: Optional[dict], model: Any = None, at: float = 0.0,
                table: Optional[dict] = None) -> dict:
    """一步到位：给桶 + 时间 + 模型，直接算钱。"""
    b = normalize_usage(buckets) if buckets else dict(ZERO_BUCKETS)
    rates = rates_at(model, at, table, input_tokens=input_tokens_of(b))
    return {"rates": rates, "cost": cost_of_buckets(b, rates)}


def estimate_cost(buckets: Any, model: Any = None, at: float = 0.0,
                  table: Optional[dict] = None) -> dict:
    """面板用的金额估算入口（app/web/dashboard.py 的 `_load_model_prices` 钩子）。

    返回形状：
        {"amount": float, "currency": "USD", "symbol": "$",
         "estimated": bool, "price_source": str,
         "rates_per_million": {...}, "tier": "peak"/"off"}

    注意：
    - **不读 config.yaml**（那里有 API key），默认用兜底模型
      （minimax-m3 = 本项目实际用的模型）。调用方想指定模型就传 model=。
    - **没指定模型时 estimated=True**：面板聚合时没告诉我们这一行是哪个模型，
      拿兜底价去算就是「估的」，得标出来（qq-bridge 同一口径）。
    - 币种跟所用量表一致（默认表 = 美元；想看人民币账单传
      table=CNY_PRICE_TABLE）。
    """
    try:
        b = normalize_usage(buckets)
        stamp = float(at or 0) or time.time() * 1000
        result = price_usage(b, model=model or None, at=stamp, table=table)
        rates = result["rates"]
        cost = result["cost"]
        return {
            "amount": round(float(cost["total"]), 8),
            "currency": rates.get("currency") or "CNY",
            "symbol": rates.get("symbol") or "¥",
            "estimated": bool(rates.get("estimated")),
            "price_source": rates.get("model") or "unknown",
            "rates_per_million": {
                "cacheHit": rates.get("cacheHit"),
                "cacheMiss": rates.get("cacheMiss"),
                "cacheWrite": rates.get("cacheWrite"),
                "output": rates.get("output"),
            },
            "tier": rates.get("tier"),
        }
    except Exception:  # noqa: BLE001 - 面板不能因为算钱失败而崩
        log.debug("estimate_cost 失败（降级为 0）", exc_info=True)
        return {
            "amount": 0.0,
            "currency": "USD",
            "symbol": "$",
            "estimated": True,
            "price_source": "error",
            "rates_per_million": {},
            "tier": "off",
        }


# ---------------------------------------------------------------------------
# 回合分组（台账没有 turn 坐标，用「同会话 + 空闲间隔」近似）
# ---------------------------------------------------------------------------

#: 多久没有请求就算「新一轮」（秒）。同一回合里的多次模型调用只隔几秒，
#: 而主人下一条消息通常隔得久得多。这个值同时是「输入预算」的回合边界。
DEFAULT_TURN_IDLE_SECONDS = 300


def _row_ts_seconds(row: dict) -> float:
    """台账行的时间戳（秒）。兼容毫秒时间戳。"""
    try:
        ts = float(row.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    if ts > 1e11:      # 毫秒
        ts = ts / 1000.0
    return ts


def group_turns(rows: Iterable[dict],
                idle_seconds: int = DEFAULT_TURN_IDLE_SECONDS) -> list[dict]:
    """把台账行按「回合」分组。

    **为什么需要分组**：台账的主键是 (session_id, request_index, backend, retry)，
    一次模型调用 = 一行，一个回合里模型可能被调用多次（调工具 → 工具结果 →
    再调模型）。台账里没有 turn 坐标，所以用「同会话 + 空闲间隔」近似：
    同一 session_id 里，相邻两行的时间差超过 idle_seconds 就另起一轮。

    返回：每轮一个 dict（turn_id / session_id / rows / 起止时间 / 桶合计）。
    """
    try:
        idle = max(1, int(idle_seconds or DEFAULT_TURN_IDLE_SECONDS))
    except (TypeError, ValueError):
        idle = DEFAULT_TURN_IDLE_SECONDS
    ordered = sorted(
        [r for r in rows if isinstance(r, dict)],
        key=lambda r: (_row_ts_seconds(r), str(r.get("session_id") or "")),
    )
    turns: list[dict] = []
    current: Optional[dict] = None
    for row in ordered:
        ts = _row_ts_seconds(row)
        sid = str(row.get("session_id") or "")
        if current is None or current["session_id"] != sid or \
                (ts - current["last_ts"]) > idle:
            current = {
                "turn_id": f"{sid}#{len(turns) + 1}",
                "session_id": sid,
                "rows": [],
                "first_ts": ts,
                "last_ts": ts,
                "buckets": dict(ZERO_BUCKETS),
            }
            turns.append(current)
        current["rows"].append(row)
        current["last_ts"] = ts if ts > current["last_ts"] else current["last_ts"]
        current["buckets"] = add_buckets(current["buckets"], normalize_usage(row))
    return turns


# ---------------------------------------------------------------------------
# 汇总（读取时算钱）
# ---------------------------------------------------------------------------

def _is_aggregate(row: dict) -> bool:
    return str(row.get("kind") or "request") == "aggregate"


def summary(rows: Iterable[dict], table: Optional[dict] = None,
            now: Optional[float] = None,
            idle_seconds: int = DEFAULT_TURN_IDLE_SECONDS) -> dict:
    """按 qq-bridge token-ledger.js::summary() 的口径汇总台账。

    **金额在读取时算**：函数只读行里的 token 桶，价目表现在是什么价就报什么价；
    改价目表不需要（也不允许）重写台账。

    输出形状（对齐 qq-bridge）：
        cost            总花费
        attributedCost  能归属到具体回合的花费
        unattributedCost 只能归属到聚合行（压实后丢了明细）的花费
        byTier.peak/off 高峰 / 空闲时段各花多少
        cacheHitRate    缓存命中率
        avgCostPerTurn  每回合平均花费
        avgCostPerStep  每次模型调用平均花费
        estimated       金额里有没有「估」的成分
    """
    tbl = _table(table)
    stamp = float(now or 0) or time.time() * 1000
    all_rows = [r for r in rows if isinstance(r, dict)]
    requests = [r for r in all_rows if not _is_aggregate(r)]
    aggregates = [r for r in all_rows if _is_aggregate(r)]

    totals_buckets = dict(ZERO_BUCKETS)
    cost = 0.0
    attributed = 0.0
    unattributed = 0.0
    by_tier = {"peak": 0.0, "off": 0.0}
    estimated = False
    models: dict = {}

    def _price(row: dict) -> tuple[float, dict]:
        b = normalize_usage(row)
        at = _row_ts_seconds(row) * 1000.0
        result = price_usage(b, model=row.get("model") or None, at=at, table=tbl)
        return float(result["cost"]["total"]), result["rates"]

    for row in requests:
        buckets = normalize_usage(row)
        totals_buckets = add_buckets(totals_buckets, buckets)
        row_cost, rates = _price(row)
        cost += row_cost
        attributed += row_cost
        by_tier[rates["tier"]] = by_tier.get(rates["tier"], 0.0) + row_cost
        if rates.get("estimated"):
            estimated = True
        key = str(row.get("model") or "unknown")
        rec = models.setdefault(key, {"model": key, "requests": 0, "cost": 0.0,
                                      "estimated": False})
        rec["requests"] += 1
        rec["cost"] += row_cost
        if rates.get("estimated"):
            rec["estimated"] = True

    for row in aggregates:
        buckets = normalize_usage(row)
        totals_buckets = add_buckets(totals_buckets, buckets)
        row_cost, rates = _price(row)
        cost += row_cost
        unattributed += row_cost
        by_tier[rates["tier"]] = by_tier.get(rates["tier"], 0.0) + row_cost
        if rates.get("estimated"):
            estimated = True

    # 逐回合明细（attributed 的那部分）
    turn_rows = []
    for turn in group_turns(requests, idle_seconds):
        turn_cost = 0.0
        turn_estimated = False
        turn_tier = None
        for row in turn["rows"]:
            row_cost, rates = _price(row)
            turn_cost += row_cost
            if rates.get("estimated"):
                turn_estimated = True
            if turn_tier is None or turn_tier == rates["tier"]:
                turn_tier = rates["tier"]
            else:
                turn_tier = "mixed"
        turn_rows.append({
            "turn_id": turn["turn_id"],
            "session_id": turn["session_id"],
            "steps": len(turn["rows"]),
            "first_ts": turn["first_ts"],
            "last_ts": turn["last_ts"],
            "buckets": turn["buckets"],
            "inputTokens": input_tokens_of(turn["buckets"]),
            "totalTokens": total_tokens_of(turn["buckets"]),
            "cost": turn_cost,
            "tier": turn_tier,
            "estimated": turn_estimated,
            "models": sorted({str(r.get("model") or "") for r in turn["rows"] if r.get("model")}),
        })
    turn_rows.sort(key=lambda t: t["last_ts"])

    most_expensive = None
    if turn_rows:
        most_expensive = max(turn_rows, key=lambda t: t["cost"])

    total_tokens = total_tokens_of(totals_buckets)
    input_tokens = input_tokens_of(totals_buckets)
    hit = int(totals_buckets.get("cacheHit") or 0)
    turns_count = len(turn_rows)
    steps = len(requests)

    # 分会话拆分（对齐 qq-bridge 的 conversations）
    conversations: dict = {}
    for turn in turn_rows:
        rec = conversations.setdefault(turn["session_id"], {
            "key": turn["session_id"],
            "cost": 0.0,
            "turns": 0,
            "steps": 0,
            "inputTokens": 0,
            "estimated": False,
        })
        rec["cost"] += turn["cost"]
        rec["turns"] += 1
        rec["steps"] += turn["steps"]
        rec["inputTokens"] += turn["inputTokens"]
        rec["estimated"] = rec["estimated"] or turn["estimated"]

    return {
        "ok": True,
        "generatedAt": stamp,
        "priceVersion": tbl.get("version"),
        "currency": tbl.get("currency") or "CNY",
        "symbol": tbl.get("symbol") or "¥",
        "nowTier": rates_at(None, stamp, tbl),
        "priceTable": {
            "version": tbl.get("version"),
            "currency": tbl.get("currency"),
            "symbol": tbl.get("symbol"),
            "unit": tbl.get("unit"),
            "peakMultiplier": tbl.get("peakMultiplier"),
            "peakWindows": tbl.get("peakWindows"),
            "peakWeekdays": tbl.get("peakWeekdays"),
            "holidayCount": len(tbl.get("holidays") or []),
            "models": {
                name: {
                    "label": entry.get("label"),
                    "currency": entry.get("currency") or tbl.get("currency"),
                    "peakMultiplier": entry.get("peakMultiplier"),
                    "estimated": bool(entry.get("estimated")),
                    "source": entry.get("source"),
                }
                for name, entry in (tbl.get("models") or {}).items()
            },
        },
        "totals": {
            "buckets": totals_buckets,
            "cost": cost,
            "attributedCost": attributed,
            "unattributedCost": unattributed,
            "byTier": by_tier,
            "requests": steps,
            "aggregateRequests": sum(int(r.get("n") or 1) for r in aggregates),
            "turns": turns_count,
            "steps": steps,
            "estimated": estimated,
            "totalTokens": total_tokens,
            "inputTokens": input_tokens,
            "cacheHitRate": (hit / input_tokens) if input_tokens > 0 else 0.0,
            "avgCostPerTurn": (cost / turns_count) if turns_count > 0 else 0.0,
            "avgCostPerStep": (cost / steps) if steps > 0 else 0.0,
        },
        "models": list(models.values()),
        "conversations": sorted(conversations.values(), key=lambda c: c["cost"], reverse=True),
        "turns": turn_rows,
        "mostExpensiveTurn": most_expensive,
    }


#: 兼容别名（有人习惯叫 summarize_rows）
summarize_rows = summary


# ---------------------------------------------------------------------------
# 输入侧预算（Stage 4.4）
# ---------------------------------------------------------------------------

#: companion_config.yaml 里预算段的键名
BUDGET_SECTION = "cost_budget"


@dataclass(frozen=True)
class CostBudget:
    """输入侧预算配置（一轮对话允许的输入 token 上限 + 告警策略）。"""

    #: 一轮对话的输入 token 上限（超过就告警）。0 = 不限。
    input_tokens_per_turn: int = 32000
    #: 多久没有请求算「新一轮」（秒）：同一回合里的多次模型调用只隔几秒
    turn_idle_seconds: int = DEFAULT_TURN_IDLE_SECONDS
    #: 超预算时是否告警（写台账 + 通知主人）
    warn: bool = True
    #: 同一个会话两次告警之间的最短间隔（秒），防止连续刷屏
    notice_min_interval_seconds: int = 300

    @property
    def enabled(self) -> bool:
        return self.input_tokens_per_turn > 0 and self.warn


def load_cost_budget(section: str = BUDGET_SECTION) -> CostBudget:
    """从 companion_config.yaml 读预算配置（**不读 config.yaml**）。

    读法跟 app/engine/tools/_core.py 读 tool_allowlist 一样：**走 yaml 原文**
    （companion_config 的 dataclass 会丢掉没登记的字段，而这段是后加的）。
    优先级：data/companion_config.yaml（用户覆盖） > app/core/companion_config.yaml
    （仓库默认） > 内置默认值。
    """
    defaults = CostBudget()
    try:
        from app.core import companion_config as cc
        base = cc._read_yaml(cc._default_yaml_path()) or {}
        user = cc._read_yaml(cc._user_yaml_path()) or {}
        merged = cc._merge(base, user)
    except Exception as e:  # noqa: BLE001 - 配置读不到就用默认值
        log.info("读 %s 段失败（用内置默认预算）：%s", section, e)
        return defaults
    block = merged.get(section)
    if not isinstance(block, dict):
        return defaults

    def _int(key: str, fallback: int) -> int:
        try:
            value = int(block.get(key, fallback))
        except (TypeError, ValueError):
            return fallback
        return value if value >= 0 else fallback

    def _bool(key: str, fallback: bool) -> bool:
        value = block.get(key, fallback)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return fallback

    return CostBudget(
        input_tokens_per_turn=_int("input_tokens_per_turn",
                                   defaults.input_tokens_per_turn),
        turn_idle_seconds=_int("turn_idle_seconds",
                               defaults.turn_idle_seconds) or
        DEFAULT_TURN_IDLE_SECONDS,
        warn=_bool("warn", defaults.warn),
        notice_min_interval_seconds=_int("notice_min_interval_seconds",
                                         defaults.notice_min_interval_seconds),
    )


__all__ = [
    "PRICE_TABLE_VERSION",
    "CN_HOLIDAYS",
    "CN_HOLIDAYS_2026",
    "CN_HOLIDAYS_2027",
    "DEFAULT_PRICE_TABLE",
    "CNY_PRICE_TABLE",
    "DEFAULT_TURN_IDLE_SECONDS",
    "MINIMAX_PRICE_SOURCE",
    "MINIMAX_PRICE_SOURCE_USD",
    "DEEPSEEK_PRICE_SOURCE",
    "CostBudget",
    "BUDGET_SECTION",
    "ZERO_BUCKETS",
    "add_buckets",
    "beijing_parts",
    "cost_of_buckets",
    "estimate_cost",
    "group_turns",
    "input_tokens_of",
    "is_peak_at",
    "load_cost_budget",
    "normalize_usage",
    "price_usage",
    "rates_at",
    "resolve_price_table",
    "sub_buckets",
    "summary",
    "summarize_rows",
    "total_tokens_of",
]
