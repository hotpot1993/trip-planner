"""行程接口测试。

城市解析走的是外部服务，这里替换掉——被测的是接口形状、状态码与
错误翻译，不是高德本身（那部分在 test_engine_amap.py 里）。
"""

from __future__ import annotations

import pytest

from lushu.engine.amap import CityMatch

NANJING = CityMatch(
    name="南京", adcode="320100", level="city", lat_gcj02=32.060255, lng_gcj02=118.796877
)


@pytest.fixture
def stub_cities(monkeypatch: pytest.MonkeyPatch):
    """替换城市解析，返回一张查得到的城市表。"""
    from lushu.api import trip_routes
    from lushu.services import trip_service

    known = {"南京": NANJING}

    def fake_resolve(name: str, **_: object):
        return known.get(name)

    def fake_lookup(name: str, **_: object):
        match = known.get(name)
        return [match] if match else []

    monkeypatch.setattr(trip_service, "resolve_city", fake_resolve)
    monkeypatch.setattr(trip_routes, "lookup_city", fake_lookup)
    return known


def _create(client, *, cities, start="2026-10-01", name=None):
    payload = {"start_date": start, "cities": cities}
    if name:
        payload["name"] = name
    return client.post("/api/trips", json=payload)


# ─── 手工创建 ────────────────────────────────────────────────


def test_create_trip_returns_201_and_the_detail(api_client, stub_cities) -> None:
    response = _create(api_client, cities=[{"name": "南京", "days": 3}])

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "南京 3 天"
    assert body["status"] == "draft"
    assert body["total_days"] == 3
    assert body["city_names"] == ["南京"]


def test_created_trip_has_consecutive_days(api_client, stub_cities) -> None:
    body = _create(api_client, cities=[{"name": "南京", "days": 3}]).json()

    days = body["stays"][0]["days"]
    assert [d["date"] for d in days] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert [d["seq_in_stay"] for d in days] == [0, 1, 2]
    assert body["end_date"] == "2026-10-03"


def test_skeleton_days_start_empty(api_client, stub_cities) -> None:
    body = _create(api_client, cities=[{"name": "南京", "days": 2}]).json()
    assert all(d["items"] == [] for d in body["stays"][0]["days"])


def test_custom_name_wins(api_client, stub_cities) -> None:
    body = _create(api_client, cities=[{"name": "南京", "days": 1}], name="国庆南京").json()
    assert body["name"] == "国庆南京"


def test_multi_city_skeleton_totals_the_days(api_client, monkeypatch, stub_cities) -> None:
    """M1 的界面只走单城市，但接口与表结构必须已经支持多城市。"""
    from lushu.services import trip_service

    stub_cities["北京"] = CityMatch(name="北京", adcode="110100", level="city")
    monkeypatch.setattr(
        trip_service, "resolve_city", lambda name, **kw: stub_cities.get(name)
    )

    body = _create(
        api_client,
        cities=[{"name": "南京", "days": 3}, {"name": "北京", "days": 2}],
    ).json()

    assert body["city_names"] == ["南京", "北京"]
    assert body["total_days"] == 5
    assert [s["stay_days"] for s in body["stays"]] == [3, 2]
    # 第二座城市的第一天必须紧接第一座城市的最后一天
    assert body["stays"][1]["days"][0]["date"] == "2026-10-04"


def test_unknown_city_returns_422_with_the_name(api_client, stub_cities) -> None:
    response = _create(api_client, cities=[{"name": "某个查不到的地方", "days": 2}])

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "city_not_found"
    assert "某个查不到的地方" in body["city_names"]


def test_city_not_found_leaves_no_trip(api_client, stub_cities) -> None:
    _create(api_client, cities=[{"name": "查不到", "days": 1}])
    assert api_client.get("/api/trips").json() == []


@pytest.mark.parametrize("days", [0, -1, 31])
def test_out_of_range_days_is_rejected(api_client, stub_cities, days: int) -> None:
    response = _create(api_client, cities=[{"name": "南京", "days": days}])
    assert response.status_code == 422


def test_empty_city_list_is_rejected(api_client, stub_cities) -> None:
    assert _create(api_client, cities=[]).status_code == 422


