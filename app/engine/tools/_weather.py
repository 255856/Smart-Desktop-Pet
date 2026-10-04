"""天气查询（open-meteo，无需 API key；走 is_safe_url 校验 endpoint）。"""
from __future__ import annotations

import json
import logging
from urllib.parse import urlencode

import httpx

from ._core import Tool, ToolRegistry, is_safe_url

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
    """地名 → (lat, lon, resolved_name)。失败时返回默认 + 默认地名。"""
    url = f"https://{_GEOCODING_HOST}/v1/search?{urlencode({'name': location, 'count': 1, 'language': 'zh', 'format': 'json'})}"
    if not is_safe_url(url, allowed_hosts={_GEOCODING_HOST}):
        log.warning("geocoding url blocked by safety check")
        return DEFAULT_LONGITUDE, DEFAULT_LATITUDE, DEFAULT_LOCATION
    try:
        with httpx.Client(timeout=8.0) as c:
            r = c.get(url)
            r.raise_for_status()
            data = r.json()
        results = data.get("results") or []
        if not results:
            return DEFAULT_LONGITUDE, DEFAULT_LATITUDE, location or DEFAULT_LOCATION
        first = results[0]
        return float(first["longitude"]), float(first["latitude"]), first.get("name", location)
    except Exception as e:  # noqa: BLE001
        log.warning("geocoding failed: %s", e)
        return DEFAULT_LONGITUDE, DEFAULT_LATITUDE, location or DEFAULT_LOCATION


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

    def get_weather(location: str = "", days: int = 1) -> str:
        """查天气。location: 城市名（中文 / 拼音 / 英文都行，空则默认北京）；days: 1~7 预报天数。"""
        days = max(1, min(int(days if days else 1), 7))
        loc = (location or DEFAULT_LOCATION).strip()
        lon, lat, resolved = _geocode(loc)
        params = {
            "latitude": lat,
            "longitude": lon,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,humidity_2m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "timezone": "auto",
            "forecast_days": days,
        }
        url = f"https://{_OPENMETEO_HOST}/v1/forecast?{urlencode(params)}"
        if not is_safe_url(url, allowed_hosts={_OPENMETEO_HOST}):
            return "错误：天气查询 endpoint 被安全策略拒绝"
        try:
            with httpx.Client(timeout=10.0) as c:
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
            f"📍 {resolved}（{lat:.1f}, {lon:.1f}）",
            f"当前：{desc}，{current.get('temperature_2m', '?')}°C"
            f"（体感 {current.get('apparent_temperature', '?')}°C）",
            f"湿度 {current.get('humidity_2m', '?')}%，"
            f"风速 {current.get('wind_speed_10m', '?')} km/h",
        ]
        if days >= 1:
            daily = data.get("daily") or {}
            dts = daily.get("time") or []
            codes = daily.get("weather_code") or []
            tmax = daily.get("temperature_2m_max") or []
            tmin = daily.get("temperature_2m_min") or []
            pops = daily.get("precipitation_probability_max") or []
            if dts:
                lines.append("--- 预报 ---")
                for i, d in enumerate(dts):
                    dc = _WEATHER_CODE.get(codes[i] if i < len(codes) else 0, "?")
                    pop = pops[i] if i < len(pops) else 0
                    lines.append(
                        f"  {d}: {dc}，{tmin[i] if i < len(tmin) else '?'}~"
                        f"{tmax[i] if i < len(tmax) else '?'}°C，降水概率 {pop}%"
                    )
        return "\n".join(lines)

    reg.register(Tool(
        name="get_weather",
        description="查询某城市当前天气 + 未来 1~7 天预报。"
                    "location 不传则用默认（" + DEFAULT_LOCATION + "）。"
                    "底层走 open-meteo（无需 API key），请求域名走白名单校验。",
        parameters={"type": "object",
                    "properties": {
                        "location": {"type": "string",
                                     "description": "城市名（中文 / 拼音 / 英文），空则默认"},
                        "days": {"type": "integer", "minimum": 1, "maximum": 7,
                                 "description": "预报天数，1~7，默认 1"},
                    }},
        fn=get_weather,
    ))


__all__ = ["register"]