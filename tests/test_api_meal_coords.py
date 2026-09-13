"""待补坐标的餐饮项：人工指定的入口。

自动判据（`services/meal_coords`）只写分得清的那些，剩下的靠人认——
机器分不清「绿柳居」的 24 家分店，人一眼就知道是夫子庙那家。

这里守三件事：

1. **列出待办不联网**。候选要打高德，一次页面加载会慢十几秒、还把额度烧在
   一眼不看的东西上。所以列表与候选是两个接口。
2. **不是待办的东西写不进去**。已经定下来的、不属于这份行程的、不是餐饮的，
   一律拒绝——写进去比报错糟得多，因为看不出来。
3. **候选按距离排，不按名字分筛**。名字分在这里是参考，不是门槛：
   「蒋有记(老门东店)」只有 0.34，可它就是答案。
"""

from __future__ import annotations

import pytest

from tests.test_api_booking import stub_cities  # noqa: F401  （夹具靠导入进本模块）

NOW = "2026-09-13T10:00:00"


@pytest.fixture
def trip_with_meals(api_client, stub_cities) -> tuple[str, list[str]]:  # noqa: F811
    """一份行程，里头有两个餐饮项（都没有坐标）。返回（行程 id, 天项 id）。"""
    from lushu.config import DB_PATH
    from lushu.store import connect as real_connect
    from lushu.store import transaction

    created = api_client.post(
        "/api/trips",
        json={"start_date": "2026-10-01", "cities": [{"name": "北京", "days": 1}]},
    ).json()
    trip_id = created["id"]

    conn = real_connect(DB_PATH)
    try:
        day = conn.execute(
            "SELECT d.id FROM day d WHERE d.trip_id = ? ORDER BY d.date LIMIT 1", (trip_id,)
        ).fetchone()
        day_id = day["id"]
    finally:
        conn.close()

    item_ids: list[str] = []
    with transaction(DB_PATH) as write:
        # 一处景点做锚点（坐标在 poi 表里——历史行程就是这个形状）
        write.execute(
            "INSERT OR REPLACE INTO poi (amap_poi_id, name, city_adcode, adcode, typecode, "
            "lat_gcj02, lng_gcj02, fetched_at) VALUES "
            "('B_GUGONG', '故宫博物院', '110100', '110101', '110201', 39.918, 116.397, ?)",
            (NOW,),
        )
        write.execute(
            "INSERT INTO day_item (id, day_id, seq, kind, poi_id, title, origin) "
            "VALUES ('di_poi', ?, 0, 'poi', 'B_GUGONG', '故宫博物院', 'ai')",
            (day_id,),
        )
        for index, title in enumerate(("绿柳居", "蒋有记锅贴"), start=1):
            item_id = f"di_meal_{index}"
            write.execute(
                "INSERT INTO day_item (id, day_id, seq, kind, title, origin) "
                "VALUES (?, ?, ?, 'meal', ?, 'ai')",
                (item_id, day_id, index, title),
            )
            item_ids.append(item_id)
    return trip_id, item_ids


def _candidates(monkeypatch: pytest.MonkeyPatch, *, near: list[dict] | None = None) -> None:
    """把高德搜索换掉：这一组测的是接口契约，不是高德。"""
    from lushu.services import meal_coords

    shops = near if near is not None else []
    monkeypatch.setattr(
        meal_coords,
        "meal_candidates",
        lambda slot, **_: meal_coords.CandidateList(
            slot=slot,
            candidates=[
                meal_coords.MealCandidate(
                    poi_id=f"B_{index}",
                    name=item["name"],
                    address=item.get("address"),
                    lat_gcj02=item.get("lat", 39.9),
                    lng_gcj02=item.get("lng", 116.4),
                    distance_m=item.get("distance_m"),
                    nearest_anchor=item.get("nearest_anchor"),
                    name_score=item.get("name_score", 0.9),
                )
                for index, item in enumerate(shops)
            ],
            total=len(shops),
            note="",
        ),
    )