# ─── 列表与详情 ──────────────────────────────────────────────


def test_list_is_empty_initially(api_client, stub_cities) -> None:
    assert api_client.get("/api/trips").json() == []


def test_list_shows_the_summary(api_client, stub_cities) -> None:
    _create(api_client, cities=[{"name": "南京", "days": 3}])
    rows = api_client.get("/api/trips").json()

    assert len(rows) == 1
    assert rows[0]["name"] == "南京 3 天"
    assert rows[0]["total_days"] == 3
    assert rows[0]["city_names"] == ["南京"]


def test_get_detail_round_trips(api_client, stub_cities) -> None:
    trip_id = _create(api_client, cities=[{"name": "南京", "days": 2}]).json()["id"]
    body = api_client.get(f"/api/trips/{trip_id}").json()

    assert body["id"] == trip_id
    assert body["total_days"] == 2
    assert len(body["stays"][0]["days"]) == 2


def test_get_missing_trip_returns_404(api_client, stub_cities) -> None:
    response = api_client.get("/api/trips/trip_不存在")
    assert response.status_code == 404
    assert "不存在" in response.json()["detail"]


# ─── 确认与删除 ──────────────────────────────────────────────


def test_confirm_flips_the_status(api_client, stub_cities) -> None:
    trip_id = _create(api_client, cities=[{"name": "南京", "days": 1}]).json()["id"]
    body = api_client.post(f"/api/trips/{trip_id}/confirm").json()

    assert body["status"] == "confirmed"
    assert api_client.get(f"/api/trips/{trip_id}").json()["status"] == "confirmed"


def test_confirm_missing_trip_returns_404(api_client, stub_cities) -> None:
    assert api_client.post("/api/trips/trip_不存在/confirm").status_code == 404


def test_delete_removes_the_trip(api_client, stub_cities) -> None:
    trip_id = _create(api_client, cities=[{"name": "南京", "days": 1}]).json()["id"]

    assert api_client.delete(f"/api/trips/{trip_id}").status_code == 204
    assert api_client.get(f"/api/trips/{trip_id}").status_code == 404
    assert api_client.get("/api/trips").json() == []


def test_delete_missing_trip_returns_404(api_client, stub_cities) -> None:
    assert api_client.delete("/api/trips/trip_不存在").status_code == 404


# ─── 城市解析接口 ────────────────────────────────────────────


def test_resolve_city_returns_candidates(api_client, stub_cities) -> None:
    rows = api_client.get("/api/cities/resolve", params={"name": "南京"}).json()

    assert len(rows) == 1
    assert rows[0]["adcode"] == "320100"
    assert rows[0]["level"] == "city"


def test_resolve_unknown_city_returns_empty_list(api_client, stub_cities) -> None:
    assert api_client.get("/api/cities/resolve", params={"name": "查不到"}).json() == []


# ─── 规划接口的错误路径 ──────────────────────────────────────


def test_plan_requires_a_query(api_client, stub_cities) -> None:
    assert api_client.post("/api/trips/plan", json={"query": ""}).status_code == 422


def test_plan_reports_missing_input_as_422(api_client, stub_cities, monkeypatch) -> None:
    """引擎说缺信息时要把该问的问题交回前端，而不是抛 500。"""
    from lushu.services import trip_service

    async def fake_plan(request, *, on_stage=None):
        raise trip_service.MissingInputError(["出行日期", "天数"])

    monkeypatch.setattr(trip_service, "plan_and_save", fake_plan)

    response = api_client.post("/api/trips/plan", json={"query": "出去玩"})
    assert response.status_code == 422

    body = response.json()
    assert body["error"] == "missing_input"
    assert body["missing_fields"] == ["出行日期", "天数"]


def test_amap_failure_is_reported_as_502(api_client, stub_cities, monkeypatch) -> None:
    from lushu.engine import AmapError
    from lushu.services import trip_service

    async def failing(*args, **kwargs):
        raise AmapError("缺少 AMAP_API_KEY，无法解析城市")

    monkeypatch.setattr(trip_service, "create_skeleton", failing)

    response = _create(api_client, cities=[{"name": "南京", "days": 1}])
    assert response.status_code == 502
    assert response.json()["error"] == "amap_unavailable"
