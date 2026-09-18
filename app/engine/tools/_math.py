"""数学 / 单位换算 / 日期工具。"""
from __future__ import annotations

import datetime as dt
import json

from ._core import Tool, ToolRegistry


# ---------- 数学计算 ----------
def _calc(expression: str) -> str:
    """安全计算数学表达式。支持 + - * / ( ) . ** sqrt sin cos tan log。"""
    import math
    allowed = {
        "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos,
        "tan": math.tan, "log": math.log, "log10": math.log10,
        "pi": math.pi, "e": math.e, "abs": abs, "round": round,
        "pow": pow,
    }
    try:
        expr = expression.replace("^", "**")
        for ch in expr:
            if not (ch.isalnum() or ch in " +-*/().,%_sqrtsinco tlgepbqrl"):
                return f"错误：表达式包含非法字符 {ch}"
        result = eval(expr, {"__builtins__": {}}, allowed)  # noqa: S307
        return f"{expression} = {result}"
    except Exception as e:  # noqa: BLE001
        return f"计算错误：{e}"


# ---------- 单位换算 ----------
def _convert(value: float, from_unit: str, to_unit: str) -> str:
    """单位换算：长度(cm/m/ft/inch)、重量(kg/g/lb/oz)、温度(C/F/K)、速度(km/h/m/s/mph)。"""
    u = from_unit.lower().strip()
    t = to_unit.lower().strip()

    # 温度
    if u in ("c", "celsius", "摄氏") or t in ("c", "celsius", "摄氏"):
        if u == t:
            return f"{value} C = {value} C"
        if u in ("c", "celsius", "摄氏"):
            result = value * 9/5 + 32 if t in ("f", "fahrenheit", "华氏") else value + 273.15
            t_name = "F" if t in ("f", "fahrenheit", "华氏") else "K"
        else:
            if u in ("f", "fahrenheit", "华氏"):
                v_c = (value - 32) * 5/9
            else:
                v_c = value - 273.15
            if t in ("c", "celsius", "摄氏"):
                result = v_c
                t_name = "C"
            elif t in ("f", "fahrenheit", "华氏"):
                result = v_c * 9/5 + 32
                t_name = "F"
            else:
                result = v_c + 273.15
                t_name = "K"
        return f"{value} {u.upper()} = {result:.2f} {t_name}"

    # 长度
    if u in ("cm", "毫米", "mm") and t in ("m", "米"):
        return f"{value} cm = {value/100:.4f} m"
    if u in ("m", "米") and t in ("cm", "毫米", "mm"):
        return f"{value} m = {value*100:.2f} cm"
    if u in ("cm", "米", "mm", "m") and t in ("in", "inches", "英寸"):
        v_in = value * 0.0393701 if u in ("cm", "mm") else value * 39.3701 if u in ("m", "米") else value
        return f"{value} {u} = {v_in:.4f} inch"
    if u in ("in", "inches", "英寸") and t in ("cm", "mm", "m", "米"):
        v = value * 2.54 if t in ("cm",) else value * 25.4 if t in ("mm",) else value * 0.0254
        return f"{value} inch = {v:.4f} {t}"
    if u in ("ft", "feet", "英尺") and t in ("m", "米", "cm"):
        v = value * 0.3048 if t in ("m", "米") else value * 30.48
        return f"{value} ft = {v:.4f} {t}"
    if u in ("m", "米", "cm") and t in ("ft", "feet", "英尺"):
        v = value * 3.28084 if u in ("m", "米") else value * 0.0328084
        return f"{value} {u} = {v:.4f} ft"

    # 重量
    if u in ("kg", "公斤", "千克") and t in ("g", "克"):
        return f"{value} kg = {value*1000:.2f} g"
    if u in ("g", "克") and t in ("kg", "公斤", "千克"):
        return f"{value} g = {value/1000:.4f} kg"
    if u in ("kg", "公斤", "千克") and t in ("lb", "lbs", "pound", "磅"):
        return f"{value} kg = {value*2.20462:.4f} lb"
    if u in ("lb", "lbs", "pound", "磅") and t in ("kg", "公斤", "千克"):
        return f"{value} lb = {value*0.453592:.4f} kg"
    if u in ("g", "克") and t in ("oz", "盎司"):
        return f"{value} g = {value*0.035274:.4f} oz"

    # 速度
    if u in ("km/h", "kmh", "公里每小时") and t in ("m/s", "mps", "米每秒"):
        return f"{value} km/h = {value/3.6:.4f} m/s"
    if u in ("m/s", "mps", "米每秒") and t in ("km/h", "kmh", "公里每小时"):
        return f"{value} m/s = {value*3.6:.2f} km/h"
    if u in ("km/h", "kmh", "公里每小时") and t in ("mph", "mi/h", "英里每小时"):
        return f"{value} km/h = {value*0.621371:.4f} mph"
    if u in ("mph", "mi/h", "英里每小时") and t in ("km/h", "kmh", "公里每小时"):
        return f"{value} mph = {value*1.60934:.2f} km/h"

    return f"不支持的换算：{from_unit} → {to_unit}。支持：长度(cm/m/ft/inch)、重量(kg/g/lb/oz)、温度(C/F/K)、速度(km/h/m/s/mph)"


