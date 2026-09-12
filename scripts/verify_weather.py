"""用真实网络验证天气服务：Open-Meteo 为主，高德兜底。

不是测试套件的一部分——测试套件全部打桩。这个脚本确认组合逻辑对着真接口也对。
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

from lushu.engine import resolve_city
from lushu.services.trip_store import CityRef
from lushu.services.weather_service import forecast_for_city

CITIES = ("南京", "西安", "北京")


async def main() -> None:
    today = date.today()
    start = today + timedelta(days=3)
    end = today + timedelta(days=9)

    print(f"今天 {today}    查询区间 {start} ~ {end}")
    print()

    for name in CITIES:
        match = resolve_city(name)
        if match is None:
            print(f"── {name}：高德查不到 ──")
            continue

        city = CityRef(
            adcode=match.adcode,
            name=match.name or name,
            lat_gcj02=match.lat_gcj02,
            lng_gcj02=match.lng_gcj02,
        )
        print(f"── {city.name}（{city.adcode}  坐标 {city.lat_gcj02},{city.lng_gcj02}）──")

        forecast = await forecast_for_city(city, start, end, today=today)
        print(f"   来源：{forecast.source.label}    天数：{len(forecast.days)}")
        if forecast.note:
            print(f"   说明：{forecast.note}")
        for day in forecast.days:
            rainy = "  ⚠ 不宜户外" if day.is_bad_outdoor else ""
            rain = (
                f"  降水 {day.precipitation_probability:.0f}%"
                if day.precipitation_probability is not None
                else ""
            )
            print(f"     {day.day}  {day.text:<8} {day.temperature_text}{rain}{rainy}")
        print()


if __name__ == "__main__":
    asyncio.run(main())
