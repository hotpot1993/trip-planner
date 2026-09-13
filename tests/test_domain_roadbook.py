"""路书导出的契约校验。

设计第八节写死了两条会**静默失败**的约束：不含交互地图（ADR-0006），
以及离线可读。两者错了都不会抛异常，只会让产物在真正要用的时候不能用——
而「真正要用的时候」是旅行途中、手机上、信号不好的时候。

所以契约要能单独跑。这里的测试盯的就是「能不能查出静默失败」。
"""

from __future__ import annotations

from datetime import date, timedelta

from lushu.domain.roadbook import (
    Roadbook,
    RoadbookBooking,
    RoadbookDay,
    RoadbookItem,
    RoadbookLeg,
    Severity,
    errors,
    validate,
)

START = date(2026, 10, 1)


def _item(title: str = "故宫博物院", **overrides: object) -> RoadbookItem:
    base: dict[str, object] = {
        "title": title,
        "kind": "poi",
        "start_time": "09:00",
        "address": "北京市东城区景山前街 4 号",
    }
    base.update(overrides)
    return RoadbookItem(**base)  # type: ignore[arg-type]


def _leg(**overrides: object) -> RoadbookLeg:
    base: dict[str, object] = {
        "mode": "walk",
        "distance_m": 400,
        "duration_min": 6,
        "from_name": "故宫博物院",
        "to_name": "景山公园",
        "nav_url": "https://uri.amap.com/marker?position=116.397,39.918",
    }
    base.update(overrides)
    return RoadbookLeg(**base)  # type: ignore[arg-type]


def _day(**overrides: object) -> RoadbookDay:
    base: dict[str, object] = {
        "date": START,
        "city_name": "北京",
        "items": (_item(),),
        "legs": (),
    }
    base.update(overrides)
    return RoadbookDay(**base)  # type: ignore[arg-type]


def _book(**overrides: object) -> Roadbook:
    base: dict[str, object] = {
        "name": "北京三日",
        "start_date": START,
        "end_date": START,
        "days": (_day(),),
        "generated_at": "2026-09-13T10:00:00",
    }
    base.update(overrides)
    return Roadbook(**base)  # type: ignore[arg-type]


def _codes(problems, severity: Severity | None = None) -> set[str]:
    return {
        item.code for item in problems if severity is None or item.severity is severity
    }


class TestCleanRoadbook:
    def test_a_minimal_good_roadbook_has_no_errors(self) -> None:
        problems = validate(_book())
        assert errors(problems) == []

    def test_single_item_day_needs_no_leg(self) -> None:
        """只有一项的一天没有「段」，不该因为 0 段而报错。"""
        assert _codes(validate(_book()), Severity.ERROR) == set()


class TestOfflineReadiness:
    def test_external_refs_are_an_error(self) -> None:
        """离线可读是硬要求——旅行途中的常态恰恰是信号不好。"""
        book = _book(external_refs=("https://fonts.googleapis.com/css2?family=Noto",))

        problems = validate(book)

        assert "external_refs" in _codes(problems, Severity.ERROR)
        assert "离线" in str(problems[0])

    def test_inline_only_passes(self) -> None:
        assert _codes(validate(_book(external_refs=())), Severity.ERROR) == set()


class TestDateContinuity:
    def test_gap_between_days_is_an_error(self) -> None:
        days = (_day(date=START), _day(date=START + timedelta(days=2)))
        problems = validate(
            _book(
                days=days,
                start_date=START,
                end_date=START + timedelta(days=2),
            )
        )
        assert "date_gap" in _codes(problems, Severity.ERROR)

    def test_end_mismatch_is_an_error(self) -> None:
        problems = validate(_book(end_date=START + timedelta(days=3)))
        assert "end_mismatch" in _codes(problems, Severity.ERROR)

    def test_start_mismatch_is_an_error(self) -> None:
        problems = validate(_book(start_date=START - timedelta(days=1)))
        assert "start_mismatch" in _codes(problems, Severity.ERROR)

    def test_empty_roadbook_is_an_error(self) -> None:
        assert "no_days" in _codes(validate(_book(days=())), Severity.ERROR)


