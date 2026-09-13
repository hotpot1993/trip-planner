"""路书：导出物的数据契约。

设计第八节：产物是**单个 HTML 文件，手机优先、离线可读**，旅行途中在手机上
查阅。这一层不是渲染器，是**契约**——先把「一份合格的路书必须有什么」
写成可检查的规则，渲染器照着它产出，生成之后再跑一遍校验。

为什么契约要单独一层：设计里写死了两条会静默失败的约束。

1. **不含交互地图，只保留导航深链**（ADR-0006）。地图一旦没有，空间关系
   就得靠**顺序、时间段与路段文字**来补偿——设计原话是「这块内容要做足」。
   一份没有路段描述的路书，在地图上看着没问题，在手机上就是一堆地名。
2. **离线可读**。任何一条外部资源引用（字体、图片、CDN 脚本）都会让它在
   信号不好的地方变成半张白纸，而「信号不好」恰恰是旅行途中的常态。

这两条都不会抛异常，只会让产物在真正要用的时候不能用。所以它们必须是
校验项，而不是注释里的提醒。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

# 相邻两个天项之间，超过这个距离就不该再建议步行。
#
# 取 1500 米（约二十分钟）：一公里出头的步行在城市里毫不稀奇，
# 把它标出来只会变成噪声；真正会让人意外的是「写着步行，实际要走半小时」。
#
# **这个数必须与 `services/roadbook_service.py` 的 `WALK_LIMIT_M` 一致**——
# 那边用它决定「多远之外改推地铁或打车」。两边取不同的数，就会出现
# 「估值器自己产出的路段被契约判为可疑」这种自相矛盾。一处判断只能有一个数字。
NEEDS_LEG_METRES = 1500.0

# 中国的经纬度包络。写得比国界松，只用来抓「经纬度写反」与「数量级写错」——
# 精确的行政边界要靠边界多边形，那是另一件事。
#
# 写反是我们真实遇到过的失败模式：把 (34.34, 108.94) 写成 (108.94, 34.34)，
# 导航会点开到中亚。这种数看一眼「不可能」，但没有任何东西会替你看。
CHINA_LAT_RANGE = (3.0, 54.0)
CHINA_LNG_RANGE = (73.0, 136.0)

# 同一个城市里，离本城中位点超过这个度数（约 110 公里）就值得怀疑。
#
# 高德返回的是 GCJ-02 坐标，但它是一次**整体偏移**，同城各点之间的差值不受影响，
# 所以直接比差值即可，不需要转换（ADR-0003）。
#
# 取 1.0 度而不是 3 度（travel-plan-viz 的 validate.js 用的是 3 度）：那个数是给
# 「一趟跨国行程」用的全局中位点，而我们的多城市行程里南京到西安本身就差 10 度，
# 拿全局中位点去比，第二天起每个点都会报警。**一个总在报警的检查等于没有检查。**
# 实测真实行程（南京、西安 5 天）：南京 15 个点离本城中位点最远 0.20 度，
# 西安 3 个点最远 0.33 度（兵马俑离市区 33 公里，是真的）。1.0 度留了五倍余量。
CITY_OUTLIER_DEGREES = 1.0

# 少于这个点数就不找中位点：两个点之间的「离群」没有意义
CITY_OUTLIER_MIN_POINTS = 3


class Severity(StrEnum):
    ERROR = "error"  # 这份路书在手机上不能用，必须修
    WARN = "warn"  # 能用，但有该补的东西


@dataclass(frozen=True)
class Problem:
    severity: Severity
    code: str
    where: str
    message: str

    def __str__(self) -> str:
        mark = "✗" if self.severity is Severity.ERROR else "!"
        return f"{mark} [{self.code}] {self.where}：{self.message}"


@dataclass(frozen=True)
class RoadbookLeg:
    """两处之间怎么走。**不含地图，这段文字就是空间关系的全部载体。**"""

    mode: str  # walk | metro | bus | taxi | drive | other
    distance_m: int | None = None
    duration_min: int | None = None
    from_name: str = ""
    to_name: str = ""
    # 导航深链：手机上点开就跳到地图 App。设计里明说这是保留的那部分
    nav_url: str | None = None
    note: str | None = None

    @property
    def described(self) -> bool:
        """这段路有没有说清「多远、多久、怎么走」。"""
        return bool(self.mode) and (self.duration_min is not None or self.distance_m is not None)


@dataclass(frozen=True)
class RoadbookItem:
    """路书里的一天项。"""

    title: str
    kind: str = "poi"  # poi | meal | rest
    start_time: str | None = None
    end_time: str | None = None
    address: str | None = None
    note: str | None = None
    # 软经验原文：网友说过的话（带出处）。导出物里必须有，
    # 否则手机上看到的只是一串地名
    highlights: tuple[str, ...] = ()
    avoids: tuple[str, ...] = ()
    # 需要预约时的提醒，含渠道
    booking: str | None = None
    lat_gcj02: float | None = None
    lng_gcj02: float | None = None


@dataclass(frozen=True)
class RoadbookDay:
    date: date
    city_name: str
    seq_in_stay: int = 0
    theme: str | None = None
    items: tuple[RoadbookItem, ...] = ()
    # 第 i 段是 items[i] 到 items[i+1] 之间怎么走。长度应当是 len(items) - 1
    legs: tuple[RoadbookLeg | None, ...] = ()
    transfer: str | None = None  # 城际转移落在这一天时的描述


@dataclass(frozen=True)
class RoadbookBooking:
    """预约清单上的一条。"""

    poi_name: str
    visit_date: date
    headline: str
    channels: tuple[str, ...] = ()
    release_date: date | None = None


@dataclass(frozen=True)
class Roadbook:
    """一份完整的路书。"""

    name: str
    start_date: date
    end_date: date
    days: tuple[RoadbookDay, ...] = ()
    bookings: tuple[RoadbookBooking, ...] = ()
    budget: tuple[tuple[str, str], ...] = ()  # (标签, 金额文本)
    generated_at: str = ""
    # 外部资源引用：离线可读的反面。这一项**必须为空**
    external_refs: tuple[str, ...] = field(default=())

    @property
    def total_days(self) -> int:
        return len(self.days)

    @property
    def item_count(self) -> int:
        return sum(len(day.items) for day in self.days)


def _minutes(value: str | None) -> int | None:
    if not value or ":" not in value:
        return None
    head, _, tail = value.partition(":")
    if not head.isdigit() or not tail.isdigit():
        return None
    return int(head) * 60 + int(tail)


def _coordinates(roadbook: Roadbook) -> dict[str, list[tuple[str, float, float]]]:
    """按城市归拢带坐标的天项，键是城市名。

    按城市而不是全局（详见 `CITY_OUTLIER_DEGREES`）；同名的城市就算不挨着
    也归到一起——它们的坐标本来就该聚在一处。
    """
    grouped: dict[str, list[tuple[str, float, float]]] = {}
    for day in roadbook.days:
        for item in day.items:
            if item.lat_gcj02 is None or item.lng_gcj02 is None:
                continue
            label = f"{day.date.isoformat()} {item.title}"
            grouped.setdefault(day.city_name, []).append((label, item.lat_gcj02, item.lng_gcj02))
    return grouped


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def validate(roadbook: Roadbook) -> list[Problem]:
    """检查一份路书能不能拿去用。

    错误排在前面——它决定这份东西能不能发到手机上。
    """
    found: list[Problem] = []

    def add(severity: Severity, code: str, where: str, message: str) -> None:
        found.append(Problem(severity, code, where, message))

    if not roadbook.days:
        add(Severity.ERROR, "no_days", roadbook.name, "一天都没有，这份路书是空的")
        return found

    # ── 离线可读 ──────────────────────────────────────────────
    if roadbook.external_refs:
        add(
            Severity.ERROR,
            "external_refs",
            roadbook.name,
            f"引用了 {len(roadbook.external_refs)} 处外部资源"
            f"（{roadbook.external_refs[0]}）——离线打开会缺东西",
        )

    # ── 日期连续且与首尾一致 ──────────────────────────────────
    dates = [day.date for day in roadbook.days]
    if dates[0] != roadbook.start_date:
        add(Severity.ERROR, "start_mismatch", roadbook.name, "第一天的日期与出发日期对不上")
    if dates[-1] != roadbook.end_date:
        add(Severity.ERROR, "end_mismatch", roadbook.name, "最后一天的日期与结束日期对不上")
    for previous, current in zip(dates, dates[1:], strict=False):
        if (current - previous).days != 1:
            add(
                Severity.ERROR,
                "date_gap",
                current.isoformat(),
                f"与前一天不连续（{previous} → {current}）",
            )

    # ── 逐天 ──────────────────────────────────────────────────
    for day in roadbook.days:
        where = day.date.isoformat()
        times = [_minutes(item.start_time) for item in day.items]
        for item in day.items:
            if item.kind == "poi" and not item.address:
                add(
                    Severity.WARN,
                    "poi_without_address",
                    f"{where} {item.title}",
                    "没有地址，到了一个陌生的城市只靠名字找不到",
                )
            if item.booking and not item.booking.strip():
                add(Severity.ERROR, "empty_booking", f"{where} {item.title}", "预约提醒是空的")

        # 项必须按时间排好；这是「用顺序补偿空间关系」的前提
        known = [(index, value) for index, value in enumerate(times) if value is not None]
        for (left_index, left), (right_index, right) in zip(known, known[1:], strict=False):
            if right < left:
                add(
                    Severity.ERROR,
                    "time_out_of_order",
                    where,
                    f"第 {left_index + 1} 项（{day.items[left_index].start_time}）"
                    f"排在第 {right_index + 1} 项（{day.items[right_index].start_time}）之后",
                )

        # ── 路段描述：地图没有，这段文字就是空间关系 ──────────
        expected = max(len(day.items) - 1, 0)
        if len(day.legs) != expected:
            add(
                Severity.ERROR,
                "leg_count",
                where,
                f"有 {len(day.items)} 个天项，路段应当是 {expected} 段，实际 {len(day.legs)} 段",
            )
        else:
            for index, leg in enumerate(day.legs):
                label = f"{where} 第 {index + 1}→{index + 2} 段"
                if leg is None:
                    add(
                        Severity.WARN,
                        "leg_missing",
                        label,
                        "这两处之间没有路段说明——没有地图，路上靠什么走就查不到了",
                    )
                    continue
                if not leg.described:
                    add(
                        Severity.WARN,
                        "leg_undescribed",
                        label,
                        "只写了怎么走，没写多远或多快",
                    )
                if leg.nav_url is None:
                    add(Severity.WARN, "leg_without_nav", label, "没有导航深链")
                if (
                    leg.distance_m is not None
                    and leg.distance_m >= NEEDS_LEG_METRES
                    and leg.mode == "walk"
                ):
                    add(
                        Severity.WARN,
                        "walk_too_far",
                        label,
                        f"写着步行但距离 {leg.distance_m} 米，现场会很意外",
                    )

    # ── 坐标 ──────────────────────────────────────────────────
    #
    # 坐标错了不会让文件打不开，只会让人**导航到别的地方**——那是「计划会失败」
    # 那一类，却在界面与文件里都看不出异常。所以它必须是校验项。
    #
    # 两项检查的严重程度不同，因为一个是「不可能」一个是「可疑」：
    # 越界是机械上不可能（中国的经度不可能是 108 度还当纬度用），可以直接判错；
    # 离群只是统计上可疑，一天跑出城 100 公里是真实存在的，所以只提醒。
    #
    # 已经判为不可能的点不再参与离群：**一个错误只出现在一处**。
    # 它同时也从本城的中位点里剔除——拿一个已知是错的坐标去当中位点，
    # 会把它的邻居也拖成「离群」。
    coordinates = _coordinates(roadbook)
    impossible: set[str] = set()
    for points in coordinates.values():
        for label, lat, lng in points:
            if not (
                CHINA_LAT_RANGE[0] <= lat <= CHINA_LAT_RANGE[1]
                and CHINA_LNG_RANGE[0] <= lng <= CHINA_LNG_RANGE[1]
            ):
                impossible.add(label)
                add(
                    Severity.ERROR,
                    "coord_impossible",
                    label,
                    f"坐标 {lat},{lng} 不在中国范围内——多半是经纬度写反了，"
                    "导航会点开到地球另一边",
                )
    for city, points in coordinates.items():
        usable = [point for point in points if point[0] not in impossible]
        if len(usable) < CITY_OUTLIER_MIN_POINTS:
            continue
        mid_lat = _median([lat for _, lat, _ in usable])
        mid_lng = _median([lng for _, _, lng in usable])
        for label, lat, lng in usable:
            if abs(lat - mid_lat) > CITY_OUTLIER_DEGREES or abs(lng - mid_lng) > CITY_OUTLIER_DEGREES:
                add(
                    Severity.WARN,
                    "coord_off_city",
                    label,
                    f"离{city}其它 {len(usable) - 1} 处超过约 110 公里，"
                    "多半查错了地方——导航会带你去别的城市",
                )

    # ── 预约清单 ──────────────────────────────────────────────
    for booking in roadbook.bookings:
        if not booking.channels:
            add(
                Severity.ERROR,
                "booking_without_channel",
                booking.poi_name,
                "知道要预约却没有渠道，用户无处可去",
            )
        if not booking.visit_date:
            add(Severity.ERROR, "booking_without_date", booking.poi_name, "没写哪天去")

    found.sort(key=lambda item: (item.severity is not Severity.ERROR, item.code))
    return found


def errors(problems: list[Problem]) -> list[Problem]:
    return [item for item in problems if item.severity is Severity.ERROR]
