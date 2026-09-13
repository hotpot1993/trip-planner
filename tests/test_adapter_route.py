"""高德路径规划适配器。

它存在的理由是：路书里的路段原先只有直线距离估算，而**真实步行距离通常是
直线的 1.3 倍**——写着「步行 1200 米」而实际要走 1800 米，正是设计里说的
「现场会很意外」。

这里守两件最容易做错的事：

1. **经纬度顺序**。高德的 `origin`/`destination` 是「经度,纬度」，
   与国内说话的「纬经」相反。写反了不会报错，只会算出一条通往地球另一边的路。
2. **没有可行路径不等于 0 米**。高德算不出来时返回空的 `paths`，把它当成
   0 米会让路书说「就在旁边」。
"""

from __future__ import annotations

import json

import httpx
import pytest

from lushu.adapters.route import (
    DRIVING_URL,
    WALKING_URL,
    RouteError,
    plan_drive,
    plan_walk,
)

# 真实响应的形状（`route.paths[0]` 里的 distance 是米、duration 是秒）
WALK_OK = {
    "status": "1",
    "info": "OK",
    "route": {
        "origin": "118.788600,32.020900",
        "destination": "118.789000,32.021500",
        "paths": [{"distance": "1420", "duration": "1020"}],
    },
}


def _client(payload: dict, *, status: int = 200) -> httpx.Client:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=json.dumps(payload, ensure_ascii=False))

    return httpx.Client(transport=httpx.MockTransport(handler))


class TestPlanWalk:
    def test_sends_lng_lat_in_that_order(self) -> None:
        """写反了不会报错，只会算出一条通往地球另一边的路。"""
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.url.params))
            return httpx.Response(200, text=json.dumps(WALK_OK))

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            plan_walk(
                from_lat=32.0209,
                from_lng=118.7886,
                to_lat=32.0215,
                to_lng=118.7890,
                api_key="k",
                client=client,
            )

        assert captured["origin"] == "118.788600,32.020900"
        assert captured["destination"] == "118.789000,32.021500"

    def test_reads_distance_and_duration(self) -> None:
        with _client(WALK_OK) as client:
            plan = plan_walk(
                from_lat=32.02, from_lng=118.79, to_lat=32.02, to_lng=118.79,
                api_key="k", client=client,
            )

        assert plan.mode == "walk"
        assert plan.distance_m == 1420
        assert plan.duration_min == 17  # 1020 秒

    def test_a_round_trip_under_a_minute_is_still_a_minute(self) -> None:
        """20 秒也是「1 分钟」，不能显示成 0——路书上写「0 分钟」是荒谬的。"""
        payload = {"status": "1", "route": {"paths": [{"distance": "30", "duration": "20"}]}}
        with _client(payload) as client:
            plan = plan_walk(
                from_lat=32.02, from_lng=118.79, to_lat=32.02, to_lng=118.79,
                api_key="k", client=client,
            )

        assert plan.duration_min == 1

    def test_no_path_is_an_error_not_zero_metres(self) -> None:
        """**算不出来不等于就在旁边。** 空 paths 要抛，不能返回 0 米。"""
        payload = {"status": "1", "route": {"paths": []}}
        with _client(payload) as client:
            with pytest.raises(RouteError, match="没有给出可行路径"):
                plan_walk(
                    from_lat=32.02, from_lng=118.79, to_lat=32.02, to_lng=118.79,
                    api_key="k", client=client,
                )

    def test_amap_error_status_raises(self) -> None:
        payload = {"status": "0", "info": "DAILY_QUERY_OVER_LIMIT", "route": {}}
        with _client(payload) as client:
            with pytest.raises(RouteError, match="DAILY_QUERY_OVER_LIMIT"):
                plan_walk(
                    from_lat=32.02, from_lng=118.79, to_lat=32.02, to_lng=118.79,
                    api_key="k", client=client,
                )

    def test_http_error_raises(self) -> None:
        with _client({}, status=502) as client:
            with pytest.raises(RouteError, match="HTTP 502"):
                plan_walk(
                    from_lat=32.02, from_lng=118.79, to_lat=32.02, to_lng=118.79,
                    api_key="k", client=client,
                )

    def test_non_json_raises(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>403</html>")

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(RouteError, match="不是合法 JSON"):
                plan_walk(
                    from_lat=32.02, from_lng=118.79, to_lat=32.02, to_lng=118.79,
                    api_key="k", client=client,
                )

    def test_network_error_raises_readable_message(self) -> None:
        def handler(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("连接超时")

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(RouteError, match="ConnectTimeout"):
                plan_walk(
                    from_lat=32.02, from_lng=118.79, to_lat=32.02, to_lng=118.79,
                    api_key="k", client=client,
                )

    def test_missing_api_key_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AMAP_API_KEY", "")
        with pytest.raises(RouteError, match="AMAP_API_KEY"):
            plan_walk(from_lat=32.02, from_lng=118.79, to_lat=32.02, to_lng=118.79)

    def test_hits_the_walking_endpoint(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url).split("?")[0])
            return httpx.Response(200, text=json.dumps(WALK_OK))

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            plan_walk(
                from_lat=32.02, from_lng=118.79, to_lat=32.02, to_lng=118.79,
                api_key="k", client=client,
            )

        assert seen == [WALKING_URL]


class TestPlanDrive:
    def test_marks_the_mode_as_taxi(self) -> None:
        """驾车接口用来回答「打车大约多久」。

        公交要解析 `transit/integrated` 的分段结果，而且方案随时刻变化、
        存下来第二天就可能不对——这一层如实只给打车。
        """
        payload = {"status": "1", "route": {"paths": [{"distance": "8200", "duration": "1500"}]}}
        with _client(payload) as client:
            plan = plan_drive(
                from_lat=32.02, from_lng=118.79, to_lat=32.10, to_lng=118.90,
                api_key="k", client=client,
            )

        assert plan.mode == "taxi"
        assert plan.distance_m == 8200

    def test_hits_the_driving_endpoint(self) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url).split("?")[0])
            return httpx.Response(200, text=json.dumps(WALK_OK))

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            plan_drive(
                from_lat=32.02, from_lng=118.79, to_lat=32.02, to_lng=118.79,
                api_key="k", client=client,
            )

        assert seen == [DRIVING_URL]
