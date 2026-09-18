"""桌宠工具系统（Function Calling）。

让大模型不只是「说」，还能「做」：
    - 定时提醒：add_reminder / list_reminders / delete_reminder
    - 长期记忆：remember_fact / recall_memory / forget_memory
    - 照顾自己：feed_self（花桌宠的钱买 foods.json 里的食物）
    - 状态感知：get_pet_status / get_current_time
    - 操作电脑：open_website / open_app
    - 主动表达：say_to_user（气泡冒一句话）

结构：
    Tool(name, description, parameters, fn)
    ToolRegistry: 注册 + to_openai()（请求体里的 tools 字段）+ execute()

fn 签名：fn(**kwargs) -> str（返回给模型的工具结果，尽量是简洁中文/JSON）。
所有 fn 都在聊天 worker 线程同步执行，必须快速返回，不要在里面开 GUI。
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import subprocess
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from app.brain.memory import MemoryStore
from app.engine.reminder import ReminderStore
from app.engine.screenshot import screenshot_to_base64
from app.engine.state import PetState
from app.engine.works import ItemStore, apply_food

log = logging.getLogger(__name__)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict            # JSON Schema
    fn: Callable[..., str]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return list(self._tools)

    def to_openai(self) -> list[dict]:
        """转成 OpenAI tools 字段格式。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def execute(self, name: str, arguments: str | dict) -> str:
        """执行一个工具调用，永远返回字符串（异常也转成错误文本给模型）。"""
        tool = self._tools.get(name)
        if tool is None:
            return f"错误：未知工具 {name}"
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else dict(arguments or {})
        except json.JSONDecodeError as e:
            return f"错误：参数不是合法 JSON（{e}）"
        try:
            return str(tool.fn(**args))
        except TypeError as e:
            return f"错误：参数不匹配（{e}）"
        except Exception as e:  # noqa: BLE001
            log.exception("工具 %s 执行失败", name)
            return f"错误：{e}"


# ---------------- 工具实现 ----------------

def _fmt_time() -> str:
    now = datetime.datetime.now()
    wd = "一二三四五六日"[now.weekday()]
    return now.strftime(f"%Y-%m-%d %H:%M:%S 星期{wd}")


def _parse_absolute_time(at_time: str) -> float | None:
    """解析绝对时间字符串，返回 unix timestamp。

    支持格式：
        - 'HH:MM'（今天，若已过则推到明天）
        - 'YYYY-MM-DD HH:MM'（指定日期时间）
        - '明天 HH:MM' / '后天 HH:MM'
        - '早上 HH:MM' / '晚上 HH:MM' / '下午 HH:MM' 等
    """
    import re
    from datetime import timedelta

    now = datetime.datetime.now()
    at_time = at_time.strip()

    # 格式1: 'HH:MM'（今天）
    m = re.match(r'^(\d{1,2}):(\d{2})$', at_time)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        target = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)  # 如果已经过了，默认明天
        return target.timestamp()

    # 格式2: 'YYYY-MM-DD HH:MM'
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})\s+(\d{1,2}):(\d{2})$', at_time)
    if m:
        try:
            target = datetime.datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                                       int(m.group(4)), int(m.group(5)))
            return target.timestamp()
        except ValueError:
            return None

    # 格式3: '明天 HH:MM' / '后天 HH:MM'
    m = re.match(r'^(明天|后天)\s*(\d{1,2}):(\d{2})$', at_time)
    if m:
        days = 1 if m.group(1) == "明天" else 2
        h, mi = int(m.group(2)), int(m.group(3))
        target = (now + timedelta(days=days)).replace(hour=h, minute=mi, second=0, microsecond=0)
        return target.timestamp()

    # 格式4: '早上 HH:MM' / '上午 HH:MM' / '中午 HH:MM' / '下午 HH:MM' / '晚上 HH:MM' / '凌晨 HH:MM'
    m = re.match(r'^(早上|上午|中午|下午|晚上|凌晨)\s*(\d{1,2}):(\d{2})$', at_time)
    if m:
        h, mi = int(m.group(2)), int(m.group(3))
        period = m.group(1)
        if period in ("晚上", "凌晨") and h < 12:
            h += 12
        target = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.timestamp()

    return None


