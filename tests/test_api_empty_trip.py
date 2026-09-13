"""空行程与退化输入：每个接口都得说清楚「这里什么都没有」。

这一组来自一次边界扫描——拿一份**只建了骨架、一行天项都没有**的行程把所有
接口打一遍。它当时扫出了三个真的坏掉的地方，都不是「接口报错」，而是
「接口给出了一个看起来正常的假答案」：

1. `.ics` 的时区块被折行切坏（那份产物后来发现还有两处 RFC 5545 的 MUST
   违规，手机日历一个事件都不认——日历导出这个功能已因此被放弃，见
   `docs/M7-STATUS.md`）；
2. 「知识库没覆盖的城市直接搜高德」那条路因为一个签名不匹配**从来没工作过**；
3. 天气两个来源都没取到时，来源照样写着「高德」。

所以这里测的不是「返回 200」，而是**空的时候那几句话对不对**：`ratio` 是
`None` 而不是 0、来源是 `None` 而不是一个没给数据的源、路书说得出「这一天还
没有安排」。静默的失败比报错贵得多，而「空」正是最容易静默出错的地方。
"""

from __future__ import annotations

import pytest

from lushu.store import connect
from tests.test_api_booking import stub_cities  # noqa: F401  （夹具靠导入进本模块）

START = "2027-03-01"
TRIP_NAME = "边界：骨架"


@pytest.fixture
def skeleton_trip(api_client, isolated_config, stub_cities) -> str:  # noqa: F811
    """一份只建了骨架的行程：一座城市、一天、一行天项都没有。

    `stub_cities` 是必须的：城市解析要问高德的行政区划接口，真跑一次会**消耗
    配额**——第一次写这个文件时就把它跑成了 `CUQPS_HAS_EXCEEDED_THE_LIMIT`，
    于是测试在别的机器上会随机变红。
    """
    conn = connect(isolated_config.DB_PATH)
    conn.execute(
        "INSERT INTO city (adcode, name, updated_at) VALUES ('110100', '北京', '2026-09-13')"
    )
    conn.commit()
    conn.close()

    created = api_client.post(
        "/api/trips",
        json={"start_date": START, "cities": [{"name": "北京", "days": 1}], "name": TRIP_NAME},
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


class TestEmptyTripAnswersHonestly:
    def test_detail_has_the_day_but_no_items(self, api_client, skeleton_trip) -> None:
        payload = api_client.get(f"/api/trips/{skeleton_trip}").json()

        days = payload["stays"][0]["days"]
        assert [day["items"] for day in days] == [[]], "日子要在，天项是空的"
        assert payload["budget"]["total"] == 0
        assert payload["budget"]["has_reference_prices"] is False

    def test_coverage_ratio_is_none_not_zero(self, api_client, skeleton_trip) -> None:
        """一个天项都没有时「覆盖率」是算不出的，不是 0%。

        报 0% 的意思是「一个都没对上」，而这里的实情是**还没有东西可对**。
        """
        payload = api_client.get(f"/api/trips/{skeleton_trip}/coverage").json()

        assert payload["total"] == 0
        assert payload["ratio"] is None

    def test_insights_are_empty_rather_than_fabricated(self, api_client, skeleton_trip) -> None:
        payload = api_client.get(f"/api/trips/{skeleton_trip}/insights").json()

        assert payload["by_poi"] == {}
        assert payload["covered_items"] == 0
        assert payload["total_claims"] == 0

    def test_booking_list_is_empty(self, api_client, skeleton_trip) -> None:
        payload = api_client.get(f"/api/trips/{skeleton_trip}/booking").json()

        assert payload["alerts"] == []
        assert payload["pending_review"] == []

    def test_pending_meals_is_empty(self, api_client, skeleton_trip) -> None:
        payload = api_client.get(f"/api/trips/{skeleton_trip}/pending-meals").json()

        assert payload["items"] == []

    def test_roadbook_says_the_day_is_unplanned(self, api_client, skeleton_trip) -> None:
        """路书是带上路的那一份：这一天没安排就要写出来，不能是一片空白。"""
        html = api_client.get(f"/api/trips/{skeleton_trip}/roadbook.html").text

        assert "这一天还没有安排" in html
        assert "<!doctype html>" in html


class TestUnknownTripIs404Everywhere:
    """不存在的行程，每个子资源都要说同一句话，而不是各自编一个。"""

    @pytest.mark.parametrize(
        "suffix",
        ["", "/coverage", "/insights", "/booking", "/pending-meals", "/roadbook.html"],
    )
    def test_sub_resources_are_404(self, api_client, suffix: str) -> None:
        response = api_client.get(f"/api/trips/trip_nope{suffix}")

        assert response.status_code == 404
        assert "trip_nope" in response.text


class TestDegenerateRequests:
    def test_unresolvable_city_is_named_not_silently_dropped(
        self, api_client, stub_cities  # noqa: F811
    ) -> None:
        """认不出的城市名要点名报错，不能排出一份少了那座城的行程。"""
        response = api_client.post(
            "/api/trips",
            json={"start_date": START, "cities": [{"name": "没有这座城", "days": 1}]},
        )

        assert response.status_code == 422
        assert response.json()["error"] == "city_not_found"
        assert "没有这座城" in response.text

    def test_empty_city_list_is_rejected(self, api_client, stub_cities) -> None:  # noqa: F811
        response = api_client.post(
            "/api/trips", json={"start_date": START, "cities": [], "name": "空"}
        )

        assert response.status_code == 422

    def test_zero_days_is_rejected(self, api_client, stub_cities) -> None:  # noqa: F811
        response = api_client.post(
            "/api/trips",
            json={"start_date": START, "cities": [{"name": "北京", "days": 0}]},
        )

        assert response.status_code == 422
