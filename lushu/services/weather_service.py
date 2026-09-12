"""多城市天气：按城市分别取预报。

分工（Q41）：**Open-Meteo 为主，高德兜底。**

- Open-Meteo 提供 16 天预报且无需 key，而高德只有约 4 天。多城市行程通常提前
  几周规划，4 天根本覆盖不到。
- 高德在 4 天以内可以作兜底，也在 Open-Meteo 不可达时顶上。

组合逻辑放在 services 而不是 adapters，是因为架构边界测试不允许 adapters 层
引用 `lushu.engine`。这不是迂回——「主 + 兜底」本来就是应用层的编排决定，
不是某个数据源自己的事。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import date

from lushu.adapters import weather as open_meteo
from lushu.domain.weather import CityForecast, DailyWeather, WeatherSource
from lushu.engine import fetch_amap_forecast
from lushu.services.trip_store import CityRef

__all__ = ["forecast_for_cities", "forecast_for_city"]


async def forecast_for_city(
    city: CityRef,
    start_date: date,
    end_date: date,
    *,
    today: date | None = None,
) -> CityForecast:
    """取一座城市的逐日预报。

    先问 Open-Meteo；它没有这座城市的坐标、或请求失败时，退到高德。
    两者都拿不到时返回一个带说明的空预报——天气查不到不该让行程页打不开。
    """
    if city.lat_gcj02 is not None and city.lng_gcj02 is not None:
        try:
            days, note = await asyncio.to_thread(
                open_meteo.fetch_forecast,
                latitude=city.lat_gcj02,
                longitude=city.lng_gcj02,
                start_date=start_date,
                end_date=end_date,
                today=today,
            )
        except open_meteo.WeatherUnavailableError as exc:
            days, note = [], f"Open-Meteo 不可用（{exc}），已改用高德"
        except (ValueError, TypeError) as exc:
            days, note = [], f"Open-Meteo 请求参数有误（{exc}），已改用高德"

        if days:
            return CityForecast(
                city_name=city.name,
                source=WeatherSource.OPEN_METEO,
                days=tuple(days),
                note=note,
            )
    else:
        note = "这座城市还没有坐标，无法用 Open-Meteo"
        days = []

    # 退到高德。它覆盖不了多远的日期，但 4 天内是准的。
    amap_days, amap_note = await asyncio.to_thread(fetch_amap_forecast, city.adcode)
    covered = _clip(amap_days, start_date, end_date)
    if covered:
        return CityForecast(
            city_name=city.name,
            source=WeatherSource.AMAP,
            days=tuple(covered),
            note=_join_notes(note, "高德只提供约 4 天预报"),
        )

    return CityForecast(
        city_name=city.name,
        source=WeatherSource.AMAP,
        days=(),
        note=_join_notes(note, amap_note or "高德也没有返回这个区间的预报"),
    )


async def forecast_for_cities(
    cities: Sequence[CityRef],
    start_date: date,
    end_date: date,
    *,
    today: date | None = None,
) -> list[CityForecast]:
    """并行取多座城市的预报。顺序与入参一致。"""
    if not cities:
        return []
    return list(
        await asyncio.gather(
            *(
                forecast_for_city(city, start_date, end_date, today=today)
                for city in cities
            )
        )
    )


def _clip(days: list[DailyWeather], start_date: date, end_date: date) -> list[DailyWeather]:
    return [day for day in days if start_date <= day.day <= end_date]


def _join_notes(*notes: str | None) -> str | None:
    kept = [note for note in notes if note]
    return "；".join(kept) if kept else None
