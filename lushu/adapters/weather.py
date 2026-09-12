"""Open-Meteo 天气适配器。

选它作主力的理由（Q41）：16 天预报、无需 key。高德的天气预报只有 4 天，
而多城市行程通常提前几周规划，4 天根本覆盖不到。

它需要的是经纬度——所以本适配器的入参是我们存在 `city` 表里的 GCJ-02 坐标。
Open-Meteo 基于全球网格，对几公里的坐标系差异不敏感，不需要做坐标转换。
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from lushu.domain.weather import DailyWeather, WeatherSource

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Open-Meteo 的预报天数上限
MAX_FORECAST_DAYS = 16

# WMO 天气编码 → 中文描述。这是 Open-Meteo 的编码体系，所以映射放在这里，
# 而不是领域层——换一家数据源，这套编码就不适用了。
_WMO_TEXT: dict[int, str] = {
    0: "晴",
    1: "少云",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "冻雾",
    51: "小毛毛雨",
    53: "毛毛雨",
    55: "大毛毛雨",
    56: "冻毛毛雨",
    57: "强冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "强冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "小阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "小阵雪",
    86: "强阵雪",
    95: "雷阵雨",
    96: "雷阵雨伴小冰雹",
    99: "雷阵雨伴冰雹",
}


class WeatherUnavailableError(RuntimeError):
    """Open-Meteo 返回了无法解析的内容。"""


def describe_wmo(code: object) -> str:
    """把 WMO 编码翻成中文。未知编码如实说不知道，不编一个像样的天气。"""
    if isinstance(code, bool) or not isinstance(code, (int, float)):
        return "天气未知"
    return _WMO_TEXT.get(int(code), f"未知天气（编码 {int(code)}）")


def fetch_forecast(
    *,
    latitude: float,
    longitude: float,
    start_date: date,
    end_date: date,
    today: date | None = None,
    client: httpx.Client | None = None,
) -> tuple[list[DailyWeather], str | None]:
    """取一段日期的逐日预报。返回（预报, 说明）。

    超出 16 天的部分会被截断，并在说明里写清楚——让调用方知道哪几天没有预报，
    而不是以为那几天是「无天气」。
    """
    if end_date < start_date:
        raise ValueError("结束日期不能早于开始日期")

    today = today or date.today()
    limit = today + timedelta(days=MAX_FORECAST_DAYS - 1)

    note: str | None = None
    effective_end = end_date
    if end_date > limit:
        effective_end = limit
        note = (
            f"Open-Meteo 只提供 {MAX_FORECAST_DAYS} 天预报，"
            f"{limit.isoformat()} 之后的日期暂时没有预报"
        )

    if start_date > effective_end:
        return [], note or f"{start_date.isoformat()} 还没有预报"

    params = {
        "latitude": f"{latitude:.4f}",
        "longitude": f"{longitude:.4f}",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "Asia/Shanghai",
        "start_date": start_date.isoformat(),
        "end_date": effective_end.isoformat(),
    }

    owned = client is None
    active = client or httpx.Client(timeout=20)
    try:
        response = active.get(FORECAST_URL, params=params)
    except httpx.HTTPError as exc:
        raise WeatherUnavailableError(f"Open-Meteo 请求失败：{type(exc).__name__}") from exc
    finally:
        if owned:
            active.close()

    if response.status_code != 200:
        raise WeatherUnavailableError(
            f"Open-Meteo 返回 HTTP {response.status_code}：{response.text[:120]}"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise WeatherUnavailableError("Open-Meteo 返回的内容不是合法 JSON") from exc

    daily = payload.get("daily") or {}
    days = _parse_daily(daily)
    if not days and note is None:
        note = "Open-Meteo 没有返回这个区间的预报"
    return days, note


def _parse_daily(daily: dict) -> list[DailyWeather]:
    times = daily.get("time") or []
    codes = daily.get("weather_code") or []
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    rains = daily.get("precipitation_probability_max") or []

    days: list[DailyWeather] = []
    for index, raw_day in enumerate(times):
        try:
            day = date.fromisoformat(str(raw_day))
        except ValueError:
            continue
        days.append(
            DailyWeather(
                day=day,
                text=describe_wmo(_at(codes, index)),
                temp_min=_as_float(_at(lows, index)),
                temp_max=_as_float(_at(highs, index)),
                precipitation_probability=_as_float(_at(rains, index)),
            )
        )
    return days


def _at(values: list, index: int):
    return values[index] if 0 <= index < len(values) else None


def _as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def source() -> WeatherSource:
    return WeatherSource.OPEN_METEO
