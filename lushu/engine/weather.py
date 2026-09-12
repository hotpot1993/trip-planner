"""高德天气的薄包装。

引擎已经实现了这个接口（`providers/weather/amap.py`），所以不必自己再写一套
HTTP 客户端。它的预报只有约 4 天，所以在本项目里只作兜底——主力是 Open-Meteo。

入参是**高德的行政区划代码**，不是城市名。引擎在出错时静默返回空列表，
这里保持同样的语义：天气拿不到不该让行程打不开。
"""

from __future__ import annotations

from datetime import date

from lushu import config
from lushu.domain.weather import DailyWeather, WeatherSource


def fetch_forecast(adcode: str, *, api_key: str | None = None) -> tuple[list[DailyWeather], str | None]:
    """取一座城市的逐日预报。返回（预报, 说明）。"""
    code = adcode.strip()
    if not code:
        return [], "没有行政区划代码，无法查高德天气"

    key = (api_key or config.amap_api_key()).strip()
    if not key:
        return [], "缺少 AMAP_API_KEY，无法查高德天气"

    # 延迟导入：必须先加载 config，再触碰引擎
    from third_party.floattrip.providers.weather.amap import fetch_forecast as engine_forecast

    try:
        raw = engine_forecast(code, key)
    except Exception as exc:  # noqa: BLE001 - 兜底数据源，失败不该往上抛
        return [], f"高德天气请求失败：{type(exc).__name__}"

    if not raw:
        return [], "高德天气没有返回这个城市的数据"

    days: list[DailyWeather] = []
    for item in raw:
        try:
            day = date.fromisoformat(str(item.get("date", "")))
        except ValueError:
            continue
        days.append(
            DailyWeather(
                day=day,
                text=str(item.get("day_weather") or "天气未知"),
                night_text=str(item.get("night_weather") or "") or None,
                temp_min=_to_float(item.get("night_temp")),
                temp_max=_to_float(item.get("day_temp")),
            )
        )

    if not days:
        return [], "高德天气返回的内容无法解析"
    return days, None


def source() -> WeatherSource:
    return WeatherSource.AMAP


def _to_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
