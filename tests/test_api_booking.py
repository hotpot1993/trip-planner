"""预约清单日历接口。

`GET /api/trips/{id}/booking.ics` 是预约子系统唯一的推送通道：本项目是本地
工具，用户不会一直开着它，而预约的要害是时点。这里守两件事——
文件能被日历客户端认（CRLF、VTIMEZONE、正确的 Content-Type），
以及**跳过的景点要说出来**（草案规则不提醒，但不能让用户以为不用预约）。
"""

from __future__ import annotations

from datetime import date

import pytest

from lushu.services import booking_store as bs

BEIJING = ("北京", "110100", 39.9042, 116.4074)


@pytest.fixture
def stub_cities(monkeypatch: pytest.MonkeyPatch):
    """城市解析换成北京。

    这一组测试关心的是日历，不是城市解析，所以自己钉一张最小城市表，
    不跟 trips 那边的夹具耦合。
    """
    from lushu.api import trip_routes
    from lushu.engine import CityMatch
    from lushu.services import trip_service

    match = CityMatch(
        name=BEIJING[0], adcode=BEIJING[1], level="city", lat_gcj02=BEIJING[2], lng_gcj02=BEIJING[3]
    )

    def fake_resolve(name: str, **_: object):
        return match if name == BEIJING[0] else None

    def fake_lookup(name: str, **_: object):
        return [match] if name == BEIJING[0] else []

    monkeypatch.setattr(trip_service, "resolve_city", fake_resolve)
    monkeypatch.setattr(trip_routes, "lookup_city", fake_lookup)
    return {BEIJING[0]: match}


def _trip_with_gugong(api_client, stub_cities) -> str:
    created = api_client.post(
        "/api/trips",
        json={"start_date": "2026-10-01", "cities": [{"name": "北京", "days": 3}]},
    ).json()
    return created["id"]


def _poi_and_visit(api_client, trip_id: str) -> str:
    """给这份行程排一天故宫，并把它写进 poi 表。"""
    from lushu.config import DB_PATH
    from lushu.store import connect as real_connect

    conn = real_connect(DB_PATH)
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO poi (amap_poi_id, name, city_adcode, adcode, "
                "typecode, lat_gcj02, lng_gcj02, fetched_at) VALUES "
                "('B000A8UIN8', '故宫博物院', '110100', '110101', '110201', 39.918, "
                "116.397, '2026-09-12')"
            )
            day = conn.execute(
                "SELECT id FROM day WHERE trip_id = ? ORDER BY date LIMIT 1", (trip_id,)
            ).fetchone()
            conn.execute(
                "INSERT INTO day_item (id, day_id, seq, kind, poi_id, title, origin) "
                "VALUES ('di_test', ?, 0, 'poi', 'B000A8UIN8', '故宫博物院', 'ai')",
                (day["id"],),
            )
    finally:
        conn.close()
    return "B000A8UIN8"


def _rule(poi_id: str, *, reviewed: bool) -> None:
    from lushu.config import DB_PATH
    from lushu.domain.booking import BookingRule, Channel, ChannelKind, RuleStatus
    from lushu.store import connect as real_connect

    conn = real_connect(DB_PATH)
    try:
        with conn:
            bs.write_rule(
                conn=conn,
                rule=BookingRule(
                    poi_id=poi_id,
                    booking_required=True,
                    status=RuleStatus.REVIEWED if reviewed else RuleStatus.DRAFT,
                    advance_days=7,
                    release_time="20:00",
                    channels=(Channel("官方小程序", ChannelKind.MINIAPP, "https://example.cn/"),),
                    requires_real_name=True,
                    evidence_url="https://www.dpm.org.cn/visit.html" if reviewed else None,
                    reviewed_at=date(2026, 1, 1) if reviewed else None,
                ),
                now="2026-09-12T10:00:00",
            )
    finally:
        conn.close()