class TestPendingMeals:
    def test_lists_the_meals_without_coordinates(
        self, api_client, trip_with_meals: tuple[str, list[str]]
    ) -> None:
        trip_id, item_ids = trip_with_meals

        payload = api_client.get(f"/api/trips/{trip_id}/pending-meals").json()

        assert payload["trip_id"] == trip_id
        assert [item["title"] for item in payload["items"]] == ["绿柳居", "蒋有记锅贴"]
        assert [item["item_id"] for item in payload["items"]] == item_ids
        # 锚点要带出来：人认店靠的是「就在哪儿附近」
        assert payload["items"][0]["anchors"] == ["故宫博物院"]

    def test_the_list_does_not_hit_the_network(
        self, api_client, trip_with_meals: tuple[str, list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """列表只查库。联网的活留给「点开某一项」那个接口。

        页面一打开就打十几次高德，用户会以为界面卡死了，额度也白烧。
        """
        trip_id, _ = trip_with_meals

        def boom(*_args, **_kwargs):
            raise AssertionError("列待办不该联网")

        monkeypatch.setattr("lushu.adapters.poi.search_pois", boom)
        monkeypatch.setattr("lushu.adapters.poi.search_around_pois", boom)

        assert api_client.get(f"/api/trips/{trip_id}/pending-meals").status_code == 200

    def test_unknown_trip_is_404(self, api_client, stub_cities) -> None:  # noqa: F811
        assert api_client.get("/api/trips/trip_nope/pending-meals").status_code == 404

    def test_no_meals_gives_an_empty_list(
        self, api_client, stub_cities  # noqa: F811
    ) -> None:
        created = api_client.post(
            "/api/trips",
            json={"start_date": "2026-10-01", "cities": [{"name": "北京", "days": 1}]},
        ).json()

        payload = api_client.get(f"/api/trips/{created['id']}/pending-meals").json()

        assert payload["items"] == []


class TestCandidates:
    def test_returns_candidates_in_the_given_order(
        self,
        api_client,
        trip_with_meals: tuple[str, list[str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        trip_id, item_ids = trip_with_meals
        _candidates(
            monkeypatch,
            near=[
                {"name": "绿柳居(故宫店)", "distance_m": 120, "nearest_anchor": "故宫博物院"},
                {"name": "绿柳居(龙江店)", "distance_m": 9800, "nearest_anchor": "故宫博物院"},
            ],
        )

        payload = api_client.get(
            f"/api/trips/{trip_id}/pending-meals/{item_ids[0]}/candidates"
        ).json()

        assert [item["name"] for item in payload["candidates"]] == [
            "绿柳居(故宫店)",
            "绿柳居(龙江店)",
        ]
        assert payload["candidates"][0]["distance_m"] == 120
        assert payload["candidates"][0]["nearest_anchor"] == "故宫博物院"
        assert payload["total"] == 2
        assert payload["title"] == "绿柳居"

    def test_a_distance_that_cannot_be_computed_is_null_not_zero(
        self,
        api_client,
        trip_with_meals: tuple[str, list[str]],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """**「算不出」不能写成 0**——0 米看起来像就在旁边。"""
        trip_id, item_ids = trip_with_meals
        _candidates(monkeypatch, near=[{"name": "绿柳居", "distance_m": None}])

        payload = api_client.get(
            f"/api/trips/{trip_id}/pending-meals/{item_ids[0]}/candidates"
        ).json()

        assert payload["candidates"][0]["distance_m"] is None

    def test_an_item_from_another_trip_is_404(
        self, api_client, trip_with_meals: tuple[str, list[str]], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trip_id, _ = trip_with_meals
        _candidates(monkeypatch)

        assert (
            api_client.get(
                f"/api/trips/{trip_id}/pending-meals/di_not_mine/candidates"
            ).status_code
            == 404
        )


class TestPin:
    def test_writes_the_chosen_one_and_returns_the_fresh_list(
        self, api_client, trip_with_meals: tuple[str, list[str]]
    ) -> None:
        trip_id, item_ids = trip_with_meals

        response = api_client.post(
            f"/api/trips/{trip_id}/pending-meals/{item_ids[0]}",
            json={"lat_gcj02": 32.0215, "lng_gcj02": 118.789, "address": "夫子庙店"},
        )

        assert response.status_code == 200
        # 返回刷新后的清单：界面少一次往返，也不会出现「写进去了但清单还挂着它」
        assert [item["title"] for item in response.json()["items"]] == ["蒋有记锅贴"]

    def test_the_pinned_meal_gets_a_leg_in_the_roadbook(
        self, api_client, trip_with_meals: tuple[str, list[str]]
    ) -> None:
        """指定之后路书里那处空洞就该没了——这才是这一步的目的。"""
        from lushu.services import roadbook_service

        trip_id, item_ids = trip_with_meals
        api_client.post(
            f"/api/trips/{trip_id}/pending-meals/{item_ids[0]}",
            json={"lat_gcj02": 39.919, "lng_gcj02": 116.4},
        )

        book, _ = roadbook_service.assemble(trip_id)
        legs = [leg for day in book.days for leg in day.legs]
        assert any(leg is not None for leg in legs), "补上坐标之后仍然一段路都算不出来"

    def test_pinning_twice_is_refused(
        self, api_client, trip_with_meals: tuple[str, list[str]]
    ) -> None:
        """已经定下来的不该被再点一次冲掉——人核对过的东西比一次点击值钱。"""
        trip_id, item_ids = trip_with_meals
        body = {"lat_gcj02": 39.919, "lng_gcj02": 116.4}
        api_client.post(f"/api/trips/{trip_id}/pending-meals/{item_ids[0]}", json=body)

        again = api_client.post(f"/api/trips/{trip_id}/pending-meals/{item_ids[0]}", json=body)

        # 已经不是待办了，第二次连门都进不去
        assert again.status_code == 404

    def test_a_poi_item_is_not_pinnable(
        self, api_client, trip_with_meals: tuple[str, list[str]]
    ) -> None:
        """景点项走 `poi` 表（ADR-0001），这个入口只补餐饮。"""
        trip_id, _ = trip_with_meals

        response = api_client.post(
            f"/api/trips/{trip_id}/pending-meals/di_poi",
            json={"lat_gcj02": 1.0, "lng_gcj02": 2.0},
        )

        assert response.status_code == 404
