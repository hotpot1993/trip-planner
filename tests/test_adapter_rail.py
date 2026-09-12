"""铁路适配器的测试。

用 httpx.MockTransport 打桩，不发真实请求——真实网络的行为由
`scripts/verify_rail_adapter.py` 人工核对。

这里钉死的是三件实测得来的事实，它们都容易在重构中被改掉：
  预售期只有 14 天
  余票响应里没有票价，只有余票数量
  站代码只用来定位城市，同城不同站返回同样的车次集合
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import httpx
import pytest

from lushu.adapters import rail
from lushu.domain.transfer import PriceSource, TrainClass, TransferMode

TODAY = date(2026, 9, 12)

# 用真实的 station_name.js 片段形状
STATION_TABLE = (
    "var station_names ='@bjb|北京北|VAP|beijingbei|bjb|0"
    "@njh|南京|NJH|nanjing|njh|1"
    "@nkh|南京南|NKH|nanjingnan|nkh|2"
    "@xay|西安|XAY|xian|xay|3"
    "@eay|西安北|EAY|xianbei|eay|4'"
)


def _ticket_row(
    *,
    service_no: str = "G1970",
    dep: str = "07:38",
    arr: str = "13:20",
    duration: str = "05:42",
    second: str = "有",
    first: str = "",
    business: str = "",
    standing: str = "16",
) -> str:
    """构造一行 33 字段以上的余票记录，下标与真实响应一致。"""
    parts = [""] * 58
    parts[2] = "5l000G1970A3"
    parts[3] = service_no
    parts[6] = "NKH"
    parts[7] = "EAY"
    parts[8] = dep
    parts[9] = arr
    parts[10] = duration
    parts[11] = "Y"
    parts[13] = "20260919"
    parts[26] = standing
    parts[30] = second
    parts[31] = first
    parts[32] = business
    return "|".join(parts)


def _make_client(
    *,
    rows: list[str] | None = None,
    station_text: str = STATION_TABLE,
    ticket_html: bool = False,
    price_busy: bool = False,
) -> httpx.Client:
    """构造一个假的 12306 客户端。"""
    rows = rows if rows is not None else [_ticket_row()]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("station_name.js"):
            return httpx.Response(200, text=station_text)
        if path.endswith("/leftTicket/init"):
            return httpx.Response(200, text="<html>init</html>")
        if path.endswith("/leftTicket/queryG"):
            if ticket_html:
                return httpx.Response(
                    200,
                    text="<!DOCTYPE html><html>铁路客户服务中心</html>",
                    headers={"content-type": "text/html"},
                )
            return httpx.Response(
                200,
                json={
                    "httpstatus": 200,
                    "status": True,
                    "data": {
                        "map": {"NKH": "南京南", "EAY": "西安北", "NJH": "南京", "XAY": "西安"},
                        "result": rows,
                    },
                },
            )
        if path.endswith("queryAllPublicPrice"):
            return httpx.Response(
                200,
                json={"httpstatus": 200, "status": False, "messages": ["系统忙，请稍后重试"]},
            )
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler), headers=rail._HEADERS)


@pytest.fixture
def stations() -> list[rail.Station]:
    return rail.parse_station_table(STATION_TABLE)


# ─── 车站名表 ────────────────────────────────────────────────


def test_parse_station_table_reads_name_and_telecode(stations) -> None:
    assert rail.Station(name="南京南", telecode="NKH", pinyin="nanjingnan") in stations
    assert len(stations) == 5


def test_parse_station_table_tolerates_garbage() -> None:
    assert rail.parse_station_table("不是车站表") == []
    assert rail.parse_station_table("@只有两段|名字") == []


def test_station_candidates_prefers_the_bare_city_name(stations) -> None:
    names = [s.name for s in rail.station_candidates("南京", stations)]
    assert names == ["南京", "南京南"]


def test_station_candidates_strips_the_shi_suffix(stations) -> None:
    assert [s.name for s in rail.station_candidates("南京市", stations)] == ["南京", "南京南"]


def test_station_candidates_does_not_match_other_cities(stations) -> None:
    assert [s.name for s in rail.station_candidates("西安", stations)] == ["西安", "西安北"]


def test_unknown_city_has_no_candidates(stations) -> None:
    assert rail.station_candidates("不存在的地方", stations) == []


def test_load_stations_uses_the_cache(tmp_path) -> None:
    cache = tmp_path / "stations.json"
    with _make_client() as client:
        first = rail.load_stations(client=client, cache_path=cache, today=TODAY)
    assert cache.is_file()
    assert len(first) == 5

    # 第二次不给客户端也应成功——它走缓存，不该发请求
    second = rail.load_stations(cache_path=cache, today=TODAY)
    assert [s.telecode for s in second] == [s.telecode for s in first]


def test_stale_cache_is_refreshed(tmp_path) -> None:
    cache = tmp_path / "stations.json"
    cache.write_text(
        json.dumps({"fetched_at": "2020-01-01", "stations": [{"name": "旧", "telecode": "OLD"}]}),
        encoding="utf-8",
    )
    with _make_client() as client:
        stations = rail.load_stations(client=client, cache_path=cache, today=TODAY)
    assert len(stations) == 5


# ─── 预售期 ──────────────────────────────────────────────────


@pytest.mark.parametrize("offset", [0, 1, 14])
def test_within_the_sale_window(offset: int) -> None:
    assert rail.within_sale_window(TODAY + timedelta(days=offset), today=TODAY)


@pytest.mark.parametrize("offset", [15, 20, 60])
def test_beyond_the_sale_window(offset: int) -> None:
    assert not rail.within_sale_window(TODAY + timedelta(days=offset), today=TODAY)


def test_past_dates_are_outside_the_window() -> None:
    assert not rail.within_sale_window(TODAY - timedelta(days=1), today=TODAY)


def test_sale_window_end_is_fourteen_days_out() -> None:
    assert rail.sale_window_end(TODAY) == TODAY + timedelta(days=14)


def test_beyond_the_window_returns_a_readable_note(stations) -> None:
    with _make_client() as client:
        result = rail.query_rail(
            "南京", "西安", TODAY + timedelta(days=20), today=TODAY, stations=stations, client=client
        )

    assert result.within_sale_window is False
    assert result.options == ()
    assert "预售期" in (result.note or "")
    assert "还没到放票期" in (result.note or "")


# ─── 余票解析 ────────────────────────────────────────────────


def test_parse_ticket_rows_reads_the_verified_columns(stations) -> None:
    options = rail.parse_ticket_rows([_ticket_row()], {"NKH": "南京南", "EAY": "西安北"})

    assert len(options) == 1
    option = options[0]
    assert option.service_no == "G1970"
    assert option.from_station == "南京南"
    assert option.to_station == "西安北"
    assert option.dep_time == "07:38"
    assert option.arr_time == "13:20"
    assert option.duration_min == 5 * 60 + 42
    assert option.mode is TransferMode.RAIL


def test_seats_are_labelled_only_for_verified_columns(stations) -> None:
    """26/30/31/32 是实测确认过的席别；23/28/29 席别不明，不对外声称。"""
    options = rail.parse_ticket_rows(
        [_ticket_row(standing="16", second="有", first="7", business="2")],
        {},
    )
    seats = options[0].seats

    assert seats == {"无座": "16", "二等座": "有", "一等座": "7", "商务座": "2"}
    assert "硬卧" not in seats
    assert "硬座" not in seats


def test_unverified_columns_still_count_as_tickets() -> None:
    """席别名不确定的字段仍然能说明「有票」，只是不报席别。"""
    row = _ticket_row(standing="", second="", first="", business="")
    parts = row.split("|")
    parts[28] = "12"  # 只在普速车上出现的那类字段
    options = rail.parse_ticket_rows(["|".join(parts)], {})

    assert options[0].has_tickets is True
    assert options[0].seats == {}


def test_sold_out_train_has_no_tickets(stations) -> None:
    row = _ticket_row(standing="无", second="无", first="无", business="无")
    options = rail.parse_ticket_rows([row], {})
    assert options[0].has_tickets is False


def test_train_class_follows_the_service_prefix() -> None:
    assert rail.parse_ticket_rows([_ticket_row(service_no="G94")], {})[0].train_class is TrainClass.HIGH_SPEED
    assert rail.parse_ticket_rows([_ticket_row(service_no="D112")], {})[0].train_class is TrainClass.BULLET
    assert rail.parse_ticket_rows([_ticket_row(service_no="K2186")], {})[0].train_class is TrainClass.CONVENTIONAL


# ─── 余票响应里没有票价 ──────────────────────────────────────


def test_price_is_estimated_and_marked_as_reference(stations) -> None:
    """12306 的余票响应没有票价，票价接口也不可用。估价必须如实标注。"""
    options = rail.parse_ticket_rows([_ticket_row()], {}, distance_km=1189.0)
    option = options[0]

    assert option.price is not None
    assert option.price_source is PriceSource.ESTIMATE
    assert option.is_reference_price is True
    assert "参考价" in (option.note or "")


def test_no_distance_means_no_price(stations) -> None:
    """没有里程就不编价格——宁可不显示，也不给一个没有依据的数。"""
    option = rail.parse_ticket_rows([_ticket_row()], {}, distance_km=None)[0]
    assert option.price is None


def test_estimates_scale_with_train_class(stations) -> None:
    high_speed = rail.parse_ticket_rows([_ticket_row(service_no="G94")], {}, distance_km=1000)[0]
    conventional = rail.parse_ticket_rows([_ticket_row(service_no="K2186")], {}, distance_km=1000)[0]

    assert high_speed.price is not None and conventional.price is not None
    assert high_speed.price > conventional.price


def test_rows_are_sorted_by_duration(stations) -> None:
    slow = _ticket_row(service_no="K1", duration="18:00")
    fast = _ticket_row(service_no="G1", duration="04:00")
    options = rail.parse_ticket_rows([slow, fast], {})

    assert [o.service_no for o in options] == ["G1", "K1"]


def test_malformed_rows_are_skipped() -> None:
    assert rail.parse_ticket_rows(["字段不够"], {}) == []
    assert rail.parse_ticket_rows([_ticket_row(duration="不是时长")], {}) == []


# ─── 端到端的桩查询 ──────────────────────────────────────────


def test_query_rail_returns_options(stations) -> None:
    rows = [
        _ticket_row(service_no="G94", duration="04:38"),
        _ticket_row(service_no="K1", duration="18:00"),
    ]
    with _make_client(rows=rows) as client:
        result = rail.query_rail(
            "南京", "西安", TODAY + timedelta(days=5),
            distance_km=1189.0, today=TODAY, stations=stations, client=client,
        )

    assert result.within_sale_window is True
    assert len(result.options) == 2
    assert result.best is not None and result.best.service_no == "G94"
    assert result.best_minutes == 278
    assert result.note is None


def test_query_rail_reports_html_instead_of_crashing(stations) -> None:
    """超出预售期或触发风控时 12306 返回 HTML 错误页，不能让它变成堆栈。"""
    with _make_client(ticket_html=True) as client:
        result = rail.query_rail(
            "南京", "西安", TODAY + timedelta(days=5),
            today=TODAY, stations=stations, client=client,
        )

    assert result.options == ()
    assert result.note is not None
    assert "非 JSON" in result.note


def test_query_rail_reports_unknown_station(stations) -> None:
    with _make_client() as client:
        result = rail.query_rail(
            "不存在的地方", "西安", TODAY + timedelta(days=5),
            today=TODAY, stations=stations, client=client,
        )

    assert result.options == ()
    assert "没能从车站名表" in (result.note or "")


def test_query_rail_reports_no_trains(stations) -> None:
    with _make_client(rows=[]) as client:
        result = rail.query_rail(
            "南京", "西安", TODAY + timedelta(days=5),
            today=TODAY, stations=stations, client=client,
        )

    assert result.options == ()
    assert "没有查到直达车次" in (result.note or "")


def test_one_request_is_enough_because_station_codes_only_locate_the_city() -> None:
    """实测：同城不同站返回的车次集合完全相同，所以按站对枚举是多余的请求。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("station_name.js"):
            return httpx.Response(200, text=STATION_TABLE)
        if request.url.path.endswith("/leftTicket/init"):
            return httpx.Response(200, text="init")
        return httpx.Response(
            200,
            json={
                "status": True,
                "data": {"map": {}, "result": [_ticket_row()]},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler), headers=rail._HEADERS) as client:
        rail.query_rail(
            "南京", "西安", TODAY + timedelta(days=5),
            today=TODAY, stations=rail.parse_station_table(STATION_TABLE), client=client,
        )

    ticket_calls = [path for path in calls if path.endswith("queryG")]
    assert len(ticket_calls) == 1, f"应当只问一次，实际问了 {len(ticket_calls)} 次"


# ─── 里程估算 ────────────────────────────────────────────────


def test_haversine_matches_a_known_distance() -> None:
    # 南京 → 西安 直线约 950 公里
    straight = rail.haversine_km(32.060255, 118.796877, 34.341574, 108.939770)
    assert 900 < straight < 1000


def test_rail_distance_is_longer_than_the_straight_line() -> None:
    straight = rail.haversine_km(32.060255, 118.796877, 34.341574, 108.939770)
    rail_km = rail.rail_distance_km((32.060255, 118.796877), (34.341574, 108.939770))
    assert rail_km > straight
    assert rail_km == pytest.approx(straight * rail.RAIL_DETOUR_FACTOR)
