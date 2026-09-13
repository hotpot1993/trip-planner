"""高德 POI 适配器的测试。

样本是 `scripts/probe_m3_poi_parent.py` 抓下来的**真实响应片段**，
字段值一字未改。这一点很重要：这个适配器存在的理由就是那些字段
（`typecode`、`parent`、`biz_ext`），编一份响应来测它等于没测。

网络请求用 `httpx.MockTransport` 拦掉，不打真接口。
"""

from __future__ import annotations

import json

import httpx
import pytest

from lushu.adapters.poi import (
    AROUND_SEARCH_URL,
    MAX_LIMIT,
    PoiSearchError,
    parse_poi,
    search_around_pois,
    search_pois,
)

# 陕西历史博物馆，实测原始响应（只留了用得到的键）
XIAN_MUSEUM = {
    "id": "B001D03PEX",
    "name": "陕西历史博物馆",
    "typecode": "140100",
    "type": "科教文化服务;博物馆;博物馆",
    "location": "108.953135,34.222085",
    "address": "小寨东路91号",
    "tel": "029-85253806",
    "adcode": "610113",
    "cityname": "西安市",
    "citycode": "029",
    "parent": [],
    "biz_ext": {
        "cost": [],
        "opentime2": (
            "4月1日至11月14日 08:30-19:00 (18:00停止检票),"
            "11月15日至3月14日周一 全天关闭(法定节假日除外), "
            "周二至周日09:00-17:30 (16:30停止检票)"
        ),
        "rating": "4.8",
        "open_time": [],
    },
    "photos": [{"title": [], "url": "https://store.is.autonavi.com/showpic/aaa"}],
}

# 故宫博物院-午门，`parent` 指向本体（ADR-0009 的判据）
GUGONG_MERIDIAN_GATE = {
    "id": "B000A84GDN",
    "name": "故宫博物院-午门",
    "typecode": "110200",
    "type": "风景名胜;风景名胜;风景名胜",
    "location": "116.397029,39.913417",
    "address": "东华门街道景山前街4号故宫博物院内(南侧)",
    "tel": [],
    "adcode": "110101",
    "cityname": "北京市",
    "citycode": "010",
    "parent": "B000A8UIN8",
    "biz_ext": {"cost": [], "opentime2": [], "rating": "3.8", "open_time": []},
    "photos": [],
}

# 廊坊的故宫文创店。搜「故宫」传 adcode 时返回的就是这一类（实测）
LANGFANG_SHOP = {
    "id": "B0FFJKHNUU",
    "name": "故宫文具(北京大兴国际机场店)",
    "typecode": "061209",
    "type": "购物服务;专卖店;礼品饰品店",
    "location": "116.417445,39.508065",
    "address": "大安路北京大兴国际机场3F层",
    "tel": [],
    "adcode": "131003",
    "cityname": "廊坊市",
    "citycode": "0316",
    "parent": "B0FFJKHNUZ",
    "biz_ext": {"cost": [], "opentime2": [], "rating": "3.6", "open_time": []},
    "photos": [{"title": [], "url": "https://store.is.autonavi.com/showpic/bbb"}],
}


def transport_returning(payload: dict, *, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=json.dumps(payload, ensure_ascii=False))

    return httpx.MockTransport(handler)


def ok_response(pois: list[dict]) -> dict:
    return {"status": "1", "info": "OK", "count": str(len(pois)), "pois": pois}