class TestBookingCalendar:
    def test_returns_a_calendar(self, api_client, stub_cities) -> None:
        trip_id = _trip_with_gugong(api_client, stub_cities)
        poi_id = _poi_and_visit(api_client, trip_id)
        _rule(poi_id, reviewed=True)

        response = api_client.get(f"/api/trips/{trip_id}/booking.ics")

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/calendar")
        body = response.text
        assert body.startswith("BEGIN:VCALENDAR")
        assert "BEGIN:VEVENT" in body
        assert "TZID:Asia/Shanghai" in body
        # 放票日 = 2026-10-01 往前 7 天
        assert "DTSTART;TZID=Asia/Shanghai:20260924T200000" in body

    def test_draft_rule_produces_no_event_but_is_reported(
        self, api_client, stub_cities
    ) -> None:
        """草案规则不提醒，但要把景点名报出来。

        静默少一条会让用户以为那个景点不用预约——正是最怕的失败模式。
        """
        trip_id = _trip_with_gugong(api_client, stub_cities)
        poi_id = _poi_and_visit(api_client, trip_id)
        _rule(poi_id, reviewed=False)

        response = api_client.get(f"/api/trips/{trip_id}/booking.ics")

        assert response.status_code == 200
        assert "BEGIN:VEVENT" not in response.text
        # 草案的规则不进提醒，但它不算「算不出放票日」，所以 skipped 是空的——
        # 界面上要区分这两种情况，这正是 pending_rule_pois 存在的理由
        from lushu.store import connect

        with connect() as conn:
            assert bs.pending_rule_pois(trip_id, conn=conn) == ["故宫博物院"]

    def test_missing_trip_is_404(self, api_client, stub_cities) -> None:
        assert api_client.get("/api/trips/trip_nope/booking.ics").status_code == 404

    def test_filename_is_rfc5987_encoded(self, api_client, stub_cities) -> None:
        """中文文件名直接写会在部分浏览器上乱码。"""
        trip_id = _trip_with_gugong(api_client, stub_cities)
        response = api_client.get(f"/api/trips/{trip_id}/booking.ics")
        disposition = response.headers["content-disposition"]
        assert "filename*=UTF-8''" in disposition
        assert "attachment" in disposition

    def test_no_store_cache(self, api_client, stub_cities) -> None:
        """规则会变，缓存住等于让用户拿到过期的放票时刻。"""
        trip_id = _trip_with_gugong(api_client, stub_cities)
        response = api_client.get(f"/api/trips/{trip_id}/booking.ics")
        assert response.headers["cache-control"] == "no-store"

    def test_calendar_without_any_rule_is_still_valid(self, api_client, stub_cities) -> None:
        trip_id = _trip_with_gugong(api_client, stub_cities)
        response = api_client.get(f"/api/trips/{trip_id}/booking.ics")
        assert response.status_code == 200
        assert response.text.rstrip().endswith("END:VCALENDAR")


class TestBookingList:
    def test_lists_the_alert_with_everything_needed_to_act(
        self, api_client, stub_cities
    ) -> None:
        trip_id = _trip_with_gugong(api_client, stub_cities)
        poi_id = _poi_and_visit(api_client, trip_id)
        _rule(poi_id, reviewed=True)

        payload = api_client.get(f"/api/trips/{trip_id}/booking?today=2026-09-20").json()

        assert len(payload["alerts"]) == 1
        alert = payload["alerts"][0]
        assert alert["poi_name"] == "故宫博物院"
        assert alert["release_date"] == "2026-09-24"
        assert alert["days_until_release"] == 4
        assert alert["urgency"] == "later"  # 3 天以内才算紧急
        # 一行结论由领域层算好，界面不自己拼
        assert alert["headline"] == "4 天后放票"
        assert alert["channels"][0]["name"] == "官方小程序"
        assert alert["requires_real_name"] is True

    def test_within_three_days_is_soon(self, api_client, stub_cities) -> None:
        trip_id = _trip_with_gugong(api_client, stub_cities)
        poi_id = _poi_and_visit(api_client, trip_id)
        _rule(poi_id, reviewed=True)

        payload = api_client.get(f"/api/trips/{trip_id}/booking?today=2026-09-22").json()
        assert payload["alerts"][0]["urgency"] == "soon"

    def test_release_day_itself_is_today(self, api_client, stub_cities) -> None:
        trip_id = _trip_with_gugong(api_client, stub_cities)
        poi_id = _poi_and_visit(api_client, trip_id)
        _rule(poi_id, reviewed=True)

        payload = api_client.get(f"/api/trips/{trip_id}/booking?today=2026-09-24").json()
        alert = payload["alerts"][0]
        assert alert["urgency"] == "today"
        # 放票时刻必须出现在这一行结论里——它是这一天唯一要紧的信息
        assert alert["headline"] == "今天 20:00 放票"

    def test_overdue_is_reported_as_such(self, api_client, stub_cities) -> None:
        trip_id = _trip_with_gugong(api_client, stub_cities)
        poi_id = _poi_and_visit(api_client, trip_id)
        _rule(poi_id, reviewed=True)

        # 放票日是 9-24；把「今天」挪到 9-25 就成了放票日已过
        payload = api_client.get(f"/api/trips/{trip_id}/booking?today=2026-09-25").json()
        assert payload["alerts"][0]["urgency"] == "overdue"
        assert "立刻确认" in payload["alerts"][0]["headline"]

    def test_pending_review_is_reported_separately(self, api_client, stub_cities) -> None:
        """清单里没有 ≠ 不用预约。这两件事必须在同一个响应里说清。"""
        trip_id = _trip_with_gugong(api_client, stub_cities)
        poi_id = _poi_and_visit(api_client, trip_id)
        _rule(poi_id, reviewed=False)

        payload = api_client.get(f"/api/trips/{trip_id}/booking").json()
        assert payload["alerts"] == []
        assert payload["pending_review"] == ["故宫博物院"]

    def test_missing_trip_is_404(self, api_client, stub_cities) -> None:
        assert api_client.get("/api/trips/trip_nope/booking").status_code == 404
