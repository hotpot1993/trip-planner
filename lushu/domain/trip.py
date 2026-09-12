"""行程领域模型：行程、城市停留、天、城际转移。

这里承载两条不能含糊的规则：

1. **总行程天数 = 各城市停留天数之和**，城际转移不额外占天（ADR-0004）。
   这是「用户设置各城市停留天数、系统自动计算总行程天数」能够成立的唯一前提。
2. **城际转移归入某一天**，默认落在出发城市的最后一天，长车程时改建议落在
   到达城市的第一天，用户可拖拽覆盖（Q45）。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from .transfer import TransferMode

# 超过这个时长的城际转移，默认建议改落在到达城市的第一天。
# 依据：早班长途车到站后，出发城市当天已无游玩价值；
# 而傍晚出发则白天仍留在出发城市，归入出发城市最后一天更自然。
LONG_TRANSFER_MINUTES = 240


@dataclass(frozen=True)
class CityStay:
    """在一座城市连续停留的若干天。行程的基本组成单元。"""

    id: str
    city_adcode: str
    city_name: str
    seq: int
    stay_days: int

    def __post_init__(self) -> None:
        if self.stay_days < 1:
            raise ValueError(f"城市停留天数必须至少为 1 天，收到 {self.stay_days}")
        if self.seq < 0:
            raise ValueError(f"城市顺序不能为负，收到 {self.seq}")


@dataclass(frozen=True)
class CalendarDay:
    """排期骨架上的一天：只有日期与归属，没有内容。

    与 `planned.PlannedDay` 的区别是阶段不同：本类型在**规划之前**由
    `plan_days()` 从城市停留序列铺出来，回答「哪一天属于哪座城市」；
    `PlannedDay` 在**规划之后**，带着主题与具体事项。
    """

    index: int  # 全行程内从 0 开始的序号
    day_date: date
    city_stay_id: str
    seq_in_stay: int

    @property
    def is_first_of_stay(self) -> bool:
        return self.seq_in_stay == 0


@dataclass(frozen=True)
class TransferPlacement:
    """城际转移的落点建议。"""

    day_index: int
    is_default: bool  # True 表示出行程规则给出的默认落点
    reason: str


def total_days(stays: Sequence[CityStay]) -> int:
    """总行程天数。城际转移不额外增加天数。"""
    return sum(stay.stay_days for stay in stays)


def plan_days(stays: Sequence[CityStay], start_date: date) -> list[CalendarDay]:
    """把城市停留序列铺成连续的日历天。

    天从 start_date 起连续排列，中途不插入额外的转移天——转移是
    某一天内部的一个组成部分，而不是独立的一天。
    """
    ordered = sorted(stays, key=lambda s: s.seq)
    if not ordered:
        return []

    planned: list[CalendarDay] = []
    cursor = start_date
    for stay in ordered:
        for offset in range(stay.stay_days):
            planned.append(
                CalendarDay(
                    index=len(planned),
                    day_date=cursor,
                    city_stay_id=stay.id,
                    seq_in_stay=offset,
                )
            )
            cursor += timedelta(days=1)
    return planned


def default_transfer_day(
    days: Sequence[CalendarDay],
    from_stay: CityStay,
    to_stay: CityStay,
    *,
    duration_min: int | None = None,
    mode: TransferMode = TransferMode.RAIL,
) -> TransferPlacement:
    """给出城际转移的默认落点。

    默认落在出发城市的最后一天：「在北京住 3 天」的自然读法就是第 3 天从北京走。
    但车程很长或需转机时，出发城市当天实际已无游玩价值，改建议落在到达城市
    的第一天。两种情况下用户都能拖拽覆盖。
    """
    from_days = [d for d in days if d.city_stay_id == from_stay.id]
    to_days = [d for d in days if d.city_stay_id == to_stay.id]
    if not from_days or not to_days:
        raise ValueError("转移的两端城市停留必须都在行程里")

    long_trip = mode is TransferMode.AIR or (duration_min is not None and duration_min > LONG_TRANSFER_MINUTES)
    if long_trip:
        target = to_days[0]
        if mode is TransferMode.AIR:
            reason = "需乘飞机，建议落在到达城市第一天"
        else:
            reason = f"车程约 {duration_min} 分钟，超过 {LONG_TRANSFER_MINUTES} 分钟，建议落在到达城市第一天"
        return TransferPlacement(day_index=target.index, is_default=True, reason=reason)

    target = from_days[-1]
    return TransferPlacement(
        day_index=target.index,
        is_default=True,
        reason="默认落在出发城市最后一天",
    )
