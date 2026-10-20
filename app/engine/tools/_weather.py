"""天气查询（open-meteo，无需 API key；走 is_safe_url 校验 endpoint）。"""
from __future__ import annotations

import logging
from urllib.parse import urlencode

import httpx

from . import _http
from ._core import ToolRegistry, is_safe_url
from ._decorator import tool

log = logging.getLogger(__name__)

# 固定 endpoint，host 写死在白名单（域名形式 is_safe_url 默认可过；
# 静态校验用白名单让请求确定性）
_OPENMETEO_HOST = "api.open-meteo.com"
_GEOCODING_HOST = "geocoding-api.open-meteo.com"

# 桌宠默认城市（用户没传 location 时）
DEFAULT_LATITUDE = 39.9042
DEFAULT_LONGITUDE = 116.4074    # 北京
DEFAULT_LOCATION = "北京"


def _geocode(location: str) -> tuple[float, float, str]:
    """地名 → (lon, lat, resolved_name)。解析失败时回落到默认城市。

    注意返回顺序是 (lon, lat, name)，不是文档里写的 (lat, lon, name)。
    回落时**必须**改写 resolved_name：原实现保留用户传入的地名，于是网络抖动时
    用户问「上海天气」会得到「地点：上海（39.9, 116.4）」——地名是上海的、
    数据是北京的，模型会照着回答上海的天气。静默的错误答案比报错更糟。
    """
    fallback_name = (f"{DEFAULT_LOCATION}（地名「{location}」解析失败，"
                     f"已按默认城市查询）")
    url = f"https://{_GEOCODING_HOST}/v1/search?{urlencode({'name': location, 'count': 1, 'language': 'zh', 'format': 'json'})}"
    if not is_safe_url(url, allowed_hosts={_GEOCODING_HOST}):
        log.warning("geocoding url blocked by safety check")
        return DEFAULT_LONGITUDE, DEFAULT_LATITUDE, fallback_name
    try:
        c = _http.get_client(8.0)
        r = c.get(url)
        r.raise_for_status()
        data = r.json()
        results = data.get("results") or []
        if not results:
            return DEFAULT_LONGITUDE, DEFAULT_LATITUDE, fallback_name
        first = results[0]
        return (float(first["longitude"]), float(first["latitude"]),
                first.get("name", location))
    except Exception as e:  # noqa: BLE001
        log.warning("geocoding failed: %s", e)
        return DEFAULT_LONGITUDE, DEFAULT_LATITUDE, fallback_name


_WEATHER_CODE = {
    0: "晴",
    1: "少云", 2: "多云", 3: "阴",
    45: "雾", 48: "冻雾",
    51: "毛毛雨", 53: "小雨", 55: "中雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴阵雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷暴", 96: "雷暴夹冰雹", 99: "强雷暴夹冰雹",
}


