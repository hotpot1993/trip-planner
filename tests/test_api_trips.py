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


def test_city_display_name_keeps_the_user_wording(api_client, monkeypatch, stub_cities) -> None:
    """高德对市级行政区有自己的叫法（北京 → 北京城区）。显示名必须用用户说的那个，
    否则天气面板上会出现「北京城区」这种没人这么说的词。"""
    from lushu.engine.amap import CityMatch
    from lushu.services import trip_service

    stub_cities["北京"] = CityMatch(name="北京城区", adcode="110100", level="city")
    monkeypatch.setattr(trip_service, "resolve_city", lambda name, **kw: stub_cities.get(name))

    body = _create(api_client, cities=[{"name": "北京", "days": 2}]).json()

    assert body["city_names"] == ["北京"]
    assert body["name"] == "北京 2 天"


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


# ─── 改城市与天数 ────────────────────────────────────────────


def test_update_stays_recomputes_total_days(api_client, stub_cities) -> None:
    trip_id = _create(api_client, cities=[{"name": "南京", "days": 3}]).json()["id"]

    response = api_client.put(
        f"/api/trips/{trip_id}/stays", json={"cities": [{"name": "南京", "days": 5}]}
    )
    assert response.status_code == 200

    body = response.json()
    assert body["total_days"] == 5
    assert body["end_date"] == "2026-10-05"
    assert body["name"] == "南京 5 天"


def test_update_stays_can_add_a_city(api_client, monkeypatch, stub_cities) -> None:
    from lushu.services import trip_service

    stub_cities["西安"] = CityMatch(name="西安", adcode="610100", level="city")
    monkeypatch.setattr(
        trip_service, "resolve_city", lambda name, **kw: stub_cities.get(name)
    )

    trip_id = _create(api_client, cities=[{"name": "南京", "days": 2}]).json()["id"]
    body = api_client.put(
        f"/api/trips/{trip_id}/stays",
        json={"cities": [{"name": "南京", "days": 2}, {"name": "西安", "days": 3}]},
    ).json()

    assert body["city_names"] == ["南京", "西安"]
    assert body["total_days"] == 5
    assert body["stays"][1]["days"][0]["date"] == "2026-10-03"


def test_update_stays_can_move_the_start_date(api_client, stub_cities) -> None:
    trip_id = _create(api_client, cities=[{"name": "南京", "days": 2}]).json()["id"]
    body = api_client.put(
        f"/api/trips/{trip_id}/stays",
        json={"cities": [{"name": "南京", "days": 2}], "start_date": "2026-12-24"},
    ).json()

    assert body["start_date"] == "2026-12-24"
    assert body["stays"][0]["days"][0]["date"] == "2026-12-24"


def test_update_stays_on_missing_trip_returns_404(api_client, stub_cities) -> None:
    response = api_client.put(
        "/api/trips/trip_不存在/stays", json={"cities": [{"name": "南京", "days": 1}]}
    )
    assert response.status_code == 404


def test_update_stays_rejects_unknown_city(api_client, stub_cities) -> None:
    trip_id = _create(api_client, cities=[{"name": "南京", "days": 2}]).json()["id"]
    response = api_client.put(
        f"/api/trips/{trip_id}/stays", json={"cities": [{"name": "查不到", "days": 2}]}
    )

    assert response.status_code == 422
    assert response.json()["error"] == "city_not_found"
    # 失败后原行程必须完好
    assert api_client.get(f"/api/trips/{trip_id}").json()["total_days"] == 2


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


# ─── 生成路径的管道 ──────────────────────────────────────────
#
# 真实的规划要跑高德与 LLM，没有 Key 就动不了。这里用打桩的引擎产出驱动
# 完整链路（接口 → 转换 → 落库 → 读回），验证的是管道本身接对了没有。


ENGINE_PLAN = {
    "query": "南京三日游",
    "destination": "南京",
    "start_date": "2026-10-01",
    "days_count": 2,
    "route_issues": ["Day2 行程较紧凑，建议提前预约餐厅"],
    "weather_note": None,
    "days": [
        {
            "day": 1,
            "date": "2026-10-01",
            "theme": "钟山风景区",
            "timeline": [
                {
                    "type": "attraction",
                    "name": "中山陵",
                    "amap_poi_id": "B000A8UIN0",
                    "location": {"lng": 118.853, "lat": 32.058},
                    "address": "南京市玄武区石象路7号",
                    "rating": 4.7,
                    "open_time": "08:30-17:00",
                    "start_time": "09:00",
                    "end_time": "11:30",
                    "tip": "台阶多，穿舒适的鞋。",
                },
                {"type": "lunch", "name": "南京大牌档", "reason": "本地口味"},
                {
                    "type": "attraction",
                    "name": "对不上的小院子",
                    "amap_poi_id": None,
                    "location": None,
                    "start_time": "14:00",
                    "end_time": "15:30",
                },
                {"type": "dinner", "name": None, "no_restaurant": True},
            ],
        },
        {
            "day": 2,
            "date": "2026-10-02",
            "theme": None,
            "timeline": [
                {
                    "type": "attraction",
                    "name": "南京博物院",
                    "amap_poi_id": "B000A8V003",
                    "location": {"lng": 118.828, "lat": 32.043},
                    "address": "南京市玄武区中山东路321号",
                    "rating": 4.8,
                    "start_time": "09:00",
                    "end_time": "12:00",
                }
            ],
        },
    ],
}


