"""规划产出 → 自有行程模型的转换测试。

这个文件不需要任何 API Key：转换层是纯函数，而它承载的正是 ADR-0004 的
不变量（总天数 = 各城市停留天数之和、日期连续无洞）。这里把它钉死。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from lushu.domain.planned import ItemKind, PlannedTrip
from lushu.services.plan_converter import PlanConversionError, plan_to_trip

START = date(2026, 10, 1)
NANJING_ADCODE = "320100"

# 高德返回的坐标是 GCJ-02，且字符串顺序是「经度,纬度」
NANJING_LNG = 118.796877
NANJING_LAT = 32.060255


def _attraction(name: str, poi_id: str | None = "B000A8UIN8", **extra: Any) -> dict[str, Any]:
    return {
        "type": "attraction",
        "name": name,
        "amap_poi_id": poi_id,
        "location": {"lng": NANJING_LNG, "lat": NANJING_LAT},
        "address": "南京市玄武区",
        "start_time": "09:00",
        "end_time": "11:30",
        "period": "morning",
        "tip": f"{name}的游玩贴士",
        **extra,
    }


def _meal(kind: str, name: str | None) -> dict[str, Any]:
    if name is None:
        return {"type": kind, "name": None, "no_restaurant": True}
    return {"type": kind, "name": name, "reason": f"{name}的理由"}


def _plan(days: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "query": "南京三日游",
        "destination": "南京",
        "start_date": START.isoformat(),
        "end_date": None,
        "days_count": len(days),
        "route_issues": [],
        "weather_note": None,
        "days": days,
    }
    base.update(overrides)
    return base


def _three_days() -> list[dict[str, Any]]:
    return [
        {
            "day": 1,
            "date": START.isoformat(),
            "theme": "钟山风景区",
            "timeline": [
                _attraction("中山陵"),
                _meal("lunch", "南京大牌档"),
                _attraction("明孝陵", "B000A8UIN9"),
                _meal("dinner", "回味鸭血粉丝"),
            ],
        },
        {
            "day": 2,
            "date": "2026-10-02",
            "theme": "秦淮河畔",
            "timeline": [
                _attraction("夫子庙", "B000A8V001"),
                _meal("lunch", None),
                _attraction("老门东", "B000A8V002"),
                _meal("dinner", "绿柳居"),
            ],
        },
        {
            "day": 3,
            "date": "2026-10-03",
            "theme": "博物馆与城墙",
            "timeline": [
                _attraction("南京博物院", "B000A8V003"),
                _meal("lunch", "狮王府"),
                _meal("dinner", None),
            ],
        },
    ]


def _convert(plan: dict[str, Any], **kwargs: Any) -> PlannedTrip:
    kwargs.setdefault("city_adcodes", {"南京": NANJING_ADCODE})
    return plan_to_trip(plan, **kwargs)


# ─── 基本形状 ────────────────────────────────────────────────


def test_single_city_plan_becomes_one_stay() -> None:
    trip = _convert(_plan(_three_days()))

    assert len(trip.stays) == 1
    stay = trip.stays[0]
    assert stay.city_name == "南京"
    assert stay.city_adcode == NANJING_ADCODE
    assert stay.seq == 0


def test_total_days_equals_stay_days_equals_day_count() -> None:
    """ADR-0004 的核心不变量。"""
    trip = _convert(_plan(_three_days()))

    assert trip.total_days == 3
    assert trip.stays[0].stay_days == 3
    assert len(trip.stays[0].days) == 3


def test_dates_are_consecutive_from_the_start_date() -> None:
    trip = _convert(_plan(_three_days()))

    assert [d.day for d in trip.stays[0].days] == [
        date(2026, 10, 1),
        date(2026, 10, 2),
        date(2026, 10, 3),
    ]
    assert trip.start_date == START
    assert trip.end_date == date(2026, 10, 3)


def test_default_name_uses_city_and_total_days() -> None:
    assert _convert(_plan(_three_days())).name == "南京 3 天"


def test_caller_supplied_name_wins() -> None:
    trip = _convert(_plan(_three_days()), trip_name="国庆南京")
    assert trip.name == "国庆南京"


def test_query_is_preserved() -> None:
    assert _convert(_plan(_three_days())).query == "南京三日游"


def test_theme_is_carried() -> None:
    trip = _convert(_plan(_three_days()))
    assert trip.stays[0].days[0].theme == "钟山风景区"


# ─── 事项转换 ────────────────────────────────────────────────


def test_attraction_with_poi_id_is_aligned() -> None:
    item = _convert(_plan(_three_days())).stays[0].days[0].items[0]

    assert item.kind is ItemKind.POI
    assert item.title == "中山陵"
    assert item.poi_id == "B000A8UIN8"
    assert item.unresolved_name is None


def test_aligned_attraction_carries_its_hard_facts() -> None:
    item = _convert(_plan(_three_days())).stays[0].days[0].items[0]

    assert item.facts is not None
    assert item.facts.lat_gcj02 == pytest.approx(NANJING_LAT)
    assert item.facts.lng_gcj02 == pytest.approx(NANJING_LNG)
    assert item.facts.address == "南京市玄武区"


def test_location_string_is_parsed_as_longitude_then_latitude() -> None:
    """高德的字符串顺序是「经度,纬度」。写反就是几百公里的偏移，必须钉死。"""
    days = [{"day": 1, "date": START.isoformat(), "theme": None,
             "timeline": [_attraction("测试景点", location=f"{NANJING_LNG},{NANJING_LAT}")]}]
    facts = _convert(_plan(days)).stays[0].days[0].items[0].facts

    assert facts is not None
    assert facts.lng_gcj02 == pytest.approx(NANJING_LNG)
    assert facts.lat_gcj02 == pytest.approx(NANJING_LAT)


def test_attraction_with_id_but_no_coordinates_is_unresolved() -> None:
    """poi 表的坐标是 NOT NULL，没有坐标就落不了库，因此不算已对齐。"""
    days = [{"day": 1, "date": START.isoformat(), "theme": None,
             "timeline": [_attraction("缺坐标的景点", location=None)]}]
    trip = _convert(_plan(days))

    item = trip.stays[0].days[0].items[0]
    assert item.poi_id is None
    assert item.unresolved_name == "缺坐标的景点"
    assert trip.unresolved_names == ("缺坐标的景点",)


def test_malformed_location_is_treated_as_missing() -> None:
    days = [{"day": 1, "date": START.isoformat(), "theme": None,
             "timeline": [_attraction("坐标坏掉的景点", location="不是坐标")]}]
    trip = _convert(_plan(days))

    assert trip.stays[0].days[0].items[0].unresolved_name == "坐标坏掉的景点"


def test_rating_and_open_time_are_parsed() -> None:
    days = [{"day": 1, "date": START.isoformat(), "theme": None,
             "timeline": [_attraction("有评分的景点", rating="4.7", open_time="08:30-17:00")]}]
    facts = _convert(_plan(days)).stays[0].days[0].items[0].facts

    assert facts is not None
    assert facts.rating == pytest.approx(4.7)
    assert facts.open_time == "08:30-17:00"


def test_attraction_without_poi_id_goes_to_alignment() -> None:
    """引擎没给实体主键时不能静默丢弃，否则这个景点永远挂不上攻略知识。"""
    days = [{"day": 1, "date": START.isoformat(), "theme": None,
             "timeline": [_attraction("某个小景点", None)]}]
    trip = _convert(_plan(days))

    item = trip.stays[0].days[0].items[0]
    assert item.poi_id is None
    assert item.unresolved_name == "某个小景点"
    assert trip.unresolved_names == ("某个小景点",)


def test_blank_poi_id_counts_as_unresolved() -> None:
    days = [{"day": 1, "date": START.isoformat(), "theme": None,
             "timeline": [_attraction("空白标识", "   ")]}]
    trip = _convert(_plan(days))

    assert trip.stays[0].days[0].items[0].unresolved_name == "空白标识"


def test_tip_becomes_note() -> None:
    item = _convert(_plan(_three_days())).stays[0].days[0].items[0]
    assert item.note == "中山陵的游玩贴士"


def test_meal_is_converted_with_reason_as_note() -> None:
    meal = _convert(_plan(_three_days())).stays[0].days[0].items[1]

    assert meal.kind is ItemKind.MEAL
    assert meal.title == "南京大牌档"
    assert meal.note == "南京大牌档的理由"


def test_meal_keeps_its_coordinates_and_address() -> None:
    """餐饮项要留下坐标与地址。

    引擎的餐饮环节是从高德周边搜索拿到的餐厅，`restaurant_to_dict` 返回了
    `location` 与 `address`——只是没有 id（给餐厅编个 id 塞进 `poi` 表会污染
    实体，见迁移 10）。以前这里把坐标一起丢了，后果是路书上
    「这段路没有坐标」出现七次，全部来自餐饮项：**中午从博物馆走多久能到
    那家店，行程里答不出来。**
    """
    days = _three_days()
    days[0]["timeline"][1] = {
        **_meal("lunch", "南京大牌档"),
        "location": {"lng": 118.79, "lat": 32.06},
        "address": "中山陵景区内",
    }
    meal = _convert(_plan(days)).stays[0].days[0].items[1]

    assert meal.facts is not None
    assert meal.facts.lat_gcj02 == pytest.approx(32.06)
    assert meal.facts.lng_gcj02 == pytest.approx(118.79)
    assert meal.facts.address == "中山陵景区内"
    # 餐厅没有实体 id，这是对的——编一个会污染实体表
    assert meal.poi_id is None


def test_meal_without_location_still_converts() -> None:
    """引擎没给坐标时照样是个天项，只是路书里那段路算不出来。"""
    meal = _convert(_plan(_three_days())).stays[0].days[0].items[1]

    assert meal.kind is ItemKind.MEAL
    assert meal.facts is not None
    assert not meal.facts.has_coordinates


def test_missing_restaurant_becomes_a_readable_placeholder() -> None:
    """引擎没找到餐厅时给 name=None，不能变成一个空标题的天项。"""
    meal = _convert(_plan(_three_days())).stays[0].days[1].items[1]

    assert meal.kind is ItemKind.MEAL
    assert meal.title == "午餐（未找到合适餐厅）"
    assert meal.note


def test_dinner_without_restaurant_says_dinner() -> None:
    meal = _convert(_plan(_three_days())).stays[0].days[2].items[-1]
    assert meal.title == "晚餐（未找到合适餐厅）"


def test_item_order_follows_the_timeline() -> None:
    items = _convert(_plan(_three_days())).stays[0].days[0].items
    assert [i.title for i in items] == [
        "中山陵",
        "南京大牌档",
        "明孝陵",
        "回味鸭血粉丝",
    ]


def test_attractions_property_filters_out_meals() -> None:
    day = _convert(_plan(_three_days())).stays[0].days[0]
    assert [i.title for i in day.attractions] == ["中山陵", "明孝陵"]


def test_unknown_timeline_type_is_skipped_with_a_warning() -> None:
    days = [{"day": 1, "date": START.isoformat(), "theme": None,
             "timeline": [_attraction("正常景点"), {"type": "shopping", "name": "商场"}]}]
    trip = _convert(_plan(days))

    assert len(trip.stays[0].days[0].items) == 1
    assert any("shopping" in w for w in trip.warnings)


def test_day_without_attractions_gets_a_warning() -> None:
    days = [{"day": 1, "date": START.isoformat(), "theme": None,
             "timeline": [_meal("lunch", "某餐厅")]}]
    trip = _convert(_plan(days))

    assert any("没有任何景点" in w for w in trip.warnings)


# ─── 日期与天数的鲁棒性 ──────────────────────────────────────


def test_caller_start_date_overrides_the_plan() -> None:
    """自有表的 trip.start_date 是 NOT NULL，不能依赖引擎一定给对了日期。"""
    override = date(2026, 12, 24)
    trip = _convert(_plan(_three_days()), start_date=override)

    assert trip.start_date == override
    assert trip.stays[0].days[0].day == override


def test_missing_start_date_uses_the_caller_value() -> None:
    plan = _plan(_three_days(), start_date=None)
    for day in plan["days"]:
        day["date"] = None

    trip = _convert(plan, start_date=START)
    assert trip.stays[0].days[0].day == START


def test_no_start_date_anywhere_is_a_hard_error() -> None:
    plan = _plan(_three_days(), start_date=None)
    for day in plan["days"]:
        day["date"] = None

    with pytest.raises(PlanConversionError, match="开始日期"):
        plan_to_trip(plan, city_adcodes={"南京": NANJING_ADCODE})


def test_missing_day_dates_are_derived_and_warned() -> None:
    plan = _plan(_three_days())
    plan["days"][1]["date"] = None

    trip = _convert(plan)
    assert trip.stays[0].days[1].day == date(2026, 10, 2)
    assert any("没有日期" in w for w in trip.warnings)


def test_inconsistent_day_date_warns_but_keeps_the_invariant() -> None:
    """引擎给的日期与我们推算的不一致时，以推算为准并留下警告。

    日期必须连续——这是 ADR-0004 的不变量，不能被引擎的错值破坏。
    """
    plan = _plan(_three_days())
    plan["days"][1]["date"] = "2026-11-11"

    trip = _convert(plan)
    assert [d.day for d in trip.stays[0].days] == [date(2026, 10, i) for i in (1, 2, 3)]
    assert any("不一致" in w for w in trip.warnings)


def test_day_number_gaps_still_produce_consecutive_dates() -> None:
    """引擎的天编号有洞时，仍要产出连续的日期，否则违反不变量。"""
    days = [
        {"day": 1, "date": None, "theme": None, "timeline": [_attraction("第一天")]},
        {"day": 4, "date": None, "theme": None, "timeline": [_attraction("第四天")]},
    ]
    trip = _convert(_plan(days))

    assert trip.total_days == 2
    assert [d.day for d in trip.stays[0].days] == [date(2026, 10, 1), date(2026, 10, 2)]


def test_days_are_sorted_by_day_number() -> None:
    days = list(reversed(_three_days()))
    trip = _convert(_plan(days))

    assert [d.theme for d in trip.stays[0].days] == ["钟山风景区", "秦淮河畔", "博物馆与城墙"]


# ─── 警告与硬错误 ────────────────────────────────────────────


def test_days_count_mismatch_warns() -> None:
    trip = _convert(_plan(_three_days(), days_count=5))
    assert any("声称 5 天" in w for w in trip.warnings)


def test_unknown_city_adcode_warns() -> None:
    trip = plan_to_trip(_plan(_three_days()), city_adcodes={})
    assert trip.stays[0].city_adcode is None
    assert any("行政区划代码" in w for w in trip.warnings)


def test_route_issues_and_weather_note_become_warnings() -> None:
    plan = _plan(_three_days(), route_issues=["Day2 行程较紧凑"], weather_note="超出预报范围")
    trip = _convert(plan)

    assert "Day2 行程较紧凑" in trip.warnings
    assert "超出预报范围" in trip.warnings


def test_empty_days_is_a_hard_error() -> None:
    with pytest.raises(PlanConversionError, match="没有任何一天"):
        _convert(_plan([]))


def test_missing_destination_is_a_hard_error() -> None:
    with pytest.raises(PlanConversionError, match="目的地"):
        _convert(_plan(_three_days(), destination=""))


def test_plan_missing_days_key_is_a_hard_error() -> None:
    with pytest.raises(PlanConversionError):
        _convert({"destination": "南京", "start_date": START.isoformat()})


# ─── 结果可以直接落库 ────────────────────────────────────────


def test_produced_trip_satisfies_domain_invariants() -> None:
    """构造出来的 PlannedTrip 必须通过领域层自己的校验。

    PlannedTrip 的 __post_init__ 会检查城市顺序连续、日期连续，
    所以这一条同时也在守护转换层的输出形状。
    """
    trip = _convert(_plan(_three_days()))
    assert isinstance(trip, PlannedTrip)
    assert trip.city_names == ("南京",)
