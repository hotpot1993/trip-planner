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

# 坐标那两项的代号。断言里只挑它们：别的规则（路段数量、地址）是别人的事，
# 一旦混进来，这里就会因为不相干的改动而红——那样的测试最后会被删掉。
_COORD_CODES = {"coord_impossible", "coord_off_city"}


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


class TestCoordinates:
    """坐标错了不会让文件打不开，只会让人**导航到别的地方**。

    这是「计划会失败」那一类，却在界面与文件里都看不出异常——是典型的静默失败。
    两项检查的严重程度不同：越界是机械上不可能，离群只是可疑（一天跑出城
    100 公里是真实存在的）。
    """

    def _at(self, lat: float | None, lng: float | None, title: str = "钟楼") -> RoadbookItem:
        return _item(title, lat_gcj02=lat, lng_gcj02=lng, address="西安市碑林区")

    def test_swapped_lat_lng_is_an_error(self) -> None:
        """(34.34, 108.94) 写反成 (108.94, 34.34)，导航会点开到中亚。"""
        day = _day(city_name="西安", items=(self._at(108.94, 34.34),))

        problems = validate(_book(days=(day,)))

        assert "coord_impossible" in _codes(problems, Severity.ERROR)
        assert "写反" in str(errors(problems)[0])

    def test_one_bad_coordinate_is_reported_once(self) -> None:
        """**一个错误只出现在一处。**

        写反的坐标同时也「离同城其它点很远」，但它已经作为不可能被报出来了；
        再补一句离群只是同一件事说两遍，让人以为有两个问题。
        它也得从本城中位点里剔除，否则会把邻居一起拖成离群。
        """
        items = (
            self._at(34.26, 108.94, "钟楼"),
            self._at(34.27, 108.95, "鼓楼"),
            self._at(34.28, 108.96, "大雁塔"),
            self._at(116.40, 39.90, "写反了的那个"),
        )

        problems = validate(_book(days=(_day(city_name="西安", items=items),)))

        assert _codes(problems) & _COORD_CODES == {"coord_impossible"}

    def test_items_without_coordinates_are_skipped(self) -> None:
        """没有坐标不是这里的问题——那是路段文字要交代的事，别报两遍。"""
        day = _day(items=(self._at(None, None), self._at(None, None, "鼓楼")))

        assert _codes(validate(_book(days=(day,)))) & _COORD_CODES == set()

    def test_a_far_point_in_the_same_city_is_a_warning(self) -> None:
        items = (
            self._at(34.26, 108.94, "钟楼"),
            self._at(34.27, 108.95, "鼓楼"),
            self._at(34.28, 108.96, "大雁塔"),
            self._at(39.90, 116.40, "故宫博物院"),
        )

        problems = validate(_book(days=(_day(city_name="西安", items=items),)))

        assert "coord_off_city" in _codes(problems, Severity.WARN)
        assert "coord_impossible" not in _codes(problems)

    def test_a_second_city_is_not_an_outlier(self) -> None:
        """南京到西安差 10 度。拿全局中位点去比，第二天起每个点都会报警。

        这就是这里按城市分组、而不是照 travel-plan-viz 用全局中位点的原因。
        """
        nanjing = _day(
            date=START,
            city_name="南京",
            items=(
                _item("中山陵", lat_gcj02=32.06, lng_gcj02=118.85),
                _item("明孝陵", lat_gcj02=32.05, lng_gcj02=118.83),
                _item("灵谷寺", lat_gcj02=32.06, lng_gcj02=118.87),
            ),
        )
        xian = _day(
            date=START + timedelta(days=1),
            city_name="西安",
            items=(
                _item("钟楼", lat_gcj02=34.26, lng_gcj02=108.94),
                _item("鼓楼", lat_gcj02=34.26, lng_gcj02=108.94),
                _item("大雁塔", lat_gcj02=34.22, lng_gcj02=108.96),
            ),
        )

        problems = validate(
            _book(days=(nanjing, xian), end_date=START + timedelta(days=1))
        )

        assert _codes(problems) & _COORD_CODES == set()

    def test_two_points_are_not_enough_for_a_median(self) -> None:
        """两个点之间的「离群」没有意义——总得有个多数才算得出少数。"""
        items = (self._at(34.26, 108.94), self._at(39.90, 116.40, "故宫博物院"))

        assert "coord_off_city" not in _codes(validate(_book(days=(_day(items=items),))))

    def test_the_real_spread_stays_under_the_threshold(self) -> None:
        """实测值：兵马俑离西安市区 0.33 度（33 公里，是真的），不该报警。

        这条钉住阈值不能被收紧到 0.3——那会把一趟真实的行程判成有问题，
        而一个总在报警的检查等于没有检查。
        """
        items = (
            self._at(34.276, 108.955, "西安城墙"),
            self._at(34.386, 109.282, "秦始皇帝陵博物院"),
            self._at(34.224, 108.955, "陕西历史博物馆"),
        )

        assert "coord_off_city" not in _codes(validate(_book(days=(_day(city_name="西安", items=items),))))


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