def _stub_engine(monkeypatch, plan: dict | None = None, *, missing: list[str] | None = None):
    """替换 trip_service 里的引擎调用。

    同时把配置"补齐"：流水线在跑之前会先检查 Key，而这里模拟的是
    「环境配好了、引擎被替换掉」的场景。缺 Key 那条路径另有专门的测试。
    """
    from lushu.engine import PlanOutcome, PlanStage
    from lushu.services import trip_service

    monkeypatch.setenv("AMAP_API_KEY", "测试用的高德 Key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "测试用的模型 Key")

    async def fake_run_plan(query, *, on_stage=None, **overrides):
        if missing:
            return PlanOutcome(success=False, missing_fields=missing)
        outcome = PlanOutcome(
            success=True,
            plan=plan or ENGINE_PLAN,
            stages=[PlanStage(node="intent", label="理解需求")],
        )
        if on_stage:
            for stage in outcome.stages:
                on_stage(stage)
        return outcome

    monkeypatch.setattr(trip_service, "run_plan", fake_run_plan)


def test_plan_creates_a_trip_from_the_engine_output(api_client, stub_cities, monkeypatch) -> None:
    _stub_engine(monkeypatch)

    response = api_client.post("/api/trips/plan", json={"query": "南京三日游"})
    assert response.status_code == 201

    body = response.json()
    assert body["name"] == "南京 2 天"
    assert body["total_days"] == 2
    assert body["city_names"] == ["南京"]
    assert body["query"] == "南京三日游"


def test_planned_trip_carries_days_and_items(api_client, stub_cities, monkeypatch) -> None:
    _stub_engine(monkeypatch)
    body = api_client.post("/api/trips/plan", json={"query": "南京三日游"}).json()

    days = body["stays"][0]["days"]
    assert [d["date"] for d in days] == ["2026-10-01", "2026-10-02"]
    assert days[0]["theme"] == "钟山风景区"

    titles = [i["title"] for i in days[0]["items"]]
    assert titles == ["中山陵", "南京大牌档", "对不上的小院子", "晚餐（未找到合适餐厅）"]


def test_planned_items_carry_hard_facts(api_client, stub_cities, monkeypatch) -> None:
    _stub_engine(monkeypatch)
    body = api_client.post("/api/trips/plan", json={"query": "南京三日游"}).json()

    attraction = body["stays"][0]["days"][0]["items"][0]
    assert attraction["poi_id"] == "B000A8UIN0"
    assert attraction["lat_gcj02"] == 32.058
    assert attraction["lng_gcj02"] == 118.853
    assert attraction["rating"] == 4.7
    assert attraction["open_time"] == "08:30-17:00"


def test_unresolved_spot_in_a_planned_trip_shows_as_unaligned(
    api_client, stub_cities, monkeypatch
) -> None:
    """引擎给的景点没有实体主键时，界面上要能看出它待对齐。"""
    _stub_engine(monkeypatch)
    body = api_client.post("/api/trips/plan", json={"query": "南京三日游"}).json()

    items = body["stays"][0]["days"][0]["items"]
    unaligned = [i for i in items if i["kind"] == "poi" and i["poi_id"] is None]
    assert len(unaligned) == 1
    assert unaligned[0]["title"] == "对不上的小院子"


def test_planned_trip_queues_the_alignment_task(api_client, stub_cities, monkeypatch) -> None:
    from lushu.store import connect

    _stub_engine(monkeypatch)
    api_client.post("/api/trips/plan", json={"query": "南京三日游"})

    conn = connect()
    try:
        row = conn.execute(
            "SELECT mention_name, city_adcode FROM alignment_task"
        ).fetchone()
    finally:
        conn.close()

    assert row is not None
    assert row["mention_name"] == "对不上的小院子"
    assert row["city_adcode"] == "320100"


def test_planned_trip_survives_a_read_back(api_client, stub_cities, monkeypatch) -> None:
    """含待对齐景点的行程必须能读回来——这个 bug 真的发生过。"""
    _stub_engine(monkeypatch)
    trip_id = api_client.post("/api/trips/plan", json={"query": "南京三日游"}).json()["id"]

    response = api_client.get(f"/api/trips/{trip_id}")
    assert response.status_code == 200
    assert response.json()["total_days"] == 2


def test_engine_issues_reach_the_client(api_client, stub_cities, monkeypatch) -> None:
    """引擎给的出行提醒目前留在 plan_json 里，不出现在行程接口上。

    这一条是**有意为之的记录**：M1 不做提醒展示，M4 会连同预约提醒一起做。
    """
    _stub_engine(monkeypatch)
    body = api_client.post("/api/trips/plan", json={"query": "南京三日游"}).json()

    assert "warnings" not in body


def test_plan_missing_input_returns_422(api_client, stub_cities, monkeypatch) -> None:
    _stub_engine(monkeypatch, missing=["出发日期"])
    response = api_client.post("/api/trips/plan", json={"query": "出去玩"})

    assert response.status_code == 422
    assert response.json()["missing_fields"] == ["出发日期"]


def test_plan_without_keys_reports_config_missing(api_client, stub_cities, monkeypatch) -> None:
    """缺配置要走「环境没配好」这条路，而不是被归成程序缺陷。

    这是真的跑一次才发现的：引擎在深处抛的裸 RuntimeError 被归成了 internal，
    而它其实该告诉用户去填 .env.local。
    """
    monkeypatch.delenv("AMAP_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    response = api_client.post("/api/trips/plan", json={"query": "南京三日游"})

    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "config_missing"
    assert "AMAP_API_KEY" in body["missing_keys"]
    assert "DEEPSEEK_API_KEY" in body["missing_keys"]