class TestParsePoi:
    def test_full_fields_from_real_payload(self) -> None:
        poi = parse_poi(XIAN_MUSEUM)

        assert poi is not None
        assert poi.poi_id == "B001D03PEX"
        assert poi.name == "陕西历史博物馆"
        assert poi.typecode == "140100"
        assert poi.type_name == "科教文化服务;博物馆;博物馆"
        assert poi.adcode == "610113"
        assert poi.city_name == "西安市"
        assert poi.address == "小寨东路91号"
        assert poi.tel == "029-85253806"
        assert poi.lng_gcj02 == pytest.approx(108.953135)
        assert poi.lat_gcj02 == pytest.approx(34.222085)

    def test_rating_and_open_time_come_from_biz_ext(self) -> None:
        """这两列是硬事实（ADR-0001），取值路径错了不会报错、只会长期是空的。"""
        poi = parse_poi(XIAN_MUSEUM)

        assert poi is not None
        assert poi.rating == pytest.approx(4.8)
        # 陕历博的 open_time 是空数组，只有 opentime2 有值——优先取它
        assert poi.open_time is not None
        assert "08:30-19:00" in poi.open_time

    def test_photo_url_taken_from_first_photo(self) -> None:
        poi = parse_poi(XIAN_MUSEUM)

        assert poi is not None
        assert poi.photo_url == "https://store.is.autonavi.com/showpic/aaa"

    def test_empty_list_becomes_none_not_the_string_bracket(self) -> None:
        """高德用空数组表示「没有值」。

        直接 `str([])` 会得到字面量 `"[]"`——一个看起来非空的垃圾值，
        它会通过「非空」判断一路流到界面上。这是必须挡住的。
        """
        poi = parse_poi(XIAN_MUSEUM)

        assert poi is not None
        assert poi.tel == "029-85253806"
        gate = parse_poi(GUGONG_MERIDIAN_GATE)
        assert gate is not None
        assert gate.tel is None, f"空电话被存成了 {gate.tel!r}"

    def test_parent_empty_list_means_root(self) -> None:
        poi = parse_poi(XIAN_MUSEUM)
        assert poi is not None
        assert poi.parent_id is None

    def test_parent_id_preserved(self) -> None:
        """ADR-0009 的归并全靠这个字段。"""
        poi = parse_poi(GUGONG_MERIDIAN_GATE)

        assert poi is not None
        assert poi.parent_id == "B000A8UIN8"

    def test_raw_json_keeps_the_whole_response(self) -> None:
        """留底是为了日后能回答「这个值当时是从哪来的」。"""
        poi = parse_poi(XIAN_MUSEUM)

        assert poi is not None
        assert poi.raw_json is not None
        restored = json.loads(poi.raw_json)
        assert restored["id"] == "B001D03PEX"
        assert restored["parent"] == []

    def test_missing_location_is_rejected(self) -> None:
        """没有坐标的景点在行程里排不出路线，而 poi 表的坐标是非空约束。"""
        payload = dict(XIAN_MUSEUM, location=[])
        assert parse_poi(payload) is None

    def test_malformed_location_is_rejected(self) -> None:
        assert parse_poi(dict(XIAN_MUSEUM, location="108.95")) is None
        assert parse_poi(dict(XIAN_MUSEUM, location="东经一百零八度")) is None

    def test_missing_id_is_rejected(self) -> None:
        """高德 id 是实体主键（ADR-0002），没有它这条数据没有身份。"""
        assert parse_poi(dict(XIAN_MUSEUM, id=[])) is None
        assert parse_poi(dict(XIAN_MUSEUM, id="")) is None

    def test_missing_biz_ext_does_not_crash(self) -> None:
        payload = {k: v for k, v in XIAN_MUSEUM.items() if k != "biz_ext"}
        poi = parse_poi(payload)

        assert poi is not None
        assert poi.rating is None
        assert poi.open_time is None