# ---------- 日期信息 ----------
def _date_info(date_str: str = "") -> str:
    """获取日期信息：公历、农历、星期、生肖、节日。"""
    target = dt.datetime.now() if not date_str else None
    if date_str:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
            try:
                target = dt.datetime.strptime(date_str.strip(), fmt)
                break
            except ValueError:
                continue
        if target is None:
            return f"无法解析日期：{date_str}，支持格式：2026-01-15、2026/01/15、20260115"

    wd = "一二三四五六日"[target.weekday()]
    info = {
        "date": target.strftime("%Y-%m-%d"),
        "weekday": f"星期{wd}",
        "day_of_year": target.timetuple().tm_yday,
    }
    try:
        from lunardate import LunarDate
        lunar = LunarDate.fromSolarDate(target.year, target.month, target.day)
        info["lunar"] = {
            "lunar_year": lunar.year,
            "lunar_month": lunar.month,
            "lunar_day": lunar.day,
            "is_leap_month": lunar.isLeapMonth,
        }
    except ImportError:
        info["lunar"] = "未安装 lunardate（pip install lunardate）"

    zodiac_zh = ["鼠", "牛", "虎", "兔", "龙", "蛇", "马", "羊", "猴", "鸡", "狗", "猪"]
    zodiac_en = ["Rat", "Ox", "Tiger", "Rabbit", "Dragon", "Snake",
                 "Horse", "Goat", "Monkey", "Rooster", "Dog", "Pig"]
    idx = (target.year - 4) % 12
    info["zodiac"] = f"{zodiac_zh[idx]}（{zodiac_en[idx]}）"

    constellation = "摩羯座"
    md = (target.month, target.day)
    if md >= (1, 20) and md < (2, 19): constellation = "水瓶座"
    elif md >= (2, 19) and md < (3, 21): constellation = "双鱼座"
    elif md >= (3, 21) and md < (4, 20): constellation = "白羊座"
    elif md >= (4, 20) and md < (5, 21): constellation = "金牛座"
    elif md >= (5, 21) and md < (6, 22): constellation = "双子座"
    elif md >= (6, 22) and md < (7, 23): constellation = "巨蟹座"
    elif md >= (7, 23) and md < (8, 23): constellation = "狮子座"
    elif md >= (8, 23) and md < (9, 23): constellation = "处女座"
    elif md >= (9, 23) and md < (10, 24): constellation = "天秤座"
    elif md >= (10, 24) and md < (11, 23): constellation = "天蝎座"
    elif md >= (11, 23) and md < (12, 22): constellation = "射手座"
    info["constellation"] = constellation

    return json.dumps(info, ensure_ascii=False)


def register(reg: ToolRegistry) -> None:
    """注册数学 / 单位换算 / 日期信息三类工具。"""

    def calculate(expression: str) -> str:
        return _calc(expression)

    def convert_units(value: float, from_unit: str, to_unit: str) -> str:
        return _convert(value, from_unit, to_unit)

    def date_info(date_str: str = "") -> str:
        return _date_info(date_str)

    reg.register(Tool(
        name="calculate",
        description="安全计算数学表达式。支持 + - * / ( ) 以及 sqrt/sin/cos/tan/log/pi/e。",
        parameters={
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "数学表达式，如 '2+3*4' 或 'sqrt(144)'"}},
            "required": ["expression"],
        },
        fn=calculate,
    ))
    reg.register(Tool(
        name="convert_units",
        description="单位换算。支持长度(cm/m/ft/inch)、重量(kg/g/lb/oz)、温度(C/F/K)、速度(km/h/m/s/mph)。",
        parameters={
            "type": "object",
            "properties": {
                "value": {"type": "number", "description": "数值"},
                "from_unit": {"type": "string", "description": "源单位，如 'kg'、'c'、'km/h'"},
                "to_unit": {"type": "string", "description": "目标单位，如 'lb'、'f'、'mph'"},
            },
            "required": ["value", "from_unit", "to_unit"],
        },
        fn=convert_units,
    ))
    reg.register(Tool(
        name="date_info",
        description="获取日期信息：公历、农历、星期、生肖、星座、节日。date_str 为空则查今天。",
        parameters={
            "type": "object",
            "properties": {"date_str": {"type": "string", "description": "日期，如 '2026-01-15'，为空则查今天"}},
        },
        fn=date_info,
    ))


__all__ = ["register"]