class TestLegs:
    """没有地图，路段文字就是空间关系的全部载体（设计原话：这块内容要做足）。"""

    def test_leg_count_must_match_the_items(self) -> None:
        day = _day(items=(_item(), _item("景山公园")), legs=())
        problems = validate(_book(days=(day,)))
        assert "leg_count" in _codes(problems, Severity.ERROR)

    def test_missing_leg_is_a_warning(self) -> None:
        day = _day(items=(_item(), _item("景山公园")), legs=(None,))
        problems = validate(_book(days=(day,)))
        assert "leg_missing" in _codes(problems, Severity.WARN)
        # 是提醒不是错误：那一整天仍然能用，只是路上要多问人
        assert errors(problems) == []

    def test_undescribed_leg_is_a_warning(self) -> None:
        day = _day(
            items=(_item(), _item("景山公园")),
            legs=(_leg(duration_min=None, distance_m=None),),
        )
        assert "leg_undescribed" in _codes(validate(_book(days=(day,))), Severity.WARN)

    def test_leg_without_navigation_link_is_a_warning(self) -> None:
        """导航深链是地图被砍掉之后保留的那部分（ADR-0006）。"""
        day = _day(items=(_item(), _item("景山公园")), legs=(_leg(nav_url=None),))
        assert "leg_without_nav" in _codes(validate(_book(days=(day,))), Severity.WARN)

    def test_long_walk_is_flagged(self) -> None:
        """写着步行但两公里，到了现场会很意外。"""
        day = _day(
            items=(_item(), _item("景山公园")),
            legs=(_leg(mode="walk", distance_m=2100, duration_min=30),),
        )
        assert "walk_too_far" in _codes(validate(_book(days=(day,))), Severity.WARN)

    def test_long_walk_by_metro_is_fine(self) -> None:
        day = _day(
            items=(_item(), _item("景山公园")),
            legs=(_leg(mode="metro", distance_m=2100, duration_min=12),),
        )
        assert "walk_too_far" not in _codes(validate(_book(days=(day,))))

    def test_complete_legs_pass(self) -> None:
        day = _day(items=(_item(), _item("景山公园")), legs=(_leg(),))
        problems = validate(_book(days=(day,)))
        assert _codes(problems) == set()


class TestDayOrder:
    def test_out_of_order_times_are_an_error(self) -> None:
        """顺序是「用文字补偿空间关系」的前提，乱了整份就读不懂。"""
        day = _day(
            items=(
                _item("午饭", kind="meal", start_time="12:00"),
                _item("故宫博物院", start_time="09:00"),
            ),
            legs=(_leg(),),
        )
        assert "time_out_of_order" in _codes(validate(_book(days=(day,))), Severity.ERROR)

    def test_items_without_time_do_not_break_ordering(self) -> None:
        day = _day(
            items=(
                _item("故宫博物院", start_time="09:00"),
                _item("随走随看", start_time=None),
                _item("景山公园", start_time="15:00"),
            ),
            legs=(_leg(), _leg()),
        )
        assert "time_out_of_order" not in _codes(validate(_book(days=(day,))))

    def test_poi_without_address_is_a_warning(self) -> None:
        day = _day(items=(_item(address=None),))
        assert "poi_without_address" in _codes(validate(_book(days=(day,))), Severity.WARN)


class TestBookings:
    def test_booking_without_channel_is_an_error(self) -> None:
        book = _book(
            bookings=(
                RoadbookBooking(
                    poi_name="故宫博物院",
                    visit_date=START,
                    headline="3 天后放票",
                    channels=(),
                ),
            )
        )
        assert "booking_without_channel" in _codes(validate(book), Severity.ERROR)

    def test_booking_with_channel_passes(self) -> None:
        book = _book(
            bookings=(
                RoadbookBooking(
                    poi_name="故宫博物院",
                    visit_date=START,
                    headline="3 天后放票",
                    channels=("「故宫博物院」官方小程序",),
                ),
            )
        )
        assert errors(validate(book)) == []


class TestProblemOrdering:
    def test_errors_come_first(self) -> None:
        """错误决定这份东西能不能发到手机上，得先看见。"""
        book = _book(
            days=(
                _day(
                    items=(_item(address=None), _item("景山公园")),
                    legs=(_leg(nav_url=None),),
                ),
            ),
            end_date=START + timedelta(days=5),
        )
        problems = validate(book)
        assert problems
        severities = [item.severity for item in problems]
        assert severities == sorted(severities, key=lambda s: s is not Severity.ERROR)

    def test_problem_reads_as_a_sentence(self) -> None:
        book = _book(external_refs=("https://cdn.example.com/x.js",))
        text = str(errors(validate(book))[0])
        assert text.startswith("✗")
        assert "外部资源" in text