class TestSearchPois:
    def test_sends_city_name_and_extensions_all(self) -> None:
        """两条实测结论都要体现在请求参数里。

        - `city` 传城市名而不是 adcode：传 adcode 时高德按「行政区内」理解，
          搜「故宫」传 110100 返回的全是廊坊的店
        - `extensions=all` 是必填：不给它就没有 biz_ext，评分与开放时间会静默全空
        """
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.url.params))
            return httpx.Response(200, text=json.dumps(ok_response([XIAN_MUSEUM])))

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            search_pois("陕西历史博物馆", city="西安", api_key="test-key", client=client)

        assert captured["city"] == "西安"
        assert captured["citylimit"] == "true"
        assert captured["extensions"] == "all"
        assert captured["keywords"] == "陕西历史博物馆"
        assert captured["page"] == "1"

    def test_parses_all_candidates(self) -> None:
        pois = [XIAN_MUSEUM, GUGONG_MERIDIAN_GATE, LANGFANG_SHOP]
        with httpx.Client(transport=transport_returning(ok_response(pois))) as client:
            result = search_pois("博物馆", city="西安", api_key="k", client=client)

        assert result.raw_count == 3
        assert len(result.candidates) == 3
        assert result.query == "博物馆"
        assert result.city == "西安"

    def test_drops_unusable_entries_but_keeps_the_rest(self) -> None:
        """一条没有坐标的不能连累整批。"""
        broken = dict(LANGFANG_SHOP, location=[])
        pois = [XIAN_MUSEUM, broken]
        with httpx.Client(transport=transport_returning(ok_response(pois))) as client:
            result = search_pois("博物馆", city="西安", api_key="k", client=client)

        assert result.raw_count == 2
        assert len(result.candidates) == 1
        assert result.candidates[0].poi_id == "B001D03PEX"

    def test_limit_is_clamped_to_amap_maximum(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.url.params))
            return httpx.Response(200, text=json.dumps(ok_response([])))

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            search_pois("博物馆", city="西安", api_key="k", limit=999, client=client)

        assert captured["offset"] == str(MAX_LIMIT)

    def test_empty_keyword_does_not_call_the_api(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("空关键字不该发起请求")

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = search_pois("   ", city="西安", api_key="k", client=client)

        assert not result
        assert result.candidates == ()

    def test_missing_city_is_rejected(self) -> None:
        """没有城市就限不住范围，跨城错误正是这么来的。"""
        with pytest.raises(PoiSearchError, match="城市名"):
            search_pois("故宫", city="  ", api_key="k")

    def test_missing_api_key_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AMAP_API_KEY", "")
        with pytest.raises(PoiSearchError, match="AMAP_API_KEY"):
            search_pois("故宫", city="北京")

    def test_api_error_status_raises(self) -> None:
        payload = {"status": "0", "info": "INVALID_USER_KEY", "pois": []}
        with httpx.Client(transport=transport_returning(payload)) as client:  # type: ignore[arg-type]
            with pytest.raises(PoiSearchError, match="INVALID_USER_KEY"):
                search_pois("故宫", city="北京", api_key="k", client=client)

    def test_http_error_status_raises(self) -> None:
        with httpx.Client(transport=transport_returning({}, status=502)) as client:  # type: ignore[arg-type]
            with pytest.raises(PoiSearchError, match="HTTP 502"):
                search_pois("故宫", city="北京", api_key="k", client=client)

    def test_non_json_body_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>403 Forbidden</html>")

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(PoiSearchError, match="不是合法 JSON"):
                search_pois("故宫", city="北京", api_key="k", client=client)

    def test_network_error_raises_readable_message(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("连接超时")

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(PoiSearchError, match="ConnectTimeout"):
                search_pois("故宫", city="北京", api_key="k", client=client)

    def test_empty_result_is_not_an_error(self) -> None:
        """搜不到东西是正常结果，不是异常——调度层要能区分这两者。"""
        with httpx.Client(transport=transport_returning(ok_response([]))) as client:
            result = search_pois("不存在的地方", city="西安", api_key="k", client=client)

        assert not result
        assert result.raw_count == 0


class TestSearchAroundPois:
    """周边搜索。

    它的用处不是「再搜一遍」，而是**换一个范围**：按城市按名搜「绿柳居」
    得到 8 家同分的分店，按夫子庙周边搜只有一家。行程里的餐饮名本来就是
    引擎按当天景点做周边搜索拿到的，所以它们的正确落点也在那一片。
    """

    def test_sends_location_as_lng_lat(self) -> None:
        """高德的 `location` 是「经度,纬度」，与国内说话的「纬经」相反。

        写反了不会报错，只会搜到地球另一边的空地——所以这条要钉死。
        """
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.url.params))
            return httpx.Response(200, text=json.dumps(ok_response([XIAN_MUSEUM])))

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            search_around_pois(
                "绿柳居", lat_gcj02=32.0209, lng_gcj02=118.7886, api_key="k", client=client
            )

        assert captured["location"] == "118.788600,32.020900"
        assert captured["keywords"] == "绿柳居"
        assert captured["radius"] == "3000"
        assert captured["extensions"] == "all"
        assert "city" not in captured

    def test_hits_the_around_endpoint(self) -> None:
        """端点不能写错——按名搜的端点会忽略坐标，返回全国的店。"""
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url).split("?")[0])
            return httpx.Response(200, text=json.dumps(ok_response([])))

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            search_around_pois("绿柳居", lat_gcj02=32.02, lng_gcj02=118.79, api_key="k", client=client)

        assert seen == [AROUND_SEARCH_URL]

    def test_radius_can_be_widened(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(dict(request.url.params))
            return httpx.Response(200, text=json.dumps(ok_response([])))

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            search_around_pois(
                "绿柳居", lat_gcj02=32.02, lng_gcj02=118.79, radius_m=8000, api_key="k", client=client
            )

        assert captured["radius"] == "8000"

    def test_blank_keywords_do_not_call_the_api(self) -> None:
        """空关键字不发请求：那会拿到附近所有店，与「找这一家」正好相反。"""
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, text=json.dumps(ok_response([])))

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = search_around_pois("   ", lat_gcj02=32.02, lng_gcj02=118.79, api_key="k", client=client)

        assert called is False
        assert result.raw_count == 0

    def test_missing_api_key_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AMAP_API_KEY", "")
        with pytest.raises(PoiSearchError, match="AMAP_API_KEY"):
            search_around_pois("绿柳居", lat_gcj02=32.02, lng_gcj02=118.79)

    def test_api_error_status_raises(self) -> None:
        """失败的长相要跟按名搜一致，调用方才不用按不同的文案判同一件事。"""
        payload = {"status": "0", "info": "CUQPS_HAS_EXCEEDED_THE_LIMIT", "pois": []}
        with httpx.Client(transport=transport_returning(payload)) as client:  # type: ignore[arg-type]
            with pytest.raises(PoiSearchError, match="CUQPS_HAS_EXCEEDED_THE_LIMIT"):
                search_around_pois("绿柳居", lat_gcj02=32.02, lng_gcj02=118.79, api_key="k", client=client)

    def test_network_error_raises_readable_message(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("连接超时")

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(PoiSearchError, match="ConnectTimeout"):
                search_around_pois("绿柳居", lat_gcj02=32.02, lng_gcj02=118.79, api_key="k", client=client)

    def test_result_keeps_the_places_own_coordinates(self) -> None:
        with httpx.Client(transport=transport_returning(ok_response([XIAN_MUSEUM]))) as client:  # type: ignore[arg-type]
            result = search_around_pois(
                "陕西历史博物馆", lat_gcj02=34.22, lng_gcj02=108.95, api_key="k", client=client
            )

        assert result.candidates[0].poi_id == "B001D03PEX"
        assert result.candidates[0].lat_gcj02 == pytest.approx(34.222085)
        assert result.candidates[0].lng_gcj02 == pytest.approx(108.953135)
