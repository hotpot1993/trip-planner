"""天气领域模型。

天气是**硬事实**（ADR-0001）：只能来自官方数据接口，攻略素材不得覆盖它。
所以这里的每个字段都带着来源，而不是一个裸的温度。

判定「不宜户外」的逻辑也在这里——它是不管数据来自哪家接口都成立的规则。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum


class WeatherSource(StrEnum):
    """预报表的来源。与 `intercity_transfer.price_source` 同理：来源必须跟着数据走。"""

    OPEN_METEO = "open-meteo"
    AMAP = "amap"

    @property
    def label(self) -> str:
        return {WeatherSource.OPEN_METEO: "Open-Meteo", WeatherSource.AMAP: "高德"}[self]


# 判断「不宜户外」的关键词。用于提醒用户把户外景点换到别的日子。
BAD_WEATHER_KEYWORDS = ("雨", "雪", "冰雹", "雾", "沙尘", "霾", "雷")


def is_bad_outdoor_weather(text: str) -> bool:
    """这种天气是否不适合安排户外行程。"""
    return any(keyword in text for keyword in BAD_WEATHER_KEYWORDS)


@dataclass(frozen=True)
class DailyWeather:
    """一天的预报。"""

    day: date
    text: str  # 给人看的中文描述，如「多云」「小雨」
    temp_min: float | None = None
    temp_max: float | None = None
    precipitation_probability: float | None = None  # 0-100
    night_text: str | None = None

    @property
    def is_bad_outdoor(self) -> bool:
        """白或夜任一时段天气不好，就算不宜户外。"""
        if is_bad_outdoor_weather(self.text):
            return True
        return bool(self.night_text) and is_bad_outdoor_weather(self.night_text or "")

    @property
    def temperature_text(self) -> str:
        if self.temp_min is None and self.temp_max is None:
            return "—"
        low = "—" if self.temp_min is None else f"{round(self.temp_min)}"
        high = "—" if self.temp_max is None else f"{round(self.temp_max)}"
        return f"{low}~{high}℃"


@dataclass(frozen=True)
class CityForecast:
    """一座城市的逐日预报。

    多城市行程里，天气必须按城市分开呈现——「北京下雨」和「西安下雨」
    对行程安排的含义完全不同。

    **`source` 为 `None` 是「两个来源都没取到」，不是「数据来自某个源」。**
    原先这里在都没取到时也填 `WeatherSource.AMAP`，于是界面上写着「来源 高德」
    配一片空白——`note` 明明说的是「高德也没有返回这个区间的预报」。
    空预报配一个假的来源名，正是「算不出 ≠ 0」那一类错：读的人会以为
    「高德查过了，那几天没数据」，而事实是谁都没查到。
    """

    city_name: str
    source: WeatherSource | None = None
    days: tuple[DailyWeather, ...] = ()
    note: str | None = None

    def for_date(self, target: date) -> DailyWeather | None:
        for day in self.days:
            if day.day == target:
                return day
        return None

    @property
    def covers(self) -> tuple[date, date] | None:
        if not self.days:
            return None
        return self.days[0].day, self.days[-1].day

    @property
    def bad_outdoor_days(self) -> tuple[DailyWeather, ...]:
        return tuple(day for day in self.days if day.is_bad_outdoor)
