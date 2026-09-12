"""城际转移编排的测试。

铁路查询被打桩，验证的是编排本身：落点怎么定、建议怎么给、预售期之外
怎么处理。真实的 12306 行为由 scripts/verify_rail_adapter.py 人工核对。
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from lushu.adapters.rail import RailQuery, Station, TransferOption  # noqa: F401
from lushu.domain.planned import PlannedDay, PlannedStay, PlannedTrip, StaySpec, lay_out
from lushu.domain.transfer import PriceSource, TransferMode
from lushu.services import transfer_service
from lushu.services.trip_store import CityRef

TODAY = date(2026, 9, 12)
START = date(2026, 10, 1)

NANJING = CityRef(adcode="320100", name="南京", lat_gcj02=32.060255, lng_gcj02=118.796877)
XIAN = CityRef(adcode="610100", name="西安", lat_gcj02=34.341574, lng_gcj02=108.939770)


def _option(service_no: str = "G94", minutes: int = 200, price: float | None = 535.0):
    """默认造一个「短途高铁」：200 分钟，低于 240 分钟的长车程阈值。

    240 / 360 两个阈值决定了落点与交通方式，所以造数据时必须心里有数：
    200 → 短途高铁；300 → 长途高铁（低于 6 小时仍推铁路）；400 → 推航空。
    """
    return TransferOption(
        mode=TransferMode.RAIL,
        service_no=service_no,
        from_station="南京南",
        to_station="西安北",
        dep_time="09:56",
        arr_time="14:34",
        duration_min=minutes,
        price=price,
        price_source=PriceSource.ESTIMATE,
        is_reference_price=True,
        has_tickets=True,
        seats={"二等座": "有"},
        note="票价是参考价：12306 的票价接口当前不可用",
    )


def _two_city_trip(nanjing_days: int = 3, xian_days: int = 2) -> PlannedTrip:
    return PlannedTrip(
        name="南京、西安",
        start_date=START,
        stays=lay_out(
            START,
            [StaySpec("南京", nanjing_days, "320100"), StaySpec("西安", xian_days, "610100")],
        ),
    )


@pytest.fixture
def stub_rail(monkeypatch: pytest.MonkeyPatch):
    """替换铁路查询，并记录每次查询用的日期。"""
    calls: list[date] = []
    state: dict = {
        "options": [_option()],
        "within_sale_window": True,
        "note": None,
    }

    def fake_query_rail(from_city, to_city, travel_date, **_kwargs):
        calls.append(travel_date)
        return RailQuery(
            travel_date=travel_date,
            within_sale_window=state["within_sale_window"],
            from_station=Station(name="南京", telecode="NJH"),
            to_station=Station(name="西安", telecode="XAY"),
            options=tuple(state["options"]),
            note=state["note"],
        )

    monkeypatch.setattr(transfer_service.rail, "query_rail", fake_query_rail)
    return calls, state


# ─── 单城市 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_city_has_no_transfers(stub_rail) -> None:
    trip = PlannedTrip(
        name="南京", start_date=START, stays=lay_out(START, [StaySpec("南京", 3, "320100")])
    )
    assert await transfer_service.build_transfers(trip, {"320100": NANJING}) == []


@pytest.mark.asyncio
async def test_empty_trip_has_no_transfers(stub_rail) -> None:
    assert await transfer_service.build_transfers(
        PlannedTrip(name="x", start_date=START, stays=(PlannedStay(
            city_name="南京", city_adcode="320100", seq=0,
            days=(PlannedDay(day=START, seq_in_stay=0),),
        ),)), {}
    ) == []


# ─── 落点 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_short_rail_lands_on_the_last_day_of_the_departure_city(stub_rail) -> None:
    """短途高铁（不到 4 小时），默认落在出发城市最后一天。"""
    _calls, state = stub_rail
    state["options"] = [_option(minutes=200)]

    transfers = await transfer_service.build_transfers(
        _two_city_trip(), {"320100": NANJING, "610100": XIAN}, today=TODAY
    )

    assert len(transfers) == 1
    transfer = transfers[0]
    assert transfer.day_index == 2  # 南京 3 天里的第 3 天
    assert transfer.mode is TransferMode.RAIL
    assert transfer.chosen is not None and transfer.chosen.service_no == "G94"


@pytest.mark.asyncio
async def test_long_rail_moves_to_the_first_day_of_the_arrival_city(stub_rail) -> None:
    """超过 4 小时但不到 6 小时：仍推铁路，但落点改到到达城市第一天。"""
    _calls, state = stub_rail
    state["options"] = [_option(minutes=300)]

    transfers = await transfer_service.build_transfers(
        _two_city_trip(), {"320100": NANJING, "610100": XIAN}, today=TODAY
    )

    assert transfers[0].day_index == 3  # 西安的第一天
    assert transfers[0].mode is TransferMode.RAIL


@pytest.mark.asyncio
async def test_very_long_rail_advises_air(stub_rail) -> None:
    """超过 6 小时就建议航空了，rail 不再是首选。"""
    _calls, state = stub_rail
    state["options"] = [_option(minutes=400)]

    transfers = await transfer_service.build_transfers(
        _two_city_trip(), {"320100": NANJING, "610100": XIAN}, today=TODAY
    )

    transfer = transfers[0]
    assert transfer.mode is TransferMode.AIR
    assert transfer.chosen is None, "建议航空时不该把高铁车次当成选中的方案"


@pytest.mark.asyncio
async def test_long_transfer_is_queried_against_the_arrival_date(stub_rail) -> None:
    """落点定了之后要用那天的时刻表，不能还拿着前一天的。"""
    calls, state = stub_rail
    state["options"] = [_option(minutes=300)]

    await transfer_service.build_transfers(
        _two_city_trip(), {"320100": NANJING, "610100": XIAN}, today=TODAY
    )

    assert len(calls) == 2
    assert calls[0] == START + timedelta(days=2)  # 先按默认落点问
    assert calls[1] == START + timedelta(days=3)  # 再按最终落点重问


@pytest.mark.asyncio
async def test_short_transfer_is_queried_only_once(stub_rail) -> None:
    """落点没变就不该多问一次。"""
    calls, state = stub_rail
    state["options"] = [_option(minutes=200)]

    await transfer_service.build_transfers(
        _two_city_trip(), {"320100": NANJING, "610100": XIAN}, today=TODAY
    )

    assert len(calls) == 1


# ─── 预售期之外 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_outside_the_sale_window_still_advises_rail_by_distance(stub_rail) -> None:
    """预售期之外查不到车次，但**不等于没有铁路**。

    把它当成「没有铁路」会因为一个与铁路无关的原因建议用户去坐飞机——
    那是个会让人做出错误决定的建议。
    """
    _calls, state = stub_rail
    state["within_sale_window"] = False
    state["options"] = []
    state["note"] = "12306 的预售期只有 14 天"

    transfers = await transfer_service.build_transfers(
        _two_city_trip(), {"320100": NANJING, "610100": XIAN}, today=TODAY
    )

    transfer = transfers[0]
    assert transfer.mode is TransferMode.RAIL, "南京到西安约 1200 公里，不该建议飞机"
    assert "按距离判断" in transfer.advice_reason
    assert "还没到 12306 的放票期" in transfer.advice_reason
    assert transfer.chosen is None, "没有真实车次就不该编一个出来"
    assert transfer.note is not None and "预售期" in transfer.note


@pytest.mark.asyncio
async def test_no_rail_line_at_all_advises_air(stub_rail) -> None:
    """确实没有铁路线路时才建议飞机。"""
    _calls, state = stub_rail
    state["options"] = []
    state["note"] = "没有查到直达车次"

    transfers = await transfer_service.build_transfers(
        _two_city_trip(), {"320100": NANJING, "610100": XIAN}, today=TODAY
    )

    assert transfers[0].mode is TransferMode.AIR
    assert "没有查到可用的铁路车次" in transfers[0].advice_reason


# ─── 距离与票价 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_distance_is_used_for_the_price_estimate(stub_rail) -> None:
    """票价靠里程估，所以查询时必须把里程带上。"""
    seen: dict = {}

    async def fake_build(**kwargs):
        raise AssertionError("不该走到这里")

    calls, state = stub_rail
    state["options"] = [_option(price=535.0)]

    original = transfer_service.rail.query_rail

    def spy(from_city, to_city, travel_date, **kwargs):
        seen["distance_km"] = kwargs.get("distance_km")
        return original(from_city, to_city, travel_date, **kwargs)

    transfer_service.rail.query_rail = spy
    try:
        await transfer_service.build_transfers(
            _two_city_trip(), {"320100": NANJING, "610100": XIAN}, today=TODAY
        )
    finally:
        transfer_service.rail.query_rail = original

    assert seen["distance_km"] is not None
    assert 1100 < seen["distance_km"] < 1300


@pytest.mark.asyncio
async def test_missing_city_coordinates_means_no_distance(stub_rail) -> None:
    """缺坐标就不估里程——不编一个距离出来。"""
    calls, state = stub_rail
    state["options"] = [_option(price=None)]

    transfers = await transfer_service.build_transfers(
        _two_city_trip(),
        {"320100": CityRef(adcode="320100", name="南京"), "610100": XIAN},
        today=TODAY,
    )

    assert transfers[0].chosen is not None
    assert transfers[0].chosen.price is None


def test_distance_is_none_when_either_side_is_missing() -> None:
    assert transfer_service._distance(None, XIAN) is None
    assert transfer_service._distance(NANJING, None) is None
    assert transfer_service._distance(CityRef(adcode="1", name="甲"), XIAN) is None
    assert transfer_service._distance(NANJING, XIAN) is not None


# ─── 备选方案 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_alternatives_are_kept_but_capped(stub_rail) -> None:
    _calls, state = stub_rail
    state["options"] = [_option(f"G{i}", minutes=200 + i) for i in range(8)]

    transfers = await transfer_service.build_transfers(
        _two_city_trip(), {"320100": NANJING, "610100": XIAN}, today=TODAY
    )

    transfer = transfers[0]
    assert transfer.chosen is not None and transfer.chosen.service_no == "G0"
    assert len(transfer.alternatives) == transfer_service.MAX_ALTERNATIVES
    assert [a.service_no for a in transfer.alternatives] == ["G1", "G2", "G3"]


@pytest.mark.asyncio
async def test_no_alternatives_without_a_chosen_option(stub_rail) -> None:
    _calls, state = stub_rail
    state["within_sale_window"] = False
    state["options"] = []

    transfers = await transfer_service.build_transfers(
        _two_city_trip(), {"320100": NANJING, "610100": XIAN}, today=TODAY
    )

    assert transfers[0].chosen is None
    assert transfers[0].alternatives == ()


# ─── 多段 ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_three_cities_produce_two_transfers(stub_rail) -> None:
    _calls, state = stub_rail
    state["options"] = [_option()]

    trip = PlannedTrip(
        name="南京、西安、成都",
        start_date=START,
        stays=lay_out(START, [
            StaySpec("南京", 2, "320100"),
            StaySpec("西安", 2, "610100"),
            StaySpec("成都", 2, "510100"),
        ]),
    )
    transfers = await transfer_service.build_transfers(
        trip,
        {
            "320100": NANJING,
            "610100": XIAN,
            "510100": CityRef(adcode="510100", name="成都", lat_gcj02=30.57, lng_gcj02=104.06),
        },
        today=TODAY,
    )

    assert [(t.from_stay_seq, t.to_stay_seq) for t in transfers] == [(0, 1), (1, 2)]
    # 第二段的默认落点是西安的最后一天，即全行程第 4 天
    assert transfers[1].day_index == 3
