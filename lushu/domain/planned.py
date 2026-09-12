"""规划产出：一次规划的结果在落库之前的形状。

它是引擎产出与自有行程表之间的中间形态。需要这一层的原因是两者形状不同：
引擎的 `final_plan` 是「一座城市 + 一串扁平的天」，而本项目的行程是
「城市停留序列」。

这一层**刻意做成多城市形状**——单城市规划产出的就是只有一个城市停留的
`PlannedTrip`。M2 把引擎的目的地扩展成序列时，这里不需要改动，这正是
「代码骨架必须先按多城市写」的落点。

命名上与 `domain/trip.py` 的两个类型区分清楚：

- `CalendarDay`（在 trip.py）是**排期骨架**上的一天：只有日期与归属，没有内容
- `PlannedDay`（在本文件）是**规划产出**的一天：有主题、有事项
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum


class ItemKind(StrEnum):
    """天项的类型。与数据库 `day_item.kind` 的取值一一对应。"""

    POI = "poi"
    MEAL = "meal"
    REST = "rest"


@dataclass(frozen=True)
class PoiFacts:
    """景点在实体真源里的硬事实。

    这些字段只能来自官方接口（ADR-0001），攻略素材不得覆盖它们。
    落库时写进 `poi` 表。坐标是 GCJ-02，字段名带后缀（ADR-0003）。
    """

    lat_gcj02: float | None = None
    lng_gcj02: float | None = None
    address: str | None = None
    tel: str | None = None
    rating: float | None = None
    open_time: str | None = None
    photo: str | None = None

    @property
    def has_coordinates(self) -> bool:
        return self.lat_gcj02 is not None and self.lng_gcj02 is not None


@dataclass(frozen=True)
class PlannedItem:
    """规划产出里的一个事项。"""

    kind: ItemKind
    title: str
    poi_id: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    note: str | None = None
    facts: PoiFacts | None = None
    # 引擎给出的名字没能对上实体真源时记在这里，由落库方送入待对齐队列（Q19）
    unresolved_name: str | None = None

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("天项必须有名称")
        if self.poi_id and self.unresolved_name:
            raise ValueError("已对齐到实体真源的天项不应同时带着待对齐的名字")
        if self.kind is ItemKind.POI and not (self.poi_id or self.unresolved_name):
            raise ValueError(f"景点「{self.title}」既没有实体标识也没有待对齐记录，无法落库")


@dataclass(frozen=True)
class PlannedDay:
    """规划产出里的一天。"""

    day: date
    seq_in_stay: int
    theme: str | None = None
    items: tuple[PlannedItem, ...] = ()

    @property
    def attractions(self) -> tuple[PlannedItem, ...]:
        return tuple(i for i in self.items if i.kind is ItemKind.POI)


@dataclass(frozen=True)
class PlannedStay:
    """规划产出里的一座城市停留。它的天数就是它有多少天。"""

    city_name: str
    city_adcode: str | None
    seq: int
    days: tuple[PlannedDay, ...]

    def __post_init__(self) -> None:
        if not self.city_name.strip():
            raise ValueError("城市停留必须有城市名")
        if not self.days:
            raise ValueError(f"城市停留「{self.city_name}」必须至少有一天")
        if self.seq < 0:
            raise ValueError(f"城市顺序不能为负，收到 {self.seq}")

    @property
    def stay_days(self) -> int:
        """停留天数。它是天列表的长度，不单独存放——两处存放必然会对不上。"""
        return len(self.days)


@dataclass(frozen=True)
class StaySpec:
    """一座城市停留的规格：城市与停留天数。

    它是用户的输入形态（「北京 3 天、西安 2 天」），与 `PlannedStay` 的区别是
    还没有铺到具体日期上。M2 的「动态添加城市并设置天数」产出的就是这个。
    """

    city_name: str
    stay_days: int
    city_adcode: str | None = None

    def __post_init__(self) -> None:
        if not self.city_name.strip():
            raise ValueError("城市规格必须有城市名")
        if self.stay_days < 1:
            raise ValueError(f"「{self.city_name}」的停留天数必须至少为 1 天，收到 {self.stay_days}")


def lay_out(start_date: date, specs: Sequence[StaySpec]) -> tuple[PlannedStay, ...]:
    """把城市停留规格铺成连续的日历天。

    天从 start_date 起连续排列，中途不插入额外的转移天——这与 ADR-0004 一致：
    城际转移是某一天内部的一个组成部分，不独立占天。因此
    「总行程天数 = 各城市停留天数之和」在这里自然成立。
    """
    if not specs:
        raise ValueError("行程至少要有一次城市停留")

    cursor = start_date
    stays: list[PlannedStay] = []
    for seq, spec in enumerate(specs):
        days: list[PlannedDay] = []
        for offset in range(spec.stay_days):
            days.append(PlannedDay(day=cursor, seq_in_stay=offset))
            cursor += timedelta(days=1)
        stays.append(
            PlannedStay(
                city_name=spec.city_name,
                city_adcode=spec.city_adcode,
                seq=seq,
                days=tuple(days),
            )
        )
    return tuple(stays)


@dataclass(frozen=True)
class PlannedTrip:
    """一次规划的完整产出。"""

    name: str
    start_date: date
    stays: tuple[PlannedStay, ...]
    query: str = ""
    # 转换过程中发现的、不足以中断落库的问题
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.stays:
            raise ValueError("规划产出至少要有一次城市停留")
        if not self.name.strip():
            raise ValueError("规划产出必须有名称")

        ordered = self.stays
        expected_seq = list(range(len(ordered)))
        actual_seq = [s.seq for s in ordered]
        if actual_seq != expected_seq:
            raise ValueError(f"城市顺序必须是连续的 0..n，收到 {actual_seq}")

        # 日期必须从 start_date 起连续铺满——这与 ADR-0004 是同一条不变量：
        # 城际转移不额外占天，所以天就是连续的日历天，中间不能有洞。
        expected_dates = [self.start_date + timedelta(days=i) for i in range(self.total_days)]
        actual_dates = [d.day for stay in ordered for d in stay.days]
        if actual_dates != expected_dates:
            raise ValueError(
                "规划产出的日期必须是自开始日期起连续的日历天："
                f"期望 {expected_dates[0]} 至 {expected_dates[-1]}，实际 {actual_dates}"
            )

    @property
    def total_days(self) -> int:
        """总行程天数 = 各城市停留天数之和。"""
        return sum(stay.stay_days for stay in self.stays)

    @property
    def end_date(self) -> date:
        return self.start_date + timedelta(days=self.total_days - 1)

    @property
    def city_names(self) -> tuple[str, ...]:
        return tuple(stay.city_name for stay in self.stays)

    @property
    def unresolved_names(self) -> tuple[str, ...]:
        """所有待对齐的景点名，按出现顺序去重。"""
        seen: dict[str, None] = {}
        for stay in self.stays:
            for day in stay.days:
                for item in day.items:
                    if item.unresolved_name:
                        seen.setdefault(item.unresolved_name, None)
        return tuple(seen)