def build_default_tools(
    *,
    state: PetState,
    reminders: ReminderStore,
    memory: MemoryStore,
    items: Optional[ItemStore] = None,
    hooks: Optional[dict[str, Callable[[str], None]]] = None,
) -> ToolRegistry:
    """用各子系统组装默认工具集。

    hooks: 可选回调，key 取 "bubble"（桌宠冒泡）/"animation"（播动画），
           由主程序提供（内部用 Qt 信号转回 UI 线程）。
    """
    hooks = hooks or {}
    reg = ToolRegistry()

    def _hook(key: str, arg: str) -> None:
        cb = hooks.get(key)
        if cb:
            try:
                cb(arg)
            except Exception:  # noqa: BLE001
                log.exception("hook %s 失败", key)

    # ---- 时间 / 状态 ----
    def get_current_time() -> str:
        return json.dumps({"now": _fmt_time()}, ensure_ascii=False)

    def get_pet_status() -> str:
        return json.dumps({
            "summary": state.stats_summary(),
            "strength": round(state.strength, 1),
            "food": round(state.strength_food, 1),
            "drink": round(state.strength_drink, 1),
            "feeling": round(state.feeling, 1),
            "health": round(state.health, 1),
            "money": round(state.money, 1),
            "exp": round(state.exp, 1),
            "level": state.level,
            "likability": round(state.likability, 1),
        }, ensure_ascii=False)

    # ---- 提醒 ----
    def add_reminder(text: str, delay_minutes: float = None, at_time: str = None) -> str:
        """设置提醒。

        二选一：
        - delay_minutes: 多少分钟后提醒
        - at_time: 绝对时间，格式如 '08:00'（今天）、'2026-09-12 08:00'（指定日期）、'明天 08:00'
        """
        if at_time:
            fire_ts = _parse_absolute_time(at_time)
            if fire_ts is None:
                return f"错误：无法解析时间「{at_time}」，支持格式：'08:00'、'2026-09-12 08:00'、'明天 08:00'"
            if fire_ts <= time.time():
                return f"错误：时间「{at_time}」已经过去"
            item = reminders.add(int(fire_ts - time.time()), text.strip())
            return f"已设置提醒「{item.text}」，将在 {at_time} 触发"
        elif delay_minutes and delay_minutes > 0:
            item = reminders.add(int(delay_minutes * 60), text.strip())
            return f"已设置提醒「{item.text}」，{delay_minutes:g} 分钟后触发（id={item.id}）"
        else:
            return "错误：请指定 delay_minutes 或 at_time"

    def list_reminders() -> str:
        items = [i for i in reminders.list() if not i.done]
        if not items:
            return "当前没有待触发的提醒。"
        now = datetime.datetime.now()
        rows = []
        for i in items:
            left_min = max(0, (i.fire_at - now.timestamp()) / 60)
            rows.append(f"id={i.id} 「{i.text}」 还有 {left_min:.0f} 分钟")
        return "待触发提醒：\n" + "\n".join(rows)

    def delete_reminder(reminder_id: str) -> str:
        return "已删除。" if reminders.remove(reminder_id) else f"找不到 id={reminder_id} 的提醒。"

    # ---- 记忆 ----
    def remember_fact(content: str, category: str = "other",
                      importance: float = 0.5) -> str:
        """记一条事实。importance 0-1：用户偏好/重要事实用 0.8+，琐事 0.3。"""
        item = memory.add(content, category, importance=importance)
        return f"已记住（[{item.category}★{item.importance:.1f}] {item.content}）"

    def recall_memory(keyword: str = "", category: str = "",
                      limit: int = 10) -> str:
        """按关键词（或留空按重要性）检索长期记忆。支持语义检索。"""
        found = memory.search(keyword=keyword, category=category,
                              limit=max(1, min(50, limit)))
        if not found:
            return "没有找到相关记忆。"
        lines = [f"[{i.id}|{i.category}★{i.importance:.1f}] {i.content}"
                 for i in found]
        return f"找到 {len(found)} 条相关记忆（按相关度排序）：\n" + "\n".join(lines)

    def forget_memory(keyword: str) -> str:
        removed = memory.forget_by_keyword(keyword)
        return f"已删除 {removed} 条匹配「{keyword}」的记忆。"

    # ---- 照顾自己 ----
    def feed_self(food_name: str = "") -> str:
        if items is None:
            return "错误：食物库未加载。"
        it = items.by_name(food_name.strip())
        if it is None:
            names = "、".join(x.name for x in items.items[:20])
            return f"没有叫「{food_name}」的食物。可选：{names}"
        if not apply_food(state, it):
            return f"钱不够（{it.price} 金币，现有 {state.money:.0f}），先去打工吧～"
        _hook("animation", "eat")
        return f"吃掉了「{it.name}」，花 {it.price} 金币。现在状态：{state.stats_summary()}"

    # ---- 操作电脑 ----
    def open_website(url: str) -> str:
        url = url.strip()
        # 严格校验：只允许 http/https，禁止 file:/// 等本地协议
        if not url.startswith(("http://", "https://")):
            return f"错误：只允许 http:// 或 https:// 开头的网址，收到：{url}"
        webbrowser.open(url)
        return f"已在浏览器打开 {url}"

    def open_app(app_name: str) -> str:
        """启动一个 Windows 应用程序。

        查找顺序：
            1. 完整路径 → 直接 os.startfile
            2. 注册表映射（内置 + 用户自定义 JSON）
            3. Program Files 目录搜索
            4. cmd /c start 兜底
        """
        from .app_registry import get_registry
        name = app_name.strip()
        p = Path(name)
        # 完整路径：直接打开
        if p.is_file():
            os.startfile(str(p))  # noqa: S606
            return f"已启动 {p.name}"
        # 注册表映射（内置 + 用户自定义）
        registry = get_registry()
        found = registry.resolve(name)
        if found:
            os.startfile(found)  # noqa: S606
            return f"已启动 {name}（{found}）"
        # 最后兜底：用 Windows start 命令
        try:
            subprocess.Popen(["cmd.exe", "/c", "start", "", name])
            return f"已尝试启动 {name}（若没弹出窗口可能未安装，可在 data/app_registry.json 中添加路径映射）"
        except Exception as e:  # noqa: BLE001
            return f"错误：无法启动「{name}」：{e}"

    # ---- 系统信息 ----
    def system_info() -> str:
        """获取系统信息：CPU、内存、磁盘、电量、OS。"""
        import platform
        import shutil

        info = {
            "os": platform.system() + " " + platform.release(),
            "python": platform.python_version(),
            "cpu": platform.processor() or platform.machine(),
            "platform": platform.platform(),
        }

        # CPU 使用率（快速采样）
        try:
            import psutil
            info["cpu_percent"] = f"{psutil.cpu_percent(interval=0.1):.1f}%"
            mem = psutil.virtual_memory()
            info["ram_total"] = f"{mem.total / 1024**3:.1f} GB"
            info["ram_used"] = f"{mem.used / 1024**3:.1f} GB"
            info["ram_percent"] = f"{mem.percent:.1f}%"
            disk = psutil.disk_usage("C:\\")
            info["disk_total"] = f"{disk.total / 1024**3:.1f} GB"
            info["disk_free"] = f"{disk.free / 1024**3:.1f} GB"
            info["disk_percent"] = f"{disk.percent:.1f}%"
            try:
                bat = psutil.sensors_battery()
                if bat:
                    info["battery"] = f"{bat.percent:.0f}%{'(充电中)' if bat.power_plug else '(未充电)'}"
            except Exception:  # noqa: BLE001
                pass
        except ImportError:
            info["cpu_percent"] = "未安装 psutil"
            info["ram"] = "未安装 psutil"
            info["disk"] = "未安装 psutil"
            info["battery"] = "未安装 psutil"

        return json.dumps(info, ensure_ascii=False)

    # ---- 数学计算 ----
    def calculate(expression: str) -> str:
        """安全计算数学表达式。支持 + - * / ( ) . ** sqrt sin cos tan log。"""
        import math

        allowed = {
            "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos,
            "tan": math.tan, "log": math.log, "log10": math.log10,
            "pi": math.pi, "e": math.e, "abs": abs, "round": round,
            "pow": pow,
        }
        try:
            # 安全替换 ^ → **
            expr = expression.replace("^", "**")
            # 检查是否包含非法字符
            for ch in expr:
                if not (ch.isalnum() or ch in " +-*/().,%_sqrtsinco tlgepbqrl"):
                    return f"错误：表达式包含非法字符 {ch}"
            result = eval(expr, {"__builtins__": {}}, allowed)  # noqa: S307
            return f"{expression} = {result}"
        except Exception as e:  # noqa: BLE001
            return f"计算错误：{e}"

    # ---- 剪贴板 ----
    def clipboard_copy(text: str) -> str:
        """把文本复制到系统剪贴板。"""
        try:
            from app.core.qt_compat import QApplication, QClipboard
            app = QApplication.instance()
            if app is None:
                return "错误：无 GUI 应用实例"
            app.clipboard().setText(text)
            return f"已复制到剪贴板（{len(text)} 字符）"
        except Exception as e:  # noqa: BLE001
            return f"错误：{e}"

    # ---- 系统通知 ----
    def send_notification(title: str, message: str) -> str:
        """发送 Windows 系统通知。"""
        try:
            import win10toast
            toast = win10toast.ToastNotifier()
            toast.show_toast(title, message, duration=5, threaded=True)
            return "通知已发送"
        except ImportError:
            return "未安装 win10toast，无法发送系统通知（pip install win10toast）"
        except Exception as e:  # noqa: BLE001
            return f"错误：{e}"

    # ---- 日期信息 ----
    def date_info(date_str: str = "") -> str:
        """获取日期信息：公历、农历、星期、生肖、节日。"""
        import datetime as dt

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

        # 尝试获取农历（lunardate 库）
        try:
            from lunardate import LunarDate
            lunar = LunarDate.fromSolarDate(target.year, target.month, target.day)
            lunar_info = {
                "lunar_year": lunar.year,
                "lunar_month": lunar.month,
                "lunar_day": lunar.day,
                "is_leap_month": lunar.isLeapMonth,
            }
            info["lunar"] = lunar_info
        except ImportError:
            info["lunar"] = "未安装 lunardate（pip install lunardate）"

        # 生肖
        zodiac_zh = ["鼠", "牛", "虎", "兔", "龙", "蛇", "马", "羊", "猴", "鸡", "狗", "猪"]
        zodiac_en = ["Rat", "Ox", "Tiger", "Rabbit", "Dragon", "Snake",
                     "Horse", "Goat", "Monkey", "Rooster", "Dog", "Pig"]
        idx = (target.year - 4) % 12
        info["zodiac"] = f"{zodiac_zh[idx]}（{zodiac_en[idx]}）"

        # 星座
        constellation = "摩羯座"
        if (target.month, target.day) >= (1, 20) and (target.month, target.day) < (2, 19):
            constellation = "水瓶座"
        elif (target.month, target.day) >= (2, 19) and (target.month, target.day) < (3, 21):
            constellation = "双鱼座"
        elif (target.month, target.day) >= (3, 21) and (target.month, target.day) < (4, 20):
            constellation = "白羊座"
        elif (target.month, target.day) >= (4, 20) and (target.month, target.day) < (5, 21):
            constellation = "金牛座"
        elif (target.month, target.day) >= (5, 21) and (target.month, target.day) < (6, 22):
            constellation = "双子座"
        elif (target.month, target.day) >= (6, 22) and (target.month, target.day) < (7, 23):
            constellation = "巨蟹座"
        elif (target.month, target.day) >= (7, 23) and (target.month, target.day) < (8, 23):
            constellation = "狮子座"
        elif (target.month, target.day) >= (8, 23) and (target.month, target.day) < (9, 23):
            constellation = "处女座"
        elif (target.month, target.day) >= (9, 23) and (target.month, target.day) < (10, 24):
            constellation = "天秤座"
        elif (target.month, target.day) >= (10, 24) and (target.month, target.day) < (11, 23):
            constellation = "天蝎座"
        elif (target.month, target.day) >= (11, 23) and (target.month, target.day) < (12, 22):
            constellation = "射手座"
        info["constellation"] = constellation

        return json.dumps(info, ensure_ascii=False)

    # ---- 单位换算 ----
    def convert_units(value: float, from_unit: str, to_unit: str) -> str:
        """单位换算：长度(cm/m/ft/inch)、重量(kg/g/lb/oz)、温度(C/F/K)、速度(km/h/m/s/mp/h)。"""
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
                else:  # K
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

    # ---- 文件操作 ----
    def list_desktop_files() -> str:
        """列出桌面文件。"""
        from pathlib import Path
        import os
        desktop = Path(os.path.join(os.environ.get("USERPROFILE", ""), "Desktop"))
        if not desktop.is_dir():
            desktop = Path.home() / "Desktop"
        if not desktop.is_dir():
            return "找不到桌面目录"
        files = sorted(desktop.iterdir(), key=lambda p: p.name)
        if not files:
            return "桌面是空的"
        lines = [f"{'📁' if p.is_dir() else '📄'} {p.name}" for p in files[:50]]
        total = len(files)
        if total > 50:
            lines.append(f"... 还有 {total - 50} 个文件")
        return f"桌面共 {total} 个文件：\n" + "\n".join(lines)

    def read_text_file(path: str) -> str:
        """读取文本文件内容（最多 2000 字符）。"""
        p = Path(path)
        if not p.is_file():
            return f"文件不存在：{path}"
        try:
            content = p.read_text(encoding="utf-8", errors="replace")[:2000]
            if len(p.read_text(encoding="utf-8", errors="replace")) > 2000:
                content += "\n...（已截断，文件较长）"
            return content
        except Exception as e:  # noqa: BLE001
            return f"读取失败：{e}"

    # ---- 快捷启动 ----
    def open_task_manager() -> str:
        """打开任务管理器。"""
        subprocess.Popen(["taskmgr.exe"])
        return "已打开任务管理器"

    def open_control_panel() -> str:
        """打开控制面板。"""
        os.startfile("control")
        return "已打开控制面板"

    def open_windows_settings() -> str:
        """打开 Windows 设置。"""
        os.startfile("ms-settings:")
        return "已打开 Windows 设置"

    def open_file_explorer(path: str = "") -> str:
        """打开文件资源管理器，可指定目录。"""
        if path:
            os.startfile(path)
        else:
            os.startfile("explorer.exe")
        return f"已打开文件资源管理器{' (' + path + ')' if path else ''}"

    def open_terminal() -> str:
        """打开终端（PowerShell）。"""
        subprocess.Popen(["powershell.exe"])
        return "已打开 PowerShell 终端"

    def open_notepad(text: str = "") -> str:
        """打开记事本，可选初始文本。"""
        if text:
            import tempfile
            tmp = Path(tempfile.gettempdir()) / "pet_notepad.txt"
            tmp.write_text(text, encoding="utf-8")
            os.startfile(str(tmp))
            return f"已打开记事本（{len(text)} 字符）"
        else:
            os.startfile("notepad.exe")
            return "已打开记事本"

    def open_calculator() -> str:
        """打开计算器。"""
        os.startfile("calc.exe")
        return "已打开计算器"

    # ---- 桌宠控制 ----
    def play_animation(anim_name: str) -> str:
        """触发表情动画。可选：happy, sad, angry, shy, think, pride, fear, doubt, surprise, stretch, jump, spin, swim, file"""
        name = anim_name.strip().lower()
        _hook("animation", name)
        return f"已触发「{name}」动画"

    def change_pet_emotion(emotion: str) -> str:
        """切换桌宠情绪表情。可选：happy, sad, angry, shy, think, pride, fear, doubt, surprise"""
        name = emotion.strip().lower()
        _hook("animation", f"emotion_{name}")
        return f"已切换到「{name}」情绪"

    # ---- 主动表达 ----
    def say_to_user(text: str) -> str:
        _hook("bubble", text.strip())
        return "已对主人说。"

    reg.register(Tool(
        name="get_current_time",
        description="获取当前的日期、时间和星期。",
        parameters={"type": "object", "properties": {}},
        fn=get_current_time,
    ))
    reg.register(Tool(
        name="get_pet_status",
        description="查看桌宠自己的状态：体力/饱食/口渴/心情/健康/金币/等级/好感度。",
        parameters={"type": "object", "properties": {}},
        fn=get_pet_status,
    ))
    reg.register(Tool(
        name="add_reminder",
        description="设置一个定时提醒。可以用 delay_minutes（多少分钟后）或 at_time（绝对时间）二选一。",
        parameters={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "提醒内容"},
                "delay_minutes": {"type": "number", "description": "多少分钟后提醒"},
                "at_time": {"type": "string", "description": "绝对时间，支持 '08:00'、'2026-09-12 08:00'、'明天 08:00' 等格式"},
            },
            "required": ["text"],
        },
        fn=add_reminder,
    ))
    reg.register(Tool(
        name="list_reminders",
        description="列出所有待触发的提醒。",
        parameters={"type": "object", "properties": {}},
        fn=list_reminders,
    ))
    reg.register(Tool(
        name="delete_reminder",
        description="删除一个提醒。id 从 list_reminders 获得。",
        parameters={
            "type": "object",
            "properties": {"reminder_id": {"type": "string"}},
            "required": ["reminder_id"],
        },
        fn=delete_reminder,
    ))
    reg.register(Tool(
        name="remember_fact",
        description=(
            "把关于主人的重要信息存入长期记忆（偏好/事实/事件等），"
            "以便以后引用。category: preference/fact/event/skill/person/other。"
            "importance 0-1：偏好/重要事实用 0.8+，琐事 0.3。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要记住的内容，一句话"},
                "category": {"type": "string", "enum": list(
                    ("preference", "fact", "event", "skill", "person", "other")),
                    "description": "类别"},
                "importance": {"type": "number", "description": "重要性 0-1，默认 0.5"},
            },
            "required": ["content"],
        },
        fn=remember_fact,
    ))
    reg.register(Tool(
        name="recall_memory",
        description=(
            "按关键词（或语义）检索长期记忆。keyword 为空时按重要性返回。"
            "可指定 category 过滤。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "关键词，为空则按重要性返回"},
                "category": {"type": "string", "enum": list(
                    ("preference", "fact", "event", "skill", "person", "other")),
                    "description": "按类别过滤"},
                "limit": {"type": "integer", "description": "返回条数（默认 10）"},
            },
        },
        fn=recall_memory,
    ))
    reg.register(Tool(
        name="forget_memory",
        description="删除匹配关键词的记忆。",
        parameters={
            "type": "object",
            "properties": {"keyword": {"type": "string"}},
            "required": ["keyword"],
        },
        fn=forget_memory,
    ))
    reg.register(Tool(
        name="feed_self",
        description="用桌宠自己的金币买食物吃（食物名必须完全匹配 foods.json）。",
        parameters={
            "type": "object",
            "properties": {"food_name": {"type": "string"}},
            "required": ["food_name"],
        },
        fn=feed_self,
    ))
    reg.register(Tool(
        name="open_website",
        description="用默认浏览器打开一个网址。",
        parameters={
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
        fn=open_website,
    ))
    reg.register(Tool(
        name="open_app",
        description="启动一个 Windows 应用程序。支持中文名/英文名/完整路径。路径映射可通过 data/app_registry.json 自定义。",
        parameters={
            "type": "object",
            "properties": {"app_name": {"type": "string"}},
            "required": ["app_name"],
        },
        fn=open_app,
    ))
    # ---- 截图分析 ----
    def take_screenshot_tool() -> str:
        """截取当前屏幕并返回 base64 编码的图片。

        注意：模型需要支持 vision 能力才能分析图片。
        """
        b64 = screenshot_to_base64()
        if b64 is None:
            return "错误：截图失败，请确保已安装 pyautogui 或 Pillow"
        return f"截图成功（base64，{len(b64)} 字符）"

    reg.register(Tool(
        name="say_to_user",
        description="在桌宠头顶气泡里冒一句话（不用等主人打开聊天窗）。适合补充说明、打招呼。",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        fn=say_to_user,
    ))
    reg.register(Tool(
        name="take_screenshot",
        description="截取当前屏幕截图（返回 base64 编码的 PNG）。用于让桌宠看到用户屏幕内容。",
        parameters={"type": "object", "properties": {}},
        fn=take_screenshot_tool,
    ))

    # ---- 系统信息 ----
    reg.register(Tool(
        name="system_info",
        description="获取系统信息：OS版本、CPU使用率、内存、磁盘、电池电量。",
        parameters={"type": "object", "properties": {}},
        fn=system_info,
    ))

    # ---- 数学计算 ----
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

    # ---- 剪贴板 ----
    reg.register(Tool(
        name="clipboard_copy",
        description="把文本复制到系统剪贴板，主人可以 Ctrl+V 粘贴。",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要复制的文本"}},
            "required": ["text"],
        },
        fn=clipboard_copy,
    ))

    # ---- 系统通知 ----
    reg.register(Tool(
        name="send_notification",
        description="发送 Windows 系统通知（右下角弹窗）。需要安装 win10toast 库。",
        parameters={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "通知标题"},
                "message": {"type": "string", "description": "通知内容"},
            },
            "required": ["title", "message"],
        },
        fn=send_notification,
    ))

    # ---- 日期信息 ----
    reg.register(Tool(
        name="date_info",
        description="获取日期信息：公历、农历、星期、生肖、星座、节日。date_str 为空则查今天。",
        parameters={
            "type": "object",
            "properties": {"date_str": {"type": "string", "description": "日期，如 '2026-01-15'，为空则查今天"}},
        },
        fn=date_info,
    ))

    # ---- 单位换算 ----
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

    # ---- 文件操作 ----
    reg.register(Tool(
        name="list_desktop_files",
        description="列出桌面上的文件和文件夹。",
        parameters={"type": "object", "properties": {}},
        fn=list_desktop_files,
    ))
    reg.register(Tool(
        name="read_text_file",
        description="读取文本文件内容（最多 2000 字符）。",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string", "description": "文件路径"}},
            "required": ["path"],
        },
        fn=read_text_file,
    ))

    # ---- 快捷启动 ----
    reg.register(Tool(name="open_task_manager", description="打开任务管理器。",
        parameters={"type": "object", "properties": {}}, fn=open_task_manager))
    reg.register(Tool(name="open_control_panel", description="打开 Windows 控制面板。",
        parameters={"type": "object", "properties": {}}, fn=open_control_panel))
    reg.register(Tool(name="open_windows_settings", description="打开 Windows 设置。",
        parameters={"type": "object", "properties": {}}, fn=open_windows_settings))
    reg.register(Tool(name="open_file_explorer", description="打开文件资源管理器，可指定目录。",
        parameters={"type": "object", "properties": {"path": {"type": "string", "description": "目录路径，为空则打开默认"}}}, fn=open_file_explorer))
    reg.register(Tool(name="open_terminal", description="打开 PowerShell 终端。",
        parameters={"type": "object", "properties": {}}, fn=open_terminal))
    reg.register(Tool(name="open_notepad", description="打开记事本，可选初始文本。",
        parameters={"type": "object", "properties": {"text": {"type": "string", "description": "初始文本，为空则打开空白"}}}, fn=open_notepad))
    reg.register(Tool(name="open_calculator", description="打开计算器。",
        parameters={"type": "object", "properties": {}}, fn=open_calculator))

    # ---- 桌宠控制 ----
    reg.register(Tool(
        name="play_animation",
        description="触发表情/动作动画。可选：happy, sad, angry, shy, think, pride, fear, doubt, surprise, stretch, jump, spin, swim, file",
        parameters={
            "type": "object",
            "properties": {"anim_name": {"type": "string", "description": "动画名称"}},
            "required": ["anim_name"],
        },
        fn=play_animation,
    ))
    reg.register(Tool(
        name="change_pet_emotion",
        description="切换桌宠情绪表情。可选：happy, sad, angry, shy, think, pride, fear, doubt, surprise",
        parameters={
            "type": "object",
            "properties": {"emotion": {"type": "string", "description": "情绪名称"}},
            "required": ["emotion"],
        },
        fn=change_pet_emotion,
    ))

    return reg
