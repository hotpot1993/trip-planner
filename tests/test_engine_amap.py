"""高德行政区划包装的测试。

用假的 HTTP 响应驱动，不需要 API Key。重点验证两件事：
一是「经度,纬度」的解析顺序（写反就是几百公里偏移），
二是城市层级的筛选（把省份当成城市会让天数分配失去意义）。
"""

from __future__ import annotations

import pytest

from lushu.engine.amap import AmapError, lookup_city, resolve_city


@pytest.fixture
def fake_amap(monkeypatch: pytest.MonkeyPatch):
    """替换引擎的 HTTP 工具，返回预先安排好的高德响应。"""
    from third_party.floattrip.core import http as engine_http

    calls: list[str] = []

    def install(payload: dict) -> list[str]:
        def fake_get(url: str, timeout: int = 15) -> dict:
            calls.append(url)
            return payload

        monkeypatch.setattr(engine_http, "http_get_json", fake_get)
        return calls

    return install


def _city_payload(*districts: dict) -> dict:
    return {"status": "1", "info": "OK", "districts": list(districts)}


NANJING = {
    "name": "南京",
    "adcode": "320100",
    "level": "city",
    "center": "118.796877,32.060255",
    "province": "江苏",
}


# ─── 解析 ────────────────────────────────────────────────────


def test_center_is_parsed_as_longitude_then_latitude(fake_amap) -> None:
    fake_amap(_city_payload(NANJING))
    matches = lookup_city("南京", api_key="测试key")

    assert len(matches) == 1
    assert matches[0].lng_gcj02 == pytest.approx(118.796877)
    assert matches[0].lat_gcj02 == pytest.approx(32.060255)


def test_fields_are_carried_through(fake_amap) -> None:
    fake_amap(_city_payload(NANJING))
    match = lookup_city("南京", api_key="测试key")[0]

    assert match.name == "南京"
    assert match.adcode == "320100"
    assert match.level == "city"
    assert match.province == "江苏"
    assert match.is_city


def test_the_api_key_is_sent(fake_amap) -> None:
    calls = fake_amap(_city_payload(NANJING))
    lookup_city("南京", api_key="我的key")

    assert calls and "key=%E6%88%91%E7%9A%84key" in calls[0]
    assert "keywords=%E5%8D%97%E4%BA%AC" in calls[0]


def test_missing_districts_returns_empty(fake_amap) -> None:
    fake_amap({"status": "1", "info": "OK"})
    assert lookup_city("不存在的地方", api_key="k") == []


def test_district_without_adcode_is_skipped(fake_amap) -> None:
    fake_amap(_city_payload({"name": "坏数据", "level": "city", "center": "1,2"}))
    assert lookup_city("坏数据", api_key="k") == []


def test_broken_center_does_not_drop_the_match(fake_amap) -> None:
    """中心点解析失败时仍要保留行政区划代码——代码比坐标更关键。"""
    fake_amap(_city_payload({**NANJING, "center": "坏坐标"}))
    matches = lookup_city("南京", api_key="k")

    assert len(matches) == 1
    assert matches[0].adcode == "320100"
    assert matches[0].lat_gcj02 is None


def test_blank_keyword_short_circuits(fake_amap) -> None:
    calls = fake_amap(_city_payload(NANJING))
    assert lookup_city("   ", api_key="k") == []
    assert calls == []


# ─── 失败 ────────────────────────────────────────────────────


def test_failed_status_raises(fake_amap) -> None:
    fake_amap({"status": "0", "info": "INVALID_USER_KEY"})
    with pytest.raises(AmapError, match="INVALID_USER_KEY"):
        lookup_city("南京", api_key="k")


def test_missing_key_raises_with_a_readable_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AMAP_API_KEY", raising=False)
    with pytest.raises(AmapError, match="AMAP_API_KEY"):
        lookup_city("南京")


# ─── 选择 ────────────────────────────────────────────────────


def test_exact_city_match_wins(fake_amap) -> None:
    fake_amap(_city_payload(
        {"name": "南京", "adcode": "320000", "level": "province", "center": "118,32"},
        NANJING,
    ))
    match = resolve_city("南京", api_key="k")

    assert match is not None
    assert match.adcode == "320100"
    assert match.level == "city"


def test_province_only_result_is_rejected(fake_amap) -> None:
    """把省份当成一座城市停留，会让整个行程的天数分配失去意义。"""
    fake_amap(_city_payload(
        {"name": "江苏", "adcode": "320000", "level": "province", "center": "118,32"},
    ))
    assert resolve_city("江苏", api_key="k") is None


def test_district_only_result_is_rejected(fake_amap) -> None:
    fake_amap(_city_payload(
        {"name": "玄武区", "adcode": "320102", "level": "district", "center": "118.79,32.05"},
    ))
    assert resolve_city("玄武区", api_key="k") is None


def test_nothing_found_returns_none(fake_amap) -> None:
    fake_amap({"status": "1", "info": "OK", "districts": []})
    assert resolve_city("某个不存在的地方", api_key="k") is None


def test_first_city_is_used_when_the_name_differs_slightly(fake_amap) -> None:
    fake_amap(_city_payload({**NANJING, "name": "南京市"}))
    match = resolve_city("南京", api_key="k")

    assert match is not None
    assert match.adcode == "320100"