def register(reg: ToolRegistry) -> None:
    """注册天气查询工具。"""

    @tool(
        description="查询某城市当前天气 + 未来 1~7 天预报。"
                    "location 不传则用默认（" + DEFAULT_LOCATION + "）。"
                    "【重要】主人问「明天/后天/周末」等未来某天时，"
                    "days 必须传够天数（明天传 2、后天传 3、否则就查不到那一天）；"
                    "只关心现在才用 1。底层走 open-meteo（无需 API key），"
                    "请求域名走白名单校验。",
        parameters={"type": "object",
                    "properties": {
                        "location": {"type": "string",
                                     "description": "城市名（中文 / 拼音 / 英文），空则默认"},
                        "days": {"type": "integer", "minimum": 1, "maximum": 7,
                                 "description": "预报天数，1~7，默认 1。"
                                                "问未来某天时要传够：明天 2、后天 3、"
                                                "本周 7，否则查不到那一天"},
                    }},
    )
    def get_weather(location: str = "", days: int = 1) -> str:
        """查天气。location: 城市名（中文 / 拼音 / 英文都行，空则默认北京）；days: 1~7 预报天数。"""
        try:
            days = max(1, min(int(days if days else 1), 7))
        except (TypeError, ValueError):
            return f"错误：days 必须是 1~7 的整数，收到 {days!r}"
        loc = (location or DEFAULT_LOCATION).strip()
        lon, lat, resolved = _geocode(loc)
        params = {
            "latitude": lat,
            "longitude": lon,
            # 注意：湿度在 open-meteo 的 current 里叫 relative_humidity_2m，
            # 写 humidity_2m 会让整个请求 400（"Cannot initialize ...
            # from invalid String value humidity_2m"）——也就是说天气工具
            # 此前**每一次调用都失败**，只是没人报出来而已。
            "current": ("temperature_2m,apparent_temperature,weather_code,"
                        "wind_speed_10m,relative_humidity_2m"),
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto",
            "forecast_days": days,
        }
        url = f"https://{_OPENMETEO_HOST}/v1/forecast?{urlencode(params)}"
        if not is_safe_url(url, allowed_hosts={_OPENMETEO_HOST}):
            return "错误：天气查询 endpoint 被安全策略拒绝"
        try:
            c = _http.get_client(10.0)
            r = c.get(url)
            r.raise_for_status()
            data = r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("weather request failed: %s", e)
            return f"错误：天气查询失败：{e}"
        current = data.get("current") or {}
        code = current.get("weather_code", 0)
        desc = _WEATHER_CODE.get(code, f"天气码 {code}")
        lines = [
            f"地点：{resolved}（{lat:.1f}, {lon:.1f}）",
            f"当前：{desc}，{current.get('temperature_2m', '?')}°C"
            f"（体感 {current.get('apparent_temperature', '?')}°C）",
            f"湿度 {current.get('relative_humidity_2m', '?')}%，"
            f"风速 {current.get('wind_speed_10m', '?')} km/h",
        ]
        if days >= 1:
            daily = data.get("daily") or {}
            dts = daily.get("time") or []
            codes = daily.get("weather_code") or []
            tmax = daily.get("temperature_2m_max") or []
            tmin = daily.get("temperature_2m_min") or []
            pops = daily.get("precipitation_probability_max") or []
            # 【关键】当天那一行不要再报天气现象和降水概率。
            #
            # open-meteo 的 daily 是**全天 24 小时汇总**：凌晨下过毛毛雨，整天的
            # weather_code 就被标成「毛毛雨」，哪怕此刻是晴天。而 current 是当下
            # 实况。两者指的是同一天却常常互相矛盾——现场实测（2026-10-14 15:15 广州）：
            #     current  weather_code=0  → 晴
            #     daily[0] weather_code=51 → 毛毛雨，降水概率 31%
            # 工具把两行都吐出来，模型采信了 daily 那行，于是对主人说
            # 「广州今天有毛毛雨，31%的降水概率」——而窗外是大太阳。
            #
            # 当天只保留高低温（这个不矛盾），天气现象一律以上面「当前」为准。
            today = str(current.get("time") or "")[:10]
            if dts:
                lines.append("--- 预报 ---")
                for i, d in enumerate(dts):
                    lo = tmin[i] if i < len(tmin) else "?"
                    hi = tmax[i] if i < len(tmax) else "?"
                    if d == today:
                        lines.append(
                            f"  今天（{d}）全天 {lo}~{hi}°C，"
                            "天气现象以上面「当前」为准（日汇总会把凌晨的天气算进去）")
                        continue
                    dc = _WEATHER_CODE.get(codes[i] if i < len(codes) else 0, "?")
                    pop = pops[i] if i < len(pops) else 0
                    lines.append(f"  {d}: {dc}，{lo}~{hi}°C，降水概率 {pop}%")
        return "\n".join(lines)

    reg.register(get_weather)


__all__ = ["register"]