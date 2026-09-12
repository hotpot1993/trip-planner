"""M2 接口面的测试：转移、预算栏、多城市天气。

外部数据源全部打桩——真实网络的行为由 scripts/verify_m2_api.py 人工核对。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from lushu.domain.transfer import PriceSource, TransferMode
from lushu.domain.weather import CityForecast, DailyWeather, WeatherSource
from lushu.engine.amap import CityMatch
from lushu.services.transfer_service import TransferPlacementPlan

TODAY = date(2026, 9, 12)
START = date(2026, 10, 1)

NANJING = CityMatch(name="南京", adcode="320100", level="city", lat_gcj02=32.06, lng_gcj02=118.80)
XIAN = CityMatch(name="西安", adcode="610100", level="city", lat_gcj02=34.34, lng_gcj02=108.94)


@pytest.fixture
def stub_cities(monkeypatch: pytest.MonkeyPatch):
    from lushu.api import trip_routes
    from lushu.services import trip_service

    table = {"南京": NANJING, "西安": XIAN}
    monkeypatch.setattr(
        trip_service, "resolve_city", lambda name, **kw: table.get(name)
    )
    monkeypatch.setattr(
        trip_routes, "lookup_city", lambda name, **kw: [table[name]] if name in table else []
    )
    return table


def _create_multi(client) -> str:
    response = client.post(
        "/api/trips",
        json={
            "start_date": START.isoformat(),
            "cities": [{"name": "南京", "days": 2}, {"name": "西安", "days": 3}],
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


# ─── 详情里带上转移与预算 ────────────────────────────────────


def test_detail_includes_empty_transfers_and_budget(api_client, stub_cities) -> None:
    trip_id = _create_multi(api_client)
    body = api_client.get(f"/api/trips/{trip_id}").json()

    assert body["transfers"] == []
    assert body["budget"]["intercity"] == []
    assert body["budget"]["others"] == []
    assert body["budget"]["total"] == 0
    assert body["budget"]["has_reference_prices"] is False


def test_detail_serialises_transfers(api_client, stub_cities) -> None:
    from lushu.services import transfer_service, trip_service

    trip_id = _create_multi(api_client)

    async def fake_refresh(tid, **_kwargs):
        stored = trip_service.get_trip(tid)
        assert stored is not None
        plans = [
            TransferPlacementPlan(
                from_stay_seq=0,
                to_stay_seq=1,
                day_index=1,
                mode=TransferMode.RAIL,
                advice_reason="最快高铁约 4 小时 38 分",
                chosen=_option(),
                alternatives=(_option(service_no="G1940"),),
                note="票价是参考价：12306 的票价接口当前不可用",
            )
        ]
        from lushu.services.trip_store import save_transfers

        save_transfers(tid, plans)
        from lushu.services.trip_store import load_transfers

        return load_transfers(tid)

    original = transfer_service.refresh_transfers
    transfer_service.refresh_transfers = fake_refresh
    try:
        api_client.post(f"/api/trips/{trip_id}/transfers/refresh")
    finally:
        transfer_service.refresh_transfers = original

    body = api_client.get(f"/api/trips/{trip_id}").json()
    assert len(body["transfers"]) == 1

    transfer = body["transfers"][0]
    assert transfer["from_city_name"] == "南京"
    assert transfer["to_city_name"] == "西安"
    assert transfer["day_index"] == 1
    assert transfer["service_no"] == "G94"
    assert transfer["from_station"] == "南京南"
    assert transfer["is_reference_price"] is True
    assert transfer["has_tickets"] is True
    assert len(transfer["alternatives"]) == 1


def _option(service_no: str = "G94"):
    from lushu.adapters.rail import TransferOption

    return TransferOption(
        mode=TransferMode.RAIL,
        service_no=service_no,
        from_station="南京南",
        to_station="西安北",
        dep_time="09:56",
        arr_time="14:34",
        duration_min=278,
        price=535.0,
        price_source=PriceSource.ESTIMATE,
        is_reference_price=True,
        has_tickets=True,
    )


# ─── 预算面板 ────────────────────────────────────────────────


def test_budget_panel_splits_intercity_from_others(api_client, stub_cities) -> None:
    """城际交通独立成栏是设计的明确要求。"""
    trip_id = _create_multi(api_client)
    _insert_budget(trip_id, "intercity", "南京南 → 西安北 G94", 535.0, reference=True)
    _insert_budget(trip_id, "ticket", "南京博物院", 0.0, reference=False)
    _insert_budget(trip_id, "lodging", "南京两晚", 800.0, reference=False)

    budget = api_client.get(f"/api/trips/{trip_id}").json()["budget"]

    assert [item["label"] for item in budget["intercity"]] == ["南京南 → 西安北 G94"]
    assert budget["intercity_total"] == 535.0
    assert len(budget["others"]) == 2
    assert budget["other_total"] == 800.0
    assert budget["total"] == 1335.0
    assert budget["has_reference_prices"] is True


def _insert_budget(trip_id: str, category: str, label: str, amount: float, *, reference: bool) -> None:
    from lushu.store import transaction
    from lushu.store.ids import BUDGET, new_id

    with transaction() as conn:
        conn.execute(
            "INSERT INTO budget_item "
            "(id, trip_id, category, label, amount, currency, is_reference_price, source) "
            "VALUES (?, ?, ?, ?, ?, 'CNY', ?, 'estimate')",
            (new_id(BUDGET), trip_id, category, label, amount, 1 if reference else 0),
        )


def test_budget_without_reference_prices_says_so(api_client, stub_cities) -> None:
    trip_id = _create_multi(api_client)
    _insert_budget(trip_id, "lodging", "住宿", 500.0, reference=False)

    budget = api_client.get(f"/api/trips/{trip_id}").json()["budget"]
    assert budget["has_reference_prices"] is False


# ─── 天气 ────────────────────────────────────────────────────


def test_weather_returns_one_forecast_per_city(api_client, stub_cities, monkeypatch) -> None:
    from lushu.services import weather_service

    async def fake_forecasts(cities, start, end, **_kwargs):
        return [
            CityForecast(
                city_name=city.name,
                source=WeatherSource.OPEN_METEO,
                days=tuple(
                    DailyWeather(
                        day=start + timedelta(days=i),
                        text="小雨" if i == 1 else "晴",
                        temp_min=18.0,
                        temp_max=27.0,
                        precipitation_probability=70.0 if i == 1 else 5.0,
                    )
                    for i in range((end - start).days + 1)
                ),
                note=f"{city.name} 的说明",
            )
            for city in cities
        ]

    monkeypatch.setattr(weather_service, "forecast_for_cities", fake_forecasts)

    trip_id = _create_multi(api_client)
    body = api_client.get(f"/api/trips/{trip_id}/weather").json()

    assert [c["city_name"] for c in body["cities"]] == ["南京", "西安"]
    assert body["start_date"] == START.isoformat()
    assert all(len(c["days"]) == 5 for c in body["cities"])
    assert body["cities"][0]["source"] == "open-meteo"
    assert body["cities"][0]["source_label"] == "Open-Meteo"


def test_weather_marks_bad_outdoor_days(api_client, stub_cities, monkeypatch) -> None:
    from lushu.services import weather_service

    async def fake_forecasts(cities, start, end, **_kwargs):
        return [
            CityForecast(
                city_name=cities[0].name,
                source=WeatherSource.OPEN_METEO,
                days=(DailyWeather(day=start, text="雷阵雨", night_text="小雨"),),
            )
        ]

    monkeypatch.setattr(weather_service, "forecast_for_cities", fake_forecasts)

    trip_id = _create_multi(api_client)
    body = api_client.get(f"/api/trips/{trip_id}/weather").json()

    day = body["cities"][0]["days"][0]
    assert day["is_bad_outdoor"] is True
    assert day["temperature_text"] == "—"


def test_weather_for_missing_trip_returns_404(api_client, stub_cities) -> None:
    assert api_client.get("/api/trips/trip_不存在/weather").status_code == 404


def test_weather_for_single_city_trip(api_client, stub_cities, monkeypatch) -> None:
    from lushu.services import weather_service

    seen: list[str] = []

    async def fake_forecasts(cities, start, end, **_kwargs):
        seen.extend(city.name for city in cities)
        return []

    monkeypatch.setattr(weather_service, "forecast_for_cities", fake_forecasts)

    trip_id = api_client.post(
        "/api/trips",
        json={"start_date": START.isoformat(), "cities": [{"name": "南京", "days": 2}]},
    ).json()["id"]
    api_client.get(f"/api/trips/{trip_id}/weather")

    assert seen == ["南京"]


# ─── 刷新转移 ────────────────────────────────────────────────


def test_refresh_on_missing_trip_returns_404(api_client, stub_cities) -> None:
    assert api_client.post("/api/trips/trip_不存在/transfers/refresh").status_code == 404


def test_refresh_on_single_city_trip_returns_nothing(api_client, stub_cities, monkeypatch) -> None:
    from lushu.services import transfer_service

    async def fake_refresh(tid, **_kwargs):
        return []

    monkeypatch.setattr(transfer_service, "refresh_transfers", fake_refresh)

    trip_id = api_client.post(
        "/api/trips",
        json={"start_date": START.isoformat(), "cities": [{"name": "南京", "days": 2}]},
    ).json()["id"]

    response = api_client.post(f"/api/trips/{trip_id}/transfers/refresh")
    assert response.status_code == 200
    assert response.json() == []
