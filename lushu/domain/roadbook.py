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

# 相邻两个天项之间，超过这个距离就该给一段文字说明怎么走。
# 取 800 米：步行十分钟上下，再远一点人就需要知道是打车还是地铁。
NEEDS_LEG_METRES = 800.0


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
