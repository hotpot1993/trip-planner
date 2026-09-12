"""多城市天气服务的测试：主用 Open-Meteo，兜底高德。

组合逻辑是本模块自己的职责，所以这里两个数据源都替换掉，只验证编排：
什么时候用主源、什么时候退到兜底、两者都没有时给出什么。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from lushu.adapters import weather as open_meteo
from lushu.domain.weather import CityForecast, DailyWeather, WeatherSource
from lushu.services import weather_service
from lushu.services.trip_store import CityRef

TODAY = date(2026, 9, 12)
START = date(2026, 10, 1)
END = date(2026, 10, 3)

NANJING = CityRef(adcode="320100", name="南京", lat_gcj02=32.060255, lng_gcj02=118.796877)
NO_COORDS = CityRef(adcode="610100", name="西安")


def _open_meteo_days(count: int = 3) -> list[DailyWeather]:
    return [
        DailyWeather(day=START + timedelta(days=i), text="晴", temp_min=18.0, temp_max=27.0)
        for i in range(count)
    ]


@pytest.fixture
def stub_sources(monkeypatch: pytest.MonkeyPatch):
    """替换两个数据源，并记录各自被调了几次。"""
    calls = {"open_meteo": 0, "amap": 0}
    state: dict = {"open_meteo": _open_meteo_days(), "open_meteo_note": None, "amap": []}

    def fake_open_meteo(**_kwargs):
        calls["open_meteo"] += 1
        if isinstance(state["open_meteo"], Exception):
            raise state["open_meteo"]
        return state["open_meteo"], state["open_meteo_note"]

    def fake_amap(_adcode, **_kwargs):
        calls["amap"] += 1
        return state["amap"], None if state["amap"] else "高德没有数据"

    monkeypatch.setattr(weather_service.open_meteo, "fetch_forecast", fake_open_meteo)
    monkeypatch.setattr(weather_service, "fetch_amap_forecast", fake_amap)
    return calls, state


@pytest.mark.asyncio
async def test_open_meteo_is_used_when_it_works(stub_sources) -> None:
    calls, _state = stub_sources
    forecast = await weather_service.forecast_for_city(NANJING, START, END, today=TODAY)

    assert forecast.source is WeatherSource.OPEN_METEO
    assert len(forecast.days) == 3
    assert calls["amap"] == 0, "主源可用时不该去问兜底"


@pytest.mark.asyncio
async def test_falls_back_to_amap_when_open_meteo_fails(stub_sources) -> None:
    calls, state = stub_sources
    state["open_meteo"] = open_meteo.WeatherUnavailableError("连接超时")
    state["amap"] = _open_meteo_days(3)

    forecast = await weather_service.forecast_for_city(NANJING, START, END, today=TODAY)

    assert forecast.source is WeatherSource.AMAP
    assert len(forecast.days) == 3
    assert calls["amap"] == 1
    assert forecast.note is not None and "高德只提供约 4 天预报" in forecast.note


@pytest.mark.asyncio
async def test_falls_back_when_open_meteo_returns_nothing(stub_sources) -> None:
    _calls, state = stub_sources
    state["open_meteo"] = []
    state["open_meteo_note"] = "超出预报范围"
    state["amap"] = _open_meteo_days(2)

    forecast = await weather_service.forecast_for_city(NANJING, START, END, today=TODAY)

    assert forecast.source is WeatherSource.AMAP
    assert forecast.note is not None and "超出预报范围" in forecast.note


@pytest.mark.asyncio
async def test_city_without_coordinates_goes_straight_to_amap(stub_sources) -> None:
    """没有坐标就用不了 Open-Meteo，不该白问一次。"""
    calls, state = stub_sources
    state["amap"] = _open_meteo_days(2)

    forecast = await weather_service.forecast_for_city(NO_COORDS, START, END, today=TODAY)

    assert calls["open_meteo"] == 0
    assert forecast.source is WeatherSource.AMAP
    assert forecast.note is not None and "还没有坐标" in forecast.note


@pytest.mark.asyncio
async def test_amap_days_are_clipped_to_the_requested_range(stub_sources) -> None:
    """高德只给 4 天，多出来的日子不该混进行程区间。"""
    _calls, state = stub_sources
    state["open_meteo"] = []
    state["amap"] = [
        DailyWeather(day=START - timedelta(days=2), text="晴"),
        DailyWeather(day=START, text="晴"),
        DailyWeather(day=END + timedelta(days=2), text="晴"),
    ]

    forecast = await weather_service.forecast_for_city(NANJING, START, END, today=TODAY)

    assert [d.day for d in forecast.days] == [START]


@pytest.mark.asyncio
async def test_both_sources_empty_yields_an_empty_forecast_with_a_note(stub_sources) -> None:
    """天气查不到不该让行程页打不开——给一个带说明的空预报。"""
    _calls, state = stub_sources
    state["open_meteo"] = []
    state["amap"] = []

    forecast = await weather_service.forecast_for_city(NANJING, START, END, today=TODAY)

    assert forecast.days == ()
    assert forecast.note


@pytest.mark.asyncio
async def test_bad_request_params_fall_back_instead_of_raising(stub_sources) -> None:
    _calls, state = stub_sources
    state["open_meteo"] = ValueError("结束日期不能早于开始日期")
    state["amap"] = _open_meteo_days(1)

    forecast = await weather_service.forecast_for_city(NANJING, START, END, today=TODAY)
    assert forecast.source is WeatherSource.AMAP


# ─── 多城市 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_each_city_gets_its_own_forecast(stub_sources) -> None:
    """多城市行程里天气必须按城市分开——「北京下雨」和「西安下雨」含义不同。"""
    _calls, state = stub_sources
    state["amap"] = []

    xian = CityRef(adcode="610100", name="西安", lat_gcj02=34.34, lng_gcj02=108.94)
    forecasts = await weather_service.forecast_for_cities([NANJING, xian], START, END, today=TODAY)

    assert [f.city_name for f in forecasts] == ["南京", "西安"]
    assert all(isinstance(f, CityForecast) for f in forecasts)
    assert all(len(f.days) == 3 for f in forecasts)


@pytest.mark.asyncio
async def test_city_order_follows_the_input(stub_sources) -> None:
    _calls, state = stub_sources
    state["amap"] = []

    xian = CityRef(adcode="610100", name="西安", lat_gcj02=34.34, lng_gcj02=108.94)
    forecasts = await weather_service.forecast_for_cities([xian, NANJING], START, END, today=TODAY)

    assert [f.city_name for f in forecasts] == ["西安", "南京"]


@pytest.mark.asyncio
async def test_no_cities_yields_no_forecasts(stub_sources) -> None:
    assert await weather_service.forecast_for_cities([], START, END, today=TODAY) == []


# ─── 领域查询 ────────────────────────────────────────────────


def test_forecast_lookup_by_date() -> None:
    forecast = CityForecast(
        city_name="南京",
        source=WeatherSource.OPEN_METEO,
        days=(DailyWeather(day=START, text="晴"), DailyWeather(day=END, text="小雨")),
    )

    assert forecast.for_date(START) is not None
    assert forecast.for_date(START).text == "晴"
    assert forecast.for_date(END).text == "小雨"
    assert forecast.for_date(START + timedelta(days=1)) is None


def test_bad_outdoor_days_are_listed() -> None:
    forecast = CityForecast(
        city_name="南京",
        source=WeatherSource.OPEN_METEO,
        days=(
            DailyWeather(day=START, text="晴"),
            DailyWeather(day=START + timedelta(days=1), text="小雨"),
            DailyWeather(day=START + timedelta(days=2), text="雷阵雨"),
        ),
    )

    assert [d.day for d in forecast.bad_outdoor_days] == [
        START + timedelta(days=1),
        START + timedelta(days=2),
    ]


def test_empty_forecast_has_no_range() -> None:
    forecast = CityForecast(city_name="南京", source=WeatherSource.AMAP)
    assert forecast.covers is None
    assert forecast.for_date(START) is None
