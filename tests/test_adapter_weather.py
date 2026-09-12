"""Open-Meteo 天气适配器的测试。用 MockTransport 打桩，不发真实请求。"""

from __future__ import annotations

from datetime import date, timedelta

import httpx
import pytest

from lushu.adapters import weather
from lushu.domain.weather import DailyWeather, WeatherSource, is_bad_outdoor_weather

TODAY = date(2026, 9, 12)
NANJING = (32.060255, 118.796877)


def _payload(*, days: list[str], codes: list[int], highs=None, lows=None, rains=None) -> dict:
    return {
        "daily": {
            "time": days,
            "weather_code": codes,
            "temperature_2m_max": highs if highs is not None else [28.0] * len(days),
            "temperature_2m_min": lows if lows is not None else [19.0] * len(days),
            "precipitation_probability_max": rains if rains is not None else [0] * len(days),
        }
    }


def _client(payload: dict, *, status: int = 200) -> httpx.Client:
    def handler(_request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, text="服务不可用")
        return httpx.Response(200, json=payload)

    return httpx.Client(transport=httpx.MockTransport(handler))


# ─── WMO 编码 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("code", "expected"),
    [(0, "晴"), (2, "多云"), (3, "阴"), (61, "小雨"), (75, "大雪"), (95, "雷阵雨")],
)
def test_wmo_codes_translate(code: int, expected: str) -> None:
    assert weather.describe_wmo(code) == expected


def test_unknown_wmo_code_says_so_instead_of_inventing_weather() -> None:
    text = weather.describe_wmo(1234)
    assert "未知" in text
    assert "1234" in text


@pytest.mark.parametrize("value", [None, "3", True])
def test_unusable_wmo_code_is_reported_as_unknown(value) -> None:
    assert "未知" in weather.describe_wmo(value)


def test_bad_outdoor_keywords_cover_rain_snow_and_haze() -> None:
    assert is_bad_outdoor_weather("小雨")
    assert is_bad_outdoor_weather("雷阵雨")
    assert is_bad_outdoor_weather("霾")
    assert not is_bad_outdoor_weather("晴")
    assert not is_bad_outdoor_weather("多云")


# ─── 解析 ────────────────────────────────────────────────────


def test_forecast_is_parsed_into_domain_days() -> None:
    days = ["2026-09-13", "2026-09-14"]
    payload = _payload(days=days, codes=[0, 61], highs=[30.0, 25.0], lows=[20.0, 18.0], rains=[0, 80])

    with _client(payload) as client:
        result, note = weather.fetch_forecast(
            latitude=NANJING[0], longitude=NANJING[1],
            start_date=TODAY, end_date=TODAY + timedelta(days=5),
            today=TODAY, client=client,
        )

    assert note is None
    assert [d.day for d in result] == [date(2026, 9, 13), date(2026, 9, 14)]
    assert result[0].text == "晴"
    assert result[1].text == "小雨"
    assert result[1].temp_max == 25.0
    assert result[1].precipitation_probability == 80


def test_temperature_text_is_readable() -> None:
    day = DailyWeather(day=TODAY, text="晴", temp_min=19.4, temp_max=28.6)
    assert day.temperature_text == "19~29℃"


def test_missing_temperatures_show_a_dash() -> None:
    assert DailyWeather(day=TODAY, text="晴").temperature_text == "—"


def test_bad_outdoor_flag_covers_the_night_too() -> None:
    day = DailyWeather(day=TODAY, text="晴", night_text="小雨")
    assert day.is_bad_outdoor is True


def test_the_request_is_clamped_to_the_forecast_horizon() -> None:
    """超出 16 天要截断并说明，不能让调用方以为那几天是「无天气」。"""
    payload = _payload(days=["2026-09-13"], codes=[0])
    with _client(payload) as client:
        _days, note = weather.fetch_forecast(
            latitude=NANJING[0], longitude=NANJING[1],
            start_date=TODAY + timedelta(days=20),
            end_date=TODAY + timedelta(days=30),
            today=TODAY, client=client,
        )

    assert note is not None
    assert "16 天" in note


def test_start_beyond_the_horizon_returns_a_clear_note() -> None:
    with _client(_payload(days=[], codes=[])) as client:
        days, note = weather.fetch_forecast(
            latitude=NANJING[0], longitude=NANJING[1],
            start_date=TODAY + timedelta(days=40),
            end_date=TODAY + timedelta(days=45),
            today=TODAY, client=client,
        )

    assert days == []
    assert note is not None


def test_empty_daily_block_reports_it() -> None:
    with _client({"daily": {}}) as client:
        days, note = weather.fetch_forecast(
            latitude=NANJING[0], longitude=NANJING[1],
            start_date=TODAY, end_date=TODAY + timedelta(days=3),
            today=TODAY, client=client,
        )

    assert days == []
    assert note is not None


def test_http_error_raises_a_typed_error() -> None:
    with _client({}, status=503) as client:
        with pytest.raises(weather.WeatherUnavailableError, match="503"):
            weather.fetch_forecast(
                latitude=NANJING[0], longitude=NANJING[1],
                start_date=TODAY, end_date=TODAY + timedelta(days=1),
                today=TODAY, client=client,
            )


def test_reversed_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="不能早于"):
        weather.fetch_forecast(
            latitude=NANJING[0], longitude=NANJING[1],
            start_date=TODAY, end_date=TODAY - timedelta(days=1), today=TODAY,
        )


def test_malformed_dates_are_skipped() -> None:
    payload = _payload(days=["不是日期", "2026-09-14"], codes=[0, 1])
    with _client(payload) as client:
        days, _note = weather.fetch_forecast(
            latitude=NANJING[0], longitude=NANJING[1],
            start_date=TODAY, end_date=TODAY + timedelta(days=3),
            today=TODAY, client=client,
        )

    assert [d.day for d in days] == [date(2026, 9, 14)]


def test_shorter_arrays_do_not_crash() -> None:
    """接口少给几列时不能整段崩掉，缺的就是缺的。"""
    payload = {"daily": {"time": ["2026-09-13", "2026-09-14"], "weather_code": [0]}}
    with _client(payload) as client:
        days, _note = weather.fetch_forecast(
            latitude=NANJING[0], longitude=NANJING[1],
            start_date=TODAY, end_date=TODAY + timedelta(days=3),
            today=TODAY, client=client,
        )

    assert len(days) == 2
    assert days[0].text == "晴"
    assert "未知" in days[1].text
    assert days[1].temp_max is None


def test_source_is_open_meteo() -> None:
    assert weather.source() is WeatherSource.OPEN_METEO
